"""接口契约与诚实性的回归测试（C3 / C4 / C5 / C6 / C7）。

覆盖五个彼此独立、但都属于"对外承诺与代码不一致"的问题：

- **C3** `run_diagnosis_stream` 每步 yield 同一个可变 dict → 所有快照都变成终态；
- **C4** `/diagnose` 丢掉 `record_id`，且落库失败会把已完成的诊断变成 500；
- **C5** 图片 MIME 硬编码 jpeg（前端却允许 png）；视觉失败静默 → 用户不知道图没被用上；
- **C6** `extract_fault_info` 自己重写了一遍重试循环，且**没有 schema 回灌**；
- **C7** tenacity 对 4xx 也重试（401/400 白等 3 次退避）。

全部离线：所有 LLM 调用打桩，DB 走 `isolated_db`。
"""

import base64
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError, APITimeoutError, BadRequestError, RateLimitError

import agents
import api
import orchestrator
from config import settings
from logging_config import clear_token_tracker


def _stub_happy_agents(monkeypatch):
    monkeypatch.setattr(orchestrator, "extract_fault_info", lambda *a, **kw: {
        "设备类型": "数控机床", "报警代码": "E-203",
        "故障现象": ["转速不稳", "异响"], "排除条件": []
    })
    monkeypatch.setattr(orchestrator, "retrieve_evidence", lambda *a, **kw: "【资料1】\n一、主轴电机\n1. 轴承损坏")
    monkeypatch.setattr(orchestrator, "is_equipment_in_evidence", lambda *a, **kw: True)
    monkeypatch.setattr(orchestrator, "check_relevance", lambda *a, **kw: True)
    monkeypatch.setattr(orchestrator, "agent_diagnose", lambda *a, **kw: {
        "报警代码": "E-203", "根因判断": "原因1：轴承损坏", "依据": "资料1", "排查建议": ["更换轴承"]
    })
    monkeypatch.setattr(orchestrator, "agent_review", lambda *a, **kw: {
        "审核意见": "通过", "理由": "有依据", "风险提示": "无"
    })
    monkeypatch.setattr(orchestrator, "agent_cost", lambda *a, **kw: {"预计成本": "930元"})
    monkeypatch.setattr(orchestrator, "agent_workorder", lambda *a, **kw: {"工单编号": "WO-1"})


# ========== C3：流式快照必须是独立对象 ==========

def test_stream_yields_independent_snapshots(monkeypatch):
    """每步 yield 的状态必须是**各自的快照**，不能全部指向同一个 dict。

    旧实现一路 `merged.update(...)` 后 `yield (label, merged)`：SSE 端因为立刻
    序列化看不出问题，但任何"把每步快照收进列表"的消费方拿到的 N 份快照
    会全部变成终态，看起来像"每一步的结论都一样"。
    """
    _stub_happy_agents(monkeypatch)
    cid = "cid-c3-stream"
    try:
        snapshots = list(orchestrator.run_diagnosis_stream("主轴异响", correlation_id=cid))
    finally:
        clear_token_tracker(cid)

    states = [s for _, s in snapshots]
    assert len(states) >= 3, "流没有产出足够的中间快照，这条测试失去意义"

    # 判别式一：对象必须是各自独立的
    assert len({id(s) for s in states}) == len(states), "多个快照是同一个 dict 对象"

    # 判别式二：倒数第二个与最后一个的 status 必须不同。
    # 旧实现下它们都是终态 "done"，这一条会直接失败。
    assert states[-2]["status"] != states[-1]["status"], (
        f"快照全被写成了终态：{states[-2]['status']} == {states[-1]['status']}"
    )


def test_stream_keeps_early_stage_status(monkeypatch):
    """对偶：第一份快照必须仍是"还没开始跑"的状态，不能被终态覆盖。"""
    _stub_happy_agents(monkeypatch)
    cid = "cid-c3-first"
    try:
        snapshots = list(orchestrator.run_diagnosis_stream("主轴异响", correlation_id=cid))
    finally:
        clear_token_tracker(cid)

    assert snapshots[0][1]["status"] == "start"
    assert snapshots[0][1]["diagnosis"] is None


