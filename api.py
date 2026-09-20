import json
import secrets
import threading
import time
import uuid
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from orchestrator import run_diagnosis, run_diagnosis_stream
from database import (
    save_diagnosis_record, get_records, count_records, get_distinct_statuses,
    get_record_by_id, delete_record, get_stats, init_db
)
from agents import (
    get_db as get_chroma_db,
    get_llm,
    get_embeddings,
    get_knowledge_base_size,
)
from config import settings
from logging_config import get_logger, configure_logging
from workorder_export import workorder_to_markdown, workorder_filename
from startup_status import (
    set_step,
    mark_ready,
    mark_failed,
    add_warning,
    snapshot as startup_snapshot,
)

logger = get_logger(__name__)

# 接口层版本号的**唯一来源**。FastAPI 元数据、/health、/health/live、/ 四处都要用它，
# 别写死字符串：本次改造时只改了 FastAPI 那一处，导致 /health/live 一直谎报旧版本，
# 而这种不一致在"探活只返回 200 就算过"的部署脚本里根本看不出来。
API_VERSION = "1.3.0"

# 健康检查结果缓存（TTL 由 settings.HEALTH_CACHE_TTL 控制）
_health_cache: Dict[str, Any] = {"ts": 0.0, "payload": None}

# 并发诊断闸门。一次诊断要串行发起 6~9 次 LLM 调用、耗时数十秒；不限并发的话
# 突发流量会占满 FastAPI 的线程池（连 /health 都得排队），并把模型配额成倍烧掉。
# 用 BoundedSemaphore 而不是普通 Semaphore：多释放一次会直接抛错，
# 让"release 次数写错"这类 bug 在测试里就暴露，而不是悄悄把闸门越放越宽。
_diagnosis_slots = threading.BoundedSemaphore(settings.MAX_CONCURRENT_DIAGNOSES)


def require_api_key(x_api_key: str = Header(default="")):
    """可选鉴权：配置了 settings.API_KEY 才校验，否则直接放行。

    默认不开启，是为了不破坏本地开发与现有调用方；
    但公网部署前必须设置 API_KEY（README 里已把"无鉴权"列为已知限制）。
    用 compare_digest 而不是 == ，避免比较耗时泄露密钥长度/前缀信息。
    """
    if not settings.API_KEY:
        return
    if not secrets.compare_digest(x_api_key, settings.API_KEY):
        raise HTTPException(status_code=401, detail="无效或缺失的 API Key（请通过 X-API-Key 请求头提供）")


@asynccontextmanager
def _check_knowledge_base() -> None:
    """启动阶段显式检查向量库是否已构建（首次克隆最容易踩的坑）。

    此前这件事只在**首次检索**（`agents._init_bm25`）才会被发现——而那时用户已经
    点下"开始诊断"，拿到的是一张降级工单，看不出根因是"没跑 build_knowledge_base.py"。
    等到降级发生再说，代价是用户白等一轮 9 次 LLM 调用。

    这里**不抛异常、不判失败**：知识库为空时历史页/统计页仍可用，`/diagnose` 也会
    诚实地降级产出工单，判成"启动失败"是过度反应。但必须让调用方**在动手之前**看到，
    所以走 `add_warning`，由 `/health/ready` 带出去、前端状态灯显示。
    """
    size = get_knowledge_base_size()
    if size < 0:
        # 查不到 ≠ 是空的。合并两者会把"路径/权限/依赖出问题"误报成"知识库没建"。
        add_warning("向量库块数读取失败，无法确认知识库是否已构建；诊断可能降级")
    elif size == 0:
        add_warning(
            "知识库为空：请先执行 build_knowledge_base.py 构建向量库，"
            "否则诊断会因缺少依据而降级"
        )
    else:
        logger.info("knowledge_base_ready", chunks=size)


