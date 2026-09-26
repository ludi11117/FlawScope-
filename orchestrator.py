import json
import time
import uuid
from functools import wraps
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END
from agents import (
    retrieve_evidence, agent_diagnose, agent_review,
    agent_cost, agent_workorder, agent_rebuttal, agent_review_final,
    extract_fault_info, is_info_sufficient, generate_followup_question,
    extract_image_info, check_relevance, is_equipment_in_evidence,
    build_degraded_report, find_cross_device_hints, extract_kb_disagreements,
    DEGRADED_REASON_NO_HIT, DEGRADED_REASON_EQUIPMENT_MISMATCH,
    DEGRADED_REASON_IRRELEVANT, DEGRADED_REASON_RELEVANCE_UNKNOWN,
    DEGRADED_REASON_LLM_FAILED,
)
from config import settings
from logging_config import get_logger, get_token_tracker, clear_token_tracker
from status_meta import TERMINAL_FAILURE_STATUSES

logger = get_logger(__name__)


class AgentState(TypedDict):
    user_input: str
    image_description: str
    # 图片没被识别出来时的可见提示（空串表示无需提示）。
    # 为什么要有它：视觉调用失败此前只是静默返回空描述，流程照跑，
    # 用户完全不知道照片没被用上——"没识别出来"和"没传照片"在界面上长得一模一样。
    image_warning: str
    fault_info: Optional[dict]
    followup_question: str
    evidence: str
    # 初检证据的副本。辩论会用更宽的查询重新检索并覆盖 evidence，
    # 若不留底，"本条故障的范围"就丢了——护栏 drop_out_of_scope_candidates
    # 需要它来判断辩论新增的候选是否越界（跨故障条目）。
    initial_evidence: str
    diagnosis: Optional[dict]
    review: Optional[dict]
    rebuttal: Optional[dict]
    final_review: Optional[dict]
    cost: Optional[dict]
    workorder: Optional[dict]
    debate_round: int
    max_debate_rounds: int
    status: str
    correlation_id: str  # 追踪 ID


# 终态集合：这些状态由失败/降级分支产生，cost / workorder 节点不得覆盖成 costed/done。
#
# **唯一来源已挪到 `status_meta.py`**（那里还有标签/配色/是否落库/是否可追问）。
# 这里保留同名模块属性，是因为 orchestrator 内部、app.py、eval_test.py 都在引用它；
# 直接删掉会变成一次跨文件改名，与本条修复无关。新增状态请改 status_meta.py。

# 终审置信度低于此值时，即便审核意见是"通过"，工单也要标风险。
#
# 为什么不能只看"通过/不通过"：终审 prompt 里"通过"的判定标准是**找不到问题**，
# 而模型对"我没把握但挑不出错"的情况同样会输出"通过"并把置信度写成 30。
# 只按意见放行，等于把"模型自己都不确定"的结论以"正常"面貌交付。
LOW_CONFIDENCE_THRESHOLD = 60

# 模型不可用时的占位诊断。刻意写成"降级形态"，让 _is_degraded 能识别，
# 从而避免辩论环节拿着空诊断反复空转。
LLM_FAILED_DIAGNOSIS = {
    "报警代码": "N/A",
    "根因判断": "模型服务调用失败，无法完成诊断",
    "依据": "无",
    "排查建议": ["请稍后重试；若持续失败请检查模型服务与网络"]
}

# 成本算不出来时的占位成本。同样写成可读的降级形态，而不是留空 dict——
# 留空会让前端显示"无成本数据"，用户分不清是"没算"还是"算出来是 0"。
COST_UNAVAILABLE = {
    "备件清单": [],
    "预计工时": "N/A",
    "预计成本": "N/A",
    "成本明细": {},
    "计费提示": "本轮未产出可用诊断结论，成本待人工核算"
}

# 图执行中途抛异常（递归超限、节点内未捕获错误等）时的兜底工单状态。
# 用 pending_human_review 而非 llm_failed：异常原因未必是模型服务，
# 一律归成"模型异常"会把用户引向错误的排查方向。
GRAPH_ABORTED_STATUS = "pending_human_review"


def build_abort_workorder(state: dict, error: str = "") -> dict:
    """图执行中断时的兜底工单。

    存在的理由：workorder_node 内部的兜底只覆盖"走到了 workorder 节点但 LLM 挂了"
    这一种情况。若异常发生在更早的节点（retrieve / diagnose / rebuttal …），
    图根本到不了 workorder，异常直接冒到 run_diagnosis 的 except 分支被 raise 出去。
    结果是：用户烧掉几十次 LLM 调用、等了十几分钟，最终拿到 0 交付物，
    而项目对外宣称的核心保证是"任何失败都产出带风险标记的工单，没有死胡同"。

    这里把兜底挪到正确层级——run_diagnosis / run_diagnosis_stream 的最外层 except。
    字段刻意只保留三样：没有真实诊断依据时，不补空章节（与 workorder_export 的
    "没有的字段就不渲染"口径一致），补出来的空"维修方案"比没有更危险。
    """
    correlation_id = (state or {}).get("correlation_id") or "unknown"
    return {
        "工单编号": f"WO-{correlation_id}-ABORT",
        "风险等级": "待人工处理（诊断流程异常中断）",
        "风险说明": (
            "诊断流程在执行途中异常中断，未产出可用结论"
            + (f"（错误类型：{error}）" if error else "")
            + "。本次未生成维修方案，请人工介入排查，"
            "切勿依据不完整信息拆机作业。"
        )
    }