# ========== C4：/diagnose 的 record_id 与落库容错 ==========

class _SpyLogger:
    """记录 logger 调用的桩。比 caplog 可靠：不受 logging 配置是否 propagate 影响。"""

    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        def _record(event, **kw):
            self.events.append((name, event, kw))

        return _record


_FULL_RESULT = {
    "status": "done",
    "followup_question": "",
    "diagnosis": {"根因判断": "原因1：轴承损坏"},
    "review": {"审核意见": "通过"},
    "rebuttal": {},
    "final_review": {},
    "cost": {"预计成本": "930元"},
    "workorder": {"工单编号": "WO-1"},
    "debate_round": 0,
    "correlation_id": "cid-c4",
    "token_usage": {},
}


def _post_diagnose(monkeypatch):
    monkeypatch.setattr(api, "run_diagnosis", lambda *a, **kw: dict(_FULL_RESULT))
    return TestClient(api.app).post("/diagnose", json={"fault_description": "主轴异响"})


def test_diagnose_returns_record_id(monkeypatch, isolated_db):
    """落库成功时响应里必须带 record_id（此前同步版丢掉了它）。"""
    monkeypatch.setattr(api, "save_diagnosis_record", lambda *a, **kw: 42)

    resp = _post_diagnose(monkeypatch)

    assert resp.status_code == 200
    assert resp.json()["record_id"] == 42


def test_diagnose_survives_persistence_failure(monkeypatch, isolated_db):
    """落库失败时仍返回 200 + 完整工单，并留下日志。

    用户已经等了数十秒、拿到了完整结论，因为一次 SQLite 抖动把它变成 500
    是不可接受的：他既丢了结果，也不知道"其实诊断是成功的"。
    """
    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(api, "save_diagnosis_record", _boom)
    spy = _SpyLogger()
    monkeypatch.setattr(api, "logger", spy)

    resp = _post_diagnose(monkeypatch)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["workorder"]["工单编号"] == "WO-1", "工单内容不该因为落库失败而丢失"
    assert body["record_id"] is None
    assert any(
        name == "exception" and event == "save_diagnosis_record_failed"
        for name, event, _ in spy.events
    ), f"落库失败没有留下日志痕迹：{spy.events}"


def test_followup_round_is_not_persisted(monkeypatch, isolated_db):
    """追问轮次不落库：否则历史列表里会塞满"半成品"。"""
    called = {"n": 0}
    monkeypatch.setattr(api, "run_diagnosis", lambda *a, **kw: {
        **_FULL_RESULT, "status": "need_more_info", "followup_question": "请补充报警代码"
    })
    monkeypatch.setattr(api, "save_diagnosis_record",
                        lambda *a, **kw: called.__setitem__("n", called["n"] + 1))

    resp = TestClient(api.app).post("/diagnose", json={"fault_description": "坏了"})

    assert resp.status_code == 200
    assert resp.json()["record_id"] is None
    assert called["n"] == 0


# ========== C5：图片 MIME 与"没识别出来"的可见性 ==========

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw + b"\x00" * 32).decode()


def test_detect_image_mime_by_magic_bytes():
    assert agents.detect_image_mime(_b64(b"\x89PNG\r\n\x1a\n")) == "image/png"
    assert agents.detect_image_mime(_b64(b"\xff\xd8\xff\xe0")) == "image/jpeg"
    assert agents.detect_image_mime(_b64(b"RIFF\x00\x00\x00\x00WEBP")) == "image/webp"
    assert agents.detect_image_mime(_b64(b"GIF89a")) == "image/gif"


def test_detect_image_mime_falls_back_conservatively():
    """认不出来时回退 JPEG，而不是抛异常或猜一个类型。"""
    assert agents.detect_image_mime("这不是 base64!!!") == "image/jpeg"
    assert agents.detect_image_mime(_b64(b"\x00\x01\x02\x03")) == "image/jpeg"
    assert agents.detect_image_mime("") == "image/jpeg"


