/**
 * SSE（Server-Sent Events）解析。
 *
 * 从 client.ts 抽出来单独成文件的两个理由：
 *   1. **可测**。这是前端最有含量也最容易写错的一段，必须是纯函数才好断言；
 *   2. **职责清晰**。网络传输（fetch / AbortController）与协议解析是两件事，
 *      混在一个文件里会让人以为改解析要动到网络层。
 *
 * 最容易踩的坑（也是本模块存在的主要理由）：
 *   **一个 SSE 消息可能被 TCP 切成多个 chunk，一个 chunk 里也可能含多条消息。**
 *   按 chunk 直接解析是最常见的错误实现——本地开发（消息大、延迟低）通常看不出问题，
 *   一上真实网络就随机丢事件。正确做法是在循环外累积 buffer、以空行分隔。
 */

export interface SSEEvent {
  event: string
  data: string
}

/**
 * 从 buffer 中切出完整的 SSE 消息，返回 [完整消息, 剩余 buffer]。
 *
 * 未以空行结尾的最后一段会被留在 buffer 里等下一个 chunk —— 那可能是
 * 被切断的半条消息，提前解析会得到残缺 JSON。
 */
export function splitSSEBuffer(buffer: string): {
  blocks: string[]
  rest: string
} {
  // 用正则而不是 `split('\n\n')`：某些反向代理会把换行改写成 CRLF，
  // 此时 `'\n\n'` 切不出任何完整消息 —— 整条流解析不出事件，
  // 前端表现为"进度一直不动、最后什么都没有"。
  // 同一文件的 `parseSSEBlock` 早就专门 `replace(/\r$/, '')` 处理了行尾 CR，
  // 只有这里没跟上，属于自相矛盾。
  const parts = buffer.split(/\r?\n\r?\n/)
  const rest = parts.pop() ?? ''
  return { blocks: parts, rest }
}

/**
 * 解析单个 SSE 消息块。
 *
 * 按规范处理三件事：
 *   - `data:` 可出现多次，需**按行拼接**而不是覆盖（后端目前每条只发一行，
 *     但规范允许分行，写死"只取第一行"将来会炸）；
 *   - 冒号后的**一个**前导空格要去掉（协议规定），多余空格保留；
 *   - 以 `:` 开头的是注释/心跳，必须忽略——不少反代会定时发它保活。
 *
 * 返回 null 表示这个块不含有效事件（空块、纯注释、缺 event 或 data）。
 */
export function parseSSEBlock(block: string): SSEEvent | null {
  let eventName = ''
  const dataLines: string[] = []

  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (!line) continue
    if (line.startsWith(':')) continue

    const colon = line.indexOf(':')
    if (colon === -1) continue

    const field = line.slice(0, colon)
    let value = line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'event') {
      eventName = value
    } else if (field === 'data') {
      dataLines.push(value)
    }
  }

  if (!eventName || dataLines.length === 0) return null
  return { event: eventName, data: dataLines.join('\n') }
}

/**
 * 把 SSE 流的原始文本喂进来，产出事件。
 *
 * 用法：维护一个 `buffer`，每次读到 chunk 就 `buffer += chunk`，
 * 然后调用本函数的"增量版"（见 splitSSEBuffer）。
 * 这里提供的是"一次性处理整段文本"的便利形式，主要用于测试与调试。
 */
export function parseSSEStream(text: string): SSEEvent[] {
  const { blocks } = splitSSEBuffer(text)
  const out: SSEEvent[] = []
  for (const b of blocks) {
    const ev = parseSSEBlock(b)
    if (ev) out.push(ev)
  }
  return out
}
