"""韧性、检索融合与可观测性的单元测试。

全部离线运行：不调用 LLM、不联网、不触碰真实数据库。
覆盖本轮新增/修改的关键逻辑：
  - RRF 混合检索融合
  - 真实 token 用量提取与按节点归因
  - LLM 调用失败时的短路行为（不空转、不产生空工单）
  - 数据库读写往返与 Prompt 模板可渲染性
"""

import json
import time

import pytest

import agents as agents_module
import database
import orchestrator
from agents import _content_to_text, _extract_real_usage, _reciprocal_rank_fusion
from logging_config import TokenTracker, clear_token_tracker, get_token_tracker
from orchestrator import (
    LLM_FAILED_DIAGNOSIS,
    _is_degraded,
    _tracked,
    route_after_diagnose,
    route_after_final_review,
    route_after_review,
    workorder_node,
)
from prompt_loader import PromptTemplates, render_prompt


# ========== RRF 融合：两路检索都不能被整段截断 ==========

def test_rrf_keeps_head_of_every_channel():
    """关键回归：向量通道的头部文档不能被 BM25 结果整体挤掉。"""
    bm25 = ["B1", "B2", "B3"]
    vector = ["V1", "V2", "V3"]
    fused = _reciprocal_rank_fusion([bm25, vector], k=4)
    assert fused[0] in ("B1", "V1")
    assert {"B1", "V1"}.issubset(set(fused))
    assert len(fused) == 4


def test_rrf_dedupes_across_channels():
    """同一文档出现在两路结果中只保留一份，且分数累加使其排名更靠前。"""
    fused = _reciprocal_rank_fusion([["A", "B"], ["B", "C"]], k=3)
    assert len(fused) == len(set(fused))
    assert fused[0] == "B"          # B 在两路都出现，分数最高


def test_rrf_ignores_empty_and_none_docs():
    fused = _reciprocal_rank_fusion([["A", "", None], []], k=5)
    assert fused == ["A"]


def test_rrf_respects_k_limit():
    ranked = [str(i) for i in range(20)]
    assert len(_reciprocal_rank_fusion([ranked], k=3)) == 3


def test_rrf_returns_empty_when_no_candidates():
    assert _reciprocal_rank_fusion([[], []], k=3) == []


# ========== 空知识库：必须优雅退化，而不是除零崩溃 ==========

class _StubDoc:
    def __init__(self, text):
        self.page_content = text


class _StubChroma:
    """只实现检索路径用到的方法：空知识库 + 固定向量命中。"""

    def __init__(self, documents):
        self._documents = documents

    def get(self):
        return {"documents": self._documents}

    def similarity_search(self, query, k):
        return [_StubDoc("主轴电机 轴承损坏 异响")][:k]


def test_bm25_degrades_gracefully_on_empty_knowledge_base(monkeypatch):
    """首次克隆尚未执行 build_knowledge_base.py 时，BM25Okapi([]) 会除零。
    这里断言我们提前短路成"无 BM25"，而不是让诊断崩在检索环节。"""
    monkeypatch.setattr(agents_module, "get_db", lambda: _StubChroma([]))
    monkeypatch.setattr(agents_module, "_bm25_initialized", False)
    monkeypatch.setattr(agents_module, "_bm25_index", "stale-index")
    monkeypatch.setattr(agents_module, "_doc_texts_cache", None)

    try:
        index, docs = agents_module._get_bm25()
        assert index is None
        assert docs == []
    finally:
        monkeypatch.setattr(agents_module, "_bm25_initialized", False)


def test_retrieve_evidence_falls_back_to_vector_only(monkeypatch):
    monkeypatch.setattr(agents_module, "get_db", lambda: _StubChroma([]))
    monkeypatch.setattr(agents_module, "_bm25_initialized", False)

    try:
        evidence = agents_module.retrieve_evidence("主轴异响", k=3)
        assert "轴承损坏" in evidence
        assert evidence.startswith("【资料1】")
    finally:
        monkeypatch.setattr(agents_module, "_bm25_initialized", False)


def test_retrieve_evidence_reports_no_grounding_when_nothing_found(monkeypatch):
    monkeypatch.setattr(agents_module, "get_db", lambda: _StubChroma([]))
    monkeypatch.setattr(agents_module, "_bm25_initialized", False)
    monkeypatch.setattr(_StubChroma, "similarity_search", lambda self, q, k: [])

    try:
        assert agents_module.retrieve_evidence("毫不相关的问题", k=3) == "【知识库无相关依据】"
    finally:
        monkeypatch.setattr(agents_module, "_bm25_initialized", False)


# ========== Token 用量：优先真实值，拿不到才估算 ==========

class _FakeResponse:
    def __init__(self, content="ok", usage_metadata=None, response_metadata=None):
        self.content = content
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


def test_extract_real_usage_prefers_langchain_usage_metadata():
    resp = _FakeResponse(usage_metadata={"input_tokens": 120, "output_tokens": 30})
    assert _extract_real_usage(resp) == (120, 30)