async def lifespan(app: FastAPI):
    """应用生命周期管理。

    启动阶段逐步上报进度（见 `startup_status`）。这不是装饰：初始化要花十几秒，
    其中 `import` 链就占了大头（torch 被 langchain_core 无条件拖进来），
    而这期间 `/health/live` 已经返回 200 了。前端状态灯若只看 liveness，
    会在这十几秒里显示"服务正常"——用户据此点下"开始诊断"必然失败。
    """
    steps = [
        ("加载日志配置", configure_logging),
        ("初始化数据库", init_db),
        # 这三步是真正的耗时项：向量库要读盘，BM25 要全库分词
        ("加载模型客户端", get_llm),
        ("加载嵌入模型", get_embeddings),
        ("加载向量库", get_chroma_db),
        # 放在最后：它要读向量库，必须等上一步加载完。空库不判失败，只记警告。
        ("校验知识库", _check_knowledge_base),
    ]
    total = len(steps)

    try:
        for i, (label, fn) in enumerate(steps, start=1):
            set_step(label, i, total)
            logger.info("startup_step", step=label, index=i, total=total)
            fn()
    except Exception as e:
        # 启动失败必须显式记录并向上抛：半初始化的服务对外宣告"就绪"比崩溃更危险，
        # 因为它会让调用方以为请求失败是自己的问题。
        mark_failed(f"{type(e).__name__}: {e}")
        logger.error("startup_failed", error=str(e))
        raise

    mark_ready()
    logger.info("api_startup_complete")
    yield
    # 关闭时清理
    logger.info("api_shutdown")


app = FastAPI(
    title="FlawScope API",
    description="多智能体工业故障诊断系统接口",
    version=API_VERSION,
    lifespan=lifespan
)

# CORS：前端是独立部署的 SPA（Vite 开发时在 5173，容器里经 nginx 反代），
# 与 API 不同源。不加这段浏览器会直接拦掉所有请求。
# 注意 allow_origins 走配置而不是写 ["*"]——带凭据的请求下 "*" 会被浏览器拒绝，
# 且生产环境放开任意源等于把 API 暴露给任何网站。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
    expose_headers=["Content-Disposition", "Retry-After"],
)


class DiagnosisRequest(BaseModel):
    fault_description: str = Field(..., min_length=1, max_length=4000, description="故障自然语言描述")
    image_base64: Optional[str] = Field(
        default="",
        max_length=settings.MAX_IMAGE_BASE64_CHARS,
        description="设备图片（可选，base64编码）。有长度上限，防止超大请求打爆内存与配额"
    )
    correlation_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="请求追踪 ID（可选，自动生成）。同一 ID 并发请求会共用 Token 统计，请勿复用"
    )


class DiagnosisResponse(BaseModel):
    status: str
    followup_question: str = ""
    diagnosis: dict = {}
    review: dict = {}
    rebuttal: dict = {}
    final_review: dict = {}
    cost: dict = {}
    workorder: dict = {}
    debate_round: int = 0
    correlation_id: str = ""
    token_usage: dict = {}


class RecordResponse(BaseModel):
    total: int
    offset: int = 0
    limit: int = 100
    statuses: list
    records: list


class HealthResponse(BaseModel):
    status: str
    components: Dict[str, Any]
    version: str = API_VERSION


@app.post("/diagnose", response_model=DiagnosisResponse)
def diagnose(request: DiagnosisRequest, _: None = Depends(require_api_key)):
    """执行故障诊断。

    并发超过 settings.MAX_CONCURRENT_DIAGNOSES 时直接返回 503，而不是排队等待：
    一次诊断动辄数十秒，无声排队只会让调用方一直挂到超时，还占着线程池不放。
    明确拒绝 + Retry-After 让调用方知道"是系统忙，不是我的请求有问题"，
    也避免把上游模型配额打爆。
    """
    correlation_id = request.correlation_id or str(uuid.uuid4())[:8]

    if not _diagnosis_slots.acquire(blocking=False):
        logger.warning("diagnose_rejected_overloaded", correlation_id=correlation_id)
        raise HTTPException(
            status_code=503,
            detail=f"系统繁忙：并发诊断已达上限（{settings.MAX_CONCURRENT_DIAGNOSES}），请稍后重试",
            headers={"Retry-After": "10"},
        )

    try:
        result = run_diagnosis(
            request.fault_description,
            image_base64=request.image_base64 or "",
            correlation_id=correlation_id
        )

        # 仅当完整诊断时才保存记录
        if result.get("status") != "need_more_info":
            save_diagnosis_record(
                request.fault_description,
                result,
                token_usage=result.get("token_usage", {})
            )

        return {
            "status": result.get("status", "unknown"),
            "followup_question": result.get("followup_question", ""),
            "diagnosis": result.get("diagnosis") or {},
            "review": result.get("review") or {},
            "rebuttal": result.get("rebuttal") or {},
            "final_review": result.get("final_review") or {},
            "cost": result.get("cost") or {},
            "workorder": result.get("workorder") or {},
            "debate_round": result.get("debate_round", 0),
            "correlation_id": result.get("correlation_id", correlation_id),
            "token_usage": result.get("token_usage", {}),
        }

    except Exception as e:
        logger.exception("diagnose_failed", correlation_id=correlation_id)
        raise HTTPException(status_code=500, detail=f"诊断失败: {str(e)}")
    finally:
        # 必须放在 finally：诊断中途抛异常时若不放闸，闸门会被永久占掉一格，
        # 连续几次失败后整个 /diagnose 就再也不接请求了。
        _diagnosis_slots.release()