def build_abort_result(state: dict, error: str = "") -> dict:
    """把中断状态补全成一份"结构完整、内容降级"的结果，供 API / 前端直接消费。

    补全的是结构而非内容：cost 用 COST_UNAVAILABLE，diagnosis 不编造，
    workorder 用 build_abort_workorder ——用户至少能下载到一张工单。
    """
    merged = {k: v for k, v in (state or {}).items()}
    merged["workorder"] = build_abort_workorder(state, error)
    # 注意不能用 setdefault：_build_initial_state 里 cost 是"键存在、值为 None"，
    # setdefault 只在键缺失时写入，这里会静默失效，前端最终拿到 cost=None。
    if not merged.get("cost"):
        merged["cost"] = dict(COST_UNAVAILABLE)
    if not merged.get("correlation_id"):
        merged["correlation_id"] = (state or {}).get("correlation_id") or "unknown"
    # 保留已产生的中间产物（fault_info / evidence / diagnosis），它们是排障线索。
    merged["status"] = GRAPH_ABORTED_STATUS
    return merged


def _tracked(node_name: str):
    """节点装饰器：把该节点内发生的 LLM 调用归因到 node_name 名下，并记录节点耗时。

    没有它，TokenTracker 的 by_node 统计全部落在初始化那一次 start_node 上，
    分节点用量形同虚设。耗时同理——只有 Token 没有耗时时，回答不了"这次为什么慢"。
    """
    def decorator(func):
        @wraps(func)
        def wrapper(state: AgentState) -> AgentState:
            tracker = get_token_tracker(state.get("correlation_id", ""))
            tracker.start_node(node_name)
            started = time.perf_counter()
            try:
                return func(state)
            finally:
                tracker.record_node_duration(node_name, (time.perf_counter() - started) * 1000)
        return wrapper
    return decorator


def extract_info_node(state: AgentState) -> AgentState:
    """节点0：从用户自由文本中提取结构化信息，支持图片描述合并"""
    full_input = state["user_input"]
    img_desc = state.get("image_description", "")
    if img_desc:
        full_input = full_input + "\n\n【图片识别信息】\n" + img_desc

    fault_info = extract_fault_info(full_input, correlation_id=state["correlation_id"])

    if fault_info is None:
        # 模型不可用 ≠ 用户没说清楚。这里必须短路成 llm_failed，
        # 否则流程会退化成"追问用户补充设备类型"——把服务故障甩锅给用户。
        logger.error("extract_info_llm_failed", correlation_id=state["correlation_id"])
        return {
            "fault_info": {},
            "diagnosis": dict(LLM_FAILED_DIAGNOSIS),
            "status": "llm_failed"
        }

    logger.info("extract_info_done", has_fault_info=bool(fault_info), correlation_id=state["correlation_id"])
    return {"fault_info": fault_info, "status": "extracted"}


def check_info_node(state: AgentState) -> AgentState:
    """节点0.5：检查提取的信息是否充分，不足则生成追问问题"""
    # 抽取阶段已判定模型不可用：保持 llm_failed 直接短路，不生成追问
    if state.get("status") == "llm_failed":
        return {"followup_question": "", "status": "llm_failed"}

    fault_info = state.get("fault_info", {})
    if is_info_sufficient(fault_info):
        return {"followup_question": "", "status": "info_sufficient"}

    question = generate_followup_question(fault_info)
    logger.info("need_more_info", question=question[:50], correlation_id=state["correlation_id"])
    return {"followup_question": question, "status": "need_more_info"}


def retrieve_node(state: AgentState) -> AgentState:
    """节点1：基于提取出的结构化信息检索证据"""
    fault_info = state.get("fault_info", {})

    query_parts = []
    if fault_info.get("设备类型"):
        query_parts.append(fault_info["设备类型"])
    if fault_info.get("报警代码"):
        query_parts.append(fault_info["报警代码"])
    if fault_info.get("故障现象"):
        query_parts.extend(fault_info["故障现象"])

    query = " ".join(query_parts) if query_parts else state["user_input"]

    evidence = retrieve_evidence(query, k=settings.RETRIEVAL_K, correlation_id=state["correlation_id"])
    return {
        "evidence": evidence,
        # 留底：辩论覆盖 evidence 后，护栏还要靠它判断"什么属于本条故障"
        "initial_evidence": evidence,
        "debate_round": 0,
        "status": "retrieved"
    }


