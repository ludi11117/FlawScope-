/**
 * 多轮追问上下文的纯函数实现。
 *
 * 单独成文件的理由与 `api/health.ts` 一致：**没有 jsdom 也要能测**。
 * 这段逻辑曾经是本项目最贵的一个前端 bug —— 它同时承担「存历史」和「拼本轮输入」，
 * 于是第 N 轮的输入里嵌套了第 N-1 轮的整段上下文，长度近似指数增长，
 * 三到四轮就撞上后端 `fault_description` 的 4000 字上限（422），而前端又把
 * 422 的数组 detail 直接渲染，最终整页白屏。
 *
 * 现在的契约（也是本模块存在的意义）：
 *   1. `Turn.userInput` **只存用户原话**，绝不存拼好的上下文；
 *   2. 上下文在「要发请求」的那一刻临时组装，不进 state；
 *   3. 组装带两道闸——轮数上限与字符预算，保证长度**线性**增长。
 *
 * 为什么是线性而不是「尽量短」：多轮追问的价值就在于把前几轮的约束带回给模型。
 * 砍得太狠会让模型反复追问同一件事；砍得太松会撞上限。线性增长是这两者之间
 * 唯一稳定的形态——每多一轮只多一轮的成本。
 */

/** 一轮对话。`userInput` 必须是用户原话，不是组装后的请求体。 */
export interface Turn {
  userInput: string
  followupQuestion?: string
}

/** 上下文里最多回带的历史轮数。再多对诊断帮助很小，却让请求持续变长。 */
export const MAX_CONTEXT_TURNS = 5

/** 上下文文本的字符预算。留出余量给「本轮补充」与后端 4000 字上限。 */
export const MAX_CONTEXT_CHARS = 3000

/**
 * 组装后整段输入的字符上限，与后端 `api.py :: DiagnosisRequest.fault_description`
 * 的 `max_length=4000` 对齐。超过就是 422，用户白等一轮。
 */
export const MAX_PAYLOAD_CHARS = 4000

/** 本轮输入的引导词。后端把整段当自然语言读，这里只是让模型知道哪段是新信息。 */
export const CURRENT_TURN_LABEL = '【本轮补充】'

/** 预算不够时的省略提示。显式标注而不是静默丢字——用户要能看出历史被截过。 */
export const OMITTED_MARKER = '…（更早的轮次已省略）'

function formatTurn(turn: Turn, round: number): string {
  const head = `第${round}轮：${turn.userInput}`
  return turn.followupQuestion ? `${head}\n（系统追问：${turn.followupQuestion}）` : head
}

/**
 * 把历史轮次拼成一段上下文。
 *
 * 轮次编号用**全局序号**（`第N轮` 的 N 是它在完整历史里的位置），不是窗口内序号。
 * 这样被裁掉的窗口不会让同一段话同时叫「第1轮」，也让用户/日志能对上号。
 *
 * 从**最新**往回累积：预算不够时丢掉的必须是最老的轮次。反过来（从最老往前攒、
 * 超了截尾）会把最相关的最近一轮丢掉，那比不带上下文还糟。
 */
export function buildContext(turns: Turn[]): string {
  const window = turns.slice(-MAX_CONTEXT_TURNS)
  const roundOffset = turns.length - window.length
  const kept: string[] = []
  let used = 0

  for (let i = window.length - 1; i >= 0; i--) {
    const turn = window[i]
    if (!turn) continue
    const block = formatTurn(turn, roundOffset + i + 1)
    // +1 是 join('\n') 的分隔符；第一块没有分隔符
    const cost = block.length + (kept.length ? 1 : 0)
    // 至少保留一轮：即使单轮就超预算也要带出去，否则上下文成空串，
    // 用户会以为系统把之前说的话全忘了。
    if (kept.length > 0 && used + cost > MAX_CONTEXT_CHARS) break
    kept.unshift(block)
    used += cost
  }

  const text = kept.join('\n')
  if (text.length <= MAX_CONTEXT_CHARS) return text

  // 单轮就超预算：保留尾部（最新内容），砍掉开头并显式标注。
  const keep = Math.max(0, MAX_CONTEXT_CHARS - OMITTED_MARKER.length - 1)
  return `${OMITTED_MARKER}\n${text.slice(text.length - keep)}`
}

/**
 * 组装本轮真正发给后端的 `fault_description`。
 *
 * 两道约束同时满足：
 *   - 整段不超过 `MAX_PAYLOAD_CHARS`（否则后端 422）；
 *   - 超限时优先保**本轮补充**，再保最近的上下文——用户刚说的话不能被历史挤掉。
 */
export function composePayload(turns: Turn[], userInput: string): string {
  const ctx = buildContext(turns)

  // 第一轮没有历史，原样发送：不给模型加无意义的引导词
  if (!ctx) {
    return userInput.length <= MAX_PAYLOAD_CHARS ? userInput : userInput.slice(0, MAX_PAYLOAD_CHARS)
  }

  const suffix = `\n\n${CURRENT_TURN_LABEL}${userInput}`
  const room = MAX_PAYLOAD_CHARS - suffix.length
  if (ctx.length <= room) return ctx + suffix

  // 上下文挤不下：保尾部（离本轮最近），前面加省略标记。
  const marker = `${OMITTED_MARKER}\n`
  const keep = Math.max(0, room - marker.length)
  if (keep === 0) {
    // 连标记都放不下，说明本轮输入本身就接近上限：保本轮、丢全部历史。
    return suffix.length <= MAX_PAYLOAD_CHARS
      ? suffix
      : suffix.slice(suffix.length - MAX_PAYLOAD_CHARS)
  }
  return marker + ctx.slice(ctx.length - keep) + suffix
}

/**
 * 记录一轮用户输入。
 *
 * ⚠️ **只存原文**。这里曾经存的是「本轮实际发给后端的那段 payload」，
 * 于是下一轮组装上下文时又把整段 payload 套进 `第N轮：` 里，长度指数膨胀。
 * 这个函数是那个 bug 的守门人：测试 `diagnosisContext.test.ts` 断言连续多轮的
 * 长度线性增长，把它改回存 payload 就会失败。
 */
export function appendUserTurn(turns: Turn[], userInput: string): Turn[] {
  return [...turns, { userInput }]
}

/** 给最后一轮补上系统追问（追问是系统的输出，不是用户说的话）。 */
export function attachFollowup(turns: Turn[], question: string): Turn[] {
  if (!question || turns.length === 0) return turns
  const last = turns.length - 1
  return turns.map((t, i) => (i === last ? { ...t, followupQuestion: question } : t))
}
