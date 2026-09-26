"""评估系统自身核心逻辑的测试（D3）。

## 为什么需要这个文件

`eval_test.py` 的卖点是"判官独立性"——判官必须用**另一个**模型，否则
"LLM 判 LLM" 会退化成自证。但在这之前，`judge_root_cause` / `_ensure_judge` /
`_parse_json_quiet` / `debate_summary` / `compare_with_snapshot` / `_missing_nodes`
在测试里**一次都没出现过**（唯一碰到 `evaluate_case` 的用例固定 `skip_judge=True`）。
也就是说：**判官独立性这个卖点改错了，测试照样全绿。**

这里把判官的模型选择、输出解析、辩论增益名单、快照比对都钉住。
全部离线：`_invoke_llm` 打桩，不构造真实 ChatOpenAI、不联网。
"""

import re
from pathlib import Path

import pytest

import eval_test


# ========== 判官：模型必须来自 judge_model，不能与诊断同源 ==========

@pytest.fixture
def judge_env(monkeypatch):
    """桩掉判官调用链，记录 `_ensure_judge` 收到的模型名。"""
    captured = {}

    class _FakeLLM:
        model_name = "fake-judge"

    def _fake_ensure_judge(model):
        captured["model"] = model
        return _FakeLLM()

    monkeypatch.setattr(eval_test, "_ensure_judge", _fake_ensure_judge)
    monkeypatch.setattr(
        eval_test, "_invoke_llm",
        lambda llm, messages, max_retries=1: '{"核心一致": true, "覆盖全部": false, "理由": "覆盖了两条中的一条"}',
    )
    return captured


def test_judge_uses_the_judge_model_not_the_diagnosis_model(judge_env):
    """核心断言：判官拿到的模型名必须是传入的 `judge_model`。

    把 `judge_root_cause(..., args.diagnosis_model, ...)` 写回去（判官与诊断同源），
    这条会立刻失败——而那正是"判官独立性"被悄悄破坏的形态。
    """
    eval_test.judge_root_cause("原因1：轴承损坏", ["轴承损坏"], "judge-model-72B")

    assert judge_env["model"] == "judge-model-72B"
    assert judge_env["model"] != "diagnosis-model-14B"


def test_judge_returns_the_documented_structure(judge_env):
    out = eval_test.judge_root_cause("原因1：轴承损坏", ["轴承损坏"], "m")

    assert set(out) == {"core", "coverage", "reason"}
    assert out["core"] is True
    assert out["coverage"] is False
    assert isinstance(out["reason"], str)