def test_extract_real_usage_falls_back_to_openai_style_metadata():
    resp = _FakeResponse(response_metadata={"token_usage": {"prompt_tokens": 80, "completion_tokens": 20}})
    assert _extract_real_usage(resp) == (80, 20)


def test_extract_real_usage_returns_none_when_unavailable():
    assert _extract_real_usage(_FakeResponse()) is None


def test_content_to_text_handles_multimodal_list():
    content = [{"type": "text", "text": "故障"}, {"type": "text", "text": "E-203"}]
    assert _content_to_text(content) == "故障E-203"


def test_content_to_text_handles_none():
    assert _content_to_text(None) == ""


def test_token_tracker_attributes_usage_to_current_node():
    tracker = TokenTracker("cid-attribution")
    tracker.start_node("diagnose")
    tracker.add_usage(100, 20, estimated=False)
    tracker.start_node("review")
    tracker.add_usage(50, 10, estimated=True)

    summary = tracker.get_summary()
    assert summary["total_tokens"] == 180
    assert summary["llm_calls"] == 2
    assert summary["estimated_calls"] == 1
    assert summary["by_node"]["diagnose"]["total_tokens"] == 120
    assert summary["by_node"]["review"]["total_tokens"] == 60


def test_tracked_decorator_sets_node_before_call():
    """_tracked 必须让节点内的调用落到该节点名下，否则 by_node 统计全挤在初始化里。"""
    correlation_id = "cid-tracked"
    tracker = get_token_tracker(correlation_id)
    try:
        @_tracked("diagnose")
        def node(state):
            get_token_tracker(state["correlation_id"]).add_usage(10, 5)
            return {}

        node({"correlation_id": correlation_id})
        assert tracker.get_summary()["by_node"]["diagnose"]["calls"] == 1
    finally:
        clear_token_tracker(correlation_id)


# ========== LLM 失败短路：不空转辩论，不产出空工单 ==========

def test_llm_failed_diagnosis_is_treated_as_degraded():
    assert _is_degraded(LLM_FAILED_DIAGNOSIS) is True


def test_route_after_diagnose_short_circuits_on_llm_failure():
    assert route_after_diagnose({"status": "llm_failed"}) == "cost"
    assert route_after_diagnose({"status": "insufficient_knowledge"}) == "cost"


def test_route_after_review_skips_debate_when_review_unavailable():
    """审核没产出时没有驳回理由可辩论，继续辩论只会白烧 2N 次调用。"""
    state = {"diagnosis": {"根因判断": "主轴轴承损坏"}, "review": {}, "debate_round": 0, "max_debate_rounds": 3}
    assert route_after_review(state) == "cost"


def test_route_after_final_review_skips_debate_when_review_unavailable():
    state = {"final_review": {}, "debate_round": 1, "max_debate_rounds": 3}
    assert route_after_final_review(state) == "cost"


def test_workorder_marks_llm_failure_risk(monkeypatch):
    monkeypatch.setattr(orchestrator, "agent_workorder", lambda *a, **kw: {"工单编号": "WO-1"})
    state = {
        "correlation_id": "cid-wo",
        "diagnosis": {"报警代码": "E-203", "根因判断": "模型服务调用失败，无法完成诊断",
                      "依据": "无", "排查建议": []},
        "review": {},
        "cost": {},
        "rebuttal": {},
        "final_review": {},
        "debate_round": 0,
        "max_debate_rounds": 3,
        "status": "llm_failed",
    }
    result = workorder_node(state)
    assert result["workorder"]["风险等级"] == "待人工确认（模型服务异常）"
    assert result["status"] == "llm_failed"      # 失败状态不能被 "done" 覆盖


def test_workorder_marks_knowledge_gap_risk(monkeypatch):
    monkeypatch.setattr(orchestrator, "agent_workorder", lambda *a, **kw: {"工单编号": "WO-2"})
    state = {
        "correlation_id": "cid-wo2",
        "diagnosis": {"报警代码": "N/A", "根因判断": "知识库无相关依据，无法诊断",
                      "依据": "无", "排查建议": []},
        "review": {},
        "cost": {},
        "rebuttal": {},
        "final_review": {},
        "debate_round": 0,
        "max_debate_rounds": 3,
        "status": "insufficient_knowledge",
    }
    result = workorder_node(state)
    assert result["workorder"]["风险等级"] == "待人工确认（知识库无依据）"
    assert result["status"] == "insufficient_knowledge"


def test_workorder_normal_path_still_returns_done(monkeypatch):
    monkeypatch.setattr(orchestrator, "agent_workorder", lambda *a, **kw: {"工单编号": "WO-3"})
    state = {
        "correlation_id": "cid-wo3",
        "diagnosis": {"报警代码": "E-203", "根因判断": "主轴轴承损坏", "依据": "资料1", "排查建议": []},
        "review": {"审核意见": "通过"},
        "cost": {},
        "rebuttal": {},
        "final_review": {"审核意见": "通过"},
        "debate_round": 0,
        "max_debate_rounds": 3,
        "status": "costed",
    }
    result = workorder_node(state)
    assert result["status"] == "done"
    assert "风险等级" not in result["workorder"]