def diagnose_node(state: AgentState) -> AgentState:
    """节点2：诊断师基于证据诊断。证据不足时诚实降级，不瞎猜"""
    evidence = state.get("evidence", "")
    fault_info = state.get("fault_info", {})

    if "【知识库无相关依据】" in evidence:
        logger.warning("diagnose_degraded_no_evidence", correlation_id=state["correlation_id"])
        report = build_degraded_report(
            fault_info, DEGRADED_REASON_NO_HIT,
            cross_device_hints=find_cross_device_hints(fault_info),
        )
        return {
            "diagnosis": {
                "报警代码": "N/A",
                "根因判断": "知识库无相关依据，无法诊断",
                "依据": "无",
                "排查建议": report["下一步建议"],
                "降级说明": report["判定说明"],
                "参考方向": report["参考方向"],
            },
            "status": "insufficient_knowledge"
        }

    # 确定性护栏：设备类型/报警代码不在资料中 → 跨设备幻觉，直接降级
    if not is_equipment_in_evidence(fault_info, evidence):
        logger.warning("diagnose_degraded_equipment_mismatch", correlation_id=state["correlation_id"])
        report = build_degraded_report(
            fault_info, DEGRADED_REASON_EQUIPMENT_MISMATCH,
            cross_device_hints=find_cross_device_hints(fault_info),
        )
        return {
            "diagnosis": {
                "报警代码": "N/A",
                "根因判断": "知识库无该设备的相关依据，无法诊断",
                "依据": "无",
                "排查建议": report["下一步建议"],
                "降级说明": report["判定说明"],
                "参考方向": report["参考方向"],
            },
            "status": "insufficient_knowledge"
        }

    # 语义相关性兜底：资料与故障完全无关时同样降级
    relevance = check_relevance(state["user_input"], evidence, correlation_id=state["correlation_id"])
    if relevance is False:
        logger.warning("diagnose_degraded_irrelevant", correlation_id=state["correlation_id"])
        report = build_degraded_report(
            fault_info, DEGRADED_REASON_IRRELEVANT,
            cross_device_hints=find_cross_device_hints(fault_info),
        )
        return {
            "diagnosis": {
                "报警代码": "N/A",
                "根因判断": "检索资料与故障不相关，无法诊断",
                "依据": "无",
                "排查建议": report["下一步建议"],
                "降级说明": report["判定说明"],
                "参考方向": report["参考方向"],
            },
            "status": "insufficient_knowledge"
        }
    if relevance is None:
        # 相关性没验成（模型不可用）。此时继续诊断风险太高，同样停止自动流程，
        # 但理由必须写清楚是"校验未完成"，而不是甩锅给"资料不相关"。
        logger.warning("diagnose_degraded_relevance_unknown", correlation_id=state["correlation_id"])
        report = build_degraded_report(fault_info, DEGRADED_REASON_RELEVANCE_UNKNOWN)
        return {
            "diagnosis": {
                "报警代码": "N/A",
                "根因判断": "相关性校验未能完成，无法确认资料可用性",
                "依据": "无",
                "排查建议": report["下一步建议"],
                "降级说明": report["判定说明"],
            },
            "status": "llm_failed"
        }

    enriched_fault = json.dumps(fault_info, ensure_ascii=False, indent=2)

    # 关键：把排除条件传给诊断师
    diagnosis = agent_diagnose(
        state["evidence"],
        enriched_fault,
        exclusion_list=fault_info.get("排除条件", []),
        correlation_id=state["correlation_id"]
    )

    if not diagnosis:
        # 模型调用失败 / 输出无法通过 Schema 校验：立刻停止，别让空诊断流进辩论环节
        logger.error("diagnose_llm_failed", correlation_id=state["correlation_id"])
        report = build_degraded_report(fault_info, DEGRADED_REASON_LLM_FAILED)
        failed = dict(LLM_FAILED_DIAGNOSIS)
        failed["排查建议"] = report["下一步建议"]
        failed["降级说明"] = report["判定说明"]
        return {"diagnosis": failed, "status": "llm_failed"}

    logger.info("diagnose_done", has_diagnosis=True, correlation_id=state["correlation_id"])
    return {"diagnosis": diagnosis, "status": "diagnosed"}


def review_node(state: AgentState) -> AgentState:
    """审核节点：把检索资料、原始报修、知识库分歧一起交给审核师，让它有对照物可查。

    只传 diagnosis 的话，审核师无法判断"诊断里写的原因是否都有出处"，
    只能确认结论内部自洽——而诊断提示词强制写满知识库全部原因，
    于是审核恒通过、辩论永不触发。传资料是让辩论真正能触发的前提。

    传 `disagreements` 补上最后一块对照物：知识库里本来就有 36 处"多位师傅
    判断不一致"，但审核师此前看不到，也就永远发现不了"诊断只取了一派"。
    这是「冲突材料是多 Agent 辩论的存在理由」在代码里的落点。
    """
    evidence = state.get("evidence", "")
    disagreements = extract_kb_disagreements(evidence)
    if disagreements:
        logger.info(
            "review_kb_disagreements",
            count=len(disagreements),
            entries=[d.get("条目") for d in disagreements],
            correlation_id=state["correlation_id"],
        )
    review = agent_review(
        state["diagnosis"],
        evidence=evidence,
        fault=json.dumps(state.get("fault_info", {}), ensure_ascii=False, indent=2),
        correlation_id=state["correlation_id"],
        disagreements=disagreements,
    )
    if not review:
        logger.error("review_llm_failed", correlation_id=state["correlation_id"])
        return {"review": {}, "status": "llm_failed"}

    logger.info("review_done", verdict=review.get("审核意见"), correlation_id=state["correlation_id"])
    return {"review": review, "status": "reviewed"}


