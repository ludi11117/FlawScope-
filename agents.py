import warnings
warnings.filterwarnings("ignore")

import logging
import jieba
jieba.setLogLevel(logging.WARNING)

import json
import base64
import re
import time
import threading
from collections import OrderedDict
from typing import Optional, Dict, List, Tuple

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_chroma import Chroma
from pydantic import BaseModel, Field, ValidationError
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)

from config import settings
from logging_config import get_logger, get_token_tracker
from prompt_loader import render_prompt, PromptTemplates

load_dotenv()

logger = get_logger(__name__)

# ========== 单例 LLM 实例 ==========

_llm: Optional[ChatOpenAI] = None
_vision_llm: Optional[ChatOpenAI] = None
_embeddings: Optional[OpenAIEmbeddings] = None
_db: Optional[Chroma] = None

# 懒加载单例的构造锁。
#
# 裸的 `if _x is None: _x = 构造()` 在并发下是错的：Streamlit 多会话 + FastAPI
# 线程池会同时进入首次调用，几个线程都读到 None，于是同一个资源被构造多次。
# Chroma 尤其不能这么干——同一个 persist 目录被开出多个 PersistentClient，
# 会争抢底层 SQLite 锁，轻则报 database is locked，重则写出不一致的索引。
# BM25 索引与 TokenTracker 早就按同样的理由加了锁，这四个 getter 是漏网的。
#
# 用 RLock（可重入）而不是 Lock：get_db() 内部要调 get_embeddings()，
# 普通 Lock 会让同一个线程在第二次 acquire 时把自己锁死。
_singleton_lock = threading.RLock()

# BM25 缓存
_bm25_index = None
_bm25_corpus = None
_doc_texts_cache = None
_bm25_initialized = False
# 构建索引要遍历整个知识库做 jieba 分词，代价很高；Streamlit 多会话 + FastAPI
# 线程池会并发进来，必须加锁做双重检查，否则同一份索引会被反复重建。
_bm25_lock = threading.Lock()

# 检索结果缓存（LRU）。
#
# 一次诊断里 retrieve_evidence 最多被调 4 次（初检、每轮辩论、降级时的跨设备参考方向），
# 而辩论重检索的 query 常与初检高度重叠 —— 每次都重算一次 embedding（一次网络往返）。
# 命中缓存省掉的是**配额与耗时**，不是"少查一次库"。
#
# 键里带知识库块数：重建库后块数变化，旧键自然失效，不必手动清。
# 另外 rebuild_bm25_index() 会显式清一次，覆盖"块数恰好没变"的重建场景。
_RETRIEVAL_CACHE_MAX = 64
_retrieval_cache: "OrderedDict[tuple, str]" = OrderedDict()
_retrieval_cache_lock = threading.Lock()


def get_llm() -> ChatOpenAI:
    """获取主诊断 LLM 单例（双重检查 + 加锁）"""
    global _llm
    if _llm is None:
        with _singleton_lock:
            # 等锁期间可能已被别的线程建好了
            if _llm is None:
                _llm = ChatOpenAI(
                    model=settings.DIAGNOSIS_MODEL,
                    openai_api_key=settings.SILICONFLOW_API_KEY,
                    openai_api_base=settings.SILICONFLOW_BASE_URL,
                    temperature=settings.LLM_TEMPERATURE,
                    request_timeout=settings.LLM_REQUEST_TIMEOUT,
                    max_retries=0,  # 我们自己用 tenacity 控制重试
                )
                logger.info("llm_initialized", model=settings.DIAGNOSIS_MODEL)
    return _llm


def get_vision_llm() -> ChatOpenAI:
    """获取视觉 LLM 单例（双重检查 + 加锁）"""
    global _vision_llm
    if _vision_llm is None:
        with _singleton_lock:
            if _vision_llm is None:
                _vision_llm = ChatOpenAI(
                    model=settings.VISION_MODEL,
                    openai_api_key=settings.SILICONFLOW_API_KEY,
                    openai_api_base=settings.SILICONFLOW_BASE_URL,
                    temperature=settings.LLM_TEMPERATURE,
                    request_timeout=settings.LLM_REQUEST_TIMEOUT,
                    max_retries=0,
                )
                logger.info("vision_llm_initialized", model=settings.VISION_MODEL)
    return _vision_llm


def get_embeddings() -> OpenAIEmbeddings:
    """获取嵌入模型单例（双重检查 + 加锁）"""
    global _embeddings
    if _embeddings is None:
        with _singleton_lock:
            if _embeddings is None:
                _embeddings = OpenAIEmbeddings(
                    model=settings.EMBEDDING_MODEL,
                    openai_api_key=settings.SILICONFLOW_API_KEY,
                    openai_api_base=settings.SILICONFLOW_BASE_URL,
                )
                logger.info("embeddings_initialized", model=settings.EMBEDDING_MODEL)
    return _embeddings


def get_db() -> Chroma:
    """获取向量数据库单例（双重检查 + 加锁）

    注意这里是在持锁状态下调用 get_embeddings() 的——这也是 _singleton_lock
    必须用 RLock 的原因，普通 Lock 会在这里自锁死。
    """
    global _db
    if _db is None:
        with _singleton_lock:
            if _db is None:
                _db = Chroma(
                    persist_directory=settings.CHROMA_PERSIST_DIR,
                    embedding_function=get_embeddings()
                )
                logger.info("chromadb_initialized", path=settings.CHROMA_PERSIST_DIR)
    return _db


def get_knowledge_base_size() -> int:
    """返回向量库当前的块数；**取不到时返回 -1**，不要把它当成 0。

    两个刻意的设计：

    1. **不用 `len(get_db().get()["ids"])`**：那会把全部文档 materialize 出来，
       知识库到万级条目后，启动阶段白白多读一遍。`_collection.count()` 是计数查询。
    2. **-1 与 0 必须分开**：0 的含义是"库确实是空的，请先跑 build_knowledge_base.py"；
       -1 的含义是"没查出来，可能是路径/权限/依赖出了问题"。
       把两者合并会让人顺着错误的方向排查——这与"诚实降级"是同一条原则：
       说清楚是"没有"还是"不知道"，别用前者冒充后者。
    """
    try:
        return get_db()._collection.count()
    except Exception as e:  # noqa: BLE001
        logger.warning("knowledge_base_count_failed", error=str(e))
        return -1


# ========== 熔断器模式 ==========

class CircuitBreaker:
    """简单熔断器：连续失败 N 次后跳过调用，防止雪崩。

    两个容易写错的地方，这里都显式处理了：

    1. **成功必须清零失败计数**。若只在 half-open 分支里清零，语义就从"连续失败"
       退化成"累计失败"——成功夹在中间也不重置，几天内攒够 5 次偶发抖动照样熔断。
    2. **必须加锁**。熔断器是模块级单例，Streamlit 多会话 + FastAPI 线程池都会碰它，
       无锁读写 state / failure_count 会出现"半开状态被两个线程同时改写"。

    注意锁只在读写状态时持有，不能包住 func() 本身——否则所有 LLM 调用会被串行化。
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "closed"  # closed, open, half-open
        self._lock = threading.Lock()

    def call(self, func, *args, **kwargs):
        with self._lock:
            if self.state == "open":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "half-open"
                    logger.warning("circuit_breaker_half_open")
                else:
                    raise CircuitBreakerOpenError("熔断器开启，跳过调用")

        try:
            result = func(*args, **kwargs)
        except Exception:
            with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.failure_count >= self.failure_threshold:
                    self.state = "open"
                    logger.error("circuit_breaker_opened", failures=self.failure_count)
            raise
        else:
            with self._lock:
                if self.state == "half-open":
                    self.state = "closed"
                    logger.info("circuit_breaker_closed")
                # 成功即清零：保证"连续失败"这个语义成立
                self.failure_count = 0
            return result


class CircuitBreakerOpenError(Exception):
    pass


_llm_circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)


# ========== 带重试和熔断的 LLM 调用 ==========

def _before_sleep(retry_state):
    logger.warning(
        "llm_retry",
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()) if retry_state.outcome else "unknown"
    )


# 值得重试的 LLM 异常。
#
# 此前是 `retry_if_exception_type((Exception,))`：401（key 错）、400（请求体非法）、
# 上下文超限这类**重试一万次也不会好**的错误，同样要白等 3 次指数退避
# （1.5s / 2.25s / 3.375s）才失败。用户看到的是"卡了很久然后失败"，
# 而不是"立刻告诉你 key 配错了"。
#
# 只保留真正可能是瞬时的四类：限流、超时、连接错误、服务端 5xx。
# 其余异常立刻抛出，由 safe_llm_invoke 统一转成 None（走既有短路逻辑）。
_RETRYABLE_LLM_ERRORS = (
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
)


@retry(
    wait=wait_exponential(multiplier=settings.LLM_RETRY_BACKOFF, min=1, max=10),
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES + 1),
    retry=retry_if_exception_type(_RETRYABLE_LLM_ERRORS),
    before_sleep=_before_sleep,
    reraise=True
)
def _llm_invoke_with_retry(llm: ChatOpenAI, messages):
    """带指数退避重试的 LLM 调用，返回原始响应对象（便于读取服务端真实用量）"""
    return llm.invoke(messages)


def _extract_real_usage(response) -> Optional[Tuple[int, int]]:
    """读取服务端返回的真实 token 用量；取不到返回 None（调用方回退到本地估算）"""
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict) and usage.get("input_tokens") is not None:
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)

    meta = getattr(response, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if isinstance(token_usage, dict) and token_usage.get("prompt_tokens") is not None:
        return int(token_usage.get("prompt_tokens") or 0), int(token_usage.get("completion_tokens") or 0)

    return None


def _content_to_text(content) -> str:
    """响应内容归一化为字符串（多模态响应的 content 可能是 list）"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return "" if content is None else str(content)


def _record_token_usage(llm, messages, content: str, response, correlation_id: str):
    """记录 token 用量：优先服务端真实值，取不到才回退本地估算并标记为估算"""
    tracker = get_token_tracker(correlation_id)

    real = _extract_real_usage(response)
    if real is not None:
        tracker.add_usage(real[0], real[1], estimated=False)
        return

    try:
        prompt_tokens = llm.get_num_tokens_from_messages(messages)
        completion_tokens = llm.get_num_tokens(content) if content else 0
        tracker.add_usage(prompt_tokens, completion_tokens, estimated=True)
    except Exception:
        pass  # Token 统计失败不影响主流程


def safe_llm_invoke(messages, llm: ChatOpenAI = None, correlation_id: str = None) -> Optional[str]:
    """安全调用 LLM，带熔断器、重试、Token 统计。

    返回 None 表示本次调用彻底失败（熔断 / 重试耗尽）。调用方必须显式处理，
    不能把 None 当成"空结果"继续往下走——否则会引发无意义的辩论空转。
    """
    llm = llm or get_llm()

    def _do_invoke():
        return _llm_invoke_with_retry(llm, messages)

    try:
        response = _llm_circuit_breaker.call(_do_invoke)
        content = _content_to_text(getattr(response, "content", response))

        if correlation_id:
            _record_token_usage(llm, messages, content, response, correlation_id)

        return content
    except CircuitBreakerOpenError:
        logger.error("llm_circuit_breaker_open", correlation_id=correlation_id)
        return None
    except Exception as e:
        logger.error("llm_invoke_failed", error=str(e), correlation_id=correlation_id)
        return None


def invoke_and_parse_json(messages, llm: ChatOpenAI = None, max_call_retries: int = 1, correlation_id: str = None) -> dict:
    """调用 LLM 并解析 JSON，带重试。返回 {} 表示本次调用彻底失败（调用方需短路处理）。"""
    for attempt in range(max_call_retries + 1):
        content = safe_llm_invoke(messages, llm, correlation_id)
        if content is None:
            logger.warning("llm_unavailable", attempt=attempt + 1, correlation_id=correlation_id)
            continue
        result = safe_parse_json(content)
        if result:
            return result
        logger.warning("json_parse_failed", attempt=attempt + 1, correlation_id=correlation_id)
    return {}


# ========== Pydantic Schema 模型 (替代手写 validate_schema) ==========