def test_workorder_falls_back_when_generation_fails(monkeypatch):
    """工单生成失败也要给出可读工单，不能让用户看到空白。"""
    monkeypatch.setattr(orchestrator, "agent_workorder", lambda *a, **kw: {})
    state = {
        "correlation_id": "cid-wo4",
        "diagnosis": {"根因判断": "主轴轴承损坏"},
        "review": {},
        "cost": {},
        "rebuttal": {},
        "final_review": {},
        "debate_round": 0,
        "max_debate_rounds": 3,
        "status": "costed",
    }
    result = workorder_node(state)
    assert result["status"] == "llm_failed"
    assert result["workorder"]["风险等级"] == "待人工确认（模型服务异常）"


# ========== 数据库：写入后能原样读回 ==========

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    database.close_db_connections()
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(database, "_db_initialized", False)
    yield database
    database.close_db_connections()


def test_save_and_read_back_record(temp_db):
    payload = {
        "status": "done",
        "diagnosis": {"根因判断": "主轴轴承损坏"},
        "review": {"审核意见": "通过"},
        "rebuttal": {},
        "final_review": {},
        "cost": {"预计成本": "1230元"},
        "workorder": {"工单编号": "WO-1"},
        "debate_round": 1,
    }
    temp_db.save_diagnosis_record("主轴异响", payload, token_usage={"total_tokens": 999})

    rows = temp_db.get_records()
    assert len(rows) == 1
    row = rows[0]
    assert row["fault_description"] == "主轴异响"
    assert row["diagnosis"]["根因判断"] == "主轴轴承损坏"
    assert row["workorder"]["工单编号"] == "WO-1"
    assert row["token_usage"]["total_tokens"] == 999
    assert row["debate_round"] == 1


def test_get_records_filters_by_keyword_and_status(temp_db):
    temp_db.save_diagnosis_record("液压泵异响", {"status": "done"}, {})
    temp_db.save_diagnosis_record("主轴异响", {"status": "insufficient_knowledge"}, {})

    assert len(temp_db.get_records(keyword="液压")) == 1
    assert len(temp_db.get_records(status="done")) == 1
    assert temp_db.get_records(status="done")[0]["fault_description"] == "液压泵异响"


def test_get_stats_aggregates_tokens(temp_db):
    temp_db.save_diagnosis_record("A", {"status": "done", "debate_round": 2}, {"total_tokens": 100})
    temp_db.save_diagnosis_record("B", {"status": "done", "debate_round": 4}, {"total_tokens": 200})

    stats = temp_db.get_stats()
    assert stats["total_records"] == 2
    assert stats["total_tokens"] == 300
    assert stats["by_status"] == {"done": 2}


def test_get_stats_tolerates_corrupt_token_json(temp_db):
    """旧库里可能存在空串/非法 JSON，统计不能因此崩掉。"""
    temp_db.save_diagnosis_record("A", {"status": "done"}, {})
    with temp_db.get_db_connection() as conn:
        conn.execute("UPDATE diagnosis_records SET token_usage = ?", ("{不是JSON",))

    assert temp_db.get_stats()["total_tokens"] == 0


def test_get_stats_reports_db_size_bytes(temp_db):
    """数据库大小由后端一并返回。

    为什么要后端给：Streamlit 统计页原本在前端 `Path(settings.DB_PATH).stat()` 读文件，
    迁到 React 之后前端拿不到（也不该知道）DB 路径，所以挪到 get_stats()。

    断言"大于 0"而不是写死字节数：临时库里刚存过记录，
    文件必然非空；写死具体大小会在 SQLite 版本变化时无谓地红。
    """
    temp_db.save_diagnosis_record("A", {"status": "done"}, {})

    size = temp_db.get_stats()["db_size_bytes"]
    assert isinstance(size, int)
    assert size > 0


def test_get_stats_db_size_never_breaks_other_metrics(temp_db, monkeypatch):
    """读文件大小失败时不能拖垮整个统计接口。

    判别式：若实现裸写 `os.path.getsize(DB_PATH)` 且不做兜底，
    一次 OSError（文件被占用、权限、路径异常）会让**整个 /stats 挂掉**，
    而统计页上真正重要的三个指标一个都显示不出来。
    次要指标绝不能拖垮主要指标。

    注意不能靠改 `DB_PATH` 来模拟——那会让 `init_db()` 先连不上库，
    测到的就不是文件大小这条路径了。
    """
    import os

    temp_db.save_diagnosis_record("A", {"status": "done"}, {})

    def boom(_path):
        raise OSError("模拟 stat 失败")

    monkeypatch.setattr(os.path, "getsize", boom)

    stats = temp_db.get_stats()
    assert stats["db_size_bytes"] == 0
    # 关键：其余指标必须照常返回
    assert stats["total_records"] == 1
    assert stats["by_status"] == {"done": 1}


def test_delete_record(temp_db):
    temp_db.save_diagnosis_record("A", {"status": "done"}, {})
    record_id = temp_db.get_records()[0]["id"]
    assert temp_db.delete_record(record_id) is True
    assert temp_db.get_records() == []
    assert temp_db.delete_record(record_id) is False


