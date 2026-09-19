"""纯函数单元测试：全部离线运行，不调用任何 LLM / 网络 / 数据库。

覆盖 agents.py 与 orchestrator.py 中决定业务正确性、且无副作用的函数：
成本计算、信息充分性判定、知识库原因解析、排除条件裁剪、路由决策等。
"""

import pytest

from agents import (
    _relevant_scope,
    calculate_cost,
    extract_kb_causes,
    generate_followup_question,
    is_equipment_in_evidence,
    is_info_sufficient,
    remove_excluded_causes,
    safe_parse_json,
    validate_and_parse,
    DiagnosisOutput,
)
from orchestrator import (
    _is_degraded,
    route_after_check_info,
    route_after_diagnose,
    route_after_final_review,
    route_after_review,
)


# ========== calculate_cost：规则化成本，绝不交给模型算 ==========

def test_calculate_cost_known_parts_and_labor():
    result = calculate_cost(["主轴轴承", "润滑脂"], 2.0)
    assert result["备件费用"] == 930          # 850 + 80
    assert result["工时费用"] == 300          # 2.0 * 150
    assert result["总费用"] == 1230
    assert result["未知备件"] == []


def test_calculate_cost_unknown_parts_are_not_billed():
    result = calculate_cost(["不存在的备件"], 1.0)
    assert result["备件费用"] == 0
    assert result["未知备件"] == ["不存在的备件"]
    assert result["总费用"] == 150


def test_calculate_cost_ignores_surrounding_whitespace():
    result = calculate_cost(["  主轴轴承  "], 0)
    assert result["备件费用"] == 850


# ========== is_info_sufficient / generate_followup_question：多轮追问 ==========

@pytest.mark.parametrize("fault_info, expected", [
    ({}, False),
    ({"设备类型": "数控机床", "故障现象": ["异响"]}, True),
    ({"设备类型": "", "报警代码": "E-203"}, True),
    ({"设备类型": "数控机床", "报警代码": "null"}, False),
    ({"设备类型": "数控机床", "故障现象": []}, False),
])
def test_is_info_sufficient(fault_info, expected):
    assert is_info_sufficient(fault_info) is expected


def test_generate_followup_question_lists_missing_fields():
    question = generate_followup_question({})
    assert "设备类型" in question
    assert "故障现象" in question


def test_generate_followup_question_empty_when_all_fields_present():
    fault_info = {"设备类型": "数控机床", "故障现象": ["异响"], "报警代码": "E-203"}
    assert generate_followup_question(fault_info) == ""


def test_generate_followup_question_alarm_code_optional_when_specific_phenomenon():
    """设计意图：报警代码是『细化信号』之一，不强制追问。
    当设备类型 + 具体故障现象都已给出时，不再为缺失的报警代码反复打扰用户——
    用户没有报警代码可能就是真的没有，给他『够了就别再问』的体验。

    旧版本把报警代码视为必填字段，所以这条曾经断言『追问里必须含「报警代码」』。
    改 `is_info_sufficient` 把报警代码从『必填』降为『细化信号』后，配套：
    充分判定放宽了，追问也得相应收敛，否则会自己打自己嘴巴。
    """
    question = generate_followup_question({"设备类型": "数控机床", "故障现象": ["异响"]})
    # 设计意图：此时故障信息已具体到能诊断，不必再追问报警代码
    assert question == ""


# ========== extract_kb_causes：解析知识库"可能原因"条目 ==========

KB_EVIDENCE = """【资料1】
一、主轴电机
1. 负载过大或负载不平衡
2. 轴承损坏或润滑不良

二、冷却系统
1. 冷却风扇故障
排查建议：检查风扇接线
"""


def test_extract_kb_causes_parses_sections_and_entries():
    entries = extract_kb_causes(KB_EVIDENCE)
    assert ("主轴电机", "负载过大或负载不平衡") in entries
    assert ("主轴电机", "轴承损坏或润滑不良") in entries
    assert ("冷却系统", "冷却风扇故障") in entries


def test_extract_kb_causes_excludes_fault_suggestion_lines():
    entries = extract_kb_causes(KB_EVIDENCE)
    texts = [t for _, t in entries]
    assert all("排查建议" not in t for t in texts)


# ========== remove_excluded_causes：程序化裁剪被排除的根因 ==========

def test_remove_excluded_causes_drops_mapped_cause():
    kb_causes = ["主轴轴承损坏或润滑不良", "电机负载过大"]
    root = "原因1：主轴轴承损坏或润滑不良；原因2：电机负载过大"
    result = remove_excluded_causes(root, kb_causes, [0])
    assert "主轴轴承" not in result
    assert "电机负载过大" in result


def test_remove_excluded_causes_returns_unchanged_without_indices():
    root = "原因1：主轴轴承损坏"
    assert remove_excluded_causes(root, ["主轴轴承损坏"], []) == root


def test_remove_excluded_causes_handles_out_of_range_index():
    root = "主轴轴承损坏"
    assert remove_excluded_causes(root, ["主轴轴承损坏"], [99]) == root


# ========== safe_parse_json / validate_and_parse：容错与强校验 ==========

def test_safe_parse_json_plain():
    assert safe_parse_json('{"a": 1}') == {"a": 1}


def test_safe_parse_json_extracts_from_code_fence():
    raw = '```json\n{"报警代码": "E-203"}\n```'
    assert safe_parse_json(raw) == {"报警代码": "E-203"}


def test_safe_parse_json_returns_empty_on_garbage():
    assert safe_parse_json("这不是 JSON") == {}