def test_image_data_url_uses_detected_mime(monkeypatch):
    """PNG 必须被贴上 image/png —— 硬编码 jpeg 会让部分视觉服务拒收或解出坏图。"""
    captured = {}

    def _fake_invoke(messages, llm=None, correlation_id=None):
        captured["messages"] = messages
        return "识别结果：主轴有明显磨损"

    monkeypatch.setattr(agents, "safe_llm_invoke", _fake_invoke)
    monkeypatch.setattr(agents, "get_vision_llm", lambda: object())

    agents.extract_image_info(_b64(b"\x89PNG\r\n\x1a\n"))

    multimodal = [m for m in captured["messages"] if isinstance(m.content, list)]
    assert multimodal, "视觉调用没有构造多模态消息"
    url = multimodal[0].content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), f"实际用了：{url[:40]}"


def test_image_warning_set_when_vision_fails(monkeypatch):
    """视觉失败时 `image_warning` 必须非空 —— 否则用户以为照片被用上了。"""
    monkeypatch.setattr(orchestrator, "extract_image_info", lambda *a, **kw: "")

    state = orchestrator._build_initial_state(
        "主轴异响", image_base64="AAAA", correlation_id="cid-c5-fail"
    )

    assert state["image_description"] == ""
    assert state["image_warning"], "图片没识别出来却没有可见提示"


def test_no_image_warning_when_vision_succeeds(monkeypatch):
    """对偶：识别成功时不得有提示（否则提示会变成常驻噪音）。"""
    monkeypatch.setattr(orchestrator, "extract_image_info", lambda *a, **kw: "识别结果：轴承磨损")

    state = orchestrator._build_initial_state(
        "主轴异响", image_base64="AAAA", correlation_id="cid-c5-ok"
    )

    assert state["image_description"] == "识别结果：轴承磨损"
    assert state["image_warning"] == ""


def test_no_image_warning_when_no_image(monkeypatch):
    """没传图时也不该有提示——"没传图"和"图没识别出来"是两回事。"""
    state = orchestrator._build_initial_state("主轴异响", correlation_id="cid-c5-none")
    assert state["image_warning"] == ""


# ========== C6：抽取链路接进统一通道，两义返回值不变 ==========

def test_extract_returns_none_when_model_unavailable(monkeypatch):
    """模型彻底失败 → None（调用方短路成 llm_failed），不能退化成追问。"""
    monkeypatch.setattr(agents, "safe_llm_invoke", lambda *a, **kw: None)
    assert agents.extract_fault_info("坏了") is None


def test_extract_returns_empty_dict_when_model_answers_garbage(monkeypatch):
    """模型答了但抽不出结构化信息 → {}（调用方走追问）。

    与上一条是**两义**关系：把两者合并就会把服务故障甩锅给用户。
    """
    monkeypatch.setattr(agents, "safe_llm_invoke", lambda *a, **kw: "我看不懂你在说什么")
    assert agents.extract_fault_info("坏了") == {}


def test_extract_returns_parsed_fields_on_success(monkeypatch):
    monkeypatch.setattr(
        agents, "safe_llm_invoke",
        lambda *a, **kw: '{"设备类型": "数控机床", "报警代码": "E-203", "故障现象": ["异响"]}',
    )
    out = agents.extract_fault_info("数控机床响")
    assert out["设备类型"] == "数控机床"
    assert out["报警代码"] == "E-203"


def test_extract_second_attempt_carries_schema_errors(monkeypatch):
    """Schema 不合规时，第二次调用必须带上 Schema + 原始输出 + 报错。

    旧实现自己写了一遍重试循环，两次调用用的是**完全相同的消息** ——
    模型没有任何新信息，大概率再错一遍。项目约定是"校验失败要回灌让模型自修"。
    """
    calls = []

    def _fake(msgs, llm=None, cid=None):
        calls.append(list(msgs))
        return '{"报警代码": "E-203"}'   # 合法 JSON，但缺必填的「设备类型」

    monkeypatch.setattr(agents, "safe_llm_invoke", _fake)

    agents.extract_fault_info("坏了")

    assert len(calls) == 2, f"没有发生第二次调用：{len(calls)}"
    second = "\n".join(m.content for m in calls[1] if isinstance(m.content, str))
    assert "【目标 JSON Schema】" in second, "第二次调用没有回灌 Schema"
    assert "【校验错误】" in second, "第二次调用没有回灌校验报错"
    assert "【你上一次的输出】" in second, "第二次调用没有带上模型的原始输出"