# ========== Prompt 模板：必须存在且能渲染 ==========

TEMPLATE_VARS = {
    PromptTemplates.EXTRACT_FAULT_INFO: {"user_input": "主轴异响"},
    PromptTemplates.CHECK_RELEVANCE: {"fault_description": "主轴异响", "evidence": "资料"},
    PromptTemplates.AGENT_DIAGNOSE: {"evidence": "资料", "fault": "{}", "exclusion_text": ""},
    PromptTemplates.AGENT_REVIEW: {"diagnosis": "{}"},
    PromptTemplates.AGENT_COST: {"diagnosis": "{}", "review": "{}"},
    PromptTemplates.AGENT_WORKORDER: {"diagnosis": "{}", "review": "{}", "cost": "{}"},
    PromptTemplates.AGENT_REBUTTAL: {"diagnosis": "{}", "review": "{}", "evidence": "资料", "fault": "{}"},
    PromptTemplates.AGENT_REVIEW_FINAL: {"original_diagnosis": "{}", "rebuttal": "{}"},
    PromptTemplates.MAP_EXCLUDED_CAUSES: {"exclusion_list": "[]", "numbered": "1. xxx"},
    PromptTemplates.EVALUATE_SEMANTIC: {"diagnosis_text": "a", "expected_text": "b"},
    PromptTemplates.EXTRACT_IMAGE_INFO: {},
    PromptTemplates.FIX_SCHEMA: {"schema": "{}", "errors": "[]", "raw": "{}"},
}


@pytest.mark.parametrize("template, kwargs", list(TEMPLATE_VARS.items()))
def test_every_prompt_template_renders(template, kwargs):
    rendered = render_prompt(template, **kwargs)
    assert rendered.strip(), f"{template} 渲染结果为空"
    # 模板里不该残留未替换的占位符
    assert "{{" not in rendered and "}}" not in rendered


def test_agent_workorder_template_embeds_cost_json():
    rendered = render_prompt(
        PromptTemplates.AGENT_WORKORDER,
        diagnosis="{}", review="{}",
        cost=json.dumps({"预计成本": "1230元"}, ensure_ascii=False)
    )
    assert "1230元" in rendered


# ========== 状态机冒烟测试：把 Agent 全部替换成桩，跑完整张图 ==========
# 这些测试保证节点装饰、条件路由、状态流转在真实 LangGraph 执行下是自洽的，
# 且全程离线、不花一分钱。

def _stub_happy_agents(monkeypatch):
    monkeypatch.setattr(orchestrator, "extract_fault_info", lambda *a, **kw: {
        "设备类型": "数控机床主轴电机", "报警代码": "E-203",
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


def test_graph_happy_path_reaches_done(monkeypatch):
    _stub_happy_agents(monkeypatch)
    correlation_id = "cid-graph-ok"
    try:
        result = orchestrator.app.invoke(orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id))
        assert result["status"] == "done"
        assert result["workorder"]["工单编号"] == "WO-1"
        assert result["debate_round"] == 0
    finally:
        clear_token_tracker(correlation_id)


def test_graph_skips_review_and_debate_when_diagnose_fails(monkeypatch):
    """诊断模型失败时：不应调用审核、不应进入辩论，直接出带风险标记的工单。"""
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "agent_diagnose", lambda *a, **kw: {})

    calls = {"review": 0, "rebuttal": 0}
    monkeypatch.setattr(orchestrator, "agent_review",
                        lambda *a, **kw: calls.__setitem__("review", calls["review"] + 1) or {})
    monkeypatch.setattr(orchestrator, "agent_rebuttal",
                        lambda *a, **kw: calls.__setitem__("rebuttal", calls["rebuttal"] + 1) or {})

    correlation_id = "cid-graph-fail"
    try:
        result = orchestrator.app.invoke(orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id))
        assert result["status"] == "llm_failed"
        assert calls == {"review": 0, "rebuttal": 0}
        assert result["workorder"]["风险等级"] == "待人工确认（模型服务异常）"
    finally:
        clear_token_tracker(correlation_id)


def test_graph_asks_followup_when_info_insufficient(monkeypatch):
    """信息不足时应在 check_info 处终止并返回追问，不进检索与诊断。"""
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "extract_fault_info", lambda *a, **kw: {})

    def _should_not_run(*a, **kw):
        raise AssertionError("信息不足时不应进入检索/诊断")

    monkeypatch.setattr(orchestrator, "retrieve_evidence", _should_not_run)
    monkeypatch.setattr(orchestrator, "agent_diagnose", _should_not_run)

    correlation_id = "cid-graph-ask"
    try:
        result = orchestrator.app.invoke(orchestrator._build_initial_state("坏了", correlation_id=correlation_id))
        assert result["status"] == "need_more_info"
        assert result["followup_question"]
        assert not result.get("diagnosis")
    finally:
        clear_token_tracker(correlation_id)


