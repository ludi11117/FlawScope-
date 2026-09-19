"""图执行中断的兜底交付物 + SSE 端点护栏测试。

为什么需要这一组：
    `workorder_node` 内部的兜底只覆盖"走到了 workorder 但 LLM 挂了"。
    若异常发生在更早的节点，图到不了 workorder，异常直接冒到最外层被 raise——
    用户烧掉几十次 LLM 调用后拿到 0 交付物，与"任何失败都产出带风险标记的工单"
    的对外保证直接矛盾。这组测试守住"兜底挪到最外层"这件事没被改回去。

    同时守住 SSE 的三个易错点：共用并发闸门、断连不吞 GeneratorExit、
    闸门在生成器内释放。

全部离线：monkeypatch 掉 app.invoke / app.stream，不调 LLM、不联网。
"""

import json

import pytest
from fastapi.testclient import TestClient

import orchestrator
import api
from config import settings


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    """把外部依赖挡在测试之外：向量库、LLM、落库。

    注意必须连 agents._db 单例一起清——嵌入函数是 get_db() 构造时绑定进单例的，
    只替换 get_embeddings 不够，旧单例仍持原引用，会真的联网。
    """
    import agents
    monkeypatch.setattr(agents, "_db", None, raising=False)
    monkeypatch.setattr(api, "init_db", lambda: None)
    yield


def _boom(*args, **kwargs):
    raise RuntimeError("simulated graph explosion")


# ---------- 第一组：图中断必须产出兜底工单（该拦的） ----------

def test_run_diagnosis_returns_workorder_on_graph_failure(monkeypatch):
    """图执行抛异常时，run_diagnosis 必须返回带工单的结果，而不是把异常抛出去。

    这是本项目最核心的对外保证：没有死胡同。
    """
    monkeypatch.setattr(orchestrator.app, "invoke", _boom)

    result = orchestrator.run_diagnosis("主轴异响", correlation_id="t-abort-1")

    assert result["workorder"], "图中断时必须产出工单，否则用户拿到 0 交付物"
    assert result["status"] in orchestrator.TERMINAL_FAILURE_STATUSES
    assert result["correlation_id"] == "t-abort-1"
    # 工单必须自带风险标记，且说明中断原因，而不是一张"看起来正常"的单子
    assert "中断" in result["workorder"]["风险等级"] or "待人工" in result["workorder"]["风险等级"]
    assert result["workorder"]["风险说明"]


def test_abort_workorder_does_not_fabricate_sections(monkeypatch):
    """兜底工单不得编造维修方案/备件清单——没有依据的章节比没有更危险。

    口径与 workorder_export 的"没有的字段就不渲染"一致：
    只给编号 + 风险等级 + 风险说明这三样。
    """
    monkeypatch.setattr(orchestrator.app, "invoke", _boom)
    result = orchestrator.run_diagnosis("主轴异响", correlation_id="t-abort-2")
    wo = result["workorder"]

    assert set(wo.keys()) == {"工单编号", "风险等级", "风险说明"}, (
        f"兜底工单只应含三项，实际：{sorted(wo.keys())}"
    )
    for forbidden in ("根因", "维修方案", "备件清单", "安全注意事项"):
        assert forbidden not in wo, f"兜底工单不得编造「{forbidden}」"


def test_abort_result_keeps_intermediate_artifacts(monkeypatch):
    """已产生的中间产物（fault_info / evidence）要保留——它们是排障线索。"""
    def _partial_then_boom(state, *args, **kwargs):
        raise RuntimeError("mid-graph failure")

    monkeypatch.setattr(orchestrator.app, "invoke", _partial_then_boom)

    captured = {}
    original = orchestrator.build_abort_result

    def _spy(state, error=""):
        captured["state"] = dict(state)
        return original(state, error)

    monkeypatch.setattr(orchestrator, "build_abort_result", _spy)
    orchestrator.run_diagnosis("主轴异响", correlation_id="t-abort-3")

    assert captured["state"].get("user_input") == "主轴异响"
    assert "status" in captured["state"]