def test_call_site_passes_judge_model_not_diagnosis_model():
    """接线断言：`evaluate_case` 里传给判官的必须是 `args.judge_model`。

    只测 `judge_root_cause` 本身是不够的——把**调用点**改成 `args.diagnosis_model`
    （判官与诊断同源）时，上面的单元测试照样全绿，而那正是"判官独立性"被破坏的形态。
    这里直接查源码里的调用点，与 `test_prompt_contract.py` 的手法一致。
    """
    src = (Path(__file__).resolve().parent.parent / "eval_test.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    # 负向后顾排掉函数**定义**那一行，只留调用点
    calls = re.findall(r"(?<!def )judge_root_cause\([^)]*\)", code)
    assert calls, "没找到 judge_root_cause 的调用点，这条测试失去意义"
    for call in calls:
        assert "args.judge_model" in call, f"判官没有用 judge_model：{call.strip()}"
        assert "args.diagnosis_model" not in call, f"判官与诊断同源了：{call.strip()}"


def test_judge_maps_non_true_to_false(monkeypatch):
    """只有**明确 true** 才算通过。

    判别式取"非 true 一律 false"：若实现写成 `data.get("核心一致") or False`
    或 `!= False`，字符串 "false"/缺失都会被当成通过，指标立刻注水。
    """
    monkeypatch.setattr(eval_test, "_ensure_judge", lambda model: object())
    monkeypatch.setattr(
        eval_test, "_invoke_llm",
        lambda llm, messages, max_retries=1: '{"核心一致": "true", "覆盖全部": null}',
    )

    out = eval_test.judge_root_cause("x", ["y"], "m")

    assert out["core"] is False, "字符串 'true' 不该被当成通过"
    assert out["coverage"] is False


def test_judge_returns_none_when_skipped_or_input_missing():
    assert eval_test.judge_root_cause("x", ["y"], "m", skip=True) is None
    assert eval_test.judge_root_cause("", ["y"], "m") is None
    assert eval_test.judge_root_cause("x", [], "m") is None


def test_judge_returns_none_when_llm_fails(monkeypatch):
    monkeypatch.setattr(eval_test, "_ensure_judge", lambda model: object())
    monkeypatch.setattr(eval_test, "_invoke_llm", lambda *a, **kw: None)
    assert eval_test.judge_root_cause("x", ["y"], "m") is None


def test_judge_returns_none_when_output_is_unparseable(monkeypatch):
    monkeypatch.setattr(eval_test, "_ensure_judge", lambda model: object())
    monkeypatch.setattr(eval_test, "_invoke_llm", lambda *a, **kw: "我看不出对错")
    assert eval_test.judge_root_cause("x", ["y"], "m") is None


def test_ensure_judge_switches_model_when_asked(monkeypatch):
    """`_ensure_judge` 换模型时必须重建客户端，不能复用旧的那一个。"""
    created = []

    class _FakeChat:
        def __init__(self, **kwargs):
            self.model_name = kwargs["model"]
            created.append(kwargs["model"])

    monkeypatch.setattr(eval_test, "ChatOpenAI", _FakeChat)
    monkeypatch.setattr(eval_test, "_judge_llm", None)

    first = eval_test._ensure_judge("model-A")
    assert first.model_name == "model-A"

    same = eval_test._ensure_judge("model-A")
    assert same is first, "同一个模型不该重建客户端"
    assert created == ["model-A"]

    switched = eval_test._ensure_judge("model-B")
    assert switched.model_name == "model-B"
    assert created == ["model-A", "model-B"]

    monkeypatch.setattr(eval_test, "_judge_llm", None)


# ========== _parse_json_quiet：围栏与截断容错 ==========

@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    # 被 Markdown 代码块包裹（判官最常见的输出形态）
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('```\n{"a": 1}\n```', {"a": 1}),
    # 前后带解释文字
    ('好的，我的判断是：{"a": 1}。以上。', {"a": 1}),
    # 前缀有花括号的干扰文本，取第一个 { 到最后一个 }
    ('{干扰} 真正的答案 {"a": 1}', None),
])
def test_parse_json_quiet_tolerates_wrapping(raw, expected):
    out = eval_test._parse_json_quiet(raw)
    if expected is None:
        # 这一条只要求"不抛异常"，具体解析结果取决于实现口径
        assert isinstance(out, dict)
    else:
        assert out == expected


def test_parse_json_quiet_returns_empty_on_garbage():
    assert eval_test._parse_json_quiet("完全不是 JSON") == {}
    assert eval_test._parse_json_quiet("") == {}
    assert eval_test._parse_json_quiet(None) == {}


def test_parse_json_quiet_handles_truncated_json():
    """截断的 JSON 不该抛异常——判官输出被 max_tokens 砍掉是常态。"""
    out = eval_test._parse_json_quiet('{"核心一致": true, "理由": "被截断的')
    assert isinstance(out, dict)


def test_parse_json_quiet_handles_nested_braces():
    """嵌套花括号：必须取最外层，而不是第一个 `}` 就收尾。"""
    out = eval_test._parse_json_quiet('前缀 {"a": {"b": 2}} 后缀')
    assert out == {"a": {"b": 2}}


# ========== debate_summary：修正 / 恶化 / 持平 的名单 ==========

def _record(case_id: str, rounds: int, initial_core, final_core):
    return {
        "id": case_id,
        "debate_rounds": rounds,
        "debate_initial_judgment": None if initial_core is None else {"core": initial_core},
        "judgment": None if final_core is None else {"core": final_core},
    }


def test_debate_summary_lists_corrected_and_worsened():
    """判别式取**具体名单**而不是计数：计数对了但名单错（把 id 弄反）同样有害。"""
    records = [
        _record("TC001", 0, None, True),        # 没进辩论
        _record("TC002", 2, False, True),       # 修正
        _record("TC003", 1, True, False),       # 恶化
        _record("TC004", 3, True, True),        # 保持正确
        _record("TC005", 1, False, False),      # 保持错误
    ]

    summary = eval_test.debate_summary(records, metrics={})

    assert summary["triggered"] == 4
    assert summary["corrected"] == ["TC002"]
    assert summary["worsened"] == ["TC003"]
    assert summary["kept_ok"] == 1
    assert summary["kept_bad"] == 1
    assert summary["initial_core"] == {"pass": 2, "total": 4}
    assert summary["final_core"] == {"pass": 2, "total": 4}