def rebuttal_node(state: AgentState) -> AgentState:
    """辩论节点：诊断师根据最新驳回理由，重新检索证据并修正/反驳"""
    # 兜底防线：诊断已是诚实降级时，绝不在辩论中编造根因
    if _is_degraded(state.get("diagnosis")):
        return {
            "rebuttal": {},
            "evidence": state.get("evidence", ""),
            "debate_round": state.get("debate_round", 0) + 1,
            "status": "insufficient_knowledge"
        }

    # 多轮时用 final_review 的最新理由，首轮用 review 的理由
    latest_feedback = state.get("final_review") or state.get("review") or {}
    fault_info = state.get("fault_info", {})
    fault_str = json.dumps(fault_info, ensure_ascii=False, indent=2)

    targeted_query = fault_str
    if latest_feedback and latest_feedback.get("理由"):
        targeted_query = fault_str + " " + latest_feedback["理由"]

    new_evidence = retrieve_evidence(targeted_query, k=settings.RETRIEVAL_K, correlation_id=state["correlation_id"])

    rebuttal = agent_rebuttal(
        diagnosis=state["diagnosis"],
        review=latest_feedback,
        evidence=new_evidence,
        fault=fault_str,
        initial_evidence=state.get("initial_evidence", ""),
        correlation_id=state["correlation_id"]
    )

    if not rebuttal:
        logger.error("rebuttal_llm_failed", correlation_id=state["correlation_id"])
        return {
            "rebuttal": {},
            "evidence": new_evidence,
            "debate_round": state.get("debate_round", 0) + 1,
            "status": "llm_failed"
        }

    return {
        "rebuttal": rebuttal,
        "evidence": new_evidence,
        "debate_round": state.get("debate_round", 0) + 1,
        "status": "rebutted"
    }


def _effective_diagnosis(state: AgentState) -> dict:
    """辩论成功后以"最终根因"为准，否则沿用初诊。cost 与 workorder 共用此口径。"""
    diagnosis = state.get("diagnosis") or {}
    rebuttal = state.get("rebuttal") or {}
    if rebuttal.get("最终根因"):
        return {
            "报警代码": diagnosis.get("报警代码", ""),
            "根因判断": rebuttal["最终根因"],
            "依据": rebuttal.get("依据", ""),
            "排查建议": diagnosis.get("排查建议", [])
        }
    return diagnosis


def _effective_review(state: AgentState) -> dict:
    """审核口径：辩论走完后以"最终复审"为准，否则沿用初始审核。

    必须与 _effective_diagnosis 用同一口径。此前 cost / workorder 只读 state["review"]，
    于是辩论翻盘成功后会出现自相矛盾的工单：根因已经按辩论结论改了，
    审核意见却还写着那版"不通过"。
    """
    final_review = state.get("final_review") or {}
    if final_review.get("审核意见"):
        return final_review
    return state.get("review") or {}


def cost_node(state: AgentState) -> AgentState:
    status = state.get("status")

    # 已是失败/降级终态：结论都没出来，再花一次 LLM 调用去"猜"备件清单没有意义，
    # 直接给可读的降级成本，并原样保留终态。
    if status in TERMINAL_FAILURE_STATUSES:
        logger.warning("cost_skipped_terminal_failure", status=status, correlation_id=state["correlation_id"])
        return {"cost": dict(COST_UNAVAILABLE), "status": status}

    cost = agent_cost(
        _effective_diagnosis(state),
        _effective_review(state),
        correlation_id=state["correlation_id"]
    )

    if not cost:
        # 与 review / final_review / workorder 三个节点对齐：算不出来就必须显式标记。
        # 此前这里没有任何检查，cost 为 {} 也会把 status 置成 costed，
        # 最终报成"✅ 诊断流程完成"，用户拿到一张没有报价的工单却不知道出了什么事。
        logger.error("cost_llm_failed", correlation_id=state["correlation_id"])
        return {"cost": dict(COST_UNAVAILABLE), "status": "llm_failed"}

    logger.info("cost_done", has_cost=True, correlation_id=state["correlation_id"])
    return {"cost": cost, "status": "costed"}


