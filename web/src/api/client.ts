/**
 * 后端 API 客户端。
 *
 * 全部请求走 /api 前缀，由 Vite 代理（开发）或 nginx（生产）转发到 FastAPI。
 * 前端代码里不出现后端主机名，环境差异全部收敛在代理配置里。
 */

import type {
  DiagnosisRequest,
  DiagnosisResult,
  ProgressEvent,
  RecordListResponse,
  DiagnosisRecord,
  Stats,
} from '../types/contracts'
import type { HealthPayload, ReadyPayload } from './health'
import { splitSSEBuffer, parseSSEBlock } from './sse'

const BASE = '/api'

/**
 * 带上 API Key（若配置了）。
 * 后端 API_KEY 留空时完全放行，所以本地开发不需要设这个变量。
 */
function authHeaders(): Record<string, string> {
  const key = import.meta.env.VITE_API_KEY
  return key ? { 'X-API-Key': key } : {}
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly retryAfter?: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** 503 + Retry-After 表示"系统忙"，与真正的失败要区分开——前者重试有用。 */
  get isOverloaded(): boolean {
    return this.status === 503
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  })

  if (!resp.ok) {
    // 503 的 Retry-After 是后端并发闸门给的，要带出来让 UI 能提示"X 秒后重试"
    const retryAfter = resp.headers.get('Retry-After')
    let detail = `请求失败（HTTP ${resp.status}）`
    try {
      const body = await resp.json()
      if (body?.detail) detail = body.detail
    } catch {
      // 响应体不是 JSON，保留默认文案
    }
    throw new ApiError(detail, resp.status, retryAfter ? Number(retryAfter) : undefined)
  }

  return (await resp.json()) as T
}