def test_debate_summary_ignores_records_without_judgment():
    """判官没给出结论的记录不能进 compared，否则增益统计会把"没测到"当成"测好了"。"""
    records = [
        _record("TC010", 2, False, None),   # 终审没判
        _record("TC011", 2, None, True),    # 初诊没判
    ]

    summary = eval_test.debate_summary(records, metrics={})

    assert summary["triggered"] == 2
    assert summary["compared"] == 0
    assert summary["corrected"] == []
    assert summary["worsened"] == []


def test_debate_summary_with_no_debate_is_all_zero():
    summary = eval_test.debate_summary([_record("TC001", 0, None, True)], metrics={})
    assert summary["triggered"] == 0
    assert summary["avg_rounds"] == 0


# ========== compare_with_snapshot / _missing_nodes ==========

def test_compare_with_snapshot_reports_no_snapshot():
    out = eval_test.compare_with_snapshot("TC999", {}, {})
    assert out["status"] == "no_snapshot"


def test_compare_with_snapshot_detects_field_level_changes():
    snapshots = {"TC001": {"status": "done", "diagnosis": {"根因判断": "旧"}, "debate_round": 0}}
    result = {"status": "done", "diagnosis": {"根因判断": "新"}, "debate_round": 0}

    out = eval_test.compare_with_snapshot("TC001", result, snapshots)

    assert out["status"] == "changed"
    assert [d["field"] for d in out["diffs"]] == ["diagnosis"]
    assert out["diffs"][0]["snapshot"] == {"根因判断": "旧"}
    assert out["diffs"][0]["current"] == {"根因判断": "新"}


def test_compare_with_snapshot_reports_unchanged():
    snapshots = {"TC001": {"status": "done"}}
    out = eval_test.compare_with_snapshot("TC001", {"status": "done"}, snapshots)
    assert out["status"] == "unchanged"
    assert out["diffs"] == []


def test_missing_nodes_skips_followup_and_degraded_cases():
    """追问轮次与诚实降级不该被算成"节点缺失"。"""
    assert eval_test._missing_nodes({"status": "need_more_info"}) == []
    assert eval_test._missing_nodes({"status": "insufficient_knowledge"}) == []
    assert eval_test._missing_nodes({"status": "pending_human_review"}) == []

    degraded = {"status": "done", "diagnosis": {"根因判断": "知识库无相关依据，无法诊断"}, "cost": {}, "workorder": {}}
    assert eval_test._missing_nodes(degraded) == ["cost", "workorder"]


def test_missing_nodes_lists_empty_nodes_on_the_normal_path():
    result = {"status": "done", "diagnosis": {"根因判断": "原因1：轴承损坏"}, "review": {}, "cost": {}, "workorder": {}}
    assert eval_test._missing_nodes(result) == ["review", "cost", "workorder"]


# ========== 快照回归率 0 的显示（rate == 0 被当成"没数据"） ==========

def test_format_rate_distinguishes_zero_from_missing():
    """`0.0` 是**最理想**的结果（一条都没变），不能显示成 N/A。

    旧写法 `format_rate(x) if x else 'N/A'` 把 `0.0` 判为假值，
    于是"全部未变更"被显示成"没有数据"。
    """
    assert eval_test.format_rate(0.0) == "0.0%"
    assert eval_test.format_rate(0) == "0.0%"
    assert eval_test.format_rate(None) == "N/A"


def test_snapshot_regression_line_shows_100_percent_when_nothing_changed():
    """端到端：`rate == 0.0` 时报告里必须出现 100.0%，而不是 N/A。"""
    def _stat(**kw):
        base = {"pass": 0, "total": 0, "rate": None}
        base.update(kw)
        return base

    metrics = {
        "total": 0,
        "core_accuracy": _stat(),
        "coverage_rate": _stat(),
        "coverage_rate_strict": _stat(),
        "hallucination_rate": {"bad": 0, "total": 0, "rate": None},
        "exclusion_compliance": {"violated": 0, "total": 0, "rate": None},
        "completeness": {"ok": 0, "total": 0, "rate": None},
        "honest_degradation": _stat(),
        "false_degradation": {"bad": 0, "total": 0, "rate": None},
        "snapshot_regression": {"total": 12, "changed": 0, "rate": 0.0},
    }
    meta = {"time": "2026-09-25", "cases": "test_cases.json", "judge_model": "m", "skip_judge": True}

    markdown = eval_test.build_markdown(
        meta, metrics, debate=eval_test.debate_summary([], {}), records=[]
    )

    line = next(ln for ln in markdown.splitlines() if "快照回归通过率" in ln)
    assert "100.0%" in line, f"rate=0 被显示成没数据：{line}"
    assert "N/A" not in line