def _sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 消息。

    用 json.dumps 而不是手拼字符串：诊断结果里含大量中文与换行，
    手工拼接时一个未转义的 \\n 就会把一条消息截成两条、前端解析直接崩。
    ensure_ascii=False 保留中文原样，便于 curl 调试时肉眼确认。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/diagnose/stream")
def diagnose_stream(request: DiagnosisRequest, _: None = Depends(require_api_key)):
    """流式诊断：以 SSE 逐节点推送进度与中间状态。

    与 /diagnose 的关系：
    - 复用同一个 orchestrator.run_diagnosis_stream() 生成器，不重复业务逻辑；
    - 复用同一把 _diagnosis_slots 闸门 —— 若 SSE 单独放行，它就成了绕开限流的后门，
      用户可以一边开着流一边并发打满配额；
    - 但闸门**不能像 /diagnose 那样在端点内 finally 释放**：这里是同步生成器，
      函数返回时诊断还没开始跑。因此 acquire 放在进入生成器前，release 放在
      生成器的 finally 里，中间任何分支（含客户端断连）都不会漏放。

    客户端断连时 StreamingResponse 会关闭生成器、触发 GeneratorExit，
    orchestrator 侧对该异常做了显式放行，不会继续烧配额。
    """
    correlation_id = request.correlation_id or str(uuid.uuid4())[:8]

    if not _diagnosis_slots.acquire(blocking=False):
        logger.warning("diagnose_stream_rejected_overloaded", correlation_id=correlation_id)
        raise HTTPException(
            status_code=503,
            detail=f"系统繁忙：并发诊断已达上限（{settings.MAX_CONCURRENT_DIAGNOSES}），请稍后重试",
            headers={"Retry-After": "10"},
        )

    def event_source():
        final_state: dict = {}
        try:
            for label, state in run_diagnosis_stream(
                request.fault_description,
                image_base64=request.image_base64 or "",
                correlation_id=correlation_id,
            ):
                final_state = state
                yield _sse_event("progress", {
                    "label": label,
                    "status": state.get("status", ""),
                    "debate_round": state.get("debate_round", 0),
                    "has_diagnosis": bool(state.get("diagnosis")),
                    "has_workorder": bool(state.get("workorder")),
                    "correlation_id": correlation_id,
                })

            # 落库口径与 /diagnose 保持一致：追问中的轮次不存，
            # 否则历史列表里会塞满"半成品"，用户按关键词翻到的都是没结论的记录。
            record_id = None
            if final_state.get("status") != "need_more_info":
                record_id = save_diagnosis_record(
                    request.fault_description,
                    final_state,
                    token_usage=final_state.get("token_usage", {}),
                )

            yield _sse_event("result", {
                "status": final_state.get("status", "unknown"),
                "followup_question": final_state.get("followup_question", ""),
                "diagnosis": final_state.get("diagnosis") or {},
                "review": final_state.get("review") or {},
                "rebuttal": final_state.get("rebuttal") or {},
                "final_review": final_state.get("final_review") or {},
                "cost": final_state.get("cost") or {},
                "workorder": final_state.get("workorder") or {},
                "debate_round": final_state.get("debate_round", 0),
                "correlation_id": final_state.get("correlation_id", correlation_id),
                "token_usage": final_state.get("token_usage", {}),
                # 带上记录 id，前端才能直接拼出工单下载地址，
                # 不必让用户跑到历史页去翻自己刚做完的这一次。
                "record_id": record_id,
            })
            yield _sse_event("done", {"correlation_id": correlation_id})

        except GeneratorExit:
            # 客户端主动断开。此时生成器已被 close()，再往里 yield 会让 Python 抛
            # `RuntimeError: generator ignored GeneratorExit`，因此这里只记日志后退出。
            logger.warning("diagnose_stream_client_disconnected", correlation_id=correlation_id)
            raise
        except Exception as e:
            # 走到这里说明连 orchestrator 的兜底都没接住（例如落库失败）。
            # 仍以 error 事件收场而不是让连接裸断——前端才有机会显示"失败"而非"卡住"。
            logger.exception("diagnose_stream_failed", correlation_id=correlation_id)
            yield _sse_event("error", {"detail": f"诊断失败: {str(e)}", "correlation_id": correlation_id})
        finally:
            # 唯一释放点。放在生成器 finally 而不是端点 finally，
            # 是因为端点早于诊断执行就返回了。
            _diagnosis_slots.release()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            # 关闭 nginx / 反向代理的响应缓冲。不设这两项时，代理会攒够一个
            # buffer 才下发，前端的"逐节点进度"会退化成"最后一次性全出来"，
            # SSE 白做。
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/records", response_model=RecordResponse)
def records(
    keyword: str = "",
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, description="跳过的记录数，用于翻页"),
    _: None = Depends(require_api_key),
):
    """查询诊断历史记录（支持 keyword / status / limit / offset 分页）"""
    # 只查一次库：此前 total 与 records 各调一次 get_records，白跑一趟 SQL。
    # total 用独立的 COUNT 查询——若拿返回行数当总数，分页时 total 永远等于 limit，
    # 调用方无法判断"还有没有更多"，翻页逻辑会一直以为已经到底。
    rows = get_records(keyword=keyword, status=status, limit=limit, offset=offset)
    return {
        "total": count_records(keyword=keyword, status=status),
        # 回显窗口参数：调用方不必自己记"这次取的是哪一段"
        "offset": offset,
        "limit": limit,
        "statuses": get_distinct_statuses(),
        "records": rows
    }