def test_abort_result_includes_cost_placeholder(monkeypatch):
    """中断结果要补 COST_UNAVAILABLE，而不是留空 dict。

    留空会让前端显示"无成本数据"，用户分不清是"没算"还是"算出来是 0"。
    """
    monkeypatch.setattr(orchestrator.app, "invoke", _boom)
    result = orchestrator.run_diagnosis("主轴异响", correlation_id="t-abort-4")

    assert result["cost"] == orchestrator.COST_UNAVAILABLE
    assert result.get("token_usage") is not None


# ---------- 第二组：不该拦的（对偶） ----------

def test_successful_diagnosis_unchanged(monkeypatch):
    """正常路径必须**不**被兜底逻辑污染：不该出现 ABORT 工单。"""
    monkeypatch.setattr(orchestrator.app, "invoke", lambda state, **kw: {
        **state,
        "status": "done",
        "workorder": {"工单编号": "WO-normal", "根因": "主轴轴承磨损"},
        "cost": {"总费用": "1000元"},
    })

    result = orchestrator.run_diagnosis("主轴异响", correlation_id="t-ok-1")

    assert result["status"] == "done"
    assert result["workorder"]["工单编号"] == "WO-normal"
    assert "ABORT" not in result["workorder"]["工单编号"]


def test_stream_emits_normal_progress_then_result(monkeypatch):
    """流式正常路径：应逐节点 yield，且不出现"中断"事件。"""
    def _fake_stream(state, **kwargs):
        yield {"extract_info": {"status": "extracted", "fault_info": {"设备类型": "数控机床"}}}
        yield {"diagnose": {"status": "diagnosed", "diagnosis": {"根因判断": "轴承磨损"}}}
        yield {"workorder": {"status": "done", "workorder": {"工单编号": "WO-s1"}}}

    monkeypatch.setattr(orchestrator.app, "stream", _fake_stream)

    events = list(orchestrator.run_diagnosis_stream("主轴异响", correlation_id="t-stream-1"))
    labels = [label for label, _ in events]

    assert any("启动" in lb for lb in labels)
    assert any("工单" in lb or "完成" in lb for lb in labels)
    assert not any("中断" in lb for lb in labels), f"正常路径不应出现中断事件：{labels}"
    assert events[-1][1]["workorder"]["工单编号"] == "WO-s1"


def test_stream_yields_abort_state_instead_of_raising(monkeypatch):
    """流式中断时，应 **yield** 一个降级终态，而不是 raise。

    区别很关键：raise 会让 SSE 连接裸断，前端只能显示"网络错误"；
    yield 让前端能以"降级完成 + 待人工处理工单"收场。
    """
    def _stream_boom(state, **kwargs):
        yield {"extract_info": {"status": "extracted"}}
        raise RuntimeError("explode midway")

    monkeypatch.setattr(orchestrator.app, "stream", _stream_boom)

    events = list(orchestrator.run_diagnosis_stream("主轴异响", correlation_id="t-stream-2"))
    last_label, last_state = events[-1]

    assert "中断" in last_label
    assert last_state["workorder"], "流式中断也必须产出工单"
    assert last_state["status"] in orchestrator.TERMINAL_FAILURE_STATUSES


# ---------- 第三组：GeneratorExit 必须放行（SSE 断连不烧配额） ----------
#
# 这一组的断言按 Python 生成器的**真实契约**写，不是按直觉写：
#   - GeneratorExit 继承自 BaseException，本来就不会被 `except Exception` 捕获；
#   - 因此"客户端断连时兜底 except 会吞掉它"这个担心，在 except Exception 写法下
#     其实不成立 —— 真正的危险是**在 close() 之后继续 yield**，
#     Python 会抛 `RuntimeError: generator ignored GeneratorExit`。
# 所以这里测的是：断连时不会误报成"诊断中断"，且 close() 干净退出。