def _compose_risk_note(head: str, degraded_note: str, tips: list, references: list = None) -> str:
    """把「降级原因 + 判定说明 + 下一步建议 + 参考方向」拼成一段可读的风险说明。

    降级工单本来就只有「工单编号 / 风险等级 / 风险说明」三样（硬约束 20），
    所以往风险说明里补具体内容**不违反**"不得补出空的维修方案"——这里补的是
    已知事实和下一步动作，不是编造的根因或维修步骤。
    """
    parts = [head.rstrip("。")]
    if degraded_note:
        parts.append(degraded_note.rstrip("。"))
    if tips:
        parts.append("可尝试：" + "；".join(str(t) for t in tips))
    note = "。".join(parts) + "。"
    if references:
        # 参考方向自带完整句式（含"非本设备根因"的免责说明），单独成段更醒目
        note += "\n" + "\n".join(references)
    return note


def workorder_node(state: AgentState) -> AgentState:
    workorder = agent_workorder(
        _effective_diagnosis(state), _effective_review(state), state["cost"],
        correlation_id=state["correlation_id"]
    )

    if not workorder:
        logger.error("workorder_llm_failed", correlation_id=state["correlation_id"])
        prior_status = state.get("status")
        return {
            "workorder": {
                "工单编号": f"WO-{state['correlation_id']}",
                "风险等级": "待人工确认（模型服务异常）",
                "风险说明": "工单生成失败，请人工根据诊断与成本信息补全后执行"
            },
            # 已处于降级终态时保留原状态：那才是本轮真正的原因。
            # 一律覆盖成 llm_failed 会把"知识库没覆盖该设备"说成"模型服务异常"，
            # 用户按错误的原因去排查（该补知识库却去查模型服务）。
            "status": prior_status if prior_status in TERMINAL_FAILURE_STATUSES else "llm_failed"
        }

    status = state.get("status")

    # 如果辩论轮数已达上限且最终复审未通过，标记高风险
    final_review = state.get("final_review") or {}
    if (state.get("debate_round", 0) >= state.get("max_debate_rounds", 3)
            and final_review.get("审核意见") != "通过"):
        workorder["风险等级"] = "高风险待复核"
        workorder["风险说明"] = "诊断结论经多轮辩论仍未通过审核，建议人工复核后执行"

    # 辩论"维持"原判：路由层会跳过最终复审（见 route_after_rebuttal，省 1 次 LLM 调用）。
    # 省调用不能变成悄悄降低把关强度 —— 此时审核意见很可能仍是"不通过"，
    # 而结论**没有经过独立复审确认**。必须在工单上写出来，否则用户无法区分
    # "审过了"和"没审"。
    rebuttal = state.get("rebuttal") or {}
    if rebuttal.get("行动") == "维持":
        workorder["风险等级"] = "中风险（辩论未改变结论）"
        workorder["风险说明"] = (
            "诊断师在辩论中维持原结论、未采纳审核意见，本轮未经最终复审确认，"
            "建议人工复核后再执行"
        )

    # 终审"通过但置信度低"：形式上过关，实质上模型自己并不确定。
    # 只按审核意见放行会让这类结论以"正常"面貌交付（见 LOW_CONFIDENCE_THRESHOLD 的说明）。
    confidence = final_review.get("置信度")
    if (final_review.get("审核意见") == "通过"
            and isinstance(confidence, int)
            and confidence < LOW_CONFIDENCE_THRESHOLD):
        workorder["风险等级"] = "中风险（终审置信度偏低）"
        workorder["风险说明"] = (
            f"最终复审判定通过，但置信度仅 {confidence}/100"
            f"（低于 {LOW_CONFIDENCE_THRESHOLD}），结论稳定性不足，建议人工复核"
        )

    # 降级时把"为什么答不了 + 下一步能做什么"一并写进风险说明。
    # 此前只有一句「建议人工介入」，用户拿到手等于没拿到东西。
    diag = _effective_diagnosis(state)
    degraded_note = (diag.get("降级说明") or "").strip()
    tips = diag.get("排查建议") or []
    references = diag.get("参考方向") or []

    # 知识库无依据直接降级时，同样标记。
    # 状态语义不变（仍是"待人工确认"——本设备确实没有依据），但附上了跨设备的
    # 排查动作作为参考，人工介入时不再是两手空空。
    if status == "insufficient_knowledge":
        workorder["风险等级"] = "待人工确认（知识库无依据）"
        workorder["风险说明"] = _compose_risk_note(
            "知识库未覆盖该故障的相关依据，无法自动诊断", degraded_note, tips, references)
    elif status == "llm_failed":
        workorder["风险等级"] = "待人工确认（模型服务异常）"
        workorder["风险说明"] = _compose_risk_note(
            "诊断链路中模型调用失败，结论可能不完整，请人工复核后再执行", degraded_note, tips)
    elif status == "pending_human_review":
        # 审核阶段判定诊断结论不可采信。此前这条路径直接 END、不出工单，
        # 用户只看到一句红字报错；现在同样产出工单，只是明确标注不可直接执行。
        workorder["风险等级"] = "待人工复核（诊断结论不可自动采信）"
        workorder["风险说明"] = "诊断结论未通过审核且已转人工复核，请勿直接按本工单执行"

    logger.info("workorder_done", risk=workorder.get("风险等级", "正常"), correlation_id=state["correlation_id"])
    return {"workorder": workorder, "status": status if status in TERMINAL_FAILURE_STATUSES else "done"}


