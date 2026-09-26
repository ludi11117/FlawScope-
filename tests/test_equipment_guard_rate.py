"""设备护栏的误放行率（C1）——把离线探针固化成测试。

## 守的是什么

`is_equipment_in_evidence` 是防跨设备幻觉的**确定性**护栏：用户问注塑机、
检索却命中了数控机床的资料时，它必须在调 LLM 之前拦下。

旧实现用 `any(t in evidence for t in _equipment_tokens(device))` 做**单 token OR**，
而 `_equipment_tokens` 还会把长词降级成 `t[:2]`（「数控机床主轴电机」→「数控」「机床」）。
「机床」「系统」「电机」这类词几乎在任何设备资料里都能命中，护栏于是形同虚设。

判别式取"**跨设备证据的放行率**"而不是"某几个样例的返回值"：
后者可以靠特判凑绿，而放行率必须整体收紧才降得下来。

## 探针构造（可复现，全部离线）

- 语料：`data/knowledge_base.txt`（不碰向量库、不联网）
- 按 `【设备章节】` → `一、条目` 两级切分
- 8 类设备 × **全部跨设备条目**（同设备的条目不计入，否则测的是同义放行）
- 每组构造 `{"设备类型": D}`（以及可选的 `{"报警代码": D 自己的代码}`），
  证据取跨设备条目的全文，调 `is_equipment_in_evidence`
- 「带报警代码」那组用的是**本设备自己的**报警代码：若取被检索条目的代码，
  等于把答案塞进问题里，那一组会反而比不带代码更容易放行（写第一版探针时踩过）

实测（2026-09-25，327 组）：
| 分组 | 旧实现 | 新实现 |
|---|---:|---:|
| 无报警代码 | 21/327 = 6.4% | 7/327 = 2.1% |
| 带报警代码 | 0/327 = 0.0% | 0/327 = 0.0% |

旧实现那 21 次误放行里，**14 次只靠「系统」蒙混过关**（「液压系统」的 token 里有
「系统」，而任何资料都可能写着"冷却系统"）。修复方式是给设备名 token 加一张
**泛词黑名单**（`agents._GENERIC_DEVICE_TOKENS`），而不是把规则整体收紧。

> 为什么不用"必须两个 token 同时命中"这种更严的规则：数控机床章节的标题写的是
> 「一、主轴电机报警代码E-203」，正文也不提「数控」——过严的规则会把**同设备**的
> 条目判成"无依据"。实测：更严的规则会把 33 条同设备条目里的 12 条拦下
> （旧实现是 10 条），等于把旗舰用例的误降级率推高。泛词黑名单做到 2.1% 的同时
> **同设备误伤数与旧实现完全一致（10/33）**。

> ⚠️ 交办书报告的是 7.7% / 1.2%。我复现不出那组数字（差异应在设备清单与
> 条目切分上）。这里以**本探针自己的构造与数字**为准，并把构造写清楚以便复核。
> 另外：在我这套构造下，"带报警代码"那组**改前就已经是 0.0%** ——
> 真正在拦的是报警代码的归一化子串检查，不是设备名。所以本次收紧的收益
> 全部体现在「无报警代码」那一组。

> ⚠️ 残余的 2.1% 是**真·跨设备词面重合**（例如某台设备的"经验分歧"里提到"变频器"），
> 单靠词面匹配消不掉，要等检索层带上设备 metadata（C2）之后才能按来源过滤。

全部离线：只读 `data/knowledge_base.txt` 与纯函数。
"""

import re
from pathlib import Path

import pytest

from agents import is_equipment_in_evidence

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "knowledge_base.txt"

# 8 类设备：设备类型（用于构造 fault_info）+ 在章节标题里的识别 key（用于判断同/跨设备）
PROBE_DEVICES = [
    ("数控机床", "数控机床"),
    ("液压系统", "液压系统"),
    ("空气压缩机", "空气压缩机"),
    ("工业冷水机组", "冷水机组"),
    ("变频器", "变频器"),
    ("工业机器人", "机器人"),
    ("输送带系统", "输送带"),
    ("离心泵", "离心泵"),
]

# 阈值取 3%：新实现实测 2.1%，旧实现 6.4%。
# 定在两者之间，既能挡住退化，也不会因为知识库内容微调就误报。
MAX_FALSE_PASS_RATE = 0.03


def _load_sections() -> list:
    """把知识库切成 [(设备章节标题, [(条目标题, 条目全文), ...]), ...]。"""
    text = KB_PATH.read_text(encoding="utf-8")
    parts = re.split(r"^【(.+?)】\s*$", text, flags=re.M)
    sections = []
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        body = parts[i + 1]
        chapters = []
        for chunk in re.split(r"^(?=[一二三四五六七八九十]+、)", body, flags=re.M):
            chunk = chunk.strip()
            if chunk:
                chapters.append((chunk.splitlines()[0], chunk))
        sections.append((title, chapters))
    return sections


def _own_alarm_code(key: str, sections: list) -> str | None:
    """取该设备自己章节里出现的第一个报警代码。"""
    for title, chapters in sections:
        if key in title:
            for chapter_title, _ in chapters:
                m = re.search(r"([A-Za-z]-\d{3})", chapter_title)
                if m:
                    return m.group(1)
    return None


