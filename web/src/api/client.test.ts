/**
 * SSE 客户端测试（A5 + A2 的流式分支）。
 *
 * 三个真实症状：
 *   1. 代理返回 HTML 200（错误页）时，旧代码照样按事件流读，用户只看到"诊断中"；
 *   2. 流被提前关闭、从未给出 result 时，旧代码 `break` 之后什么都不做，
 *      reducer 的 running 永远是 true → 界面僵死；
 *   3. 422 的 detail 是数组，旧代码把它当字符串赋给 error → 渲染时抛错。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  diagnoseStream,
  STREAM_INCOMPLETE_MESSAGE,
  type StreamHandlers,
} from './client'

function sseResponse(chunks: string[], contentType = 'text/event-stream'): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'content-type': contentType } })
}

function htmlResponse(): Response {
  return new Response('<html><body>502 Bad Gateway</body></html>', {
    status: 200,
    headers: { 'content-type': 'text/html; charset=utf-8' },
  })
}

/** 等到条件成立或超时。diagnoseStream 是"发射后不管"的，只能轮询。 */
async function waitFor(predicate: () => boolean, label: string): Promise<void> {
  const deadline = Date.now() + 2000
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`等待超时：${label}`)
    await new Promise((r) => setTimeout(r, 1))
  }
}

function collect() {
  const errors: string[] = []
  const results: unknown[] = []
  const dones: string[] = []
  const handlers: StreamHandlers = {
    onError: (m) => errors.push(m),
    onResult: (r) => results.push(r),
    onDone: (c) => dones.push(c),
  }
  return { errors, results, dones, handlers }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('diagnoseStream —— Content-Type 校验', () => {
  it('返回 text/html 时立即报错，不按事件流解析', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => htmlResponse()))
    const { errors, handlers } = collect()

    diagnoseStream({ fault_description: 'x' }, handlers)
    await waitFor(() => errors.length > 0, 'onError 被调用')

    expect(errors[0]).toContain('不是事件流')
    expect(errors[0]).toContain('text/html')
  })
})

describe('diagnoseStream —— 流提前关闭', () => {
  it('只收到 progress、没有 result 时兜底报错（否则 running 永远为 true）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse([
          'event: progress\ndata: {"label":"① 信息抽取","status":"start","debate_round":0}\n\n',
        ]),
      ),
    )
    const { errors, results, handlers } = collect()

    diagnoseStream({ fault_description: 'x' }, handlers)
    await waitFor(() => errors.length > 0, 'onError 被调用')

    expect(results.length).toBe(0)
    expect(errors).toEqual([STREAM_INCOMPLETE_MESSAGE])
  })

  it('收到 result 后不再报错', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse([
          'event: progress\ndata: {"label":"① 信息抽取","status":"start","debate_round":0}\n\n',
          'event: result\ndata: {"status":"done","workorder":{"工单编号":"WO-1"},"correlation_id":"abc"}\n\n',
          'event: done\ndata: {"correlation_id":"abc"}\n\n',
        ]),
      ),
    )
    const { errors, results, dones, handlers } = collect()

    diagnoseStream({ fault_description: 'x' }, handlers)
    await waitFor(() => dones.length > 0, 'done 事件被处理')

    expect(errors).toEqual([])
    expect(results.length).toBe(1)
    expect(dones).toEqual(['abc'])
  })

  it('服务端发 error 事件时不追加"未收到结果"的兜底报错', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse(['event: error\ndata: {"detail":"诊断失败: 模型超时"}\n\n']),
      ),
    )
    const { errors, handlers } = collect()

    diagnoseStream({ fault_description: 'x' }, handlers)
    await waitFor(() => errors.length > 0, 'onError 被调用')

    expect(errors).toEqual(['诊断失败: 模型超时'])
  })
})

describe('diagnoseStream —— 非 2xx 的 detail 提取', () => {
  it('422 的数组 detail 被拼成字符串（旧代码会把它当字符串赋值 → 渲染白屏）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              detail: [
                {
                  type: 'string_too_long',
                  loc: ['body', 'fault_description'],
                  msg: 'String should have at most 4000 characters',
                },
              ],
            }),
            { status: 422, headers: { 'content-type': 'application/json' } },
          ),
      ),
    )
    const { errors, handlers } = collect()

    diagnoseStream({ fault_description: 'x' }, handlers)
    await waitFor(() => errors.length > 0, 'onError 被调用')

    expect(typeof errors[0]).toBe('string')
    expect(errors[0]).toContain('fault_description')
    expect(errors[0]).toContain('at most 4000')
  })
})
