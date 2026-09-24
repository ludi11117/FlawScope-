"""审核师的分歧检查：把知识库里的「经验分歧」接到辩论触发上。

背景（实测）：辩论触发率只有 30–33%，唯一触发条件是审核师判"不通过"
（`orchestrator.route_after_review`）。而知识库里刻意保留了 36 处"多位师傅对
同一故障判断不一致"，**审核师却看不到**——它只审诊断结论的自洽性。于是
"诊断只取了一派、把有争议的判断说成定论"永远不会被判不通过，README 里
"冲突材料是多 Agent 辩论的存在理由"这句话在代码里是断的。

这个文件守两件事：

1. 分歧能被**确定性**地解析出来（纯字符串匹配，零 LLM 调用，不受判官抖动影响）；
2. 审核提示词里那条第 5 项检查不会被"顺手删掉"——删了这里就红。
"""

import pytest

from agents import extract_kb_disagreements, _format_disagreements

EVIDENCE = """【资料1】
一、主轴电机报警代码E-203
故障现象：主轴转速不稳定，伴有异常噪音。
可能原因：
1. 主轴电机负载过大或负载不平衡。
2. 主轴轴承损坏或润滑不良。
排查建议：
- 检查加工程序，确认是否存在过重切削。
- 检查主轴轴承磨损情况。
经验分歧：
- 张师傅（12年机床维修）认为E-203八成是负载问题，先查程序最省事。
- 李师傅（18年经验）坚持先拆轴承。
- 两人一致的地方：冷却系统必须一起看。

【资料2】
二、主轴运转异响（无报警代码）
故障现象：主轴旋转时持续发出异响。
经验分歧：
- 张师傅认为八成是润滑问题。
- 李师傅主张先查刀柄。
"""


class TestExtractKbDisagreements:
    """分歧解析：认得出、分得清、不越界。"""

    def test_extracts_all_disagreement_sections(self):
        got = extract_kb_disagreements(EVIDENCE)
        assert len(got) == 2, f"应抽出 2 段分歧，实际 {len(got)}"

    def test_attributes_each_section_to_its_entry(self):
        """分歧要能归属到具体条目，否则审核师不知道争议是关于哪条知识的。"""
        got = extract_kb_disagreements(EVIDENCE)
        assert got[0]["条目"] == "主轴电机报警代码E-203"
        assert got[1]["条目"] == "主轴运转异响（无报警代码）"

    def test_collects_every_bullet_in_section(self):
        got = extract_kb_disagreements(EVIDENCE)
        assert len(got[0]["分歧"]) == 3, "同一条目的分歧条目一条都不能漏"
        assert len(got[1]["分歧"]) == 2

    def test_does_not_leak_advice_into_disagreements(self):
        """「排查建议」下的动作不能混进分歧——那会让审核师拿着处置措施当争议点。"""
        got = extract_kb_disagreements(EVIDENCE)
        joined = " ".join(x for d in got for x in d["分歧"])
        assert "检查加工程序" not in joined
        assert "检查主轴轴承磨损情况" not in joined

    def test_stops_at_next_known_section(self):
        """分歧段后面跟了别的小节时，不能继续往下收。"""
        text = (
            "一、某故障\n"
            "经验分歧：\n"
            "- 甲师傅说 A。\n"
            "可能原因：\n"
            "1. 这是原因不是分歧。\n"
        )
        got = extract_kb_disagreements(text)
        assert len(got) == 1
        assert got[0]["分歧"] == ["甲师傅说 A。"]

    def test_heading_form_is_accepted(self):
        """`## 经验分歧` 与 `经验分歧：` 是同一个意思，两种写法都要认。"""
        text = "## 某设备\n## 经验分歧\n- 甲师傅说 A。\n- 乙师傅说 B。\n"
        got = extract_kb_disagreements(text)
        assert len(got) == 1 and len(got[0]["分歧"]) == 2

    @pytest.mark.parametrize("text", ["", None, "一、故障\n可能原因：\n1. 甲。\n"])
    def test_no_disagreement_is_normal(self, text):
        """没命中带分歧的条目是**正常状态**，不是故障——调用方不该据此告警。"""
        assert extract_kb_disagreements(text) == []

    def test_is_pure_and_repeatable(self):
        """同一输入必须给同一输出：这条链路上不允许有 LLM 抖动。"""
        assert extract_kb_disagreements(EVIDENCE) == extract_kb_disagreements(EVIDENCE)


class TestFormatDisagreements:
    """渲染给审核师的文本。"""

    def test_empty_says_explicitly_none(self):
        """空必须渲染成明确的"未发现"，不能留空——留空与"模板没渲染上"长得一样，
        审核师会分不清"这次真没有"和"系统没查"。"""
        out = _format_disagreements([])
        assert "未发现" in out

    def test_renders_entry_and_bullets(self):
        out = _format_disagreements(
            [{"条目": "主轴电机报警代码E-203", "分歧": ["甲师傅说 A。", "乙师傅说 B。"]}]
        )
        assert "主轴电机报警代码E-203" in out
        assert "甲师傅说 A。" in out and "乙师傅说 B。" in out

    def test_missing_entry_name_does_not_crash(self):
        """条目名缺失时退化成占位，而不是抛 KeyError 把审核整条带崩。"""
        out = _format_disagreements([{"分歧": ["甲师傅说 A。"]}])
        assert "甲师傅说 A。" in out


class TestReviewPromptDisagreementClause:
    """契约守卫：提示词里那条第 5 项检查不能被删。

    判别式取"关键约束仍在"，而不是"包含某个新词"——措辞可以改，
    但**只要这一项没了，分歧就又会退回审核师的盲区**。
    """

    def _text(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parent.parent / "prompts" / "agent_review.j2").read_text(
            encoding="utf-8"
        )

    def test_prompt_has_disagreement_input(self):
        assert "知识库中的经验分歧" in self._text(), "审核提示词必须带上分歧输入"

    def test_prompt_has_disagreement_check(self):
        text = self._text()
        assert "分歧检查" in text, "审核要求里必须保留「分歧检查」这一项"

    def test_prompt_requires_flagging_one_sided_claims(self):
        """核心判据：只取一派且不说明另一派 = 不通过。"""
        text = self._text()
        assert "只取" in text or "只取了" in text
        assert "定论" in text, "必须写明「把有争议的判断说成定论」才算问题"

    def test_prompt_forbids_inventing_disagreement(self):
        """反向约束同样重要：没分歧时不许臆造，否则会为了显得严格而滥判不通过。"""
        text = self._text()
        assert "臆造" in text or "不要因为" in text

    def test_boundary_section_counts_five_checks(self):
        """边界段说"五项"——漏改这里会让审核师以为只有四项检查，第 5 项形同虚设。"""
        assert "五项检查" in self._text()
