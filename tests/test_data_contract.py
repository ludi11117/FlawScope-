"""数据接入契约的守卫测试：格式放宽 + 格式自查。

为什么需要这一组：
    知识库是可替换的数据容器。别人换完数据后，若 `extract_kb_causes()` 解析不出
    条目，**排除条件映射会静默失效**——系统照样出诊断，只是不再遵守用户说的
    "已排除某某"，且不报错。这组测试守住两件事：

      ① 解析器认得真实资料里常见的多种写法（Markdown / 顿号 / 制表符 / 省略小节名）
      ② 解析器**不**把『排查建议』下的处置措施误当成原因

    ② 与 ① 同样重要：只测"该解析出来"会让解析器越改越松，最后把什么都当原因，
    排除映射就会拿处置措施去匹配用户的排除条件（张冠李戴），比漏解析更坏。

对照口径：现有 `data/knowledge_base.txt` 必须**逐条稳定**解析出 104 条——
放宽格式不得改变既有库的解析结果（否则评估基线会无声漂移）。
"""

import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from agents import extract_kb_causes, knowledge_base_health  # noqa: E402


# ---------- ① 该解析出来的：多种真实写法 ----------

@pytest.mark.parametrize("name,text", [
    ("标准中文序号+半角点", "一、主轴电机\n可能原因：\n1. 负载过大或负载不平衡\n2. 轴承损坏\n"),
    ("Markdown二级标题+无序列表", "## 主轴电机\n\n**可能原因**：\n- 负载过大或负载不平衡\n- 轴承损坏\n"),
    ("三级标题+星号列表", "### 主轴电机\n可能原因：\n* 负载过大或负载不平衡\n* 轴承损坏\n"),
    ("中文顿号", "一、主轴电机\n可能原因：\n1、负载过大或负载不平衡\n2、轴承损坏\n"),
    ("Excel 制表符", "设备类型\t数控机床\n可能原因\t负载过大或负载不平衡\n可能原因\t轴承损坏\n"),
    ("省略小节名（设备标题下直接列）", "一、主轴电机\n1. 负载过大或负载不平衡\n2. 轴承损坏\n"),
])
def test_common_real_world_formats_are_parsed(name, text):
    """真实资料里常见的写法都要能解析出原因。"""
    entries = extract_kb_causes(text)
    texts = [c for _, c in entries]
    assert len(entries) >= 2, f"{name}：只解析出 {len(entries)} 条"
    assert any("负载过大" in t for t in texts), f"{name}：漏了第一条原因"
    assert any("轴承损坏" in t for t in texts), f"{name}：漏了第二条原因"


def test_markdown_decoration_is_stripped():
    """`**原因**` 与 `原因` 是同一条，装饰不该进候选集。"""
    entries = extract_kb_causes("## 主轴电机\n可能原因：\n- **主轴轴承损坏**\n")
    texts = [c for _, c in entries]
    assert texts == ["主轴轴承损坏"], f"装饰没抹干净：{texts}"


def test_tab_separated_entry_parses_without_section_label():
    """制表符分隔 + **带小节名**的组合必须解析出来。

    单独测这一条的原因：`_CODE_ENTRY`（可能原因\\t原因）与 `_KEYED_LINE`（通用键值行）
    都能匹配制表符形式，前者先判。此前只写"制表符"参数化用例时，
    把 `_CODE_ENTRY` 的制表符分支去掉测试**照样全绿**（被 `_KEYED_LINE` 兜住），
    等于这条分支没被测到。这里用"章节名 + 制表符条目"的组合把两条路径区分开：
    只有 `_CODE_ENTRY` 命中时，'可能原因' 才不会被当成新的章节名。
    """
    text = "## 主轴电机\n可能原因\t轴承损坏\n"
    entries = extract_kb_causes(text)
    assert entries == [("主轴电机", "轴承损坏")], f"制表符条目解析异常：{entries}"


def test_section_name_is_captured():
    """章节名要被记下来，用于按设备收窄。"""
    entries = extract_kb_causes("## 主轴电机 E-203\n可能原因：\n- 轴承损坏\n")
    assert entries and entries[0][0] == "主轴电机 E-203", f"章节名丢了：{entries}"


# ---------- ② 不该解析出来的：处置措施不是原因 ----------

def test_suggestion_section_is_not_treated_as_causes():
    """『排查建议』下的符号列表**不得**被当成原因条目。

    这是对偶测试的另一半：解析放宽时最容易把这一段也收进来，
    而处置措施混进候选集会让排除映射张冠李戴。
    """
    text = (
        "一、主轴电机\n"
        "可能原因：\n"
        "1. 负载过大或负载不平衡\n"
        "排查建议：\n"
        "- 检查加工程序\n"
        "- 更换轴承\n"
    )
    texts = [c for _, c in extract_kb_causes(text)]
    assert "负载过大或负载不平衡" in texts
    assert "检查加工程序" not in texts, f"处置措施被当成原因了：{texts}"
    assert "更换轴承" not in texts, f"处置措施被当成原因了：{texts}"


def test_fault_phenomenon_is_not_treated_as_cause():
    """『故障现象』是症状描述，不是原因。"""
    text = "一、主轴电机\n故障现象：\n- 转速不稳定\n可能原因：\n1. 负载过大\n"
    texts = [c for _, c in extract_kb_causes(text)]
    assert "转速不稳定" not in texts, f"现象被当成原因了：{texts}"
    assert "负载过大" in texts


def test_plain_paragraph_without_marker_is_not_parsed():
    """没有可识别条目边界的纯段落 → 解析为空（这是"不合格"该有的样子）。"""
    text = "## 主轴电机\n\n**可能原因**\n\n我忘了加列表符号\n"
    assert extract_kb_causes(text) == []


# ---------- ③ 回归：既有知识库逐条稳定 ----------

def test_existing_knowledge_base_parses_stably():
    """放宽格式后，现有知识库的解析结果必须与基线一致（112 条 / 36 章节）。

    这条是防"放宽解析"无意改变既有库的候选集——那会让评估基线无声漂移。

    基准变更记录：2026-09-20 给数控机床、空气压缩机各补了一个「无报警代码」的
    症状型章节（104→112 条、34→36 章节）。**改数字前先确认是"有意扩库"而不是
    "解析规则变了"**——后者才是这条测试要抓的回归。
    """
    kb = (BASE_DIR / "data" / "knowledge_base.txt").read_text(encoding="utf-8")
    entries = extract_kb_causes(kb)
    assert len(entries) == 112, f"既有知识库解析条数变了：{len(entries)}（基线 112）"
    sections = {s for s, _ in entries if s}
    assert len(sections) == 36, f"章节数变了：{len(sections)}（基线 36）"


def test_duplicate_causes_are_deduped():
    """同一份证据里重复出现的原因只算一条（否则候选集会虚高）。"""
    text = "一、主轴电机\n可能原因：\n1. 轴承损坏\n2. 轴承损坏\n"
    assert len(extract_kb_causes(text)) == 1


# ---------- ④ 健康检查：把"静默失效"变成显式信号 ----------

def test_health_reports_failure_on_unparseable_text():
    """解析不出条目时必须报 ok=False 且给出原因文案。"""
    health = knowledge_base_health("## 主轴电机\n\n**可能原因**\n\n纯段落没有条目边界\n")
    assert health["ok"] is False
    assert health["entries"] == 0
    assert "静默失效" in health["reason"]


def test_health_passes_on_good_text():
    health = knowledge_base_health("一、主轴电机\n可能原因：\n1. 轴承损坏\n")
    assert health["ok"] is True
    assert health["entries"] == 1
    assert health["reason"] == ""