def test_generator_exit_is_not_caught_by_except_exception(monkeypatch):
    """契约：生成器内部抛 GeneratorExit 时，应直接传播，不被 except Exception 吞掉。

    驱动路径数容易数错，这里说明清楚：桩生成器先 yield 一个节点更新，
    run_diagnosis_stream 会把它原样转发出来（第 2 个事件），
    要到**第 3 次** next() 才会推进到桩里的 raise 语句。
    """
    def _stream_client_gone(state, **kwargs):
        yield {"extract_info": {"status": "extracted"}}
        raise GeneratorExit()

    monkeypatch.setattr(orchestrator.app, "stream", _stream_client_gone)

    gen = orchestrator.run_diagnosis_stream("主轴异响", correlation_id="t-ge-1")
    assert next(gen)[0].startswith("🚀")          # 启动事件
    assert "信息抽取" in next(gen)[0]              # 转发的节点更新
    with pytest.raises(GeneratorExit):
        next(gen)                                  # 才轮到桩里的 raise


def test_close_after_disconnect_does_not_raise_runtime_error(monkeypatch):
    """关键契约：客户端 close() 生成器时必须干净退出。

    若实现里在 close 之后还尝试 yield（例如把兜底 except 放宽到了 BaseException），
    Python 会抛 `RuntimeError: generator ignored GeneratorExit`，
    在 ASGI 层表现为一个难查的 500，而不是"客户端断连"。
    """
    def _stream_long(state, **kwargs):
        yield {"extract_info": {"status": "extracted"}}
        yield {"retrieve": {"evidence": ["a"]}}
        yield {"workorder": {"status": "done"}}

    monkeypatch.setattr(orchestrator.app, "stream", _stream_long)

    gen = orchestrator.run_diagnosis_stream("主轴异响", correlation_id="t-ge-2")
    next(gen)  # 启动事件
    next(gen)  # 第一个节点更新（信息抽取）

    gen.close()  # 不得抛 RuntimeError


def test_disconnect_is_not_reported_as_abort(monkeypatch):
    """反向断言：客户端断连**不应**产生"中断工单"事件。

    这是对偶测试的"不该拦的那一半"：如果实现把断连也走兜底分支，
    用户会在已离开的情况下多拿一张莫名其妙的工单，日志里也会误报成诊断事故。
    """
    def _stream_client_gone(state, **kwargs):
        yield {"extract_info": {"status": "extracted"}}
        raise GeneratorExit()

    monkeypatch.setattr(orchestrator.app, "stream", _stream_client_gone)

    collected = []
    gen = orchestrator.run_diagnosis_stream("主轴异响", correlation_id="t-ge-3")
    try:
        for label, state in gen:
            collected.append((label, state))
    except GeneratorExit:
        pass

    assert not any("中断" in lb for lb, _ in collected), (
        "客户端断连被误报成诊断中断"
    )


def test_abort_yield_only_after_real_exception(monkeypatch):
    """对照：真实异常（非 GeneratorExit）时，兜底 yield 必须生效。

    与上一条构成对偶——防止有人为了"避免契约 C"把兜底 yield 整个删掉。
    """
    def _stream_boom(state, **kwargs):
        yield {"extract_info": {"status": "extracted"}}
        raise RuntimeError("real failure")

    monkeypatch.setattr(orchestrator.app, "stream", _stream_boom)

    events = list(orchestrator.run_diagnosis_stream("主轴异响", correlation_id="t-ge-4"))
    assert "中断" in events[-1][0]
    assert events[-1][1]["workorder"]


# ---------- 第四组：SSE 端点护栏 ----------

def _client():
    return TestClient(api.app)