export function diagnoseSync(payload: DiagnosisRequest): Promise<DiagnosisResult> {
  return request<DiagnosisResult>('/diagnose', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function listRecords(params: {
  keyword?: string
  status?: string
  limit?: number
  offset?: number
}): Promise<RecordListResponse> {
  const qs = new URLSearchParams()
  if (params.keyword) qs.set('keyword', params.keyword)
  if (params.status) qs.set('status', params.status)
  qs.set('limit', String(params.limit ?? 20))
  qs.set('offset', String(params.offset ?? 0))
  return request<RecordListResponse>(`/records?${qs}`)
}

export function getRecord(id: number): Promise<DiagnosisRecord> {
  return request<DiagnosisRecord>(`/records/${id}`)
}

export function deleteRecord(id: number): Promise<{ message: string }> {
  return request<{ message: string }>(`/records/${id}`, { method: 'DELETE' })
}

export function getStats(): Promise<Stats> {
  return request<Stats>('/stats')
}

/**
 * 存活探针：只确认后端进程还在，不触碰任何外部依赖，毫秒级返回。
 *
 * ⚠️ **它不能用来判断"能不能开始诊断"**：启动初始化期间它同样是 200。
 * 判断可用性要用 `getHealthReady()`。
 *
 * 顶栏状态灯用它做"进程还在吗"的兜底。**不要换成 `/health`**——那个会真去探
 * LLM / Embedding / ChromaDB，每 5 秒调一次等于持续烧配额。
 */
export function getHealthLive(): Promise<HealthPayload> {
  return request<HealthPayload>('/health/live')
}

/**
 * 就绪探针：初始化是否完成、可以承接诊断请求。
 *
 * 与 `getHealthLive()` 的分工见 `api/health.ts` 的模块注释。
 * 这里**不用 `request()`**：未就绪时后端返回 503，而 `request()` 会把非 2xx
 * 一律抛成 ApiError，于是"正在启动"这个正常状态会被当成异常。
 * 我们要的是原始状态码 + body，所以直接 fetch。
 *
 * 返回 `null` 表示网络层失败（后端根本没起）。
 */
export async function getHealthReady(): Promise<{
  status: number
  body: ReadyPayload | null
} | null> {
  try {
    const resp = await fetch(`${BASE}/health/ready`, { headers: authHeaders() })
    let body: ReadyPayload | null = null
    try {
      body = (await resp.json()) as ReadyPayload
    } catch {
      // body 不是 JSON（例如 nginx 返回了 HTML 错误页），保留 null
    }
    return { status: resp.status, body }
  } catch {
    return null
  }
}

/** 工单 Markdown 的下载地址（由浏览器直接打开，不走 fetch）。 */
export function workorderUrl(id: number): string {
  return `${BASE}/records/${id}/workorder.md`
}

export interface StreamHandlers {
  onProgress?: (e: ProgressEvent) => void
  onResult?: (r: DiagnosisResult) => void
  onDone?: (correlationId: string) => void
  onError?: (message: string) => void
}

/**
 * 流式诊断：消费 /diagnose/stream 的 SSE。
 *
 * 为什么不用浏览器的 EventSource：
 *   1. EventSource 只支持 GET，而 /diagnose/stream 是 POST（故障描述可能很长，
 *      还带 base64 图片，塞进 URL 既不现实也会被代理的 URL 长度限制挡掉）；
 *   2. EventSource 无法自定义请求头，带不了 X-API-Key；
 *   3. EventSource 断线后会自动重连——对诊断这种"重连就重跑一遍、每次烧 9 次
 *      LLM 调用"的场景，自动重连是有害的。
 * 所以用 fetch + ReadableStream 手写解析。这也是"必须自己实现"的地方，
 * 而不是库能用就上的地方。
 *
 * @returns abort 函数，调用即中断请求（同时触发后端生成器 close，
 *          后端已对 GeneratorExit 做了放行，不会继续跑完剩余节点）
 */
export function diagnoseStream(
  payload: DiagnosisRequest,
  handlers: StreamHandlers,
): () => void {
  const controller = new AbortController()

  void (async () => {
    let resp: Response
    try {
      resp = await fetch(`${BASE}/diagnose/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          ...authHeaders(),
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      })
    } catch (err) {
      if ((err as Error).name === 'AbortError') return
      handlers.onError?.(`无法连接后端：${(err as Error).message}`)
      return
    }

    if (!resp.ok) {
      const retryAfter = resp.headers.get('Retry-After')
      let detail = `请求失败（HTTP ${resp.status}）`
      try {
        const body = await resp.json()
        if (body?.detail) detail = body.detail
      } catch {
        /* 保留默认文案 */
      }
      if (retryAfter) detail += `（建议 ${retryAfter} 秒后重试）`
      handlers.onError?.(detail)
      return
    }

    if (!resp.body) {
      handlers.onError?.('后端未返回响应流')
      return
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    // 必须在循环外累积 buffer：一个 SSE 消息可能被 TCP 切成多个 chunk，
    // 也可能一个 chunk 里含多条消息。按 chunk 直接解析是最常见的 SSE 实现错误。
    let buffer = ''

    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // 用可测的纯函数切分：一个消息可能跨 chunk，一个 chunk 也可能含多条消息。
        // 最后一段可能被切断，留在 buffer 里等下一个 chunk（见 splitSSEBuffer 的说明）。
        const { blocks, rest } = splitSSEBuffer(buffer)
        buffer = rest
        for (const block of blocks) {
          dispatchSSEBlock(block, handlers)
        }
      }

      // 流结束后 buffer 里若还有残留（后端未以空行结尾），补处理一次
      if (buffer.trim()) dispatchSSEBlock(buffer, handlers)
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        handlers.onError?.(`读取响应流失败：${(err as Error).message}`)
      }
    }
  })()

  return () => controller.abort()
}

function dispatchSSEBlock(block: string, handlers: StreamHandlers): void {
  const parsed = parseSSEBlock(block)
  if (!parsed) return

  let payload: unknown
  try {
    payload = JSON.parse(parsed.data)
  } catch {
    // 单条消息解析失败不应终止整个流，但必须让调用方知道
    handlers.onError?.(`无法解析服务端事件（${parsed.event}）`)
    return
  }

  switch (parsed.event) {
    case 'progress':
      handlers.onProgress?.(payload as ProgressEvent)
      break
    case 'result':
      handlers.onResult?.(payload as DiagnosisResult)
      break
    case 'done':
      handlers.onDone?.((payload as { correlation_id: string }).correlation_id)
      break
    case 'error':
      handlers.onError?.((payload as { detail: string }).detail)
      break
    default:
      break
  }
}