@app.get("/records/{record_id}/workorder.md", response_class=PlainTextResponse)
def workorder_markdown_endpoint(record_id: int, _: None = Depends(require_api_key)):
    """把某条记录的工单导出为可直接打印/粘贴的 Markdown。

    工单是这套系统的最终交付物，但此前只能拿到 JSON——现场维修人员要的是
    一张能打印的单子，不是嵌套字典。
    """
    record = get_record_by_id(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")

    workorder = record.get("workorder") or {}
    if not workorder:
        raise HTTPException(status_code=404, detail="该记录没有工单（可能只走到了追问环节）")

    markdown = workorder_to_markdown(
        workorder,
        cost=record.get("cost") or {},
        correlation_id=record.get("correlation_id") or "",
    )
    filename = workorder_filename(workorder)
    return PlainTextResponse(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/records/{record_id}")
def get_record(record_id: int, _: None = Depends(require_api_key)):
    """获取单条记录详情"""
    record = get_record_by_id(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在")
    return record


@app.delete("/records/{record_id}")
def delete_record_endpoint(record_id: int, _: None = Depends(require_api_key)):
    """删除指定记录"""
    success = delete_record(record_id)
    if not success:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"message": "删除成功"}


@app.get("/stats")
def stats(_: None = Depends(require_api_key)):
    """获取统计信息"""
    return get_stats()


@app.get("/health/live")
def health_live():
    """存活探针：只确认进程还在，不触碰任何外部依赖，毫秒级返回。
    编排层的 livenessProbe 应该用这个，避免把"上游抖动"误判成"进程该重启"。

    ⚠️ **它在启动过程中也会立刻返回 200**（进程确实活着），
    所以**不能**用它判断"能不能开始诊断"——那要问 `/health/ready`。
    """
    return {"status": "alive", "version": API_VERSION}


@app.get("/health/ready")
def health_ready():
    """就绪探针：启动初始化是否已完成，可否承接诊断请求。

    与 `/health/live` 的分工是刻意区分的：
      - liveness（/health/live）回答"进程要不要被重启" → 启动期间就该是 200
      - readiness（这里）回答"请求现在能不能成功" → 初始化完成前必须是 503

    两者混用是部署里很常见的一类错误：把 liveness 当成"可用"来用，
    就会在冷启动窗口内对外宣告可用，把初始化耗时转嫁成用户的第一次失败。

    不探 LLM / Embedding / ChromaDB（那是 `/health` 的职责，会真烧配额），
    这里只读一个内存里的状态标志，毫秒级返回，可以放心高频轮询。
    """
    st = startup_snapshot()
    payload = {
        "ready": st["ready"],
        "step": st["step"],
        "step_index": st["step_index"],
        "step_total": st["step_total"],
        "elapsed_ms": st["elapsed_ms"],
        "error": st["error"],
        # "起来了但会退化"的提示（如知识库为空）。放在 ready 载荷里而不是只写日志，
        # 是因为用户要在点"开始诊断"**之前**看到——事后从降级工单反推根因太贵。
        "warnings": st["warnings"],
        "version": API_VERSION,
    }
    if not st["ready"]:
        # 503 而不是 200 + ready:false：部署脚本/负载均衡大多只看状态码，
        # 用状态码表达"还不能接活"才不会被忽略。同时保留 body 供前端显示进度。
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/health", response_model=HealthResponse)
def health(fresh: bool = False):
    """就绪探针：探活 LLM、Embedding、ChromaDB、SQLite。

    结果默认缓存 settings.HEALTH_CACHE_TTL 秒——Docker healthcheck 每 30s 打一次，
    每次都真调 LLM + Embedding 既拖慢探活又白烧配额。加 ?fresh=true 可强制真探。
    """
    now = time.time()
    if (not fresh
            and _health_cache["payload"] is not None
            and now - _health_cache["ts"] < settings.HEALTH_CACHE_TTL):
        return _health_cache["payload"]

    payload = _collect_health()
    _health_cache["ts"] = now
    _health_cache["payload"] = payload
    return payload


def _collect_health() -> dict:
    components = {}
    overall_healthy = True

    # 1. SQLite
    try:
        init_db()
        from database import get_db_connection
        with get_db_connection() as conn:
            conn.execute("SELECT 1")
        components["sqlite"] = {"status": "healthy", "path": settings.DB_PATH}
    except Exception as e:
        components["sqlite"] = {"status": "unhealthy", "error": str(e)}
        overall_healthy = False

    # 2. ChromaDB
    try:
        db = get_chroma_db()
        # 简单查询测试
        db.similarity_search("test", k=1)
        components["chromadb"] = {"status": "healthy", "path": settings.CHROMA_PERSIST_DIR}
    except Exception as e:
        components["chromadb"] = {"status": "unhealthy", "error": str(e)}
        overall_healthy = False

    # 3. LLM (诊断模型)
    try:
        llm = get_llm()
        # 简单调用测试
        resp = llm.invoke([{"role": "user", "content": "ping"}])
        components["llm_diagnosis"] = {
            "status": "healthy",
            "model": settings.DIAGNOSIS_MODEL,
            "response_len": len(resp.content) if resp.content else 0
        }
    except Exception as e:
        components["llm_diagnosis"] = {"status": "unhealthy", "error": str(e), "model": settings.DIAGNOSIS_MODEL}
        overall_healthy = False

    # 4. Embedding
    try:
        emb = get_embeddings()
        vec = emb.embed_query("test")
        components["embedding"] = {
            "status": "healthy",
            "model": settings.EMBEDDING_MODEL,
            "dimension": len(vec)
        }
    except Exception as e:
        components["embedding"] = {"status": "unhealthy", "error": str(e), "model": settings.EMBEDDING_MODEL}
        overall_healthy = False

    # 5. Vision LLM (可选)
    try:
        from agents import get_vision_llm
        # 只验证能否构建出视觉模型客户端；不做真实调用（探活不该消耗视觉配额）
        get_vision_llm()
        components["llm_vision"] = {"status": "healthy", "model": settings.VISION_MODEL}
    except Exception as e:
        components["llm_vision"] = {"status": "degraded", "error": str(e), "model": settings.VISION_MODEL}
        # Vision 不影响整体健康状态

    return {
        "status": "healthy" if overall_healthy else "degraded",
        "components": components
    }


@app.get("/")
def root():
    return {
        "name": "FlawScope API",
        "version": API_VERSION,
        "docs": "/docs",
        # 三个探针的分工写在这里，避免调用方挑错端点：
        # live = 进程活着；ready = 能接诊断请求；health = 依赖全健康（会烧配额）
        "health": "/health",
        "health_live": "/health/live",
        "health_ready": "/health/ready"
    }