def test_sse_endpoint_shares_concurrency_gate(monkeypatch):
    """SSE 端点必须共用 /diagnose 的闸门。

    否则它就是个绕开限流的后门：用户可以一边挂着流、一边并发打满配额。
    做法：先占满闸门，再请求 SSE，应当拿到 503 而不是排上队。
    """
    acquired = []
    original = api._diagnosis_slots
    total = settings.MAX_CONCURRENT_DIAGNOSES

    # 把闸门占满（真实 BoundedSemaphore，不是替身）
    for _ in range(total):
        acquired.append(original.acquire(blocking=False))
    assert all(acquired), "测试前置：闸门应能被占满"

    try:
        resp = _client().post("/diagnose/stream", json={"fault_description": "主轴异响"})
        assert resp.status_code == 503, (
            f"闸门占满时 SSE 端点应返回 503，实际 {resp.status_code}"
        )
        assert resp.headers.get("Retry-After") == "10"
    finally:
        for _ in acquired:
            original.release()


def test_sse_endpoint_returns_event_stream(monkeypatch):
    """闸门空闲时，SSE 端点应返回 text/event-stream 且事件格式可解析。"""
    def _fake_stream(state, **kwargs):
        yield {"extract_info": {"status": "extracted"}}
        yield {"workorder": {"status": "done", "workorder": {"工单编号": "WO-sse-1"}}}

    monkeypatch.setattr(orchestrator.app, "stream", _fake_stream)
    monkeypatch.setattr(api, "save_diagnosis_record", lambda *a, **kw: None)

    resp = _client().post("/diagnose/stream", json={"fault_description": "主轴异响"})

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: progress" in body
    assert "event: result" in body
    assert "event: done" in body
    assert "WO-sse-1" in body

    # SSE 消息体必须是合法 JSON（中文与换行没被手拼字符串搞坏）
    for block in body.split("\n\n"):
        if block.startswith("event: "):
            payload = block.split("data: ", 1)[1]
            json.loads(payload)


def test_sse_releases_gate_after_completion(monkeypatch):
    """SSE 跑完后必须把闸门还回去，否则连续几次后整个端点不再接请求。"""
    def _fake_stream(state, **kwargs):
        yield {"workorder": {"status": "done", "workorder": {"工单编号": "WO-sse-2"}}}

    monkeypatch.setattr(orchestrator.app, "stream", _fake_stream)
    monkeypatch.setattr(api, "save_diagnosis_record", lambda *a, **kw: None)

    _client().post("/diagnose/stream", json={"fault_description": "主轴异响"})
    _client().post("/diagnose/stream", json={"fault_description": "主轴异响"})

    # 两次都跑通了就说明闸门没被漏占；再补一次显式确认
    assert api._diagnosis_slots.acquire(blocking=False), "SSE 完成后闸门未释放"
    api._diagnosis_slots.release()


def test_sse_no_buffer_headers(monkeypatch):
    """必须带 X-Accel-Buffering: no，否则反代会缓冲整个响应，SSE 退化成一次性返回。"""
    def _fake_stream(state, **kwargs):
        yield {"workorder": {"status": "done", "workorder": {"工单编号": "WO-sse-3"}}}

    monkeypatch.setattr(orchestrator.app, "stream", _fake_stream)
    monkeypatch.setattr(api, "save_diagnosis_record", lambda *a, **kw: None)

    resp = _client().post("/diagnose/stream", json={"fault_description": "主轴异响"})
    assert resp.headers.get("x-accel-buffering") == "no"
    assert "no-cache" in resp.headers.get("cache-control", "")


def test_sse_skips_saving_need_more_info(monkeypatch):
    """追问轮次不落库——与 /diagnose 口径一致，否则历史列表塞满半成品。"""
    def _fake_stream(state, **kwargs):
        yield {"check_info": {"status": "need_more_info", "followup_question": "请补充设备型号"}}

    monkeypatch.setattr(orchestrator.app, "stream", _fake_stream)
    saved = []
    monkeypatch.setattr(api, "save_diagnosis_record", lambda *a, **kw: saved.append(a))

    resp = _client().post("/diagnose/stream", json={"fault_description": "机器坏了"})

    assert resp.status_code == 200
    assert saved == [], f"追问轮次不应落库，实际保存了 {len(saved)} 条"
    assert "请补充设备型号" in resp.text


# ---------- 第五组：recursion_limit 必须显式收口 ----------