def test_graph_debate_loop_is_capped(monkeypatch):
    """审核始终不通过时，辩论必须在上限处收敛，不能无限循环。"""
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "agent_review", lambda *a, **kw: {
        "审核意见": "不通过", "理由": "证据不足", "风险提示": "无"
    })
    monkeypatch.setattr(orchestrator, "agent_rebuttal", lambda *a, **kw: {
        "行动": "反驳", "最终根因": "原因1：轴承损坏", "依据": "资料1", "置信度": 80, "反驳理由": "有据"
    })
    monkeypatch.setattr(orchestrator, "agent_review_final", lambda *a, **kw: {
        "审核意见": "不通过", "理由": "仍不认可", "置信度": 60, "风险提示": "无"
    })

    correlation_id = "cid-graph-loop"
    try:
        state = orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id)
        state["max_debate_rounds"] = 2
        result = orchestrator.app.invoke(state)
        assert result["debate_round"] == 2                      # 收敛在上限，不多不少
        assert result["workorder"]["风险等级"] == "高风险待复核"
    finally:
        clear_token_tracker(correlation_id)


# ========== 一致性修复的回归测试 ==========
# 下面这些分支此前没有任何测试覆盖，这正是它们能长期存活的原因。

def test_graph_short_circuits_when_extraction_fails(monkeypatch):
    """抽取阶段模型挂掉：必须短路，不能退化成"追问用户补充设备类型"。

    此前 extract_fault_info 把"模型失败"和"信息不足"都表示成 {}，
    check_info 只能判定信息不足，于是服务故障被甩锅给用户。
    """
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "extract_fault_info", lambda *a, **kw: None)

    def _should_not_run(*a, **kw):
        raise AssertionError("抽取失败时不应进入检索/诊断")

    monkeypatch.setattr(orchestrator, "retrieve_evidence", _should_not_run)
    monkeypatch.setattr(orchestrator, "agent_diagnose", _should_not_run)

    correlation_id = "cid-graph-extract-fail"
    try:
        result = orchestrator.app.invoke(orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id))
        assert result["status"] == "llm_failed"
        assert result["followup_question"] == ""                 # 关键：不是追问
        assert result["workorder"]["风险等级"] == "待人工确认（模型服务异常）"
    finally:
        clear_token_tracker(correlation_id)


def test_extract_info_node_keeps_empty_dict_as_insufficient_info(monkeypatch):
    """模型正常响应但没抽到东西：这才是真正的"信息不足"，要照常追问。"""
    monkeypatch.setattr(orchestrator, "extract_fault_info", lambda *a, **kw: {})
    result = orchestrator.extract_info_node({"user_input": "坏了", "correlation_id": "cid-x"})
    assert result["status"] == "extracted"
    assert result["fault_info"] == {}


def test_graph_marks_failure_when_cost_unavailable(monkeypatch):
    """成本算不出来时必须标记失败。此前 cost 是唯一没有失败短路的分支：
    返回 {} 也会把状态置成 costed，最后报"✅ 诊断流程完成"，用户拿到没有报价的工单。"""
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "agent_cost", lambda *a, **kw: {})

    correlation_id = "cid-graph-cost-fail"
    try:
        result = orchestrator.app.invoke(orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id))
        assert result["status"] == "llm_failed"
        assert result["cost"]["预计成本"] == "N/A"
        assert result["workorder"]["风险等级"] == "待人工确认（模型服务异常）"
    finally:
        clear_token_tracker(correlation_id)


def test_cost_node_skips_llm_call_on_terminal_failure(monkeypatch):
    """已是失败终态时，再花一次调用去"猜"备件清单没有意义。"""
    def _should_not_run(*a, **kw):
        raise AssertionError("终态失败时不该再调用成本模型")

    monkeypatch.setattr(orchestrator, "agent_cost", _should_not_run)
    result = orchestrator.cost_node({
        "correlation_id": "cid-cost-skip",
        "diagnosis": dict(orchestrator.LLM_FAILED_DIAGNOSIS),
        "review": {},
        "status": "llm_failed",
    })
    assert result["status"] == "llm_failed"
    assert result["cost"]["预计成本"] == "N/A"


def test_graph_human_review_still_produces_workorder(monkeypatch):
    """转人工不再是死胡同：同样要产出工单，只是明确标注不可直接执行。"""
    _stub_happy_agents(monkeypatch)
    # 构造"状态正常、但根因文本是降级形态"，触发 route_after_review → human_review
    monkeypatch.setattr(orchestrator, "agent_diagnose", lambda *a, **kw: {
        "报警代码": "E-203", "根因判断": "资料不相关，无法诊断", "依据": "无", "排查建议": []
    })

    correlation_id = "cid-graph-human"
    try:
        result = orchestrator.app.invoke(orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id))
        assert result["status"] == "pending_human_review"
        assert result["workorder"]["风险等级"] == "待人工复核（诊断结论不可自动采信）"
    finally:
        clear_token_tracker(correlation_id)


def test_effective_review_prefers_final_review():
    """辩论翻盘后，审核口径必须跟着走，否则工单会自相矛盾。"""
    state = {"review": {"审核意见": "不通过"}, "final_review": {"审核意见": "通过"}}
    assert orchestrator._effective_review(state)["审核意见"] == "通过"