def _measure(with_alarm_code: bool) -> tuple:
    sections = _load_sections()
    total = passed = 0
    for device, key in PROBE_DEVICES:
        code = _own_alarm_code(key, sections) if with_alarm_code else None
        for section_title, chapters in sections:
            if key in section_title:
                continue  # 同设备：那测的是"同义放行"，不是误放行
            for _, body in chapters:
                fault = {"设备类型": device}
                if code:
                    fault["报警代码"] = code
                total += 1
                if is_equipment_in_evidence(fault, body):
                    passed += 1
    return passed, total


def test_probe_actually_covers_the_knowledge_base():
    """探针自身必须先可信：组数太少说明切分坏了，后面的比率就没有意义。"""
    _, total = _measure(with_alarm_code=False)
    assert total >= 300, f"探针只覆盖了 {total} 组，切分可能已经失效"


def test_cross_device_evidence_rarely_passes_without_alarm_code():
    """核心断言：无报警代码时，跨设备证据的放行率必须 ≤3%。

    去掉修复（还原单 token OR + `t[:2]` 降级）后这里是 6.4%，会直接失败。
    """
    passed, total = _measure(with_alarm_code=False)
    rate = passed / total
    assert rate <= MAX_FALSE_PASS_RATE, (
        f"跨设备误放行 {passed}/{total} = {rate:.1%}，超过阈值 {MAX_FALSE_PASS_RATE:.0%}"
    )


def test_cross_device_evidence_rarely_passes_with_alarm_code():
    """带报警代码时同样收紧（这一组主要由报警代码归一化检查在拦）。"""
    passed, total = _measure(with_alarm_code=True)
    rate = passed / total
    assert rate <= MAX_FALSE_PASS_RATE, f"跨设备误放行 {passed}/{total} = {rate:.1%}"


# ---------- 对偶：收紧不能把本该放行的同义案例一起拦掉 ----------
#
# 这一组与上面的比率断言是**配对**的：只测比率会让实现退化成"恒返回 False"
# （放行率 0% 当然达标），而恒 False 会把所有能答的案例都降级掉。

_SYNONYM_CASES = [
    ({"设备类型": "空气压缩机"}, "【资料1】\n一、空压机报警代码A-203\n故障现象：排气温度过高。",
     "用户说空气压缩机、知识库写空压机"),
    # 证据里带上章节头：真实检索回来的块里至少有一块是【设备章节】那一行
    # （`build_knowledge_base.py` 的切分产物，见 --dry-run 的第 1 块）。
    # 数控机床章节的**条目**标题写的是"主轴电机"，只有章节头才含"数控机床"——
    # 这正是"过严的规则会误伤同设备条目"的来源。
    ({"设备类型": "数控机床"},
     "【资料1】\n【数控机床主轴电机常见故障与排查】\n一、主轴电机报警代码E-203\n故障现象：主轴转速不稳定。",
     "数控机床 vs 数控机床章节头"),
    ({"设备类型": "工业冷水机组"}, "【资料1】\n一、冷机组报警代码C-301\n故障现象：冷却水温度异常升高。",
     "工业冷水机组 vs 冷机组章节"),
    ({"设备类型": "输送带系统"}, "【资料1】\n一、输送带跑偏\n故障现象：输送带向一侧偏移。",
     "完整设备名命中"),
    ({"设备类型": "螺杆式空气压缩机"}, "【资料1】\n一、空压机报警代码A-203\n故障现象：排气温度过高。",
     "带型号前缀"),
]


@pytest.mark.parametrize("fault,evidence,note", _SYNONYM_CASES)
def test_synonyms_still_pass(fault, evidence, note):
    assert is_equipment_in_evidence(fault, evidence) is True, note


_MISMATCH_CASES = [
    ({"设备类型": "注塑机"}, "【资料1】\n一、主轴电机报警代码E-203\n故障现象：主轴转速不稳定。",
     "注塑机不在数控机床资料里"),
    ({"设备类型": "龙门加工中心"}, "【资料1】\n一、主轴电机报警代码E-203\n故障现象：主轴转速不稳定。",
     "龙门加工中心不在主轴电机资料里"),
    ({"设备类型": "离心泵"}, "【资料1】\n一、空压机报警代码A-203\n故障现象：排气温度过高。",
     "离心泵不在空压机资料里"),
    ({"设备类型": "数控机床"}, "【资料1】\n一、空压机报警代码A-203\n故障现象：排气温度过高。",
     "数控机床 vs 空压机资料"),
    ({"设备类型": "液压系统"}, "【资料1】\n一、冷机组报警代码C-301\n故障现象：冷却水温度异常升高。",
     "液压系统 vs 冷水机组资料"),
]


@pytest.mark.parametrize("fault,evidence,note", _MISMATCH_CASES)
def test_genuine_mismatch_is_still_blocked(fault, evidence, note):
    assert is_equipment_in_evidence(fault, evidence) is False, note


def test_guard_is_not_a_constant():
    """最后一道自检：护栏不能退化成恒真或恒假。

    恒真 → 跨设备幻觉全部放行；恒假 → 所有诊断都被降级成"无依据"。
    两者都能让上面某一组断言"看起来合理"。
    """
    results = {
        is_equipment_in_evidence(f, e) for f, e, _ in _SYNONYM_CASES + _MISMATCH_CASES
    }
    assert results == {True, False}, f"护栏退化成常量了：{results}"