def test_recursion_limit_is_configured():
    """recursion_limit 必须有配置项且被真实传入。

    LangGraph 默认 10007，意味着最坏情况能跑上万步、成倍烧配额才报错。
    """
    assert hasattr(settings, "GRAPH_RECURSION_LIMIT")
    assert settings.GRAPH_RECURSION_LIMIT <= 1000, (
        f"步数上限过大（{settings.GRAPH_RECURSION_LIMIT}），失去防护意义"
    )


def test_recursion_limit_passed_to_invoke(monkeypatch):
    """断言 config 真的传给了 app.invoke，而不是只在 settings 里躺着一个没人读的数字。"""
    seen = {}

    def _capture(state, config=None, **kwargs):
        seen["config"] = config
        return {**state, "status": "done"}

    monkeypatch.setattr(orchestrator.app, "invoke", _capture)
    orchestrator.run_diagnosis("主轴异响", correlation_id="t-rec-1")

    assert seen.get("config", {}).get("recursion_limit") == settings.GRAPH_RECURSION_LIMIT


def test_recursion_limit_passed_to_stream(monkeypatch):
    """流式路径同样要传——只改 invoke 会让 SSE 仍然跑默认 10007 步。"""
    seen = {}

    def _capture(state, config=None, **kwargs):
        seen["config"] = config
        yield {"workorder": {"status": "done", "workorder": {"工单编号": "WO-rec"}}}

    monkeypatch.setattr(orchestrator.app, "stream", _capture)
    list(orchestrator.run_diagnosis_stream("主轴异响", correlation_id="t-rec-2"))

    assert seen.get("config", {}).get("recursion_limit") == settings.GRAPH_RECURSION_LIMIT


def test_recursion_error_produces_abort_workorder(monkeypatch):
    """端到端：递归超限（LangGraph 真实会抛的异常类型）也必须出工单。"""
    class GraphRecursionError(Exception):
        pass

    def _recursion_boom(state, config=None, **kwargs):
        raise GraphRecursionError("Recursion limit of 100 reached")

    monkeypatch.setattr(orchestrator.app, "invoke", _recursion_boom)
    result = orchestrator.run_diagnosis("主轴异响", correlation_id="t-rec-3")

    assert result["workorder"]["工单编号"].endswith("-ABORT")
    assert "GraphRecursionError" in result["workorder"]["风险说明"]


# ---------------------------------------------------------------------------
# 第六组：版本号只有一处定义
# ---------------------------------------------------------------------------
# 背景：本次全栈改造把 FastAPI 的 version 从 1.2.0 提到 1.3.0，但 /health、
# /health/live、/ 三处各自硬编码了 "1.2.0"，谁都没报错——探活只看 HTTP 200，
# 版本号漂移在部署脚本里完全不可见。收口成 API_VERSION 之后，这条测试负责
# 保证它不再散开：任何一处重新写死字符串就会红。


def test_api_version_is_defined_once():
    """接口层只允许存在一个版本号字面量。"""
    import re
    from pathlib import Path

    src = Path(api.__file__).read_text(encoding="utf-8")
    # 捕获引号**内部**的内容：用捕获组而不是整段匹配，
    # 否则拿到的字面量自带引号，与 API_VERSION 永远不相等，
    # 这条测试就成了"无论如何都红"的假护栏。
    literals = re.findall(r'"(\d+\.\d+\.\d+)"', src)

    assert literals == [api.API_VERSION], (
        f"接口层出现了多余的版本号字面量：{literals}。"
        "请统一使用 API_VERSION，不要就地写死字符串。"
    )


def test_all_version_endpoints_agree():
    """四处对外暴露的版本号必须一致，且等于 API_VERSION。"""
    from fastapi.testclient import TestClient

    expected = api.API_VERSION
    client = TestClient(api.app)

    assert client.get("/health/live").json()["version"] == expected
    assert client.get("/").json()["version"] == expected
    assert api.app.version == expected
    # HealthResponse 的默认值也要跟着走，否则 /health 在未显式传 version 时又会漂
    assert api.HealthResponse(status="healthy", components={}).version == expected