def test_effective_review_falls_back_to_initial_review():
    state = {"review": {"审核意见": "通过"}, "final_review": {}}
    assert orchestrator._effective_review(state)["审核意见"] == "通过"


def test_workorder_uses_final_review_after_successful_debate(monkeypatch):
    """工单里的审核意见必须是最终复审那版，不能还写着初始的"不通过"。"""
    captured = {}

    def fake_workorder(diagnosis, review, cost, correlation_id=None):
        captured["review"] = review
        captured["diagnosis"] = diagnosis
        return {"工单编号": "WO-1"}

    monkeypatch.setattr(orchestrator, "agent_workorder", fake_workorder)
    state = {
        "correlation_id": "cid-wo-review",
        "diagnosis": {"报警代码": "E-203", "根因判断": "主轴轴承损坏", "依据": "资料1", "排查建议": []},
        "review": {"审核意见": "不通过", "理由": "证据不足"},
        "rebuttal": {"最终根因": "主轴轴承损坏"},
        "final_review": {"审核意见": "通过", "理由": "补充证据后认可"},
        "cost": {},
        "debate_round": 1,
        "max_debate_rounds": 3,
        "status": "costed",
    }
    orchestrator.workorder_node(state)
    assert captured["review"]["审核意见"] == "通过"


# ========== 熔断器：成功必须清零，且要线程安全 ==========

def test_circuit_breaker_resets_failure_count_on_success():
    """成功必须清零失败计数。若只在 half-open 分支里清零，语义就从"连续失败"
    退化成"累计失败"——成功夹在中间也不重置，几天内攒够阈值照样熔断。"""
    breaker = agents_module.CircuitBreaker(failure_threshold=3, recovery_timeout=60)

    def boom():
        raise RuntimeError("upstream down")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(boom)
    assert breaker.failure_count == 2

    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.failure_count == 0
    assert breaker.state == "closed"


def test_circuit_breaker_opens_after_consecutive_failures():
    breaker = agents_module.CircuitBreaker(failure_threshold=3, recovery_timeout=60)

    def boom():
        raise RuntimeError("upstream down")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            breaker.call(boom)

    assert breaker.state == "open"
    with pytest.raises(agents_module.CircuitBreakerOpenError):
        breaker.call(lambda: "ok")


# ========== Schema 校验失败：应当自修，而不是直接短路整条链路 ==========

def test_invoke_and_validate_repairs_schema_mismatch(monkeypatch):
    """模型把"排查建议"写成字符串时，回灌报错让它自修即可，
    不该像"模型彻底挂了"那样短路整条诊断链路。"""
    calls = []

    def fake_invoke(messages, llm=None, correlation_id=None):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps({
                "报警代码": "E-203", "根因判断": "轴承损坏",
                "依据": "资料1", "排查建议": "更换轴承"
            }, ensure_ascii=False)
        return json.dumps({
            "报警代码": "E-203", "根因判断": "轴承损坏",
            "依据": "资料1", "排查建议": ["更换轴承"]
        }, ensure_ascii=False)

    monkeypatch.setattr(agents_module, "safe_llm_invoke", fake_invoke)

    model = agents_module.invoke_and_validate([], agents_module.DiagnosisOutput)
    assert model is not None
    assert model.排查建议 == ["更换轴承"]
    assert len(calls) == 2                # 第二次是带着校验报错的重试
    assert len(calls[1]) == 1             # 修复提示被追加进消息列表


def test_invoke_and_validate_returns_none_when_model_unavailable(monkeypatch):
    monkeypatch.setattr(agents_module, "safe_llm_invoke", lambda *a, **kw: None)
    assert agents_module.invoke_and_validate([], agents_module.DiagnosisOutput) is None


def test_invoke_and_validate_returns_none_after_exhausting_repairs(monkeypatch):
    """修完仍不合规就老实返回 None，不能假装成功。"""
    monkeypatch.setattr(agents_module, "safe_llm_invoke", lambda *a, **kw: json.dumps({"报警代码": "E-203"}))
    assert agents_module.invoke_and_validate([], agents_module.DiagnosisOutput) is None


# ========== 相关性判定：非预期输出不能默认放行 ==========

def test_check_relevance_returns_none_on_unrecognized_verdict(monkeypatch):
    """此前是 `"不相关" not in content`，模型答"无法判断"会被当成"相关"放行。"""
    monkeypatch.setattr(agents_module, "safe_llm_invoke", lambda *a, **kw: "无法判断")
    assert agents_module.check_relevance("主轴异响", "资料") is None


@pytest.mark.parametrize("verdict, expected", [("相关", True), ("不相关", False)])
def test_check_relevance_accepts_explicit_verdicts(monkeypatch, verdict, expected):
    monkeypatch.setattr(agents_module, "safe_llm_invoke", lambda *a, **kw: verdict)
    assert agents_module.check_relevance("主轴异响", "资料") is expected


