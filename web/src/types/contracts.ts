/**
 * 与后端 Pydantic 模型一一对应的类型定义。
 *
 * 为什么手写而不是自动生成：
 *   后端用的是中文键名（`根因判断` / `工单编号` …），这些是**跨层契约**——
 *   orchestrator / agents / app.py / 前端四处都靠它对齐。手写一份显式映射，
 *   配合下面每个类型的"契约来源"注释，改后端时能一眼找出前端要同步的地方。
 *   自动生成工具（openapi-typescript）会把中文键原样搬过来，但丢掉语义，
 *   反而看不出哪个字段是"可能缺失的降级字段"。
 *
 * 契约来源标注规则：
 *   agents.py 的 Pydantic 模型 → 结构稳定，字段名以它为准
 *   orchestrator.py 的降级分支 → 字段可能整体缺失，必须按可选处理
 */

/** 诊断终态。与 orchestrator.TERMINAL_FAILURE_STATUSES 保持一致。 */
export const TERMINAL_FAILURE_STATUSES = [
  'insufficient_knowledge',
  'llm_failed',
  'pending_human_review',
] as const

export type TerminalFailureStatus = (typeof TERMINAL_FAILURE_STATUSES)[number]

/** 全部可能出现的过程态与终态。 */
export type DiagnosisStatus =
  | 'start'
  | 'extracted'
  | 'need_more_info'
  | 'retrieved'
  | 'diagnosed'
  | 'reviewed'
  | 'rebutted'
  | 'final_reviewed'
  | 'costed'
  | 'done'
  | TerminalFailureStatus
  | 'unknown'

export function isTerminalFailure(status: DiagnosisStatus): boolean {
  return (TERMINAL_FAILURE_STATUSES as readonly string[]).includes(status)
}

/** 契约来源：agents.py :: DiagnosisOutput */
export interface Diagnosis {
  报警代码: string
  根因判断: string
  依据: string
  排查建议: string[]
}

/** 契约来源：agents.py :: ReviewOutput */
export interface Review {
  审核意见: string
  理由: string
  风险提示?: string
}

/** 契约来源：agents.py :: FinalReviewOutput（比 ReviewOutput 多一个置信度） */
export interface FinalReview extends Review {
  置信度?: number
}

/** 契约来源：agents.py :: RebuttalOutput */
export interface Rebuttal {
  行动?: string
  最终根因?: string
  依据?: string
  置信度?: number
  反驳理由?: string
}

/**
 * 契约来源：agents.py :: calculate_cost() 的返回结构。
 *
 * 注意 预计工时 / 预计成本 在降级时是字符串 "N/A" 而不是数字——
 * 这不是类型设计失误，是刻意的：前端要能区分"没算"和"算出来是 0"。
 */
export interface Cost {
  备件清单?: string[]
  预计工时?: string | number
  预计成本?: string | number
  成本明细?: Record<string, number>
  计费提示?: string
}

/**
 * 契约来源：agents.py :: WorkOrderOutput
 *
 * 降级工单**只有** 工单编号 / 风险等级 / 风险说明 三项——
 * 这是刻意设计（没有依据的字段就不渲染），因此这里全部声明为可选，
 * 并在 UI 侧按"缺什么就不显示什么"处理，不要给缺失字段补占位文案。
 */
export interface WorkOrder {
  工单编号?: string
  故障现象?: string
  根因?: string
  维修方案?: string
  备件清单?: string[]
  预计成本?: string
  安全注意事项?: string
  风险等级?: string
  风险说明?: string
}

/** 契约来源：agents.py :: CostExtractOutput 抽取的结构化故障信息 */
export interface FaultInfo {
  设备类型?: string
  设备型号?: string
  报警代码?: string
  故障现象?: string
  [key: string]: unknown
}

/** 契约来源：agents.py 的 TokenTracker.get_summary() */
export interface TokenUsage {
  总Token?: number
  总耗时?: number
  按节点?: Record<string, { token?: number; 耗时?: number }>
  [key: string]: unknown
}