def human_review_node(state: AgentState) -> AgentState:
    logger.warning("human_review_required", correlation_id=state["correlation_id"])
    return {"status": "pending_human_review"}


def final_review_node(state: AgentState) -> AgentState:
    """最终复审：审核师对诊断师的辩论反驳进行最终判断。

    与初审 review_node 一样要把 evidence / disagreements 传下去：终审才是决定
    终态的那一步，对照物比初审少就只剩下"看谁说得更顺"。此前这里只传了
    「原诊断 + 反驳」两样。
    """
    if not state.get("rebuttal"):
        # 辩论没产出（模型失败或已降级），复审没有可审对象，直接跳过这次调用
        logger.warning("final_review_skipped_no_rebuttal", correlation_id=state["correlation_id"])
        return {"final_review": {}, "status": state.get("status", "llm_failed")}

    evidence = state.get("evidence", "")
    disagreements = extract_kb_disagreements(evidence)
    if disagreements:
        logger.info(
            "final_review_kb_disagreements",
            count=len(disagreements),
            entries=[d.get("条目") for d in disagreements],
            correlation_id=state["correlation_id"],
        )
    final_review = agent_review_final(
        original_diagnosis=state.get("diagnosis", {}),
        rebuttal=state.get("rebuttal", {}),
        correlation_id=state["correlation_id"],
        evidence=evidence,
        disagreements=disagreements,
    )
    if not final_review:
        logger.error("final_review_llm_failed", correlation_id=state["correlation_id"])
        return {"final_review": {}, "status": "llm_failed"}

    logger.info("final_review_done", verdict=final_review.get("审核意见"), correlation_id=state["correlation_id"])
    return {"final_review": final_review, "status": "final_reviewed"}


DEGRADED_MARKERS = ("无法诊断", "无法判断", "无相关依据", "不相关", "调用失败", "校验未能完成")


def _is_degraded(diagnosis: Optional[dict]) -> bool:
    """诊断是否为诚实降级状态（知识库无依据 / 资料不相关 / 模型不可用）。"""
    return bool(diagnosis) and any(m in (diagnosis.get("根因判断") or "") for m in DEGRADED_MARKERS)


def route_after_final_review(state: AgentState):
    """最终复审后路由：通过→cost；没审出结果→cost；不通过且未达上限→继续辩论"""
    final_review = state.get("final_review") or {}

    # 复审没产出（模型失败）：继续辩论只会继续空转，直接出结果并标记风险
    if not final_review:
        return "cost"

    if final_review.get("审核意见") == "通过":
        return "cost"

    if state.get("debate_round", 0) < state.get("max_debate_rounds", 3):
        return "rebuttal"

    return "cost"


def route_after_rebuttal(state: AgentState):
    """辩论后路由：维持原判→直接出结果；有修正→进最终复审。

    诊断师说"维持"意味着它不认可审核意见、结论没有变化。此时再跑一次最终复审，
    审核师手上的材料与上一轮完全相同（同一份诊断 + 同一份证据），结论大概率还是
    "不通过"，等于白烧一次 LLM 调用（终审占总用量约 12%、单次 3~4 秒）。
    反驳/修正则不同——结论变了，审核师才有新东西可审，这一次调用是值得的。

    省下这一次的前提是**工单必须标出"未经最终复审"**（见 workorder_node）：
    少跑一步是提速，隐瞒少跑一步是降低把关强度，两者不能混为一谈。
    """
    rebuttal = state.get("rebuttal") or {}
    if rebuttal.get("行动") == "维持":
        return "cost"
    return "final_review"


def route_after_review(state: AgentState):
    review = state.get("review", {})

    # 诚实降级的诊断不得进入辩论/成本流程：直接转人工，防止辩论环节凭空编造根因
    if _is_degraded(state.get("diagnosis")):
        return "human_review"

    # 审核没产出（模型失败）：没有驳回理由可供辩论，跳过辩论直接出结果
    if not review:
        return "cost"

    if review.get("审核意见") == "通过":
        return "cost"

    if state.get("debate_round", 0) < state.get("max_debate_rounds", 3):
        return "rebuttal"

    return "cost"


def route_after_check_info(state: AgentState):
    """信息检查后路由：充分→检索；模型失败→直接出降级工单；不足→结束（返回追问）"""
    status = state.get("status")
    if status == "llm_failed":
        return "cost"
    if status == "info_sufficient":
        return "retrieve"
    return "need_more_info"


def route_after_diagnose(state: AgentState):
    """诊断后路由：降级/模型失败→跳过审核辩论直接出工单；否则进入审核"""
    if state.get("status") in TERMINAL_FAILURE_STATUSES:
        return "cost"
    return "review"


# 构建状态图
# 每个节点都包一层 _tracked：节点内的 LLM 调用会归因到该节点，Token 分节点统计才有意义
graph = StateGraph(AgentState)

