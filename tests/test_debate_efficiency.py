"""辩论链的省钱与把关回归测试（B1 / B2）。

## B1：辩论产出的 `行动` 与 `置信度` 此前被采集后从未使用

`agents.py` 的 `RebuttalOutput` 一直有 `行动`（反驳/修正/维持）与 `置信度`，
但 `orchestrator.py` 全文一次都没读过（grep 确认）。后果有两个：

  1. 诊断师说"维持"（结论没变）时，仍然白跑一次最终复审 —— 审核师手上的材料
     与上一轮完全相同，结论大概率还是"不通过"，纯属烧配额（终审约占总用量 12%、
     单次 3~4 秒）；
  2. 终审"通过但置信度 30"时，工单以**正常**面貌交付 —— 模型自己都不确定，
     用户却看不到。

这两条都要**成对**测：该省的要省，不该省的（有修正）必须照跑；
该标风险的要标，高置信度通过**不得**被顺手标成风险（否则风险标记会贬值）。

## B2：终审的对照物比初审还少

`agent_review_final` 此前只收到「原诊断 + 反驳」，没有 evidence、没有经验分歧，
而初审刚刚被补上这些。终审才是决定终态的那一步，这里用桩捕获 messages
断言证据文本真的进了提示词。

全部离线：所有 Agent 打桩，跑的是**真实 LangGraph**（`orchestrator.app`）。
"""

import agents
import orchestrator
from logging_config import clear_token_tracker


def _stub_happy_agents(monkeypatch):
    """与 test_resilience_and_retrieval.py 同款的桩：只留出一条可走通的链路。"""
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
        "审核意见": "不通过", "理由": "证据不足", "风险提示": "无"
    })
    monkeypatch.setattr(orchestrator, "agent_cost", lambda *a, **kw: {"预计成本": "930元"})
    monkeypatch.setattr(orchestrator, "agent_workorder", lambda *a, **kw: {"工单编号": "WO-1"})


def _run_graph(monkeypatch, correlation_id: str, rounds: int = 3) -> dict:
    state = orchestrator._build_initial_state("主轴异响", correlation_id=correlation_id)
    state["max_debate_rounds"] = rounds
    try:
        return orchestrator.app.invoke(state)
    finally:
        clear_token_tracker(correlation_id)


# ========== B1-①：行动 == 维持 时不再白跑最终复审 ==========

def test_maintain_skips_final_review(monkeypatch):
    """核心断言：诊断师"维持"原判时，`agent_review_final` 调用次数必须是 0。

    去掉修复（把 rebuttal 的边改回无条件 `add_edge("rebuttal", "final_review")`）
    后这里会变成 1。
    """
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "agent_rebuttal", lambda *a, **kw: {
        "行动": "维持", "最终根因": "原因1：轴承损坏", "依据": "资料1", "置信度": 80, "反驳理由": "有据"
    })

    calls = {"final": 0}

    def _spy_final(*a, **kw):
        calls["final"] += 1
        return {"审核意见": "通过", "理由": "ok", "置信度": 90, "风险提示": "无"}

    monkeypatch.setattr(orchestrator, "agent_review_final", _spy_final)

    result = _run_graph(monkeypatch, "cid-b1-maintain")

    assert calls["final"] == 0, "维持原判时不该再跑一次最终复审（审核师没有新材料可看）"
    # 省了调用，但必须标出来：审核意见仍是"不通过"，结论并未经独立复审确认
    assert result["workorder"]["风险等级"] != "正常"
    assert "未改变结论" in result["workorder"]["风险等级"]


def test_revise_still_runs_final_review(monkeypatch):
    """对偶：结论有修正时必须照跑终审。

    只测"维持要跳过"会让实现退化成"永远跳过终审"——那样把关强度被整体削弱，
    测试却全绿。
    """
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "agent_rebuttal", lambda *a, **kw: {
        "行动": "修正", "最终根因": "原因1：轴承磨损", "依据": "资料1", "置信度": 80, "反驳理由": "新证据"
    })

    calls = {"final": 0}

    def _spy_final(*a, **kw):
        calls["final"] += 1
        return {"审核意见": "通过", "理由": "ok", "置信度": 90, "风险提示": "无"}

    monkeypatch.setattr(orchestrator, "agent_review_final", _spy_final)

    result = _run_graph(monkeypatch, "cid-b1-revise")

    assert calls["final"] == 1, "结论变了就必须让终审看一眼"
    assert result["workorder"].get("风险等级", "正常") == "正常"


# ========== B1-②：终审"通过但置信度低"必须标风险 ==========