/** 契约来源：orchestrator.py :: run_diagnosis() 的返回 / api.py :: DiagnosisResponse */
export interface DiagnosisResult {
  status: DiagnosisStatus
  followup_question: string
  /**
   * 这四个字段在降级轮次里是 **`{}`**（空对象）而不是缺失，所以不能简单标成
   * `Diagnosis | undefined`——那样读取 `dg.根因判断` 会被 TS 判定为
   * "property does not exist on type '{}'"。
   * 用 `Partial<...>` 才是对真实形状的准确描述：字段可能存在，也可能整体缺席，
   * 调用方必须自行兜底（UI 侧的做法是"没有就当作空"，不是补占位文案）。
   */
  diagnosis: Partial<Diagnosis>
  review: Partial<Review>
  rebuttal: Partial<Rebuttal>
  final_review: Partial<FinalReview>
  cost: Cost
  workorder: WorkOrder
  debate_round: number
  correlation_id: string
  token_usage: TokenUsage
  /**
   * 图片没被识别出来时的可见提示（空串 = 无需提示）。
   * 契约来源：orchestrator.py :: `_build_initial_state` 的 `image_warning`。
   *
   * 视觉调用失败此前是静默的：流程照跑，用户不知道照片没被用上——
   * "没识别出来"和"没传照片"在界面上长得一模一样。
   */
  image_warning?: string
  /**
   * 落库后的记录 id，前端拿它拼工单下载地址。
   * 同步 /diagnose 与 SSE 的 result 事件都会带（口径已对齐）；
   * 追问轮次不落库、或落库失败时为 null。
   */
  record_id?: number | null
}

/** 契约来源：api.py :: DiagnosisRequest */
export interface DiagnosisRequest {
  fault_description: string
  image_base64?: string
  correlation_id?: string
}

/** SSE 事件类型。与 api.py :: /diagnose/stream 的 event 名一一对应。 */
export type SSEEventName = 'progress' | 'result' | 'done' | 'error'

/** progress 事件：每次节点推进推一条，只带轻量状态，不带完整结果。 */
export interface ProgressEvent {
  label: string
  status: string
  debate_round: number
  has_diagnosis: boolean
  has_workorder: boolean
  correlation_id: string
}

export interface DoneEvent {
  correlation_id: string
}

export interface ErrorEvent {
  detail: string
  correlation_id: string
}

/** 契约来源：api.py :: RecordResponse */
export interface RecordListResponse {
  total: number
  offset: number
  limit: number
  statuses: string[]
  records: DiagnosisRecord[]
}

/**
 * 契约来源：database.py :: get_records()（`SELECT *`，字段即表列名）+ 反向代理到
 * api.py :: RecordResponse.records。
 *
 * 注意字段名是 **`fault_description`** 而不是 `user_input`——
 * 表列名与 `api.py :: DiagnosisRequest` 同名，但不叫 `user_input`。
 * JSON 类列（diagnosis/cost/workorder/...）在 database._parse_json_columns() 里
 * 已反解析为 dict，降级轮次可能整体缺失或为空对象，因此一律可选。
 */
export interface DiagnosisRecord {
  id: number
  /** 表列名，非 `user_input`。历史页展示的"故障描述"就是它。 */
  fault_description: string
  status: string
  created_at?: string
  correlation_id?: string
  diagnosis?: Diagnosis
  review?: Review
  rebuttal?: Rebuttal
  final_review?: FinalReview
  cost?: Cost
  workorder?: WorkOrder
  debate_round?: number
  token_usage?: TokenUsage
  total_tokens?: number
}

/**
 * 契约来源：database.py :: get_stats()
 *
 * ⚠️ 此处曾长期与实际返回**不一致**——旧版写的是 `total` / `已解决` /
 * `需人工复核` / `平均Token`，是照着 Streamlit 统计页的猜想写的，
 * 而 `get_stats()` 实际返回的是 `total_records` / `by_status` /
 * `avg_debate_rounds` / `total_tokens`，四个字段名全对不上。
 * 由于 `getStats()` 当时没有任何调用点，这个错一直没暴露（类型检查也查不出
 * ——它只查声明内部是否自洽，不查后端到底返回什么）。
 * 接统计页时以 `database.py :: get_stats()` 的 `return` 字面量为准。
 */
export interface Stats {
  total_records: number
  /** 状态 → 条数。键是状态机的跨层契约值（done / llm_failed / …）。 */
  by_status: Record<string, number>
  /** 仅统计 debate_round > 0 的记录，空表时为 0。 */
  avg_debate_rounds: number
  total_tokens: number
  /**
   * 数据库文件字节数。由后端顺带返回（前端不该知道 DB 路径）。
   * 早期后端没有这个字段，读不到时按缺失处理而不是当 0——
   * "0 字节"和"没统计到"是两回事，界面上不能显示成 0 KB。
   */
  db_size_bytes?: number
}
