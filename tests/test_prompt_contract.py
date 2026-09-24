"""Prompt 契约守卫：抽取提示词的两条关键约束。

为什么需要单独一个文件：LLM 的抽取行为**不稳定且不可离线断言**，prompt 被
改回去（或被人"顺手简化"）时不会有任何测试变红，但系统会悄悄退化。实测踩过：

1. 设备类型的示例写成"数控机床主轴电机"，等于**示范了"设备 + 部件"的写法**。
   LLM 照学，把"注塑机主轴异响"抽成设备类型 `"注塑机主轴"`；其中"主轴"
   命中了数控机床资料里的"主轴"，跨设备护栏 `is_equipment_in_evidence` 放行
   ⇒ **拿数控机床的知识去回答注塑机的问题**（跨设备幻觉）。
2. "排除条件"只给了"液压油位正常"这一个示例，LLM 把"控制面板没有报警代码"
   也类推成排除项——那是**信息缺失**，不是排除条件。

这两条约束本身是**离线可测**的（只查模板文本），所以在这里守住。
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EXTRACT_PROMPT = BASE_DIR / "prompts" / "extract_fault_info.j2"


def _prompt_text() -> str:
    return EXTRACT_PROMPT.read_text(encoding="utf-8")


class TestExtractPromptDeviceScope:
    """设备类型必须是**设备本体**，不能并进部件 / 部位。"""

    def test_requires_device_body_only(self):
        text = _prompt_text()
        assert "设备本体" in text, "抽取提示词必须要求设备类型只填设备本体"

    def test_warns_against_merging_parts(self):
        """必须明说"不要把部件并进来"，并解释后果。"""
        text = _prompt_text()
        assert "部件" in text and "不要" in text, (
            "必须明确禁止把部件并入设备类型——否则会出现跨设备幻觉"
        )

    def test_old_bad_example_is_gone(self):
        """旧版拿"数控机床主轴电机"当设备类型示例，正是这次事故的源头。

        判别式取"旧示例不再出现"而不是"包含某个新词"：新词可能以任何措辞
        出现，但**错误的示例只要还在，退化就随时可能发生**。
        """
        text = _prompt_text()
        assert "如数控机床主轴电机" not in text, (
            "设备类型示例不应再含部件——旧写法会让 LLM 把部件并进设备类型，"
            "进而让跨设备护栏被部件名蒙混过关"
        )


class TestExtractPromptExclusionScope:
    """排除条件只收"明确说正常 / 已修好"，不收"没有报警代码"这类信息缺失。"""

    def test_declares_missing_info_is_not_exclusion(self):
        text = _prompt_text()
        assert "信息缺失" in text, "必须写明「没有报警代码」是信息缺失而非排除条件"

    def test_gives_positive_example(self):
        """要给出"什么才算排除条件"的正例，否则模型只能靠猜。"""
        text = _prompt_text()
        assert "正常" in text or "已修好" in text or "刚换过" in text