def test_low_confidence_pass_marks_risk(monkeypatch):
    """置信度 30 + 审核意见"通过" → 工单风险等级不能是正常值。"""
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "agent_rebuttal", lambda *a, **kw: {
        "行动": "修正", "最终根因": "原因1：轴承磨损", "依据": "资料1", "置信度": 80, "反驳理由": "新证据"
    })
    monkeypatch.setattr(orchestrator, "agent_review_final", lambda *a, **kw: {
        "审核意见": "通过", "理由": "挑不出问题", "置信度": 30, "风险提示": "无"
    })

    result = _run_graph(monkeypatch, "cid-b1-lowconf")

    assert result["workorder"]["风险等级"] != "正常"
    assert "置信度" in result["workorder"]["风险等级"]
    assert "30" in result["workorder"]["风险说明"]


def test_high_confidence_pass_stays_normal(monkeypatch):
    """对偶：置信度 90 + 通过时**不得**标风险。

    否则风险标记会变成"每张工单都有"，用户很快学会忽略它——
    与知识库为空时那条警告是同一个道理。
    """
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "agent_rebuttal", lambda *a, **kw: {
        "行动": "修正", "最终根因": "原因1：轴承磨损", "依据": "资料1", "置信度": 80, "反驳理由": "新证据"
    })
    monkeypatch.setattr(orchestrator, "agent_review_final", lambda *a, **kw: {
        "审核意见": "通过", "理由": "ok", "置信度": 90, "风险提示": "无"
    })

    result = _run_graph(monkeypatch, "cid-b1-highconf")

    assert result["workorder"].get("风险等级", "正常") == "正常"


def test_route_after_rebuttal_is_the_discriminator():
    """路由函数本身也要测：图接线断了它照样"看起来对"。"""
    maintain = {"rebuttal": {"行动": "维持"}}
    revise = {"rebuttal": {"行动": "修正"}}
    empty = {"rebuttal": {}}

    assert orchestrator.route_after_rebuttal(maintain) == "cost"
    assert orchestrator.route_after_rebuttal(revise) == "final_review"
    # 辩论没产出时不能走 cost：final_review 节点会按"无反驳对象"处理并保留原状态
    assert orchestrator.route_after_rebuttal(empty) == "final_review"


# ========== B2：终审提示词必须带上证据 ==========

class _FakeFinalReview:
    def model_dump(self):
        return {"审核意见": "通过", "理由": "ok", "置信度": 90, "风险提示": "无"}


def test_final_review_prompt_contains_evidence(monkeypatch):
    """断言证据文本真的进了终审提示词（用桩捕获 messages）。"""
    captured = {}

    def _spy(messages, model_class, **kw):
        captured["messages"] = messages
        captured["model_class"] = model_class
        return _FakeFinalReview()

    monkeypatch.setattr(agents, "invoke_and_validate", _spy)

    agents.agent_review_final(
        original_diagnosis={"根因判断": "原因1：轴承损坏"},
        rebuttal={"行动": "修正", "最终根因": "原因1：轴承磨损"},
        evidence="【资料1】\n一、离心泵\n1. 机械密封泄漏\n2. 底阀失效",
    )

    text = "".join(m.content for m in captured["messages"])
    assert "机械密封泄漏" in text, "终审提示词里没有证据文本 —— 越界检查无从执行"
    assert "【资料1】" in text
    assert captured["model_class"] is agents.FinalReviewOutput


def test_final_review_prompt_keeps_out_of_scope_boundary(monkeypatch):
    """补对照物的同时必须保留"禁止超范围质疑"的边界。

    只加资料不写边界，审核师会开始要求诊断引入资料之外的原因——
    那是另一种编造。
    """
    captured = {}

    def _spy(messages, model_class, **kw):
        captured["messages"] = messages
        return _FakeFinalReview()

    monkeypatch.setattr(agents, "invoke_and_validate", _spy)

    agents.agent_review_final(
        original_diagnosis={"根因判断": "原因1：轴承损坏"},
        rebuttal={"行动": "修正", "最终根因": "原因1：轴承磨损"},
        evidence="【资料1】\n一、主轴电机\n1. 轴承损坏",
    )

    text = "".join(m.content for m in captured["messages"])
    assert "不得要求诊断引入" in text
    assert "知识库中的经验分歧" in text


def test_final_review_node_forwards_evidence(monkeypatch):
    """接线断言：final_review_node 真的把 evidence 传下去了。

    "函数改对了 ≠ 调用点接对了" —— 只测 agent_review_final 本身，
    节点忘了传参数照样绿。
    """
    _stub_happy_agents(monkeypatch)
    monkeypatch.setattr(orchestrator, "agent_rebuttal", lambda *a, **kw: {
        "行动": "修正", "最终根因": "原因1：轴承磨损", "依据": "资料1", "置信度": 80, "反驳理由": "新证据"
    })

    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return {"审核意见": "通过", "理由": "ok", "置信度": 90, "风险提示": "无"}

    monkeypatch.setattr(orchestrator, "agent_review_final", _spy)

    _run_graph(monkeypatch, "cid-b2-wiring")

    assert captured, "终审没有被调用，这条测试失去意义"
    assert captured.get("evidence"), "final_review_node 没有把 evidence 传给终审"
    assert "轴承" in captured["evidence"]
