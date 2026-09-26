/**
 * 从后端错误响应体里取出**人能读的**错误文案。
 *
 * 为什么值得单独一个模块：FastAPI 的错误体有两种形状，前端此前只处理了一种。
 *   - 业务错误（我们主动 `raise HTTPException(detail="...")`）→ `detail` 是 **string**；
 *   - 请求校验失败（422）→ `detail` 是 **数组**，元素形如
 *     `{ type, loc: ["body","fault_description"], msg: "String should have at most 4000 characters", input }`。
 *
 * 旧代码直接 `detail = body.detail`，于是 422 时 `state.error` 变成数组，
 * 再被 React 当子节点渲染 → 抛错 → 没有 ErrorBoundary → 整页白屏。
 * 用户看到的不是"你输入太长了"，而是一片空白。
 *
 * 返回 `null` 表示"这个响应体里没有可用文案"，调用方保留自己的默认提示
 * ——不要在这里编造文案，那会把"没拿到原因"伪装成"拿到了原因"。
 */

/** 单条校验错误的形状（FastAPI / pydantic v2）。字段全部按可选处理：这是外部输入。 */
interface ValidationErrorItem {
  loc?: unknown
  msg?: unknown
}

/**
 * 把 `loc` 数组拼成可读路径。
 *
 * 去掉开头的 `"body"`：用户不知道什么是 body，看到 `body.fault_description`
 * 只会更困惑。剩下的部分（如 `fault_description`）才是他能对上号的字段名。
 */
function formatLoc(loc: unknown): string {
  if (!Array.isArray(loc)) return ''
  const parts = loc
    .filter((p) => typeof p === 'string' || typeof p === 'number')
    .map((p) => String(p))
  // 只丢掉**第一个** body：嵌套模型里可能还有 body
  if (parts[0] === 'body') parts.shift()
  return parts.join('.')
}

function formatItem(item: unknown): string {
  if (typeof item === 'string') return item
  if (!item || typeof item !== 'object') return ''

  const { loc, msg } = item as ValidationErrorItem
  const where = formatLoc(loc)
  const message = typeof msg === 'string' ? msg : ''
  if (where && message) return `${where}: ${message}`
  return where || message
}

/**
 * 提取可读错误文案。
 *
 * @returns 非空字符串表示取到了；`null` 表示响应体里没有可用的 detail。
 */
export function extractDetail(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null

  const detail = (body as { detail?: unknown }).detail

  if (typeof detail === 'string') {
    return detail.trim() ? detail : null
  }

  if (Array.isArray(detail)) {
    // 多条校验错误用中文分号连起来：一次请求可能同时缺字段又超长，
    // 只报第一条会让用户改一次、再错一次。
    const parts = detail.map(formatItem).filter((s) => s.length > 0)
    return parts.length > 0 ? parts.join('；') : null
  }

  return null
}