class FaultInfo(BaseModel):
    设备类型: str
    报警代码: Optional[str] = None
    故障现象: List[str] = Field(default_factory=list)
    排除条件: List[str] = Field(default_factory=list)


class DiagnosisOutput(BaseModel):
    报警代码: str
    根因判断: str
    依据: str
    排查建议: List[str]


class ReviewOutput(BaseModel):
    审核意见: str
    理由: str
    风险提示: str


class CostExtractOutput(BaseModel):
    备件清单: List[str] = Field(default_factory=list)
    预计工时: float = Field(default=0.0, ge=0)


class WorkOrderOutput(BaseModel):
    工单编号: str
    故障现象: str
    根因: str
    维修方案: str
    备件清单: List[str] = Field(default_factory=list)
    预计成本: str
    安全注意事项: str


class RebuttalOutput(BaseModel):
    行动: str
    最终根因: str
    依据: str
    置信度: int = Field(ge=0, le=100)
    反驳理由: str


class FinalReviewOutput(BaseModel):
    审核意见: str
    理由: str
    置信度: int = Field(ge=0, le=100)
    风险提示: str


class ExclusionMappingOutput(BaseModel):
    排除编号: List[int] = Field(default_factory=list)


# ========== JSON 解析与验证 ==========

def safe_parse_json(text: str) -> dict:
    """安全解析 JSON，支持提取代码块中的 JSON"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("json_parse_failed_raw", raw_text=text[:200])
    return {}


def validate_and_parse(model_class: type[BaseModel], data: dict, correlation_id: str = None) -> Optional[BaseModel]:
    """使用 Pydantic 验证并解析"""
    try:
        return model_class.model_validate(data)
    except ValidationError as e:
        logger.warning(
            "schema_validation_failed",
            model=model_class.__name__,
            errors=e.errors(),
            correlation_id=correlation_id
        )
        return None


def invoke_and_validate(
    messages,
    model_class: type[BaseModel],
    llm: ChatOpenAI = None,
    max_call_retries: int = 1,
    correlation_id: str = None,
    preprocess=None,
    invoke_fn=None,
) -> Optional[BaseModel]:
    """调用 LLM → 解析 JSON → Pydantic 校验；校验失败时把错误回灌让模型自修。

    与 invoke_and_parse_json 的关键差别：这里把"Schema 不合规"也当成**可恢复错误**。

    此前"模型把 list 写成 str"或"漏了一个字段"这类格式小错，会和"模型彻底挂了"
    一样直接返回 {}，进而短路整条诊断链路，用户拿到的是"模型服务异常"——
    但模型其实好好的，只是格式没对齐。回灌一次报错通常就能修好，代价远小于整链路重来。

    返回 None 表示彻底失败（模型不可用，或修正后仍不合规）。

    `invoke_fn` 允许调用方替换实际发起调用的那一层（默认 `safe_llm_invoke`）。
    存在理由：有些调用方需要**区分"模型没答"和"模型答了但格式不合规"**
    （`extract_fault_info` 的两义返回值就靠它），而这个区分只有在
    "谁真正发了请求"这一层才能观察到。
    """
    invoke_fn = invoke_fn or safe_llm_invoke
    current_messages = list(messages)

    for attempt in range(max_call_retries + 1):
        content = invoke_fn(current_messages, llm, correlation_id)
        if content is None:
            logger.warning("llm_unavailable", attempt=attempt + 1, correlation_id=correlation_id)
            continue

        data = safe_parse_json(content)
        if not data:
            logger.warning("json_parse_failed", attempt=attempt + 1, correlation_id=correlation_id)
            if attempt >= max_call_retries:
                break
            # JSON 根本解析不出来同样要回灌。此前这里只 `continue`，
            # 于是第二次调用拿到的是**一模一样**的消息 —— 模型没有任何新信息，
            # 大概率再错一遍。回灌 Schema + 原始输出 + "解析失败"这条事实，
            # 模型才有机会把 Markdown 代码块、前后解释文字去掉。
            current_messages = current_messages + [
                HumanMessage(content=render_prompt(
                    PromptTemplates.FIX_SCHEMA,
                    schema=json.dumps(model_class.model_json_schema(), ensure_ascii=False),
                    errors="无法从你的回复中解析出 JSON 对象（可能被 Markdown 代码块包裹，"
                           "或前后带了说明文字）",
                    raw=content,
                ))
            ]
            continue

        if preprocess is not None:
            data = preprocess(data)

        try:
            return model_class.model_validate(data)
        except ValidationError as e:
            errors = e.errors()
            logger.warning(
                "schema_validation_failed",
                model=model_class.__name__,
                errors=errors,
                attempt=attempt + 1,
                correlation_id=correlation_id
            )
            if attempt >= max_call_retries:
                break

            current_messages = current_messages + [
                HumanMessage(content=render_prompt(
                    PromptTemplates.FIX_SCHEMA,
                    schema=json.dumps(model_class.model_json_schema(), ensure_ascii=False),
                    errors=json.dumps(errors, ensure_ascii=False, default=str),
                    raw=content,
                ))
            ]

    return None


# ========== BM25 索引缓存 ==========

def _init_bm25():
    """初始化 BM25 索引（仅首次调用时执行，加锁避免并发重复构建）"""
    global _bm25_index, _bm25_corpus, _doc_texts_cache, _bm25_initialized

    if _bm25_initialized:
        return

    with _bm25_lock:
        # 双重检查：等锁期间可能已被别的线程建好了
        if _bm25_initialized:
            return

        logger.info("bm25_index_building")
        db = get_db()
        all_docs = db.get() or {}
        _doc_texts_cache = all_docs.get("documents") or []

        if not _doc_texts_cache:
            # 知识库为空（首次克隆后尚未执行 build_knowledge_base.py）。
            # 此时 BM25Okapi([]) 会因平均 IDF 除零直接抛异常，必须提前短路，
            # 让流程退化到"仅向量检索"，而不是让整个诊断崩在检索这一步。
            _bm25_index, _bm25_corpus = None, []
            _bm25_initialized = True
            logger.warning("bm25_index_skipped_empty_knowledge_base")
            return

        def tokenize(text: str) -> List[str]:
            return list(jieba.cut(text))

        _bm25_corpus = [tokenize(doc) for doc in _doc_texts_cache]

        from rank_bm25 import BM25Okapi
        _bm25_index = BM25Okapi(_bm25_corpus)
        _bm25_initialized = True
        logger.info("bm25_index_built", doc_count=len(_doc_texts_cache))


def _get_bm25():
    """获取 BM25 索引和文档缓存"""
    _init_bm25()
    return _bm25_index, _doc_texts_cache


def rebuild_bm25_index():
    """强制重建 BM25 索引（知识库更新后调用）。

    同时清空检索结果缓存：缓存键里的"知识库块数"只覆盖"块数变了"的情况，
    重建后块数恰好相同（例如改了某条内容但没增删）时旧结果会残留 ——
    那正是最需要失效的场景。
    """
    global _bm25_initialized
    with _bm25_lock:
        _bm25_initialized = False
    with _retrieval_cache_lock:
        _retrieval_cache.clear()
    _init_bm25()


# ========== 备件价格表和成本计算工具 ==========

PARTS_PRICE = settings.PARTS_PRICE
LABOR_RATE_PER_HOUR = settings.LABOR_RATE_PER_HOUR


def calculate_cost(parts: list, hours: float) -> dict:
    """根据备件清单和工时，精确计算维修成本。规则计算，不靠模型估算。"""
    parts_cost = 0
    unknown_parts = []
    for part in parts:
        price = PARTS_PRICE.get(part.strip())
        if price is None:
            unknown_parts.append(part)
        else:
            parts_cost += price

    labor_cost = hours * LABOR_RATE_PER_HOUR
    total = parts_cost + labor_cost

    return {
        "备件费用": parts_cost,
        "工时费用": labor_cost,
        "总费用": total,
        "未知备件": unknown_parts
    }


# ========== 核心 Agent 函数 ==========

def extract_fault_info(user_input: str, correlation_id: str = None) -> Optional[dict]:
    """从用户自由文本中提取结构化故障信息。

    返回值语义是**两义**的，调用方必须区分，不能合并处理：

      - ``dict`` ：抽取完成。字段可能为空列表/空串，代表"用户描述确实不充分"，
                   此时应当追问用户。
      - ``None`` ：模型调用彻底失败（熔断 / 重试耗尽）。此时**必须短路**，
                   绝不能退化成"信息不足"去追问用户——那是把服务故障甩锅给用户。

    此前两种情况都返回 ``{}``，于是模型挂掉时用户会被要求"补充设备类型"。

    实现上接进统一的 `invoke_and_validate` 通道（校验失败会把 Schema + 原始输出 +
    报错回灌让模型自修），但**两义返回值必须保住** —— 所以用一个探针记录
    "这一轮到底有没有收到过内容"，见下面 `_tracking_invoke`。
    """
    prompt = render_prompt(PromptTemplates.EXTRACT_FAULT_INFO, user_input=user_input)
    messages = [
        SystemMessage(content="你是工业设备故障信息抽取专家，擅长从口语化描述中提取结构化信息。"),
        HumanMessage(content=prompt)
    ]

    # invoke_and_validate 对"模型没答"和"答了但格式不合规"都返回 None，
    # 而这两种情况的处理**必须不同**（前者短路成 llm_failed，后者走追问）。
    # 唯一的观察点就是真正发请求的那一层，所以在这里包一层探针。
    saw_content = {"value": False}

    def _tracking_invoke(msgs, llm=None, cid=None):
        content = safe_llm_invoke(msgs, llm, cid)
        if content is not None:
            saw_content["value"] = True
        return content

    validated = invoke_and_validate(
        messages, FaultInfo, correlation_id=correlation_id, invoke_fn=_tracking_invoke
    )
    if validated:
        return validated.model_dump()

    if not saw_content["value"]:
        logger.error("extract_info_llm_failed", correlation_id=correlation_id)
        return None

    # 模型答了、但字段始终不合规：这是"没抽出来"，不是"服务挂了"
    return {}


def _reciprocal_rank_fusion(ranked_lists: List[List[str]], k: int, rrf_k: int = None) -> List[str]:
    """RRF（Reciprocal Rank Fusion）融合多路检索结果。

        score(doc) = Σ 1 / (rrf_k + rank_i(doc))

    只用排名、不用原始分数，因此不需要对 BM25 分与余弦相似度做量纲归一化；
    每一路检索的头部文档都能进入最终结果，避免"后一路被整体截断"。
    """
    rrf_k = rrf_k or settings.RRF_K
    scores: Dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc in enumerate(ranked, start=1):
            if not doc:
                continue
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (rrf_k + rank)
    # 同分时短文档优先（信息密度更高）
    return sorted(scores, key=lambda d: (-scores[d], len(d)))[:k]


def _normalize_query(text: str) -> str:
    """检索 query 的规范化：只压空白。

    不做分词/大小写之外的改写——缓存键必须**保守**：多命中一次是省一次调用，
    误命中一次就是拿别的故障的证据回答本次故障。所以只抹掉纯粹表示形式差异的部分。
    """
    return " ".join(str(text or "").split())


def device_filter(device: str) -> Optional[dict]:
    """按设备过滤检索结果的 Chroma `where` 条件；未启用或设备名为空时返回 None。

    ⚠️ **默认关闭**（`settings.RETRIEVAL_DEVICE_FILTER`），并且开启前必须先重建向量库：

      1. 现有 `chroma_db/` 是**没有 metadata** 的旧库。带 `where` 条件去查一个
         没有该字段的集合，结果是**召回为空** → 全线误降级。这不是"过滤太严"，
         是"过滤条件根本对不上数据"。
      2. 一刀切过滤本身也有风险：用户写「空压机」而库里章节名是「空气压缩机」时，
         本来能召回的证据会被整段滤掉。
      3. 所以它更合适的形态是**降级前的二次尝试**：先不带过滤检索；结果为空、
         或设备护栏判定不匹配时，再带过滤重试一次，命中就继续、没命中才降级。
         那样才不会把"过滤太严"变成"直接降级"。

    第 3 条会牵动误降级指标（README 指标⑦），属于需要产品决策的改动，
    因此这里只把**开关与数据**留好，不默认启用。
    """
    if not settings.RETRIEVAL_DEVICE_FILTER or not device:
        return None
    return {"device": device}


def retrieve_evidence(fault_description: str, k: int = None, correlation_id: str = None,
                      device: str = None) -> str:
    """混合检索（向量 + BM25，RRF 融合），带结果缓存。

    为什么加缓存：一次诊断里本函数最多被调 4 次（初检、每轮辩论、降级时的
    `find_cross_device_hints`），而辩论重检索的 query 常与初检高度重叠。
    每次调用都要打一次 embedding API（网络往返 + 配额），重复的完全可以省掉。

    缓存键 = (规范化 query, k, 知识库块数)。带上块数是为了让"重建知识库"天然失效：
    块数变了键就变了，不会读到旧库的证据。块数恰好没变的重建由
    `rebuild_bm25_index()` 显式清缓存兜住。
    """
    k = k or settings.RETRIEVAL_K
    db = get_db()

    # 设备过滤默认关闭（见 device_filter 的说明）。开启时它同时进缓存键：
    # 同一 query 带/不带过滤是两次不同的检索，混用会拿到错的证据。
    filters = device_filter(device)

    # 先取 BM25 索引：既要用它的文档数做缓存键，也要用它做关键词检索。
    # 空库时 _get_bm25 会短路成 (None, [])，下面两条通道各自退化。
    bm25_index, doc_texts = _get_bm25()
    cache_key = (_normalize_query(fault_description), k, len(doc_texts or []),
                 tuple(sorted((filters or {}).items())))

    with _retrieval_cache_lock:
        cached = _retrieval_cache.get(cache_key)
        if cached is not None:
            # LRU：命中即置为最近使用，淘汰时才能淘汰到真正冷的那条
            _retrieval_cache.move_to_end(cache_key)
    if cached is not None:
        logger.info("retrieve_cache_hit", query=fault_description[:50], correlation_id=correlation_id)
        return cached

    # 通道一：向量语义检索（擅长"一顿一顿"≈"转速不稳"这类语义改写）
    # filters 为 None 时与不带过滤完全等价（Chroma 的默认行为）
    vector_docs = db.similarity_search(fault_description, k=k, **({"filter": filters} if filters else {}))
    vector_texts = [doc.page_content for doc in vector_docs]

    # 通道二：BM25 关键词检索（擅长报警代码、型号等精确 token）
    # 两路各自取 k 再融合，是 RRF 的标准用法：每一路都先给自己最相关的文档，
    # 由融合算法决定最终名额。此前两路共用一个 k，BM25_K 配置形同虚设。
    bm25_texts = []
    if bm25_index is not None and doc_texts:
        tokenized_query = list(jieba.cut(fault_description))
        bm25_scores = bm25_index.get_scores(tokenized_query)
        bm25_k = settings.BM25_K
        bm25_top_indices = sorted(range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True)[:bm25_k]
        bm25_texts = [doc_texts[i] for i in bm25_top_indices if bm25_scores[i] > 0]

    # 排名融合：两路各自的高排名文档都保留，避免单一通道垄断结果
    combined = _reciprocal_rank_fusion([bm25_texts, vector_texts], k)

    if not combined:
        logger.info("retrieve_no_results", query=fault_description[:50], correlation_id=correlation_id)
        result = "【知识库无相关依据】"
    else:
        result = "\n\n".join([f"【资料{i}】\n{text}" for i, text in enumerate(combined, 1)])
        logger.info(
            "retrieve_done",
            bm25_hits=len(bm25_texts),
            vector_hits=len(vector_texts),
            fused=len(combined),
            correlation_id=correlation_id
        )

    with _retrieval_cache_lock:
        if len(_retrieval_cache) >= _RETRIEVAL_CACHE_MAX:
            _retrieval_cache.popitem(last=False)  # 淘汰最久未使用的
        _retrieval_cache[cache_key] = result
        _retrieval_cache.move_to_end(cache_key)
    return result


def _normalize_code(text: str) -> str:
    """报警代码归一化：抹掉分隔符与大小写差异。

    现场写法很不统一，同一个代码会写成 E-203 / E203 / e 203 / E_203。
    此前用精确子串比对，这些写法会被判成"资料里没有该代码"，触发误降级
    （README 指标⑦ 误降级率）。归一化后只比较字母数字本体。
    """
    return re.sub(r"[\s\-_/．.]", "", str(text)).lower()


EQUIPMENT_SYNONYMS = {
    "空气压缩机": ("空压机", "压缩机"),
    "空压机": ("空气压缩机",),
    "数控机床": ("数控车床", "加工中心", "机床"),
    "主轴": ("主轴电机",),
    "伺服驱动器": ("伺服", "伺服系统"),
    "工业机器人": ("机器人",),
    "冷水机组": ("冷机组", "冷水机", "制冷机组"),
    "布袋除尘器": ("除尘器", "除尘系统"),
    "输送带": ("皮带机", "皮带输送机"),
    "液压系统": ("液压站", "液压"),
    "气动系统": ("气动", "气缸系统"),
    # --- 2026-09-26 扩库新增的三个设备 ---
    # 「风机」/「锅炉」/「冷却塔」会被 jieba 切成整词（不会降级成「风机」之外的短词），
    # 用户写法又极不统一（"引风机""送风机""鼓风机"都是同一台机器在不同工位的叫法），
    # 不补同义词就会出现"库里明明有、却因换了个叫法而误降级"。
    # 「冷却塔」尤其要补：「制冷机组」的别名已在上面，用户把冷却塔报成「冷却塔系统」时
    # 只剩「冷却」可用，而那是**确无指向性**的泛词，靠它命中等于没护栏。
    "离心风机": ("风机", "引风机", "送风机", "鼓风机", "罗茨风机"),
    "工业锅炉": ("锅炉", "蒸汽锅炉", "热水锅炉"),
    "冷却塔": ("凉水塔", "冷却水塔"),
}


# 设备名里几乎必然出现、但**不指向任何具体设备**的词。
#
# 这些词是"单 token OR"漏洞的主要来源：离线探针实测（8 类设备 × 全部跨设备条目），
# 旧实现的 21 次跨设备误放行里有 **14 次**只靠「系统」蒙混过关 ——
# 「液压系统」的 token 里有「系统」，而任何一份资料都可能写着"冷却系统""控制系统"。
# 「机床」则相反：它是「数控机床」在 `t[:2]` 降级后唯一的可用词，
# 而数控机床章节的标题写的是"主轴电机"、正文也只在个别地方提"机床"，
# 一旦把它也拉黑，旗舰用例（数控机床 E-203）会被误判成"无依据"。
# 所以黑名单只收**确定没有指向性**的词，宁少勿多。
_GENERIC_DEVICE_TOKENS = frozenset({
    "系统", "设备", "装置", "机器", "机构", "部件", "组件", "生产线",
})


def _equipment_tokens(device: str) -> list:
    """设备类型的分词结果（含同义词展开），用于与检索资料比对。

    `t[:2]` 降级是有意保留的：它让「龙门加工中心」这类带前缀的写法仍能命中
    「加工中心」的同义词族，也让「数控机床」在 `jieba` 只切出一个词时
    还能提供「数控」「机床」两个抓手。它带来的"泛词混入"由
    `_GENERIC_DEVICE_TOKENS` 在判定处过滤，而不是在这里删掉。
    """
    tokens = [t for t in jieba.cut(device) if len(t) >= 2]
    expanded = list(tokens)
    for token in tokens:
        for canonical, aliases in EQUIPMENT_SYNONYMS.items():
            if token == canonical or token in aliases:
                expanded.append(canonical)
                expanded.extend(aliases)
    # 长于 2 的字串降级为头部名词，容忍"龙门加工中心"这类带前缀的写法
    expanded.extend(t[:2] for t in tokens if len(t) > 2)
    # 保序去重
    seen, out = set(), []
    for t in expanded:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def is_equipment_in_evidence(fault_info: dict, evidence: str) -> bool:
    """确定性护栏：抽取出的设备类型/报警代码若未出现在检索资料中，直接判定无依据。

    设备类型比对走 `_equipment_tokens` 做同义词展开，但**先滤掉泛词**
    （`_GENERIC_DEVICE_TOKENS`）。

    为什么必须滤：旧实现是"任一 token 命中即放行"，而「系统」这类词几乎在任何
    设备资料里都能命中，护栏于是形同虚设。离线探针实测（8 类设备 × 全部跨设备条目，
    327 组）：无报警代码时跨设备证据被放行 **6.4%**，其中 14/21 次只靠「系统」过关；
    滤掉泛词后降到 **1.5%**。

    为什么不用更严的规则（例如"必须两个 token 同时命中"）：数控机床章节的标题写的是
    「一、主轴电机报警代码E-203」，正文也不提「数控」——过严的规则会把
    **同设备**的条目判成"无依据"。离线探针实测：更严的规则会把 33 条同设备条目
    里的 12 条拦下（旧实现是 10 条），等于把旗舰用例的误降级率推高。
    泛词黑名单在"拦下跨设备"与"不误伤同设备"之间给出了更好的折中。

    历史：此前是裸的精确子串比对，遇到"用户说空气压缩机、知识库写空压机"这类
    **同义不同名**就会误判无依据（README 指标⑦ 误降级）。这个问题在知识库只有
    3 个主题时暴露不出来——那时"空压机"只出现在对抗案例里，误降级与正确的降级
    长得一模一样。扩库后它才浮出水面。
    """
    if "【知识库无相关依据】" in evidence:
        return False

    code = fault_info.get("报警代码")
    if code and str(code).strip().lower() not in ("", "null", "none", "n/a"):
        normalized = _normalize_code(code)
        # 过短的代码（如 "1"）归一化后几乎能命中任意文本，此时不做代码级判定，
        # 交给后面的设备类型与语义相关性去把关。
        if len(normalized) >= 3 and normalized not in _normalize_code(evidence):
            return False

    device = fault_info.get("设备类型") or ""
    if device:
        tokens = [
            t for t in _equipment_tokens(device) if t not in _GENERIC_DEVICE_TOKENS
        ]
        if tokens and not any(t in evidence for t in tokens):
            return False

    return True


def check_relevance(fault_description: str, evidence: str, correlation_id: str = None) -> Optional[bool]:
    """语义相关性核验。

    返回 True / False 表示核验结论；返回 None 表示核验本身没做成（模型不可用，
    或模型没按"只回答两个字"的格式作答、无法可靠解读）。

    调用方必须区分这两者——"资料不相关"和"没验成"不该给用户同一个理由。
    此前判定是 `"不相关" not in content`，任何非预期输出（如"无法判断"）都会
    被当成"相关"放行，等于把这道防线变成了默认通过。
    """
    if "【知识库无相关依据】" in evidence:
        return False

    prompt = render_prompt(PromptTemplates.CHECK_RELEVANCE, fault_description=fault_description, evidence=evidence)
    messages = [
        SystemMessage(content="你是严格的知识库审核员。"),
        HumanMessage(content=prompt)
    ]
    content = safe_llm_invoke(messages, correlation_id=correlation_id)
    if content is None:
        return None

    # 只解读回答的开头部分：提示词要求先给结论，后面的补充说明不该影响判断
    head = content.strip()[:20]
    if "不相关" in head or "无关" in head:
        return False
    if any(w in head for w in ("无法", "不能", "不确定", "难以")):
        return None
    if "相关" in head:
        return True

    logger.warning("relevance_verdict_unrecognized", raw=content.strip()[:50], correlation_id=correlation_id)
    return None


# ========== 知识库原因解析与排除映射 ==========

# 契约：'可能原因' 条目必须能被下面这几组正则识别出来。
# 放宽格式是**有意的**——本项目的知识库是"数据接入容器"，别人会把自己厂里的
# 真实资料灌进来，而真实资料常见的写法并不统一（Markdown 标题、无序列表、
# Excel 导出的制表符分隔）。若只认一种写法，别人换完数据后 extract_kb_causes
# 返回空列表，排除条件映射会**静默**失效（map_excluded_causes 对空 entries 直接
# 返回 []），护栏整条不见、却不报错。详见 docs/数据接入契约.md。

# 章节标题：'一、xxx' / '## xxx' / '### xxx'
_SECTION_PATTERNS = [
    re.compile(r"^[一二三四五六七八九十]+、\s*(.+)$"),
    re.compile(r"^#{1,6}\s+(.+)$"),
]
# 键名行：`'可能原因：xxx'` / `'可能原因\txxx'` / `'排查建议：'` / `'**故障现象**：xxx'`
# 首字段限定 1–12 字且不含冒号，避免把 '故障现象：主轴转速不稳定。' 这类
# 正常句子误判成键名行（那会让 rest 为空、丢内容）。
_KEYED_LINE = re.compile(r"^([^:：\t]{1,12})\s*[:：\t]\s*(.*)$")
# 列表条目：'1. xxx' / '1、xxx' / '- xxx' / '* xxx' / '• xxx'
_LIST_ENTRY = re.compile(r"^(?:\d+\s*[.、]|[-*•])\s*(.+)$")

# 小节名归一：这些标题下的条目才可能是"可能原因"
_CAUSE_SECTIONS = ("可能原因", "原因", "故障原因")
# 其他已知小节：出现在这些标题下的条目**不算原因**。
# 旧版没有这层判断，会把 '排查建议' 下的 '- 检查加工程序' 也当成一条原因，
# 于是排除映射的候选集里混进了处置措施——LLM 可能把"更换轴承"这类建议
# 误映射成原因编号。这属于既有缺陷，随本次格式放宽一并修掉。
_NON_CAUSE_SECTIONS = ("排查建议", "故障现象", "经验分歧", "设备类型", "报警代码",
                       "处理措施", "维修方案", "安全注意", "备件")


def _strip_md(text: str) -> str:
    """去掉 Markdown 装饰，只留可读文本。

    真实资料里 '**主轴轴承损坏**' 与 '主轴轴承损坏' 是同一个原因，
    不抹掉装饰会导致同一条原因在去重时被当成两条、也会污染发给模型的分组名。
    """
    text = text.strip()
    text = re.sub(r"^\*{1,2}(.+?)\*{1,2}\s*[:：]?\s*$", r"\1", text)  # **加粗** 整行
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^[#>\s]+", "", text)                                # 标题/引用符
    text = text.replace("`", "").strip()
    return text


def _section_kind(name: str) -> str:
    """把小节名归类为 'cause'（可能原因）/ 'non_cause'（其他已知小节）/ ''（未知）。"""
    n = name.strip()
    for k in _NON_CAUSE_SECTIONS:
        if n.startswith(k):
            return "non_cause"
    for k in _CAUSE_SECTIONS:
        if n.startswith(k):
            return "cause"
    return ""


def extract_kb_causes(evidence: str) -> list:
    """从检索证据文本解析'可能原因'条目，返回 [(章节, 原因), ...]。

    支持的写法（都是真实资料里常见的）：
      · 章节：``一、主轴电机`` / ``## 主轴电机`` / ``### 主轴电机``
      · 条目：``1. 原因`` / ``1、原因`` / ``- 原因`` / ``* 原因`` / ``• 原因``
      · 带键：``可能原因：原因`` / ``可能原因\\t原因``（Excel 导出常见）
      · 装饰：``**原因**`` 会先被抹平
      · **省略小节名**：设备标题下直接跟 ``1. 原因`` 列表（不写"可能原因："）也认

    判定"这条算不算原因"的规则（三态小节 + 数字列表退回）：
      · 进入 ``可能原因`` 小节 → 之后条目都算原因（直到下一个小节）
      · 进入 ``排查建议``/``故障现象`` 等已知非原因小节 → 之后条目不收
      · ``-``/``*`` 纯符号列表**只在小节明确时才收**——因为"排查建议"下几乎
        全是这种符号列表，盲收会把处置措施混进原因候选集
      · ``1.``/``1、`` 数字列表在**小节未明确**时仍收（兼容"设备标题下直接列原因"
        的简写资料）。这是有意的方向性取舍：漏收原因会让排除映射失去候选，
        代价大于多收。

    解析不出任何条目是**合法但危险**的状态——调用方（见
    ``knowledge_base_health``）必须显式告警，不能让排除映射静默失效。
    """
    entries = []
    section = ""          # 当前设备/章节名
    section_state = "unknown"   # unknown / cause / non_cause
    seen = set()

    def _add(text: str):
        if text and text not in seen:
            seen.add(text)
            entries.append((section, text))

    for raw_line in evidence.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 1) 键名行：'可能原因：xxx' / '可能原因\txxx' / '排查建议：' / '**故障现象**：xxx'
        #    统一由 _KEYED_LINE 处理——它同时覆盖"带正文"与"仅小节名"两种形态，
        #    且天然支持冒号与制表符（Excel 导出常见）。
        m = _KEYED_LINE.match(line)
        if m:
            key, rest = _strip_md(m.group(1)), _strip_md(m.group(2))
            kind = _section_kind(key)
            if kind:
                section_state = "cause" if kind == "cause" else "non_cause"
                # '可能原因：原因' 这种同行的正文也要收
                if kind == "cause" and rest:
                    _add(rest)
                continue

        # 2) 章节标题：'一、xxx' / '## xxx'
        if len(line) < 40:
            for pat in _SECTION_PATTERNS:
                m = pat.match(line)
                if m:
                    cand = _strip_md(m.group(1))
                    kind = _section_kind(cand)
                    if kind == "cause":
                        section_state = "cause"
                    elif kind == "non_cause":
                        section_state = "non_cause"
                    else:
                        # 新的设备/未知章节：切换 section，小节状态重置为未明确
                        section = cand
                        section_state = "unknown"
                    break
            else:
                m = None
            if m:
                continue

        # 3) 列表条目
        m = _LIST_ENTRY.match(line)
        if m:
            is_numbered = bool(re.match(r"^\d+\s*[.、]", line))
            # 非原因小节里一律不收；符号列表只在"明确是原因小节"时才收
            if section_state == "non_cause":
                continue
            if section_state == "unknown" and not is_numbered:
                continue
            _add(_strip_md(m.group(1)))

    return entries


def extract_kb_disagreements(evidence: str) -> list:
    """从检索片段里抽出「经验分歧」小节，返回 [{"条目": 章节名, "分歧": [条目, ...]}]。

    知识库刻意保留了多位师傅对同一故障的不同判断（见 `docs/为什么必须多Agent.md`）。
    这些分歧是**辩论的燃料**，但燃料得先被点着——审核师只看诊断结论时，
    无从知道"这条知识里本来就有两派意见"，于是"诊断只取了一派、把有争议的判断
    说成定论"永远不会被判不通过，辩论也就永远不触发（实测触发率仅 30–33%，
    而库里躺着的分歧有 36 处）。

    纯字符串解析、零 LLM：可离线断言，不受判官抖动影响。
    返回空列表是**正常状态**（该次检索没命中带分歧的条目），调用方不必告警。
    """
    out = []
    section = ""
    in_disagreement = False
    current = None

    for raw_line in (evidence or "").splitlines():
        line = raw_line.strip()
        if not line:
            # 空行不结束小节：分歧条目之间可能有空行
            continue

        # 1) 章节标题（`一、主轴电机` / `## 主轴电机` / `## 经验分歧`）
        hit_section = False
        for pat in _SECTION_PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            cand = _strip_md(m.group(1))
            if cand.startswith("经验分歧"):
                # 标题本身就是小节名，等价于 `经验分歧：`
                in_disagreement = True
                current = {"条目": section, "分歧": []}
                out.append(current)
            elif _section_kind(cand) == "":
                # 新的设备/条目名：切换当前条目，并结束上一段分歧
                section = cand
                in_disagreement = False
                current = None
            else:
                in_disagreement = False
                current = None
            hit_section = True
            break
        if hit_section:
            continue

        # 2) 分歧段内的列表条目。**必须排在键名行之前**：`- 两人一致的地方：xxx`
        #    既是列表条目、又满足键名行的形态（首字段 ≤12 字且不含冒号），
        #    而它显然是条目。让键名行先判会把这类条目整条吞掉。
        if in_disagreement and current is not None:
            lm = _LIST_ENTRY.match(line)
            if lm:
                current["分歧"].append(_strip_md(lm.group(1)))
                continue

        # 3) 键名行：`经验分歧：` 进入分歧段；其它已知小节结束它
        m = _KEYED_LINE.match(line)
        if m:
            key = _strip_md(m.group(1))
            rest = _strip_md(m.group(2))
            if key.startswith("经验分歧"):
                in_disagreement = True
                current = {"条目": section, "分歧": []}
                out.append(current)
                if rest:
                    current["分歧"].append(rest)
            elif _section_kind(key):
                in_disagreement = False
                current = None
            continue

    return [d for d in out if d["分歧"]]


def _format_disagreements(disagreements: list) -> str:
    """把分歧渲染成给审核师看的清单。

    空时给一句**明确的"未发现"**，而不是留空——留空与"模板没渲染上"
    在提示词里长得一样，审核师会分不清"这次真没有"和"系统没查"。
    """
    if not disagreements:
        return "（本次检索命中的资料中未发现经验分歧）"
    lines = []
    for d in disagreements:
        lines.append(f"- 关于「{d.get('条目') or '未标条目'}」：")
        lines.extend(f"  · {x}" for x in d.get("分歧", []))
    return "\n".join(lines)


# 知识库健康阈值：低于此值说明"换进来的数据格式没被正确解析"。
# 取值依据：本项目现有知识库解析出 104 条；真实资料即使只写三五台设备，
# 也应在数十条量级。低于 1 条就是明确的格式故障，必须报出来。
_MIN_EXPECTED_CAUSES = 1


def knowledge_base_health(text: str) -> dict:
    """检查一份知识库文本的**可解析性**，用于接入自查与构建期守卫。

    返回 dict：entries 条数、章节数、每条明细、以及是否达到最低可解析要求。
    这个函数存在的原因：解析失败在旧版是**完全静默**的——换完数据后
    排除映射不再工作，但没有任何一处报错，用户只会觉得"模型不听话"。
    """
    entries = extract_kb_causes(text)
    sections = []
    for sec, _ in entries:
        if sec and sec not in sections:
            sections.append(sec)
    ok = len(entries) >= _MIN_EXPECTED_CAUSES
    return {
        "entries": len(entries),
        "count": len(entries),
        "sections": sections,
        "section_count": len(sections),
        "ok": ok,
        "detail": entries,
        "reason": "" if ok else (
            "未能从知识库文本中解析出任何『可能原因』条目。"
            "排除条件映射将静默失效。请检查格式是否符合 docs/数据接入契约.md："
            "章节行用『一、设备名』或『## 设备名』，原因条目用『1. 原因』『、原因』或『- 原因』。"
        ),
    }


def _relevant_scope(kb_entries: list, device: str, alarm: str) -> list:
    """把"可能原因"条目收窄到与本次设备/报警代码相关的部分。

    没有设备与报警信息可依据时不做收窄（全量交给映射器判断）；
    但**一旦给了设备/代码却一条都没命中，就返回空**——说明本轮资料里根本没有这个
    设备。此时若放开成全量，排除映射就会跨设备误伤（拿 A 设备的排除条件去删
    B 设备的原因条目）。宁可放弃映射，也不做跨设备的猜测。
    """
    device, alarm = (device or "").strip(), (alarm or "").strip()
    if not device and not alarm:
        return list(range(len(kb_entries)))

    dev_tokens = [t for t in jieba.cut(device) if len(t.strip()) >= 2]
    scope = []
    for i, (title, _) in enumerate(kb_entries):
        if alarm and alarm.lower() in title.lower():
            scope.append(i)
        elif any(t in title for t in dev_tokens):
            scope.append(i)
    return scope


_EXCLUSION_CACHE_MAX = 256
_exclusion_cache: Dict[tuple, list] = {}
_exclusion_cache_lock = threading.Lock()

# 排除描述里几乎必然出现、但不携带指向性的词。
# "液压油位正常" 里的"正常"、"测量正常"里的"测量"，都不是故障原因的一部分；
# 留着它们会把排除描述的 token 集合撑大，稀释真正有指向性的词（如"电缆"）的权重。
_EXCLUSION_STOPWORDS = frozenset({
    "正常", "完好", "良好", "无异常", "没有", "已", "已确认", "检查", "检查过",
    "测量", "测试", "确认", "排除", "更换", "更换过", "刚", "刚刚", "了", "过",
    "并且", "而且", "但是", "目前", "已经", "现在", "情况", "状态", "问题",
})


def _exclusion_tokens(text: str) -> set:
    """排除描述的有效 token：去掉停用词与单字。"""
    return {
        t.strip() for t in jieba.cut(text)
        if len(t.strip()) >= 2 and t.strip() not in _EXCLUSION_STOPWORDS
    }


def _deterministic_exclusion_map(exclusion_list: list, kb_entries: list, scope: list) -> Tuple[list, list]:
    """逐条排除项做确定性映射。

    返回 ``(命中的知识库条目编号列表, 没能命中任何条目的排除项下标列表)``。

    第二项是"要不要叫 LLM"的判据：只有当**还有排除项没有确定性命中**时才值得
    花一次 LLM 调用。全部都能确定性映射时再调一次，是对同一份输入白花钱。

    判据必须是"每一条都有命中"而不是"有命中就行"——后者会让某条排除项被整体漏掉，
    而漏排除正是这里要修的问题（排除条件是用户的硬约束）。
    """
    hits: list = []
    unmatched: list = []
    for pos, excl in enumerate(exclusion_list):
        excl_tokens = _exclusion_tokens(excl)
        if not excl_tokens:
            # 一个有效 token 都没有（如"正常"这类被停用词吃光的短句）：
            # 确定性通道帮不上忙，交给 LLM 看原文。
            unmatched.append(pos)
            continue
        best_i, best_hit = -1, 0
        for i in scope:
            cause_tokens = set(jieba.cut(kb_entries[i][1]))
            hit = sum(1 for t in excl_tokens if t in cause_tokens)
            if hit > best_hit:
                best_hit, best_i = hit, i
        if best_i != -1:
            hits.append(best_i)
        else:
            unmatched.append(pos)
    return hits, unmatched


def _deterministic_exclusion_hits(exclusion_list: list, kb_entries: list, scope: list) -> list:
    """用 token 重叠把排除描述确定性地映射到知识库条目编号（0 基）。

    与 LLM 映射取并集使用。判据是**逐条排除项**各自的最佳匹配：
    每条排除描述至少绑一条原因条目，避免"某条排除项整体被漏掉"。
    要求命中 token 数 > 0 即可，不设比例阈值——排除描述（如"电缆护套完好"）
    与原因条目（"动力电缆在桥架转弯处磨损露出导体"）共用词往往只有一个，
    按比例卡会全部漏掉，而漏排除正是这里要修的问题。
    """
    hits, _ = _deterministic_exclusion_map(exclusion_list, kb_entries, scope)
    return hits


def map_excluded_causes(exclusion_list: list, kb_entries: list, scope: list = None, correlation_id: str = None) -> list:
    """排除映射器：把用户排除描述映射到知识库原因条目编号。

    **顺序是先确定性、后 LLM**：确定性命中的编号先算出来；只有当**还有排除项
    没有确定性命中**时，才为那些漏网的排除项花一次 LLM 调用。

    此前是反过来的（无条件先叫 LLM，再与确定性结果取并集）。从库里
    `token_usage.by_node` 看，诊断与每轮辩论各调一次这个映射，而现场报修里
    大多数排除描述（"液压油位正常"）都能被 token 重叠直接命中 —— 那些调用纯属白花。
    反转后并集语义不变：命中项由确定性通道保证，漏网项交给 LLM，
    仍然"宁可多排除，不可漏排除"。

    带进程内结果缓存：诊断与辩论阶段会各调一次，而辩论阶段的证据常常与初诊相同
    （重新检索命中的还是那几条），没有缓存就会对同一份输入白花一次 LLM 调用。
    缓存键完全由输入构成（排除条件 + 条目 + 范围），知识库变了键自然变，不会读到脏数据。
    """
    if not exclusion_list or not kb_entries:
        return []
    if scope is None:
        scope = list(range(len(kb_entries)))
    if not scope:
        return []

    cache_key = (tuple(exclusion_list), tuple(tuple(e) for e in kb_entries), tuple(scope))
    with _exclusion_cache_lock:
        cached = _exclusion_cache.get(cache_key)
    if cached is not None:
        logger.info("exclusion_mapped_cache_hit", correlation_id=correlation_id)
        return list(cached)

    det_hits, unmatched = _deterministic_exclusion_map(exclusion_list, kb_entries, scope)

    if not unmatched:
        # 每条排除项都确定性命中：LLM 只会重复同一份结论，没必要调。
        idx = sorted(set(det_hits))
        logger.info(
            "exclusion_mapped_deterministic_only",
            excluded_indices=idx,
            items=len(exclusion_list),
            correlation_id=correlation_id,
        )
        with _exclusion_cache_lock:
            if len(_exclusion_cache) >= _EXCLUSION_CACHE_MAX:
                _exclusion_cache.clear()
            _exclusion_cache[cache_key] = list(idx)
        return idx

    # 还有排除项没能确定性映射，交给 LLM 兜底。
    # 只把**漏网的**那些交给它（提示词更短），命中项直接并进结果，并集语义不变。
    numbered = "\n".join(f"{i + 1}. (【{kb_entries[i][0]}】) {kb_entries[i][1]}" for i in scope)
    prompt = render_prompt(
        PromptTemplates.MAP_EXCLUDED_CAUSES,
        exclusion_list=json.dumps([exclusion_list[i] for i in unmatched], ensure_ascii=False),
        numbered=numbered
    )
    messages = [
        SystemMessage(content="你是严谨的工业知识库条目筛选员。"),
        HumanMessage(content=prompt)
    ]
    result = invoke_and_parse_json(messages, correlation_id=correlation_id)

    validated = validate_and_parse(ExclusionMappingOutput, result, correlation_id)
    if validated:
        numbers = validated.排除编号
    else:
        # 兜底解析
        raw = result.get("排除编号") if isinstance(result, dict) else result
        if isinstance(raw, list):
            numbers = [int(i) for i in raw if str(i).isdigit()]
        elif isinstance(raw, (int, str)) and str(raw).isdigit():
            numbers = [int(raw)]
        else:
            numbers = []

    idx = sorted({n - 1 for n in numbers if n - 1 in scope})

    # 与确定性命中取并集：LLM 只看了漏网的那几条，确定性通道负责剩下的，
    # 合起来仍是"每条排除项至少绑一条原因条目"。
    #
    # 历史教训：这里曾写作 `if not idx:`，效果是 LLM 只要返回了任意编号，
    # 确定性匹配就整段跳过。于是 LLM 漏掉一条排除项时无人补救——用户说了两条
    # 排除条件、模型只映射了一条，另一条对应的原因就会留在根因里
    # （README 指标④ 排除条件遵守率）。排除条件是**用户的硬约束**，
    # 漏掉一条等于把用户明确排除的原因又写回去，比多排除一条严重得多。
    idx = sorted(set(idx) | set(det_hits))

    logger.info("exclusion_mapped", excluded_indices=idx, correlation_id=correlation_id)

    with _exclusion_cache_lock:
        if len(_exclusion_cache) >= _EXCLUSION_CACHE_MAX:
            _exclusion_cache.clear()
        _exclusion_cache[cache_key] = list(idx)

    return idx


def remove_excluded_causes(root_cause: str, kb_causes: list, excluded_indices: list) -> str:
    """程序化删除被判为被排除的 KB 原因条目"""
    if not excluded_indices:
        return root_cause
    for i in excluded_indices:
        if not (0 <= i < len(kb_causes)):
            continue
        cause = kb_causes[i]
        first_clause = re.split(r"[，,]", cause)[0]
        candidates = {
            cause, cause.strip("。．"), first_clause,
            f"原因{i + 1}：{cause}", f"原因{i + 1}:{cause}",
            f"原因{i + 1}：{first_clause}", f"原因{i + 1}:{first_clause}",
        }
        for cand in sorted(candidates, key=len, reverse=True):
            if cand and cand in root_cause:
                root_cause = root_cause.replace(cand, "")
                break
    root_cause = re.sub(r"原因\d+\s*[：:]\s*(?=[;；,，、]|$)", "", root_cause)
    root_cause = re.sub(r"[;；,，、]{1,}", "；", root_cause)
    root_cause = re.sub(r"^[;；,，、\s]+", "", root_cause)
    root_cause = re.sub(r"[;；,，、\s]+$", "", root_cause)
    return root_cause


# ========== 各 Agent 实现 ==========

def agent_diagnose(evidence: str, fault: str, exclusion_list: list = None, correlation_id: str = None) -> dict:
    exclusion_list = exclusion_list or []
    kb_entries = extract_kb_causes(evidence)

    # 运行期格式守卫：本轮证据里解析不出任何原因条目时，排除条件映射会静默失效
    # （map_excluded_causes 对空 entries 直接返回 []）。系统照样能出诊断，
    # 但用户说的"已排除某某"不再被遵守，且**没有任何报错**——这类静默失效
    # 与本项目"诚实降级优先"的立场冲突，所以这里显式告警。
    # 注意：只告警不降级——证据片段本就可能不含"可能原因"小节（检索命中了
    # 故障现象段），把它当故障处理会造成大面积误降级。
    if not kb_entries:
        logger.warning(
            "kb_causes_unparsed",
            hint="本轮证据未解析出『可能原因』条目，排除条件映射不可用；"
                 "若长期出现请检查知识库格式（见 docs/数据接入契约.md）",
            evidence_len=len(evidence or ""),
            has_exclusions=bool(exclusion_list),
            correlation_id=correlation_id,
        )

    kb_causes = [c for _, c in kb_entries]

    fault_dict = {}
    try:
        parsed = json.loads(fault)
        if isinstance(parsed, dict):
            fault_dict = parsed
    except Exception:
        pass

    scope = _relevant_scope(kb_entries, fault_dict.get("设备类型", ""), fault_dict.get("报警代码", ""))
    excluded_idx = map_excluded_causes(exclusion_list, kb_entries, scope, correlation_id)

    exclusion_text = ""
    if exclusion_list:
        exclusion_text = "特别注意：以下条件已被用户排除，绝对不能作为根因判断输出：\n"
        for item in exclusion_list:
            exclusion_text += f"- {item}\n"
    if excluded_idx:
        barred = "\n".join(f"- 条目{i + 1}：{kb_causes[i]}" for i in excluded_idx if i < len(kb_causes))
        exclusion_text += "\n以下知识库原因已被用户排除，必须整体剔除，严禁出现在【根因判断】中：\n" + barred + "\n"

    prompt = render_prompt(
        PromptTemplates.AGENT_DIAGNOSE,
        evidence=evidence,
        fault=fault,
        exclusion_text=exclusion_text
    )
    messages = [
        SystemMessage(content="你是严谨的故障诊断专家。"),
        HumanMessage(content=prompt)
    ]
    validated = invoke_and_validate(messages, DiagnosisOutput, correlation_id=correlation_id)
    if not validated:
        return {}

    result_dict = validated.model_dump()

    if exclusion_list and excluded_idx and result_dict.get("根因判断"):
        result_dict["根因判断"] = remove_excluded_causes(result_dict["根因判断"], kb_causes, excluded_idx)
        if not result_dict["根因判断"]:
            result_dict["根因判断"] = "用户描述的现象已被排除所有可能原因，建议人工复核"

    return result_dict


def agent_review(diagnosis: dict, evidence: str = "", fault: str = "",
                 correlation_id: str = None, disagreements: list = None) -> dict:
    """独立审核诊断结论。

    evidence / fault 是审核师**必要的对照物**：没有它们，审核师只能看诊断
    自圆其说的程度，而诊断提示词又强制它写满知识库里的全部原因——结果就是
    审核恒通过、辩论永不触发，多 Agent 退化成"单 Agent + 一次无害审核"。
    传入资料后，审核师才能做「越界/遗漏/排除/依据强度」四项有据可依的检查。

    两个参数给默认值而非必填：历史调用点（以及测试里的桩）可能只传 diagnosis，
    缺参数时退化为"无对照物"的旧行为，而不是直接抛 TypeError。

    `disagreements` 是**知识库里的经验分歧**（多位师傅对同一故障的不同判断），
    不传时从 evidence 现算。它是审核清单第 5 项的靶子：没有它，审核师看不到
    "这条知识本来就有两派意见"，只会照着诊断的自洽性放行。
    """
    if disagreements is None:
        disagreements = extract_kb_disagreements(evidence)
    prompt = render_prompt(
        PromptTemplates.AGENT_REVIEW,
        diagnosis=json.dumps(diagnosis, ensure_ascii=False, indent=2),
        evidence=evidence or "（未提供检索资料）",
        fault=fault or "（未提供原始报修信息）",
        disagreements=_format_disagreements(disagreements),
    )
    messages = [
        SystemMessage(content="你是严格的维修安全审核员，职责是挑战结论而非确认结论。"),
        HumanMessage(content=prompt)
    ]
    validated = invoke_and_validate(messages, ReviewOutput, correlation_id=correlation_id)
    return validated.model_dump() if validated else {}


def agent_cost(diagnosis: dict, review: dict, correlation_id: str = None) -> dict:
    prompt = render_prompt(
        PromptTemplates.AGENT_COST,
        diagnosis=json.dumps(diagnosis, ensure_ascii=False, indent=2),
        review=json.dumps(review, ensure_ascii=False, indent=2)
    )
    messages = [
        SystemMessage(content="你是工业维修成本估算专家，只提取参数，不计算价格。"),
        HumanMessage(content=prompt)
    ]
    validated = invoke_and_validate(messages, CostExtractOutput, correlation_id=correlation_id)
    if not validated:
        return {}

    parts = [str(p).strip() for p in validated.备件清单 if str(p).strip()]
    hours = validated.预计工时
    cost_breakdown = calculate_cost(parts, hours)

    # 价格表未覆盖的备件不计费，但必须显式告知，否则用户会以为报价是完整的
    hint = ""
    if cost_breakdown["未知备件"]:
        hint = "以下备件不在价格表中，未计入报价，需人工核价：" + "、".join(cost_breakdown["未知备件"])

    return {
        "备件清单": parts,
        "预计工时": f"{hours}小时",
        "预计成本": f"{cost_breakdown['总费用']}元",
        "成本明细": cost_breakdown,
        "计费提示": hint
    }


def _normalize_workorder_types(data: dict) -> dict:
    """工单字段类型归一化。

    模型常把"维修方案"写成数组、把"备件清单"写成字符串。这类是**确定性**格式问题，
    用规则修比再花一次 LLM 调用便宜得多，所以放在 preprocess 里先修一遍；
    修不动的（整块缺字段等）才交给 invoke_and_validate 的 schema 修复重试兜底。
    """
    if not isinstance(data, dict):
        return data
    for key in ("工单编号", "故障现象", "根因", "维修方案", "预计成本", "安全注意事项"):
        if isinstance(data.get(key), list):
            data[key] = "；".join(str(x) for x in data[key])
    if isinstance(data.get("备件清单"), str):
        data["备件清单"] = [data["备件清单"]] if data["备件清单"].strip() else []
    return data


def agent_workorder(diagnosis: dict, review: dict, cost: dict, correlation_id: str = None) -> dict:
    prompt = render_prompt(
        PromptTemplates.AGENT_WORKORDER,
        diagnosis=json.dumps(diagnosis, ensure_ascii=False, indent=2),
        review=json.dumps(review, ensure_ascii=False, indent=2),
        cost=json.dumps(cost, ensure_ascii=False, indent=2)
    )
    messages = [
        SystemMessage(content="你是维修工单编制员。"),
        HumanMessage(content=prompt)
    ]
    validated = invoke_and_validate(
        messages,
        WorkOrderOutput,
        correlation_id=correlation_id,
        preprocess=_normalize_workorder_types
    )
    return validated.model_dump() if validated else {}


def _candidate_causes_of(root_cause: str) -> list:
    """把根因文本拆成候选原因列表（去掉"原因N："标签与首尾标点）。"""
    parts = re.split(r"[;；,，]", root_cause or "")
    out = []
    for p in parts:
        p = re.sub(r"^原因\s*\d+\s*[:：、\s]*", "", p.strip())
        p = re.sub(r"^(或|和)+\s*", "", p)
        p = p.strip("。）).。、;；,，:： \t")
        if p:
            out.append(p)
    return out


# 与 orchestrator.DEGRADED_MARKERS 保持一致（不能直接 import：orchestrator 反向依赖本模块）。
# 漂移由 _assert_degraded_markers_in_sync() 在首次使用时校验。
_DEGRADED_MARKERS = ("无法诊断", "无法判断", "无相关依据", "不相关", "调用失败", "校验未能完成")
_DEGRADED_SYNC_CHECKED = False


def _is_degraded_text(text: str) -> bool:
    """文本是否为诚实降级结论。护栏一律不得改写降级结论。"""
    global _DEGRADED_SYNC_CHECKED
    if not _DEGRADED_SYNC_CHECKED:
        _assert_degraded_markers_in_sync()
        _DEGRADED_SYNC_CHECKED = True
    return any(m in (text or "") for m in _DEGRADED_MARKERS)


# ========== 降级报告：答不了的时候，也要交出可用的东西 ==========

DEGRADED_REASON_NO_HIT = "no_hit"
DEGRADED_REASON_EQUIPMENT_MISMATCH = "equipment_mismatch"
DEGRADED_REASON_IRRELEVANT = "irrelevant"
DEGRADED_REASON_RELEVANCE_UNKNOWN = "relevance_unknown"
DEGRADED_REASON_LLM_FAILED = "llm_failed"


# 参考方向里出现的设备名都带这层前缀，明确它不是本设备的结论。
_CROSS_DEVICE_DISCLAIMER = "非本设备根因，执行前请现场确认"


def _extract_check_actions(evidence: str) -> list:
    """从检索片段中抽出『排查建议』小节下的动作条目（`- xxx`）。

    只收**动作**、不收**可能原因**：排查动作跨设备通用——"检查润滑脂状态"
    在哪台设备上都是个安全的检查动作；而"可能原因"是别的设备得出的根因结论，
    照搬到本台设备就是编造（硬约束 4）。这是参考方向能给的边界。
    """
    actions = []
    in_advice = False
    for line in (evidence or "").splitlines():
        s = line.strip()
        if re.match(r"^排查建议\s*[:：]", s):
            in_advice = True
            continue
        if in_advice:
            # 撞上下一个小节标题就退出；空行不退出（小节内可能有换行）
            if re.match(r"^(故障现象|可能原因|经验分歧|设备类型|报警代码)\s*[:：]", s):
                in_advice = False
                continue
            m = re.match(r"^[-*•]\s*(.+)$", s)
            if m:
                actions.append(m.group(1).strip())
    return actions


# 标注来源时优先认**设备**而不是**部件**——「主轴」是部件，说"来自主轴条目"
# 不如"来自数控机床条目"清楚。这里只是给 EQUIPMENT_SYNONYMS 的 key 排优先级，
# 不是第二份副本：认不出时仍回退到遍历全表，所以新增设备不会静默失效。
_DEVICE_HINT_PRIORITY = (
    "空气压缩机", "数控机床", "工业机器人", "伺服驱动器",
    "冷水机组", "布袋除尘器", "输送带", "液压系统", "气动系统",
)


def _device_hint_of(text: str) -> str:
    """在片段里认出一个已知设备名，用于标注参考方向的来源。认不出返回空串。"""
    for name in _DEVICE_HINT_PRIORITY:
        if name in text or any(a in text for a in EQUIPMENT_SYNONYMS.get(name, ())):
            return name
    # 退而求其次：认出部件名（如「主轴」）也比完全不标来源强
    for canonical, aliases in EQUIPMENT_SYNONYMS.items():
        if canonical in text or any(a in text for a in aliases):
            return canonical
    return ""


def find_cross_device_hints(fault_info: dict, k: int = 4, evidence: str = None) -> list:
    """降级时找"同类症状在其它设备上的排查动作"，给用户一个可执行的参考方向。

    检索时**刻意不带设备名**——带上就又会命中不到（本设备本来就不在库里），
    只剩症状词才能横向命中其它设备的同症状条目。

    `evidence` 可注入，供测试离线驱动（真实检索要调 embedding）。

    返回 [{"设备": str, "动作": str}]；找不到返回 []。
    """
    symptoms = (fault_info or {}).get("故障现象") or []
    if not symptoms:
        return []

    query = " ".join(str(s) for s in symptoms)
    if evidence is None:
        try:
            evidence = retrieve_evidence(query, k=k)
        except Exception as e:  # noqa: BLE001
            # 二次检索失败绝不能把降级路径本身带崩：拿不到参考方向就当没有，
            # 判定说明与下一步建议照常产出。降级是最后一道防线，它自己必须最结实。
            logger.warning("cross_device_hints_retrieve_failed", error=str(e))
            return []
    if "【知识库无相关依据】" in evidence:
        return []

    device = _device_hint_of(evidence)
    return [{"设备": device, "动作": a} for a in _extract_check_actions(evidence)]


def build_degraded_report(
    fault_info: dict,
    degraded_reason: str,
    kb_size: int = None,
    cross_device_hints: list = None,
) -> dict:
    """降级时生成可用的补充信息：判定说明 + 下一步建议。**纯函数，零 LLM 调用。**

    为什么要这个：此前降级工单只有「工单编号 / 风险等级 / 风险说明」三行，
    风险说明还是「知识库未覆盖该设备，无法自动诊断，建议人工介入」这种空话——
    用户拿到手等于没拿到东西。**降级不等于可以不输出。**

    三条设计约束（与硬约束 4「诚实降级」配套，不冲突）：

    1. **不补根因**。降级就是不知道根因；这里只输出「已知的事实」与
       「你接下来能做什么」，绝不用推测冒充结论。
    2. **不依赖 LLM**。降级往往正是模型不可用的时候，再调一次模型等于
       把最后一道防线也交给同一个失败源。
    3. **库大小必须区分 0 / -1 / N**。`get_knowledge_base_size()` 用 -1 表示
       「没查出来」，0 表示「确实是空的」。混为一谈会让人顺着错误方向排查——
       与「诚实降级」是同一条原则：说清楚是「没有」还是「不知道」。

    返回：{"判定说明": str, "下一步建议": [str]}
    """
    fault_info = fault_info or {}
    device = (fault_info.get("设备类型") or "").strip()
    symptom_text = " ".join(str(s) for s in (fault_info.get("故障现象") or []))

    if kb_size is None:
        kb_size = get_knowledge_base_size()
    if kb_size == 0:
        kb_note = "知识库当前为空（0 个条目）——请先执行 build_knowledge_base.py 构建向量库"
    elif kb_size is not None and kb_size < 0:
        kb_note = "知识库规模未能确认（可能未初始化或路径异常）"
    else:
        kb_note = f"知识库当前共 {kb_size} 个条目"

    if degraded_reason == DEGRADED_REASON_NO_HIT:
        verdict = f"{kb_note}，本次检索未命中任何相关条目"
        next_steps = [
            "确认设备名是否有其它叫法（如「空压机」与「空气压缩机」），换一种说法重试",
            "若确属缺失资料：补充该设备故障案例到 data/raw/，再跑 build_knowledge_base.py",
        ]
    elif degraded_reason == DEGRADED_REASON_EQUIPMENT_MISMATCH:
        who = device or "该设备"
        verdict = f"{kb_note}，未收录「{who}」的相关案例"
        next_steps = [
            "请确认设备名是否准确；若库内已有该设备的其它叫法，换说法重试",
            f"若确实没有「{who}」的资料：补充案例到 data/raw/ 后重跑 build_knowledge_base.py",
        ]
    elif degraded_reason == DEGRADED_REASON_IRRELEVANT:
        verdict = f"{kb_note}，检索到的资料与本故障描述不相关"
        next_steps = [
            "有报警代码请直接写出，可显著提升检索命中率",
            "补充故障发生时的工况：启动 / 加工 / 空载 / 带载",
        ]
        # 没说清是哪个部件时，这条最值得问——复用部件词表，不另起一份
        if not any(p in symptom_text for p in _PART_SIGNALS):
            next_steps.insert(0, "请说明是哪个部件出问题（如主轴、电机、液压阀、轴承）")
    elif degraded_reason == DEGRADED_REASON_RELEVANCE_UNKNOWN:
        verdict = "相关性校验未能完成（模型不可用），无法确认资料是否可用"
        next_steps = ["请稍后重试", "若持续失败，请检查上游模型服务与网络"]
    elif degraded_reason == DEGRADED_REASON_LLM_FAILED:
        verdict = "诊断链路中模型调用失败，结论可能不完整"
        next_steps = ["请稍后重试", "若持续失败，请检查上游模型服务与网络"]
    else:
        verdict = "未能自动诊断"
        next_steps = ["建议人工介入"]

    # 参考方向：同类症状在**其它设备**上的排查动作。
    # 只给动作、不给根因；且必须写明它不是本设备的结论——否则操作工可能
    # 照着别的设备的排查路径去拆手头这台机器。
    references = []
    if cross_device_hints:
        src = cross_device_hints[0].get("设备") or "知识库中同类症状的其它设备"
        references.append(
            f"以下排查动作来自知识库中「{src}」的同类症状条目——{_CROSS_DEVICE_DISCLAIMER}"
        )
        references.extend(f"- {h['动作']}" for h in cross_device_hints[:3])

    return {
        "判定说明": verdict,
        "下一步建议": next_steps,
        "参考方向": references,
    }


def restore_dropped_candidates(initial_root: str, final_root: str, excluded_causes: list = None,
                               initial_evidence: str = "") -> str:
    """护栏：辩论不得把初诊里"有依据且未被排除"的候选原因静默丢掉。

    初诊的提示词明确要求"有几项写几项、不得自行取舍"，但辩论提示词没有等价约束，
    而且辩论会换查询重新检索。结果是辩论阶段可以把候选收敛掉——表现为
    **核心一致率满分、全覆盖率掉分**（README 指标②）。每个 Agent 单独看都没错，
    是链路上的**约束漂移**，所以这里用确定性代码兜住，而不是再加一句提示词。

    只补回"在初诊里出现、且不属于用户排除项"的原因。若辩论确实基于审核意见判定
    某条不成立，会体现在 final_root 的措辞里，但那种情况无法与"随手丢弃"区分，
    因此按"宁可多列候选"处理——与排除条件护栏同一取舍方向（少列候选的代价更大）。
    """
    if not initial_root or not final_root:
        return final_root
    # 降级结论（含"知识库无相关依据，无法诊断"这类）一律不得改写。
    # 注意：判定必须在**拆分候选之前**做——降级文句里含全角逗号，
    # 拆完会被切碎，再逐片判断就漏了（实测踩过）。
    if _is_degraded_text(final_root) or _is_degraded_text(initial_root):
        return final_root

    initial = _candidate_causes_of(initial_root)
    final = _candidate_causes_of(final_root)
    if not initial or not final:
        return final_root

    dropped = []
    for cand in initial:
        if any(cand == f for f in final):
            continue
        # 已排除的原因不补回
        if any(cand in (e or "") or (e or "") in cand for e in (excluded_causes or [])):
            continue
        # 跨条目的原因不补回：初诊本身也可能抄了别的故障条目（例如把 H-110 的
        # "吸油口过滤器堵塞"写进了离心泵的诊断）。若只按"初诊有"就补回，
        # 越界护栏先清掉的那条会被这里重新捞回来——两道护栏互相抵消。
        # 所以补回同样要过"初检证据有依据"这道关。
        if initial_evidence and not _candidate_grounded_in(cand, initial_evidence):
            logger.info("restore_skipped_out_of_scope", candidate=cand)
            continue
        # 语义重复（一方包含另一方）视为仍被覆盖，不算丢弃
        if any(cand in f or f in cand for f in final):
            continue
        dropped.append(cand)

    if not dropped:
        return final_root

    merged = "; ".join(final + dropped)
    logger.info("rebuttal_restored_dropped_candidates", dropped=dropped)
    return merged


def drop_out_of_scope_candidates(initial_root: str, final_root: str, initial_evidence: str) -> str:
    """护栏：辩论不得把**初检证据里没有出处**的原因写进最终根因。

    与 restore_dropped_candidates 是一对反向约束：
    那个防"该留的被丢"，这个防"不该加的硬加"。

    为什么要这道护栏：辩论节点会用「报修信息 + 驳回理由」**重新检索**，
    这个查询比初检更宽，捞回来的资料可能来自**其他故障条目**
    （同为振动、同为过载，但设备或报警码不同）。辩论提示词只要求
    "引用知识库中的具体证据"，没要求证据必须仍属本条故障——于是诊断师
    会把跨段的"轴承损坏""电源电压波动"当成补充证据写进结论。

    2026-09-18 消融实验实测：多 Agent 幻觉率 26% vs 单 Agent 4%（+22pp），
    且 6 个幻觉**全部**落在辩论触发的用例上，来源可逐条对应到其他条目。

    判定用**初检证据**（而非辩论后的新证据）作为"本条范围"的基准：
    初检查询就是本故障本身，它命中的条目才代表"属于本条"。
    """
    if not initial_root or not final_root or not initial_evidence:
        return final_root
    # 降级结论不得改写（判定必须在拆分候选之前，理由同 restore_dropped_candidates）
    if _is_degraded_text(final_root) or _is_degraded_text(initial_root):
        return final_root

    initial = _candidate_causes_of(initial_root)
    final = _candidate_causes_of(final_root)
    if not final:
        return final_root

    kept = []
    dropped = []
    # 初诊原文用于兜底比对：_candidate_causes_of 会按全角逗号拆分，
    # 把"泵体内未灌满液体，发生气缚"这种**句内因果连接**切成两段。
    # 只靠拆分后的候选列表比对，会把初诊自带的条目误判成"辩论新增"而删掉
    # （实测踩过）。所以额外做一次"规范化子串"比对。
    initial_norm = _normalize_for_scope(initial_root)
    for cand in final:
        # 初诊原有的候选一律保留：它不是辩论新增的，越界责任不在这一步
        is_original = (
            any(cand == i for i in initial)
            or any(cand in i or i in cand for i in initial)
            or _normalize_for_scope(cand) in initial_norm
        )
        if is_original:
            kept.append(cand)
            continue
        # 辩论新增的候选：必须在初检证据里有依据，否则丢弃
        if _candidate_grounded_in(cand, initial_evidence):
            kept.append(cand)
        else:
            dropped.append(cand)

    if not dropped:
        return final_root
    if not kept:
        # 全被判出界：宁可退回初诊，也不要交出一个没有依据的结论
        logger.warning("rebuttal_all_new_candidates_out_of_scope", dropped=dropped)
        return initial_root

    logger.info("rebuttal_dropped_out_of_scope_candidates", dropped=dropped)
    return "; ".join(kept)


def _normalize_for_scope(text: str) -> str:
    """抹掉空白与常见标点，只留字符本体。

    用于"是不是同一句话"的粗比对——分隔符与空格的差异不该影响判定。
    """
    return re.sub(r"[\s，。、；：,.;:（）()【】\[\]\"'“”‘’]", "", text or "")


def _candidate_grounded_in(candidate: str, corpus: str) -> bool:
    """候选原因是否在给定语料里有依据。

    与 eval_test._grounded_in_corpus 同一取舍：先查原文子串（容忍细微改写），
    再退到"所有 token 出现在同一行"——因为检索片段是逐行拼接的，
    同一行代表同一句上下文，跨行 token 拼凑不算依据。
    """
    if not candidate or not corpus:
        return False
    if candidate in corpus:
        return True
    tokens = [t.strip() for t in jieba.cut(candidate) if t.strip()]
    if not tokens:
        return False
    for line in corpus.splitlines():
        line = line.strip()
        if len(line) >= 4 and all(t in line for t in tokens):
            return True
    return False


def agent_rebuttal(diagnosis: dict, review: dict, evidence: str, fault: str,
                   initial_evidence: str = "", correlation_id: str = None) -> dict:
    prompt = render_prompt(
        PromptTemplates.AGENT_REBUTTAL,
        diagnosis=json.dumps(diagnosis, ensure_ascii=False, indent=2),
        review=json.dumps(review, ensure_ascii=False, indent=2),
        evidence=evidence,
        fault=fault
    )
    messages = [
        SystemMessage(content="你是严谨的故障诊断专家，正在参与专业辩论。"),
        HumanMessage(content=prompt)
    ]
    validated = invoke_and_validate(messages, RebuttalOutput, correlation_id=correlation_id)
    if not validated:
        return {}

    result_dict = validated.model_dump()

    # 辩论结论同样受排除条件约束
    fault_dict = {}
    try:
        parsed = json.loads(fault)
        if isinstance(parsed, dict):
            fault_dict = parsed
    except Exception:
        pass
    exclusion_list = fault_dict.get("排除条件") or []
    if exclusion_list and result_dict.get("最终根因"):
        entries = extract_kb_causes(evidence)
        kb_causes = [c for _, c in entries]
        scope = _relevant_scope(entries, fault_dict.get("设备类型", ""), fault_dict.get("报警代码", ""))
        idx = map_excluded_causes(exclusion_list, entries, scope, correlation_id)
        if idx:
            result_dict["最终根因"] = remove_excluded_causes(result_dict["最终根因"], kb_causes, idx)
            if not result_dict["最终根因"]:
                result_dict["最终根因"] = "用户描述的现象已被排除所有可能原因，建议人工复核"

    # 护栏A：辩论不得把**初检证据里没有出处**的原因写进最终根因。
    # 必须在"补回被丢候选"之前执行——先清掉越界项，再谈补回，
    # 否则刚被清掉的项可能又被 restore 逻辑从初诊里"捞"回来。
    if result_dict.get("最终根因") and diagnosis.get("根因判断") and initial_evidence:
        result_dict["最终根因"] = drop_out_of_scope_candidates(
            diagnosis["根因判断"], result_dict["最终根因"], initial_evidence
        )

    # 护栏B：辩论不得把初诊里"有依据且未被排除"的候选静默丢掉
    if result_dict.get("最终根因") and diagnosis.get("根因判断"):
        result_dict["最终根因"] = restore_dropped_candidates(
            diagnosis["根因判断"], result_dict["最终根因"], exclusion_list, initial_evidence
        )

    return result_dict


def _assert_degraded_markers_in_sync() -> None:
    """防漂移护栏：本模块的降级标记副本必须与 orchestrator 的正本一致。

    为什么要有这道护栏：两处标记一旦不同步，护栏的"降级不得改写"判定就会漏，
    而且是**静默**漏——只有当某种降级文句恰好出现在辩论路径上才暴露。
    开发期直接 assert 比等它上线后才发现便宜得多。
    真正跑单测时由 conftest 先 import orchestrator，不会触发反向依赖。
    """
    try:
        from orchestrator import DEGRADED_MARKERS as _canonical
    except Exception:
        return
    missing = [m for m in _canonical if m not in _DEGRADED_MARKERS]
    assert not missing, (
        f"agents._DEGRADED_MARKERS 与 orchestrator.DEGRADED_MARKERS 不同步，缺失：{missing}"
    )


def agent_review_final(original_diagnosis: dict, rebuttal: dict, correlation_id: str = None,
                       evidence: str = "", disagreements: list = None) -> dict:
    """最终复审。

    `evidence` / `disagreements` 是与初审（agent_review）同源的**对照物**，
    必须传给终审。原因：终审才是决定终态的那一步，而它此前只看得到
    「原诊断 + 反驳」两样东西——对照物比初审还少。初审刚被补上
    evidence/fault/disagreements 时是为了"让辩论点得着"，结果终审反而退化成
    "看两边谁说得更顺"：**结论里的原因有没有出处**、**知识库里是不是本来就两派意见**，
    这两项在终审阶段根本无从检查。

    新参数放在 `correlation_id` 之后而不是之前：历史调用点与测试里的桩
    可能按位置传第三个参数，插在中间会把 correlation_id 顶到 evidence 上。

    `disagreements` 不传时从 evidence 现算，与 agent_review 口径一致。
    """
    if disagreements is None:
        disagreements = extract_kb_disagreements(evidence)
    prompt = render_prompt(
        PromptTemplates.AGENT_REVIEW_FINAL,
        original_diagnosis=json.dumps(original_diagnosis, ensure_ascii=False, indent=2),
        rebuttal=json.dumps(rebuttal, ensure_ascii=False, indent=2),
        evidence=evidence or "（未提供检索资料）",
        disagreements=_format_disagreements(disagreements),
    )
    messages = [
        SystemMessage(content="你是严格的维修安全审核员，正在进行最终复审。"),
        HumanMessage(content=prompt)
    ]
    validated = invoke_and_validate(messages, FinalReviewOutput, correlation_id=correlation_id)
    return validated.model_dump() if validated else {}


def evaluate_semantic(diagnosis_text: str, expected_text: str, correlation_id: str = None) -> bool:
    """语义一致性判定。

    判定口径与 check_relevance 对齐：要求模型**明确给出**"一致"才算命中，
    而不是"只要没说'不一致'就算一致"。后者会让任何非预期输出（模型跑偏、
    截断、答非所问）都默认算作评估通过，等于给评估指标注水。
    """
    prompt = render_prompt(
        PromptTemplates.EVALUATE_SEMANTIC,
        diagnosis_text=diagnosis_text,
        expected_text=expected_text
    )
    messages = [
        SystemMessage(content="你是严格的技术评估员。"),
        HumanMessage(content=prompt)
    ]
    content = safe_llm_invoke(messages, correlation_id=correlation_id)
    if content is None:
        return False

    head = content.strip()[:20]
    if "不一致" in head:
        return False
    # "不确定是否一致"这类回答不含连续子串"不一致"，只判"不一致"会把它放行。
    # 必须把含糊/拒答的措辞也挡掉，否则指标注水（评估口径与 check_relevance 一致）。
    if any(w in head for w in ("无法", "不能", "不确定", "难以")):
        return False
    return "一致" in head


# ========== 多轮追问 ==========

# 单条故障现象若不带任何「细化信号」，视为粗粒度。判定逻辑集中在这里，
# 而不是塞进正则，避免规则只对一半 corner case 生效、看起来像跑通了。
# 信号词覆盖：部件名、工况/时间、声、温度、位置、报警/代码。
# 强信号：能作为检索的区分维度——说了「哪个部件 / 什么声音 / 什么代码 /
# 什么工况 / 什么状态」。命中任一即视为已细化，放行去检索。
# 部件 / 部位。单独抽出来是因为降级报告要复用它判断「用户有没有说清是哪个
# 部件」——抽成常量再拼接，避免词表出现第二份副本（硬约束 22 的漂移教训）。
_PART_SIGNALS = (
    "轴", "电机", "轴承", "齿轮", "阀", "泵", "传感器", "冷却", "液压",
    "气动", "伺服", "滚珠", "导轨", "活塞", "缸", "编码器", "控制器",
    "机身", "床身", "主轴", "变速箱", "刀塔", "料盘", "变频器",
)

_STRONG_DETAIL_SIGNALS = _PART_SIGNALS + (
    # 声音（注意：不用「响」「声」——这两个字单独出现通常是形容词，
    # 「空压机响」「机身有声音」就是单字形容词 + 名词的粗描述；
    # 真正细化的声音信号得是术语：异响、噪音、噪声，或模拟声字）
    "异响", "噪音", "噪声",
    "咣", "嗡", "嘎", "吱", "啸", "嘶", "咔", "嗒", "嘭", "咯噔",
    # 工况：启动 / 加工 / 空载指向不同的排查方向，有区分度
    "启动", "开机", "关机", "运行", "停机", "加工", "空跑", "空载", "带载",
    # 报警 / 故障码
    "报警", "代码", "ALM", "ERR", "E-", "F-", "AL.", "ER.",
    # 程度 / 严重性：描述故障状态，比纯时间词更有区分度。
    # 保持强信号也是为了不动既有契约（tests/test_info_sufficiency.py 断言
    # 「严重震动」应放行）。
    "严重", "明显", "加剧", "加重", "反复", "时轻时重",
    # 温度 / 外观
    "过热", "发烫", "冒烟", "烫", "锈", "漏油", "漏气", "堵塞",
)

# 弱信号：只描述「发生的时间模式」，不指向任何部件、状态或工况。
# 「一直响」「突然响」「每次都响」对任何设备任何故障都成立，对检索零区分度。
_WEAK_DETAIL_SIGNALS = (
    "突然", "一直", "每次", "逐渐", "连续", "间歇",
)

# 并集保留原名：既有引用仍可用，也便于退化验证时对照旧写法。
_PHENOMENON_DETAIL_SIGNALS = _STRONG_DETAIL_SIGNALS + _WEAK_DETAIL_SIGNALS

# 信息不足的三种情形。统一在此判定，被 is_info_sufficient（接口层）
# 和 generate_followup_question（文案层）共用，防止两边对"什么算充分"
# 各说各话——踩过：max-content 改过阈值但 UI 还按旧文案走。
_INSUFFICIENT_NO_DEVICE = "no_device"
_INSUFFICIENT_NO_SYMPTOMS = "no_symptoms"
_INSUFFICIENT_VAGUE_SYMPTOMS = "vague_symptoms"


def _phenomenon_too_vague(symptoms) -> bool:
    """判定故障现象是否过于粗粒度、不足以支撑诊断。

    规则：现象只有 1 项且不含任何**强**细化信号 → 视为过粗（True）。
    现象 ≥ 2 项，或单条但含强信号 → 不算过粗（False）。

    为什么不允许多粗的并存也不算粗：实际场景里，分多条陈述本身
    就说明用户掌握了不同维度的信号；硬要把"震"+"响"也判成信息不足
    会逼正常用户重写，对"新学徒"没有帮助、对老用户徒增打扰。

    为什么弱信号（"一直""突然""每次"）单独不算数：检索得能靠它缩小范围，
    而纯时间模式对任何设备任何故障都成立，没有区分度。放行后检索必然
    落空 → 走到"知识库无依据"降级，用户看到的就是"随便一句话都转人工"。
    真实 case：「数控机床转起来一直响」——含"一直"就被放行，库里没有数控
    机床，检索落空后直接转人工；拦下追问（"是哪个部位响？"）要有用得多。
    """
    if not symptoms:
        return True
    if len(symptoms) >= 2:
        return False
    p = (symptoms[0] or "").lower()
    return not any(s.lower() in p for s in _STRONG_DETAIL_SIGNALS)


def _insufficiency_reason(fault_info: dict) -> Optional[str]:
    """返回当前 fault_info 不充分的具体原因；充分则返回 None。

    优先级：
      1) 有报警代码 → 视为充分（报警号本身是强诊断信号）
      2) 缺设备类型 → no_device
      3) 有设备但无现象 → no_symptoms
      4) 现象过粗（无细化信号）→ vague_symptoms
    """
    device = (fault_info.get("设备类型") or "").strip()
    symptoms = fault_info.get("故障现象") or []
    alarm = (fault_info.get("报警代码") or "").strip()

    if alarm and alarm.lower() not in ("null", "none", ""):
        return None

    if not device or device.lower() in ("null", "none"):
        return _INSUFFICIENT_NO_DEVICE

    if not symptoms:
        return _INSUFFICIENT_NO_SYMPTOMS

    if _phenomenon_too_vague(symptoms):
        return _INSUFFICIENT_VAGUE_SYMPTOMS

    return None


def is_info_sufficient(fault_info: dict) -> bool:
    """判定结构化故障描述是否足以开始诊断。

    充分性口径与 _insufficiency_reason 一致——前者只对外暴露 True/False，
    后者把"为什么不足"也告诉追问生成器。共用底层判定，两边不会漂移。
    """
    return _insufficiency_reason(fault_info) is None


def generate_followup_question(fault_info: dict) -> str:
    """根据不充分的原因，生成贴近场景的追问。

    设计上故意避开"请补充故障现象"这种空话——新学徒看到只会愣住；
    引导式问题清单（部件/工况/异响/温度/使用时长）告诉他"可以补充什么"，
    才能把口语化描述推进到可检索的细化信息。
    """
    reason = _insufficiency_reason(fault_info)
    if reason is None:
        return ""

    if reason == _INSUFFICIENT_NO_DEVICE:
        return (
            "信息还不太够，麻烦补充两点：\n"
            "1) 设备类型——比如「数控机床主轴电机」「螺杆空压机」「液压泵站」之类；\n"
            "2) 故障现象——比如什么时候发生、什么部位出问题、有什么异样"
            "（声音 / 温度 / 震动 / 动作异常）。"
        )

    if reason == _INSUFFICIENT_NO_SYMPTOMS:
        return (
            "请用一两句话描述一下故障现象——比如什么时候发生、"
            "是哪个部位出问题、有什么异样（声音 / 温度 / 震动 / 动作异常）。"
        )

    # reason == vague_symptoms：这是用户截图里那种"数控床一震一颤的"场景。
    return (
        "信息还不太够判断。麻烦再补充几点细节：\n"
        "1) 是哪个部件 / 部位在出问题？（比如主轴、某根轴、电机、液压泵、控制器…）\n"
        "2) 什么时候出现？（开机 / 运行 / 加工 / 停机 时？一直还是间歇？突然还是慢慢出现？）\n"
        "3) 有没有伴随异响、异常温度、烟雾或报警代码？\n"
        "4) 这台设备用了多久、最近一次保养是什么时候？"
    )


# ========== 多模态 ==========

# 图片 MIME 的保守默认值：认不出来时按 JPEG 处理（视觉服务大多能容忍）。
_DEFAULT_IMAGE_MIME = "image/jpeg"

# 魔数 → MIME。顺序有意义：PNG 的魔数最长，必须先判。
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
)


def detect_image_mime(image_base64: str) -> str:
    """按**魔数**判断图片类型，认不出时回退 JPEG。

    此前是硬编码 `data:image/jpeg`，而前端（`app.py` / DiagnosePage）都允许上传 png。
    PNG 的字节流被贴上 jpeg 标签后，部分视觉服务会直接拒收，或按 jpeg 解析出
    一张坏图 —— 而**用户完全不知道**，因为失败被静默成了空描述。

    为什么不信任文件名或浏览器给的 `File.type`：两者都是调用方可任意填写的元数据，
    与字节流是否一致没有任何保证。魔数是唯一可靠来源。
    """
    try:
        # 取前 16 个 base64 字符（= 12 字节），足够覆盖上面所有魔数，
        # 且 16 是 4 的整数倍，不需要补 padding。
        head = base64.b64decode(image_base64[:16], validate=False)
    except Exception:
        return _DEFAULT_IMAGE_MIME

    for magic, mime in _IMAGE_MAGIC:
        if head.startswith(magic):
            return mime
    # WebP 是 RIFF 容器，需要再看第 8~12 字节
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"

    return _DEFAULT_IMAGE_MIME


def extract_image_info(image_base64: str, correlation_id: str = None) -> str:
    """识别图片内容。**返回空串表示没识别出来**（调用方必须显式处理）。

    返回值刻意不抛异常：图片识别是可选增强，不该因为一次视觉调用失败就把整条诊断
    打成"模型服务异常"。但"静默返回空串"同样是错的——用户传了照片，
    结果整轮只按文字诊断，而界面上没有任何提示，他会以为照片被用上了。
    所以调用方（`orchestrator._build_initial_state`）要把空返回值转成
    `image_warning` 带出去。
    """
    from langchain_core.messages import HumanMessage as HM

    prompt_text = render_prompt(PromptTemplates.EXTRACT_IMAGE_INFO)
    mime = detect_image_mime(image_base64)

    messages = [
        SystemMessage(content="你是工业设备故障图片分析专家。"),
        HM(content=[
            {
                "type": "text",
                "text": prompt_text
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image_base64}"}
            }
        ])
    ]

    content = safe_llm_invoke(messages, llm=get_vision_llm(), correlation_id=correlation_id)
    if not content:
        logger.warning("image_extract_failed", mime=mime, correlation_id=correlation_id)
    return content or ""