def test_extract_second_attempt_carries_parse_error(monkeypatch):
    """**JSON 根本解析不出来**时同样要回灌。

    这一支此前只 `continue`：第二次调用拿到一模一样的消息，等于让模型再猜一次。
    被 Markdown 代码块包裹、前后带说明文字是极常见的失败形态，
    把"解析失败"这条事实告诉它通常一次就能修好。
    """
    calls = []

    def _fake(msgs, llm=None, cid=None):
        calls.append(list(msgs))
        return "我抽不出结构化信息，这是纯文字回复"   # 没有任何 JSON 结构

    monkeypatch.setattr(agents, "safe_llm_invoke", _fake)

    agents.extract_fault_info("坏了")

    assert len(calls) == 2
    second = "\n".join(m.content for m in calls[1] if isinstance(m.content, str))
    assert "【校验错误】" in second, "解析失败没有被回灌"
    assert "解析" in second, "回灌内容没有说清是「解析不出 JSON」"


def test_extract_does_not_loop_forever(monkeypatch):
    """模型一直答不对时必须收敛，不能无限回灌。"""
    calls = {"n": 0}

    def _fake(msgs, llm=None, cid=None):
        calls["n"] += 1
        return "还是不对"

    monkeypatch.setattr(agents, "safe_llm_invoke", _fake)
    assert agents.extract_fault_info("坏了") == {}
    assert calls["n"] == 2, f"重试次数失控：{calls['n']}"


# ========== C7：只重试可重试的异常 ==========

class _RaisingLLM:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        raise self.exc


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def _status_error(cls, code: int):
    return cls("boom", response=httpx.Response(code, request=_request()), body=None)


@pytest.fixture
def no_backoff(monkeypatch):
    """把退避改成 0：否则每次重试要真等 1.5s / 2.25s，测试变成秒级。"""
    import tenacity

    monkeypatch.setattr(agents._llm_invoke_with_retry.retry, "wait", tenacity.wait_none())


def test_bad_request_is_not_retried(no_backoff):
    """400 立即抛出：重试一万次也不会好，白等 3 次退避只是让用户多等 4 秒。"""
    llm = _RaisingLLM(_status_error(BadRequestError, 400))

    with pytest.raises(BadRequestError):
        agents._llm_invoke_with_retry(llm, [])

    assert llm.calls == 1, f"4xx 被重试了 {llm.calls} 次"


def test_unauthorized_is_not_retried(no_backoff):
    """401 同理：key 配错了，重试没有意义。"""
    from openai import AuthenticationError

    llm = _RaisingLLM(_status_error(AuthenticationError, 401))

    with pytest.raises(AuthenticationError):
        agents._llm_invoke_with_retry(llm, [])

    assert llm.calls == 1


def test_rate_limit_is_retried(no_backoff):
    """429 必须重试：限流是典型的瞬时错误。"""
    llm = _RaisingLLM(_status_error(RateLimitError, 429))

    with pytest.raises(RateLimitError):
        agents._llm_invoke_with_retry(llm, [])

    assert llm.calls == settings.LLM_MAX_RETRIES + 1, f"限流只试了 {llm.calls} 次"


def test_timeout_and_connection_error_are_retried(no_backoff):
    """超时与连接错误同样必须重试。"""
    for exc in (APITimeoutError(request=_request()), APIConnectionError(request=_request())):
        llm = _RaisingLLM(exc)
        with pytest.raises(type(exc)):
            agents._llm_invoke_with_retry(llm, [])
        assert llm.calls == settings.LLM_MAX_RETRIES + 1, f"{type(exc).__name__} 没有被重试"