graph.add_node("extract_info", _tracked("extract_info")(extract_info_node))
graph.add_node("check_info", check_info_node)          # 纯规则判断，无 LLM 调用
graph.add_node("retrieve", _tracked("retrieve")(retrieve_node))
graph.add_node("diagnose", _tracked("diagnose")(diagnose_node))
graph.add_node("review", _tracked("review")(review_node))
graph.add_node("rebuttal", _tracked("rebuttal")(rebuttal_node))
graph.add_node("final_review", _tracked("final_review")(final_review_node))
graph.add_node("cost", _tracked("cost")(cost_node))
graph.add_node("workorder", _tracked("workorder")(workorder_node))
graph.add_node("human_review", human_review_node)

graph.set_entry_point("extract_info")
graph.add_edge("extract_info", "check_info")

graph.add_conditional_edges(
    "check_info",
    route_after_check_info,
    {
        "retrieve": "retrieve",
        "need_more_info": END,
        "cost": "cost"
    }
)

graph.add_edge("retrieve", "diagnose")
graph.add_conditional_edges(
    "diagnose",
    route_after_diagnose,
    {
        "review": "review",
        "cost": "cost"
    }
)

graph.add_conditional_edges(
    "review",
    route_after_review,
    {
        "cost": "cost",
        "rebuttal": "rebuttal",
        "human_review": "human_review"
    }
)

graph.add_conditional_edges(
    "rebuttal",
    route_after_rebuttal,
    {
        "final_review": "final_review",
        "cost": "cost"
    }
)

graph.add_conditional_edges(
    "final_review",
    route_after_final_review,
    {
        "cost": "cost",
        "rebuttal": "rebuttal"
    }
)

graph.add_edge("cost", "workorder")
graph.add_edge("workorder", END)
# 转人工不再是"死胡同"：同样走 cost → workorder，让用户拿到一张标注了
# "不可自动采信"的工单，而不是只看到一句报错、什么都拿不到。
graph.add_edge("human_review", "cost")

app = graph.compile()


NODE_DESCRIPTIONS = {
    "extract_info": "① 信息抽取：把口语化描述转成结构化字段",
    "check_info": "② 信息核验：判断信息是否充分",
    "retrieve": "③ 知识检索：混合检索相关故障资料",
    "diagnose": "④ 诊断分析：诊断师基于证据判断根因",
    "review": "⑤ 方案审核：审核师把关诊断结论",
    "rebuttal": "⑥ 辩论反驳：诊断师回应驳回并修正",
    "final_review": "⑦ 最终复审：审核师终审",
    "cost": "⑧ 成本精算：提取备件工时并计算费用",
    "workorder": "⑨ 工单生成：汇总输出维修工单",
    "human_review": "⚠️ 转人工审核",
}


def _build_initial_state(user_input: str, image_base64: str = "", correlation_id: str = None) -> dict:
    if correlation_id is None:
        correlation_id = str(uuid.uuid4())[:8]

    image_description = ""
    image_warning = ""
    if image_base64:
        # 视觉调用发生在图执行之前，单独归因，不混进第一个节点
        get_token_tracker(correlation_id).start_node("image_extract")
        image_description = extract_image_info(image_base64, correlation_id=correlation_id)
        if not image_description:
            # 识别失败必须让用户看得见：他传了照片，却只按文字诊断，
            # 而界面上毫无提示 —— 他会以为照片被用上了。
            # 注意这**不是**降级：文字描述本身可能已经足够，诊断照常进行。
            image_warning = "图片未能识别，本轮仅依据文字描述诊断"
            logger.warning("image_extract_unavailable", correlation_id=correlation_id)

    return {
        "user_input": user_input,
        "image_description": image_description,
        "image_warning": image_warning,
        "fault_info": None,
        "followup_question": "",
        "evidence": "",
        "initial_evidence": "",
        "diagnosis": None,
        "review": None,
        "rebuttal": None,
        "final_review": None,
        "cost": None,
        "workorder": None,
        "debate_round": 0,
        "max_debate_rounds": settings.MAX_DEBATE_ROUNDS,
        "status": "start",
        "correlation_id": correlation_id
    }


def run_diagnosis(user_input: str, image_base64: str = "", correlation_id: str = None):
    """执行完整诊断流程"""
    correlation_id = correlation_id or str(uuid.uuid4())[:8]
    initial_state = _build_initial_state(user_input, image_base64, correlation_id)

    logger.info("diagnosis_start", correlation_id=correlation_id, input_len=len(user_input))

    try:
        result = app.invoke(initial_state, config={"recursion_limit": settings.GRAPH_RECURSION_LIMIT})

        # 收集 token 使用统计
        tracker = get_token_tracker(correlation_id)
        token_usage = tracker.get_summary()
        tracker.log_summary()

        # 保存 token 统计到结果
        result["token_usage"] = token_usage
        result["correlation_id"] = correlation_id

        logger.info("diagnosis_complete", status=result.get("status"), correlation_id=correlation_id)
        return result

    except Exception as e:
        # 图执行中断也必须产出交付物。此前这里只有 log + raise，
        # 用户烧完配额拿到的是 500，与"没有死胡同"的对外保证直接矛盾。
        logger.exception("diagnosis_aborted", correlation_id=correlation_id, error=type(e).__name__)
        result = build_abort_result(initial_state, type(e).__name__)
        tracker = get_token_tracker(correlation_id)
        result["token_usage"] = tracker.get_summary()
        logger.info("diagnosis_abort_workorder_emitted", status=result["status"],
                    correlation_id=correlation_id)
        return result
    finally:
        clear_token_tracker(correlation_id)