def test_validate_and_parse_accepts_valid_payload():
    data = {"报警代码": "E-203", "根因判断": "轴承损坏", "依据": "资料1", "排查建议": ["更换轴承"]}
    model = validate_and_parse(DiagnosisOutput, data)
    assert model is not None
    assert model.根因判断 == "轴承损坏"


def test_validate_and_parse_rejects_missing_required_field():
    data = {"报警代码": "E-203", "根因判断": "轴承损坏", "依据": "资料1"}
    assert validate_and_parse(DiagnosisOutput, data) is None


# ========== is_equipment_in_evidence：跨设备幻觉确定性护栏 ==========

def test_equipment_guard_passes_when_device_present():
    fault = {"设备类型": "数控机床主轴电机", "报警代码": "E-203"}
    evidence = "E-203 主轴电机 轴承损坏"
    assert is_equipment_in_evidence(fault, evidence) is True


def test_equipment_guard_blocks_code_mismatch():
    fault = {"设备类型": "数控机床主轴电机", "报警代码": "E-203"}
    evidence = "E-101 电机电源异常"
    assert is_equipment_in_evidence(fault, evidence) is False


def test_equipment_guard_blocks_empty_evidence_marker():
    fault = {"设备类型": "数控机床", "报警代码": None}
    assert is_equipment_in_evidence(fault, "【知识库无相关依据】") is False


@pytest.mark.parametrize("code, evidence", [
    ("E-203", "报警代码 E203 主轴电机"),
    ("E203", "报警代码 E-203 主轴电机"),
    ("e 203", "E-203 主轴电机"),
    ("E_203", "E-203 主轴电机"),
])
def test_equipment_guard_tolerates_alarm_code_formatting(code, evidence):
    """现场写法不统一（E-203 / E203 / e 203 / E_203），不该因此误判成"资料无依据"。

    此前是精确子串比对，这些等价写法都会触发误降级（README 指标⑦）。"""
    assert is_equipment_in_evidence({"设备类型": "主轴电机", "报警代码": code}, evidence) is True


def test_equipment_guard_skips_code_check_for_very_short_codes():
    """过短的代码归一化后几乎能命中任意文本，此时不做代码级判定，交给其他护栏。"""
    assert is_equipment_in_evidence({"设备类型": "主轴电机", "报警代码": "1"}, "主轴电机 1 号位") is True


# ========== _relevant_scope：排除映射的作用域收窄 ==========

def test_relevant_scope_returns_empty_when_device_absent_from_knowledge_base():
    """设备不在本轮资料里时不能放开全量——否则排除映射会跨设备误伤
    （拿 A 设备的排除条件去删 B 设备的原因条目）。"""
    entries = [("主轴电机", "轴承损坏"), ("冷却系统", "风扇故障")]
    assert _relevant_scope(entries, "液压泵站", "") == []


def test_relevant_scope_does_not_narrow_without_device_or_alarm():
    """没有任何设备/报警信息可依据时，不做收窄，全量交给映射器判断。"""
    entries = [("主轴电机", "轴承损坏"), ("冷却系统", "风扇故障")]
    assert _relevant_scope(entries, "", "") == [0, 1]


def test_relevant_scope_matches_by_device_and_alarm():
    entries = [("主轴电机", "轴承损坏"), ("冷却系统", "风扇故障"), ("主轴电机 E-203", "负载过大")]
    assert _relevant_scope(entries, "主轴电机", "") == [0, 2]
    assert _relevant_scope(entries, "", "E-203") == [2]


# ========== 路由函数：状态机的流程正确性 ==========

def test_is_degraded_detects_honest_degradation():
    assert _is_degraded(None) is False
    assert _is_degraded({"根因判断": "知识库无相关依据，无法诊断"}) is True
    assert _is_degraded({"根因判断": "主轴轴承损坏"}) is False


def test_route_after_check_info():
    assert route_after_check_info({"status": "info_sufficient"}) == "retrieve"
    assert route_after_check_info({"status": "need_more_info"}) == "need_more_info"


def test_route_after_diagnose():
    assert route_after_diagnose({"status": "insufficient_knowledge"}) == "cost"
    assert route_after_diagnose({"status": "diagnosed"}) == "review"


def test_route_after_review_degraded_goes_to_human():
    state = {"diagnosis": {"根因判断": "资料不相关，无法诊断"}, "review": {}}
    assert route_after_review(state) == "human_review"


def test_route_after_review_pass_goes_to_cost():
    state = {"diagnosis": {"根因判断": "轴承损坏"}, "review": {"审核意见": "通过"}}
    assert route_after_review(state) == "cost"


def test_route_after_review_fail_retries_debate():
    state = {"diagnosis": {"根因判断": "轴承损坏"}, "review": {"审核意见": "不通过"},
             "debate_round": 0, "max_debate_rounds": 3}
    assert route_after_review(state) == "rebuttal"


def test_route_after_review_fail_at_limit_forced_cost():
    state = {"diagnosis": {"根因判断": "轴承损坏"}, "review": {"审核意见": "不通过"},
             "debate_round": 3, "max_debate_rounds": 3}
    assert route_after_review(state) == "cost"


def test_route_after_final_review_branches():
    assert route_after_final_review({"final_review": {"审核意见": "通过"}}) == "cost"
    assert route_after_final_review(
        {"final_review": {"审核意见": "不通过"}, "debate_round": 1, "max_debate_rounds": 3}
    ) == "rebuttal"
    assert route_after_final_review(
        {"final_review": {"审核意见": "不通过"}, "debate_round": 3, "max_debate_rounds": 3}
    ) == "cost"
