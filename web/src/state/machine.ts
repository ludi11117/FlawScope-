/**
 * 诊断状态机的节点定义与流转映射。
 *
 * 这是前端的"知识"部分：把 orchestrator.py 里的图结构在这里镜像一份，
 * 用于把后端推来的 label / status 映射到"当前走到哪个节点"。
 *
 * 为什么不用后端推的 label 直接显示：
 *   后端 label 是给人看的文案（`① 信息抽取：把口语化描述转成结构化字段`），
 *   前端需要的是**稳态的节点标识**才能做"已走过/进行中/未开始"的状态渲染。
 *   两者混用会导致改文案就改前端。
 *
 * 契约来源：orchestrator.py 的 NODE_DESCRIPTIONS 与路由函数。
 * 改后端图结构时这里要同步——工具函数 `describeNodes()` 的测试会兜住漂移。
 */

export type NodeId =
  | 'extract_info'
  | 'check_info'
  | 'retrieve'
  | 'diagnose'
  | 'review'
  | 'rebuttal'
  | 'final_review'
  | 'cost'
  | 'workorder'
  | 'human_review'

export interface NodeMeta {
  id: NodeId
  /** 序号文案，与后端 label 的前缀对应 */
  index: string
  title: string
  /** 一句话说明这个节点在干什么——用于 hover 提示 */
  detail: string
  /** 是否属于辩论环（可多轮），UI 上单独标识 */
  inDebateLoop?: boolean
  /**
   * 只在特定分支才走的节点（目前只有转人工）。
   *
   * 它们必须出现在 NODES 里，否则转人工时 `computeNodeStates` 找不到这个节点，
   * 状态机视图上**没有任何节点是 active** —— 用户看到的是"卡住了"，
   * 而实际上系统正在等他做人工复核。
   *
   * 但也不能算进"进度 N/M"的分母：正常链路永远走不到它，
   * 算进去会让进度条最高只到 9/10 = 90%，看起来像没跑完。
   * 分母用 `PRIMARY_NODES`（见下）。
   */
  conditional?: boolean
}

export const NODES: NodeMeta[] = [
  {
    id: 'extract_info',
    index: '①',
    title: '信息抽取',
    detail: '把口语化描述转成结构化字段（设备类型 / 报警代码 / 故障现象）',
  },
  {
    id: 'check_info',
    index: '②',
    title: '信息校验',
    detail: '信息不足则生成追问，不猜测、不带病往下走',
  },
  {
    id: 'retrieve',
    index: '③',
    title: '混合检索',
    detail: 'BM25 关键词 + 向量语义，用 RRF 按排名融合',
  },
  {
    id: 'diagnose',
    index: '④',
    title: '初步诊断',
    detail: '基于检索证据给出根因候选与排查建议',
  },
  {
    id: 'review',
    index: '⑤',
    title: '审核',
    detail: '对照证据核查：越界 / 遗漏 / 排除 / 依据强度',
    inDebateLoop: true,
  },
  {
    id: 'rebuttal',
    index: '⑥',
    title: '辩论反驳',
    detail: '针对争议点补检索、给反驳理由，但不丢候选根因',
    inDebateLoop: true,
  },
  {
    id: 'final_review',
    index: '⑦',
    title: '最终复审',
    detail: '对辩论结果做终裁，输出置信度',
    inDebateLoop: true,
  },
  {
    id: 'cost',
    index: '⑧',
    title: '成本核算',
    detail: '模型只提备件与工时，价格由确定性函数算',
  },
  {
    id: 'workorder',
    index: '⑨',
    title: '工单生成',
    detail: '任何失败或降级都产出带风险标记的工单，没有死胡同',
  },
]

/**
 * 转人工复核。只在审核判定结论不可自动采信时才走。
 *
 * index 用 `!` 而不是 `⚠️`：后端那条中断提示（"⚠️ 诊断流程异常中断，…"）
 * 同样以 `⚠️` 开头，拿它当匹配键会把"异常中断"误判成"转人工"。
 * 所以这个节点不做前缀匹配，由 `nodeFromLabel` 里的 `includes('人工')` 兜。
 */
const HUMAN_REVIEW: NodeMeta = {
  id: 'human_review',
  index: '!',
  title: '转人工复核',
  detail: '审核判定结论不可自动采信，工单已标注请勿直接执行',
  conditional: true,
}

// 把转人工并进 NODES：状态机视图要能高亮它（见 NodeMeta.conditional 的说明）
NODES.push(HUMAN_REVIEW)

/** 正常链路会走到的节点。进度分母用它——转人工不该把 100% 拉低到 90%。 */
export const PRIMARY_NODES: NodeMeta[] = NODES.filter((n) => !n.conditional)

export function nodeById(id: NodeId): NodeMeta {
  return NODES.find((n) => n.id === id) ?? HUMAN_REVIEW
}

/**
 * 从后端推来的 label 反查节点。
 *
 * 后端 label 形如 `① 信息抽取：把口语化描述转成结构化字段`，
 * 我们用「序号」做匹配键而不是标题文案——序号比文案稳定得多，
 * 文案改了不会让前端高亮错位。
 */
export function nodeFromLabel(label: string): NodeMeta | null {
  const match = NODES.find((n) => label.startsWith(n.index))
  if (match) return match
  if (label.includes('中断') || label.includes('异常')) return null
  if (label.includes('人工')) return HUMAN_REVIEW
  return null
}

/** 是否已经进入辩论环（出现 review 之后）。用于判断"辩论环是否被走到"。 */
export function enteredDebateLoop(visited: Set<NodeId>): boolean {
  return visited.has('review')
}

/**
 * 归一化：把已访问节点集合转成每个节点的渲染状态。
 *
 * 关键点：节点可能被访问多次（辩论环），但这里只表达"走过没走过"。
 * 轮次信息由 debate_round 单独承载，不要在这里表达——否则一次辩论
 * 会让已有节点的状态在"完成/进行中"之间反复横跳，进度条看起来在倒退。
 */
export type NodeState = 'pending' | 'active' | 'done'

export function computeNodeStates(
  visited: Set<NodeId>,
  active: NodeId | null,
): Map<NodeId, NodeState> {
  const result = new Map<NodeId, NodeState>()
  for (const node of NODES) {
    if (node.id === active) {
      result.set(node.id, 'active')
    } else if (visited.has(node.id)) {
      result.set(node.id, 'done')
    } else {
      result.set(node.id, 'pending')
    }
  }
  return result
}