def test_check_relevance_returns_none_when_llm_unavailable(monkeypatch):
    monkeypatch.setattr(agents_module, "safe_llm_invoke", lambda *a, **kw: None)
    assert agents_module.check_relevance("主轴异响", "资料") is None


# ========== 节点耗时归因 ==========

def test_tracked_records_node_duration():
    correlation_id = "cid-duration"
    try:
        @_tracked("diagnose")
        def node(state):
            time.sleep(0.01)
            return {}

        node({"correlation_id": correlation_id})
        by_node = get_token_tracker(correlation_id).get_summary()["by_node"]
        assert by_node["diagnose"]["duration_ms"] >= 10
    finally:
        clear_token_tracker(correlation_id)


# ========== 排除映射缓存：辩论阶段不该对同一份输入重复调用 ==========

def test_map_excluded_causes_caches_identical_inputs(monkeypatch):
    calls = {"n": 0}

    def fake_invoke(messages, llm=None, correlation_id=None):
        calls["n"] += 1
        return json.dumps({"排除编号": [1]}, ensure_ascii=False)

    monkeypatch.setattr(agents_module, "safe_llm_invoke", fake_invoke)
    agents_module._exclusion_cache.clear()

    entries = [("主轴电机", "轴承损坏"), ("主轴电机", "负载过大")]
    try:
        first = agents_module.map_excluded_causes(["轴承没问题"], entries, [0, 1])
        second = agents_module.map_excluded_causes(["轴承没问题"], entries, [0, 1])
        assert first == second == [0]
        assert calls["n"] == 1                 # 第二次命中缓存，没有再调模型
    finally:
        agents_module._exclusion_cache.clear()


# ========== 旧库迁移：total_tokens 回填 ==========

def test_backfill_total_tokens_from_legacy_rows(temp_db):
    """老库只有 token_usage JSON，聚合统计要能正确回填；脏数据记 0 而不是炸掉迁移。"""
    temp_db.save_diagnosis_record("A", {"status": "done"}, {"total_tokens": 123})

    with temp_db.get_db_connection() as conn:
        conn.execute("UPDATE diagnosis_records SET total_tokens = NULL")
        conn.execute(
            "INSERT INTO diagnosis_records (created_at, fault_description, status, token_usage, total_tokens)"
            " VALUES (?, ?, ?, ?, NULL)",
            ("2026-01-01T00:00:00", "老记录", "done", '{"total_tokens": 77}')
        )
        conn.execute(
            "INSERT INTO diagnosis_records (created_at, fault_description, status, token_usage, total_tokens)"
            " VALUES (?, ?, ?, ?, NULL)",
            ("2026-01-01T00:00:00", "脏数据", "done", "{不是JSON")
        )

    with temp_db.get_db_connection() as conn:
        temp_db._backfill_total_tokens(conn.cursor())

    assert temp_db.get_stats()["total_tokens"] == 200      # 123 + 77，脏数据记 0



# ========== 审核师"有牙"的回归守卫 ==========
# 这一组防的是"多 Agent 退化成单 Agent + 一次无害审核"。
#
# 背景：审核节点曾只传 diagnosis，不传检索资料。审核师没有对照物，
# 只能判断诊断结论是否自圆其说；而诊断提示词又强制模型写满知识库里的
# 全部原因。结果审核恒通过、辩论永不触发——架构图上有辩论环，
# 实际一次都不走。这个缺陷在图上完全看不出来，只有读提示词才发现。

def test_review_node_passes_evidence_and_fault_to_reviewer(monkeypatch):
    """审核节点必须把检索资料、报修信息、知识库分歧一起交给审核师。

    这是"审核有牙"的唯一保障：没有对照物，它无法判断诊断里写的原因
    是否都有出处，也就只能一律放行。分歧是**最后补上的那块对照物**——
    库里躺着 36 处"多位师傅判断不一致"，审核师看不到就只能当没有。
    """
    _stub_happy_agents(monkeypatch)
    # 让检索资料带上「经验分歧」：分歧也必须能流到审核师手上
    monkeypatch.setattr(
        orchestrator, "retrieve_evidence",
        lambda *a, **kw: (
            "【资料1】\n一、主轴电机报警代码E-203\n"
            "可能原因：\n1. 轴承损坏\n"
            "经验分歧：\n- 张师傅认为先查程序最省事。\n- 李师傅坚持先拆轴承。\n"
        ),
    )
    captured = {}

    def _spy(diagnosis, evidence="", fault="", correlation_id=None, disagreements=None):
        captured["evidence"] = evidence
        captured["fault"] = fault
        captured["disagreements"] = disagreements
        return {"审核意见": "通过", "理由": "有依据", "风险提示": "无"}

    monkeypatch.setattr(orchestrator, "agent_review", _spy)

    correlation_id = "cid-review-has-evidence"
    try:
        orchestrator.app.invoke(
            orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id)
        )
    finally:
        clear_token_tracker(correlation_id)

    assert captured, "审核节点没有调用 agent_review"
    assert captured["evidence"], "审核师没拿到检索资料，等于没有对照物"
    assert "轴承" in captured["evidence"], f"传给审核师的不是真实证据：{captured['evidence']!r}"
    assert captured["fault"], "审核师没拿到报修信息，无法做排除条件检查"
    assert "E-203" in captured["fault"], f"报修信息内容不对：{captured['fault']!r}"
    assert captured["disagreements"], (
        "审核师没拿到知识库分歧——分歧就仍然是它的盲区，"
        "「诊断只取了一派」永远判不出来，辩论也就永远不触发"
    )
    assert "李师傅" in str(captured["disagreements"]), "分歧内容没传到审核师手上"


