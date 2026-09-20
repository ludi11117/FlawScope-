"""降级报告：答不了的时候也要交出可用的东西。

此前降级工单只有「工单编号 / 风险等级 / 风险说明」三行，风险说明还是
「知识库未覆盖该设备，无法自动诊断，建议人工介入」这种空话——用户拿到手
等于没拿到东西。**降级不等于可以不输出。**

对偶测试（硬约束 31）——两个方向缺一不可：
  - 必须输出实质内容（库状态 + 具体下一步），不能只有一句"建议人工介入"
  - **不得编造根因**：输出里不许出现根因推断词

只测「有内容」会让这条护栏越改越松——**顺手编个根因也算"有内容"**，
那正好是硬约束 4 要挡的。所以第二半比第一半更重要。
"""

import pytest

import orchestrator
from agents import (
    build_degraded_report,
    DEGRADED_REASON_NO_HIT,
    DEGRADED_REASON_EQUIPMENT_MISMATCH,
    DEGRADED_REASON_IRRELEVANT,
    DEGRADED_REASON_RELEVANCE_UNKNOWN,
    DEGRADED_REASON_LLM_FAILED,
)

# 降级报告里不该出现的词：出现就意味着在编造根因（硬约束 4）
_ROOT_CAUSE_MARKERS = ("根因是", "原因是", "可能原因", "由于", "导致", "应该是", "大概率")

ALL_REASONS = (
    DEGRADED_REASON_NO_HIT,
    DEGRADED_REASON_EQUIPMENT_MISMATCH,
    DEGRADED_REASON_IRRELEVANT,
    DEGRADED_REASON_RELEVANCE_UNKNOWN,
    DEGRADED_REASON_LLM_FAILED,
)


def _report(reason, device="数控机床", symptoms=None, kb_size=73):
    return build_degraded_report(
        {"设备类型": device, "故障现象": symptoms or ["异响"]},
        reason,
        kb_size=kb_size,
    )


class TestReportHasContent:
    """每种降级原因都必须给出判定说明 + 具体建议，不能是空话。"""

    @pytest.mark.parametrize("reason", ALL_REASONS)
    def test_verdict_and_tips_not_empty(self, reason):
        rep = _report(reason)
        assert rep["判定说明"].strip()
        assert len(rep["下一步建议"]) >= 1
        assert all(t.strip() for t in rep["下一步建议"])

    @pytest.mark.parametrize("reason", ALL_REASONS)
    def test_more_than_the_old_placeholder(self, reason):
        """必须比旧的「建议人工介入或补充知识库」信息量更大。

        旧文案只有一句话、没说库里有什么、也没说能做什么。若哪天退化回
        单个短句，这里必须红。
        """
        rep = _report(reason)
        assert rep["下一步建议"] != ["建议人工介入或补充知识库"]


class TestReportNeverInventsRootCause:
    """对偶的另一半：有内容 ≠ 可以编。降级报告不得含有根因推断。"""

    @pytest.mark.parametrize("reason", ALL_REASONS)
    def test_no_root_cause_markers(self, reason):
        rep = _report(reason)
        text = rep["判定说明"] + " ".join(rep["下一步建议"])
        for marker in _ROOT_CAUSE_MARKERS:
            assert marker not in text, f"降级报告不得含根因推断词 {marker!r}"

    def test_equipment_mismatch_names_device_not_cause(self):
        """设备不匹配时要说清"库里没有这台设备"，而不是猜它出了什么故障。"""
        rep = _report(DEGRADED_REASON_EQUIPMENT_MISMATCH, device="数控机床")
        assert "数控机床" in rep["判定说明"]


class TestKnowledgeBaseSizeTristate:
    """0 / -1 / N 三态必须分开，与 get_knowledge_base_size 的语义一致。

    把 0 和 -1 混为一谈会让人顺着错误方向排查：库确实是空的 vs 没查出来，
    两种情况的处置完全不同。
    """

    def test_zero_says_empty(self):
        assert "空" in _report(DEGRADED_REASON_NO_HIT, kb_size=0)["判定说明"]

    def test_negative_says_unknown(self):
        assert "未能确认" in _report(DEGRADED_REASON_NO_HIT, kb_size=-1)["判定说明"]

    def test_positive_includes_count(self):
        assert "73" in _report(DEGRADED_REASON_NO_HIT, kb_size=73)["判定说明"]

    def test_zero_and_negative_differ(self):
        """判别式必须能区分两者：只看"有没有内容"是测不出来的。"""
        assert (
            _report(DEGRADED_REASON_NO_HIT, kb_size=0)["判定说明"]
            != _report(DEGRADED_REASON_NO_HIT, kb_size=-1)["判定说明"]
        )


class TestPartQuestionAskedOnlyWhenMissing:
    """只在该问的时候问：用户已经说了部件，就别再问"哪个部件"。"""

    def test_asks_when_no_part_mentioned(self):
        rep = _report(DEGRADED_REASON_IRRELEVANT, symptoms=["一直响"])
        assert any("部件" in t for t in rep["下一步建议"])

    def test_not_asked_when_part_given(self):
        rep = _report(DEGRADED_REASON_IRRELEVANT, symptoms=["主轴异响"])
        assert not any("部件" in t for t in rep["下一步建议"])


class TestWiredIntoOrchestrator:
    """接线测试（硬约束 33）：函数改对了 ≠ 调用点接对了。

    把 build_degraded_report 换成 spy，驱动真实的 diagnose_node，确认降级
    分支确实调用了它、并且把结果放进了 diagnosis。
    """

    def test_no_hit_branch_uses_report(self, monkeypatch):
        seen = []
        real = orchestrator.build_degraded_report

        def spy(fault_info, reason, kb_size=None):
            seen.append(reason)
            return real(fault_info, reason, kb_size=73)

        monkeypatch.setattr(orchestrator, "build_degraded_report", spy)

        state = {
            "correlation_id": "t-degraded",
            "fault_info": {"设备类型": "数控机床", "故障现象": ["异响"]},
            "evidence": "【知识库无相关依据】",
            "user_input": "数控机床异响",
        }
        out = orchestrator.diagnose_node(state)

        assert seen == [DEGRADED_REASON_NO_HIT], "降级分支必须调用 build_degraded_report"
        assert out["status"] == "insufficient_knowledge"
        # 内容必须真的进了 diagnosis，否则下游工单拿不到
        assert out["diagnosis"]["降级说明"].strip()
        assert out["diagnosis"]["排查建议"] != ["建议人工介入或补充知识库"]

    def test_risk_note_composition(self):
        """风险说明必须把原因、判定、下一步三者拼起来，而不是只留一句空话。"""
        rep = _report(DEGRADED_REASON_EQUIPMENT_MISMATCH)
        note = orchestrator._compose_risk_note(
            "知识库未覆盖该故障的相关依据，无法自动诊断",
            rep["判定说明"],
            rep["下一步建议"],
        )
        assert "无法自动诊断" in note
        assert rep["判定说明"] in note
        assert "可尝试" in note
        # 旧文案只有一句、长度有限；拼装后必须显著更长
        assert len(note) > 60