def run_diagnosis_stream(user_input: str, image_base64: str = "", correlation_id: str = None):
    """流式版：边执行边 yield (阶段说明, 当前累计状态)。
    调用方循环结束后，最后一个 state_snapshot 即最终结果。

    ⚠️ 每次 yield 的都是**浅拷贝**。此前一路 `merged.update(...)` 之后直接
    `yield (label, merged)`，所有快照指向同一个 dict —— SSE 端因为立刻序列化看不出问题，
    但任何"把每步快照收进列表"的消费方（回放、调试面板、离线评估）拿到的 N 份快照
    会**全部变成终态**，看起来像"每一步的结论都一样"。拷贝的代价可以忽略。
    """
    correlation_id = correlation_id or str(uuid.uuid4())[:8]
    initial_state = _build_initial_state(user_input, image_base64, correlation_id)
    merged = {k: v for k, v in initial_state.items()}

    logger.info("diagnosis_stream_start", correlation_id=correlation_id)

    yield ("🚀 启动多Agent协作诊断...", dict(merged))

    try:
        for update in app.stream(initial_state, config={"recursion_limit": settings.GRAPH_RECURSION_LIMIT}, stream_mode="updates"):
            if not update:
                continue
            node_name = next(iter(update))
            node_update = update[node_name]
            if node_update:
                merged.update(node_update)
            label = NODE_DESCRIPTIONS.get(node_name, f"执行节点: {node_name}")
            yield (label, dict(merged))

        # 最终收集 token 统计
        tracker = get_token_tracker(correlation_id)
        token_usage = tracker.get_summary()
        merged["token_usage"] = token_usage
        merged["correlation_id"] = correlation_id
        tracker.log_summary()

        logger.info("diagnosis_stream_complete", status=merged.get("status"), correlation_id=correlation_id)

    except GeneratorExit:
        # 客户端断连（SSE 场景下浏览器关标签页 / 主动 abort）。
        #
        # 严格说这个分支是**显式文档化**而非必需：GeneratorExit 继承自 BaseException，
        # 本来就不会被下面的 `except Exception` 捕获（已实测确认）。
        # 保留它是为了两件事：
        #   1. 让"断连 ≠ 失败"这个语义在代码里可读，不依赖读者记得 BaseException 层级；
        #   2. 万一将来有人把下面的 except 放宽到 BaseException，这里能先接住，
        #      避免走进"close() 之后还 yield"——那会让 Python 抛
        #      `RuntimeError: generator ignored GeneratorExit`，在 ASGI 层变成一个
        #      难查的 500，而不是一次正常的断连。
        logger.warning("diagnosis_stream_client_disconnected", correlation_id=correlation_id)
        raise
    except Exception as e:
        logger.exception("diagnosis_stream_aborted", correlation_id=correlation_id, error=type(e).__name__)
        abort_result = build_abort_result(merged, type(e).__name__)
        tracker = get_token_tracker(correlation_id)
        abort_result["token_usage"] = tracker.get_summary()
        # 把中断终态推给调用方，而不是 raise —— 前端才能以"降级完成"而非"连接断开"收场。
        yield ("⚠️ 诊断流程异常中断，已生成待人工处理工单", abort_result)
    finally:
        clear_token_tracker(correlation_id)


if __name__ == "__main__":
    fault = "那台数控机床主轴转起来一顿一顿的，还有怪声，温度也高得离谱"
    result = run_diagnosis(fault)

    print("=" * 60)
    print("最终状态：", result["status"])
    print("辩论轮数：", result.get("debate_round", 0))
    print("追踪 ID：", result.get("correlation_id"))
    print("=" * 60)

    if result.get("followup_question"):
        print("\n【追问】")
        print(result["followup_question"])

    if result.get("fault_info"):
        print("\n【提取的结构化信息】")
        print(json.dumps(result["fault_info"], ensure_ascii=False, indent=2))

    if result.get("diagnosis"):
        print("\n【诊断结果】")
        print(json.dumps(result["diagnosis"], ensure_ascii=False, indent=2))

    if result.get("workorder"):
        print("\n【最终工单】")
        print(json.dumps(result["workorder"], ensure_ascii=False, indent=2))

    if result.get("token_usage"):
        print("\n【Token 使用统计】")
        print(json.dumps(result["token_usage"], ensure_ascii=False, indent=2))