def test_review_prompt_declares_five_checks_and_forbids_overreach():
    """审核提示词必须同时具备：五项可据检查 + 禁止超范围质疑。

    只加检查不加边界，审核师会开始编造驳回理由（"你还没检查液压系统"，
    而资料里压根没有液压内容）；只加边界不加检查，则退回"一律放行"。
    两者必须成对。

    第 5 项（分歧检查）是 2026-09-25 补的：前四项都查不出"诊断只取了知识库
    两派意见中的一派"，而库里这样的分歧有 36 处，此前完全落在审核师盲区里。
    """
    from prompt_loader import PromptTemplates, render_prompt

    rendered = render_prompt(
        PromptTemplates.AGENT_REVIEW,
        diagnosis='{"根因判断": "原因1：轴承损坏"}',
        evidence="【资料1】\n1. 轴承损坏",
        fault='{"报警代码": "E-203"}',
    )

    # 五项检查
    for kw in ("越界", "遗漏", "排除", "证据强度", "分歧"):
        assert kw in rendered, f"审核提示词缺少「{kw}」检查"
    # 必须把对照物渲染进去，而不是留占位符
    assert "轴承损坏" in rendered, "evidence 没有被渲染进提示词"
    assert "E-203" in rendered, "fault 没有被渲染进提示词"
    assert "{{" not in rendered, "提示词里仍有未渲染的占位符"
    # 防超范围质疑的边界
    assert "不得" in rendered and "之外" in rendered, "缺少禁止超范围质疑的边界"
    # 防"为显得严格而编造驳回"
    assert "应当判" in rendered, "缺少'找不到问题就应判通过'的兜底指令"


def test_review_prompt_renders_when_evidence_and_fault_omitted():
    """历史调用点只传 diagnosis 时不能炸——缺参数必须退化为占位文案。

    agent_review 的两个新参数给了默认值而非设为必填，正是为了这个：
    否则任何只传 diagnosis 的老调用点（含测试桩）会直接 TypeError。
    """
    from prompt_loader import PromptTemplates, render_prompt

    rendered = render_prompt(PromptTemplates.AGENT_REVIEW, diagnosis='{"根因判断": "x"}')
    assert "{{" not in rendered
    assert "未提供" in rendered, "缺少对照物时应给出明确的占位文案，而不是静默留空"


def test_agent_review_tolerates_missing_evidence_arguments():
    """agents.agent_review 只传 diagnosis 也必须能跑（不抛 TypeError）。"""
    import agents as agents_module

    captured = {}

    def _fake(messages, schema, correlation_id=None):
        captured["prompt"] = messages[-1].content
        class _M:
            def model_dump(self):
                return {"审核意见": "通过", "理由": "x", "风险提示": "无"}
        return _M()

    original = agents_module.invoke_and_validate
    agents_module.invoke_and_validate = _fake
    try:
        out = agents_module.agent_review({"根因判断": "轴承损坏"})
    finally:
        agents_module.invoke_and_validate = original

    assert out["审核意见"] == "通过"
    assert "轴承损坏" in captured["prompt"]



def test_rebuttal_node_passes_initial_evidence(monkeypatch):
    """辩论节点必须把**初检证据**传给 agent_rebuttal。

    辩论会用自己的查询重新检索并**覆盖** state["evidence"]，所以必须另存
    initial_evidence；否则越界护栏拿不到"本条故障的范围"，只能退化成不设防
    （消融实验实测：多 Agent 幻觉率 26% vs 单 Agent 4%，全部落在辩论触发用例上）。
    """
    _stub_happy_agents(monkeypatch)
    captured = {}

    def _spy(diagnosis, review, evidence, fault, initial_evidence="", correlation_id=None):
        captured["initial_evidence"] = initial_evidence
        captured["evidence"] = evidence
        return {"行动": "维持", "最终根因": "原因1：轴承损坏", "依据": "资料1", "置信度": 80, "反驳理由": "x"}

    monkeypatch.setattr(orchestrator, "agent_review", lambda *a, **kw: {
        "审核意见": "不通过", "理由": "依据不足", "风险提示": "无"
    })
    monkeypatch.setattr(orchestrator, "agent_rebuttal", _spy)

    correlation_id = "cid-initial-evidence"
    try:
        state = orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id)
        state["max_debate_rounds"] = 1
        orchestrator.app.invoke(state)
    finally:
        clear_token_tracker(correlation_id)

    assert captured, "辩论没有被触发，这条测试失去意义"
    assert captured["initial_evidence"], "辩论节点没有传初检证据，越界护栏将失效"
    assert "轴承" in captured["initial_evidence"]

