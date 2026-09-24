"""把维修工单渲染成可打印的 Markdown。

工单是这套系统最终的交付物，但此前只能以 JSON 形式拿到（前端 `st.json`、
API 直接返回 dict）。现场维修人员要的是一张能直接打印、或复制进工单系统的单子，
JSON 对他们没有可读性。

设计原则与整个项目一致：**没有的字段就不渲染**。
降级工单（模型失败 / 知识库无依据 / 转人工）本来就只有「工单编号 / 风险等级 /
风险说明」三个字段，渲染器不能替它们补出空的「维修方案」章节——
那看起来像是"方案就是空的"，而不是"这一轮根本没产出方案"。
"""

import re
from typing import Optional

# 渲染顺序：先风险（决定这张单子能不能直接执行），再现象/根因/方案，最后费用与安全
# 元组是 (章节标题, 工单里的字段名)——注意字段名取自 WorkOrderOutput，是「根因」而不是
# 诊断侧的「根因判断」，写错会静默丢掉整个章节。
_SECTIONS = (
    ("故障现象", "故障现象"),
    ("根因", "根因"),
    ("维修方案", "维修方案"),
)

# 文件名里不能出现的字符（Windows 保留字符 + 路径分隔符）
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _as_text(value) -> str:
    """把字段值归一化成一行文本。

    工单字段在模型手里偶尔会写成列表（"维修方案": ["换轴承", "补润滑"]），
    `agent_workorder` 的 preprocess 会修掉大部分，但导出器不该假设它一定修过。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _parts_table(parts: list) -> str:
    rows = ["| # | 备件名称 |", "| --- | --- |"]
    for i, part in enumerate(parts, start=1):
        rows.append(f"| {i} | {_as_text(part)} |")
    return "\n".join(rows)


def workorder_filename(workorder: dict) -> str:
    """生成导出文件名。

    工单编号来自模型输出，可能带 `/` `:` 之类字符，直接当文件名会写到别的目录去，
    这里统一替换掉；编号缺失时回退成通用名，而不是产出一个 `None.md`。
    """
    raw = _as_text(workorder.get("工单编号")) or "workorder"
    safe = _UNSAFE_FILENAME_CHARS.sub("_", raw).strip(" .") or "workorder"
    return f"{safe}.md"


def workorder_to_markdown(
    workorder: dict,
    cost: Optional[dict] = None,
    correlation_id: str = "",
) -> str:
    """渲染工单为 Markdown。

    Args:
        workorder: 工单字典（orchestrator.workorder_node 的产物）。
        cost: 可选的成本明细，用于展示费用构成与"未计价备件"提示。
        correlation_id: 可选，写进页脚便于把这张单子对回日志。

    Returns:
        Markdown 文本。即使 workorder 是空 dict 也返回一份带说明的文档，
        而不是抛异常——导出接口不该因为"这轮没出工单"而 500。
    """
    if not workorder:
        return "# 维修工单\n\n> 本轮未生成工单。\n"

    lines = [f"# 维修工单 {_as_text(workorder.get('工单编号'))}".rstrip()]

    risk = _as_text(workorder.get("风险等级"))
    if risk:
        lines.append("")
        lines.append(f"> **风险等级：{risk}**")
        note = _as_text(workorder.get("风险说明"))
        if note:
            # 风险说明可能是**多行**（降级时会附"参考方向"的逐条排查动作）。
            # 每一行都得带 `> ` 前缀，否则后续行会跳出引用块、破坏整段结构——
            # 只给头部加一次前缀在多行时是不够的。
            lines.append(">")
            lines.extend(f"> {ln}" if ln.strip() else ">" for ln in note.splitlines())

    for heading, key in _SECTIONS:
        text = _as_text(workorder.get(key))
        if text:
            lines.append("")
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(text)

    parts = workorder.get("备件清单")
    if isinstance(parts, list) and parts:
        lines.append("")
        lines.append("## 备件清单")
        lines.append("")
        lines.append(_parts_table(parts))

    cost = cost or {}
    lines.extend(_render_cost_section(workorder, cost))

    safety = _as_text(workorder.get("安全注意事项"))
    if safety:
        lines.append("")
        lines.append("## 安全注意事项")
        lines.append("")
        lines.append(safety)

    footer = []
    if correlation_id:
        footer.append(f"追踪 ID：`{correlation_id}`")
    if footer:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("　".join(footer))

    return "\n".join(lines) + "\n"


def _render_cost_section(workorder: dict, cost: dict) -> list:
    """费用章节：优先展开明细，只有总价时退化成一行。

    明细里的「未知备件」必须显式写出来——价格表没覆盖的备件是不计费的，
    只给一个总数会让现场以为报价是完整的。
    """
    total = _as_text(workorder.get("预计成本")) or _as_text(cost.get("预计成本"))
    detail = cost.get("成本明细") if isinstance(cost.get("成本明细"), dict) else {}
    hours = _as_text(workorder.get("预计工时")) or _as_text(cost.get("预计工时"))
    hint = _as_text(cost.get("计费提示"))

    if not any((total, hours, detail, hint)):
        return []

    lines = ["", "## 费用", ""]
    if total:
        lines.append(f"- **预计成本**：{total}")
    if hours:
        lines.append(f"- **预计工时**：{hours}")
    if detail:
        if detail.get("备件费用") is not None:
            lines.append(f"- 备件费用：{detail['备件费用']}")
        if detail.get("工时费用") is not None:
            lines.append(f"- 工时费用：{detail['工时费用']}")

    unknown = detail.get("未知备件") if isinstance(detail, dict) else None
    if isinstance(unknown, list) and unknown:
        lines.append("")
        lines.append("> ⚠️ 以下备件不在价格表中，**未计入报价**，需人工核价："
                     + "、".join(_as_text(u) for u in unknown))
    if hint:
        lines.append("")
        lines.append(f"> ⚠️ {hint}")
    return lines
