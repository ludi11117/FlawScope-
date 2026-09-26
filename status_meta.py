"""诊断状态值的**单一来源**。

## 为什么需要这个模块

状态值是跨层契约：orchestrator 产生它、api.py 用它决定落不落库、app.py 用它决定
横幅文案与列表配色、eval_test.py 用它判定诚实降级、前端用它决定标签与颜色。
此前这些口径**散在 6 处**：

  1. `orchestrator.TERMINAL_FAILURE_STATUSES`
  2. `app.py` 的横幅 if/elif
  3. `app.py` 的列表颜色字典
  4. `api.py` 的落库判断（`!= "need_more_info"`）
  5. `api.py` 的 SSE 落库判断（同一句话抄了第二遍）
  6. `eval_test.py` 的降级判定

加一个状态要改 6 处，漏一处就是 bug —— 而且是"界面显示成未知状态""该落库的没落库"
这种**不报错、只误导**的 bug。把口径收进一张表，加状态时 IDE 的补全与类型检查
就能把人带到该改的地方；`tests/test_status_meta.py` 还负责在"漏登记"时变红。

## 为什么不把前端也合并进来

前端（`web/src/types/contracts.ts` / `historyUtils.ts` 的 STATUS_META）已经自己收敛
到一处了，而且它的字段（`bg`/`color` 的十六进制色值）是**渲染细节**，
后端不该知道。两边各自收敛即可，不需要跨语言共享——那会引入一个构建期依赖，
收益远小于成本。是否新增 `GET /meta/statuses` 把这张表暴露给前端，
属于接口新增，需要先决策（见批次 D 报告）。
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StatusMeta:
    """一个状态的全部对外口径。"""

    #: 给人看的中文短标签（列表、统计、日志）
    label: str
    #: 列表前的圆点 emoji（app.py 的历史列表用；前端有自己的配色，不用它）
    icon: str
    #: 横幅级别：error / warning / success / info。app.py 据此选 st.error/st.warning/…
    level: str
    #: 横幅正文。空串表示"这个状态不需要横幅"（过程态）
    banner: str
    #: 是否终态（此状态之后不会再变）
    terminal: bool
    #: 是否属于"失败/降级终态"。cost / workorder 不得把它覆盖成 costed/done
    failure: bool
    #: 是否应当落库。追问轮次不落库，否则历史列表里塞满"半成品"
    persisted: bool
    #: 是否属于"需要用户补充信息"（可追问）
    followup: bool


# 过程态（图中流转用，不会作为最终结果返回给调用方）。
# 它们的 banner 刻意留空：过程态出现在结果里说明有 bug，
# 不该顺手给它编一句看起来正常的话。
_PROCESS = dict(icon="⚪", level="info", banner="", terminal=False,
                failure=False, persisted=True, followup=False)

STATUS_META: dict = {
    "start": StatusMeta(label="开始", **_PROCESS),
    "extracted": StatusMeta(label="已抽取", **_PROCESS),
    "info_sufficient": StatusMeta(label="信息充分", **_PROCESS),
    "retrieved": StatusMeta(label="已检索", **_PROCESS),
    "diagnosed": StatusMeta(label="已诊断", **_PROCESS),
    "reviewed": StatusMeta(label="已审核", **_PROCESS),
    "rebutted": StatusMeta(label="已辩论", **_PROCESS),
    "final_reviewed": StatusMeta(label="已终审", **_PROCESS),
    "costed": StatusMeta(label="已核算", **_PROCESS),

    # ---- 需要用户补充信息：不落库，是唯一"非终态但会作为结果返回"的状态 ----
    "need_more_info": StatusMeta(
        label="需要补充信息", icon="🔵", level="info",
        banner="信息不足，已生成追问",
        terminal=True, failure=False, persisted=False, followup=True,
    ),

    # ---- 正常完成 ----
    "done": StatusMeta(
        label="诊断完成", icon="🟢", level="success",
        banner="✅ 诊断流程完成，工单已生成",
        terminal=True, failure=False, persisted=True, followup=False,
    ),

    # ---- 失败 / 降级终态 ----
    "insufficient_knowledge": StatusMeta(
        label="知识库无依据", icon="🟡", level="warning",
        banner="⚠️ 知识库无相关依据，建议人工介入",
        terminal=True, failure=True, persisted=True, followup=False,
    ),
    "llm_failed": StatusMeta(
        label="模型服务失败", icon="🔴", level="error",
        banner="⚠️ 模型服务调用失败，本轮诊断未完成，请稍后重试",
        terminal=True, failure=True, persisted=True, followup=False,
    ),
    "pending_human_review": StatusMeta(
        label="转人工复核", icon="🔴", level="error",
        banner="⚠️ 系统无法自动解决冲突，已转人工审核",
        terminal=True, failure=True, persisted=True, followup=False,
    ),

    # ---- 兜底：api.py 在结果里读不到 status 时会用 "unknown" ----
    "unknown": StatusMeta(
        label="未知状态", icon="⚪", level="info",
        banner="⚠️ 本轮结果的状态值无法识别，请人工确认",
        terminal=False, failure=False, persisted=True, followup=False,
    ),
}

#: 兜底项。未知状态一律按"要落库、不算失败终态"处理 —— 宁可多存一条，
#: 也不要因为状态名对不上就把用户等了数十秒的结果丢掉。
_FALLBACK = STATUS_META["unknown"]


def status_meta(status: Optional[str]) -> StatusMeta:
    """取某个状态的元信息；未登记的状态返回兜底项（不抛异常）。"""
    return STATUS_META.get(status or "", _FALLBACK)


#: 失败/降级终态集合。orchestrator 的 TERMINAL_FAILURE_STATUSES 直接引用它，
#: 不再各自维护一份字符串元组。
TERMINAL_FAILURE_STATUSES: tuple = tuple(
    name for name, meta in STATUS_META.items() if meta.failure
)


def is_terminal_failure(status: Optional[str]) -> bool:
    return status in TERMINAL_FAILURE_STATUSES


def should_persist(status: Optional[str]) -> bool:
    """该状态的结果是否应当落库。

    此前 api.py 两处各写一遍 `status != "need_more_info"`：
    判断本身没错，但"哪些状态不落库"这件事散在调用点，加状态时容易漏。
    """
    return status_meta(status).persisted


def banner(status: Optional[str]) -> tuple:
    """返回 (level, banner)；banner 为空表示不需要横幅。"""
    meta = status_meta(status)
    return meta.level, meta.banner
