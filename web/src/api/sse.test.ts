/**
 * SSE 解析测试。
 *
 * 这是前端最值得测的一块：本地开发时消息通常一个 chunk 就到齐，
 * 错误实现看不出问题；一上真实网络，TCP 分片会让事件随机丢失或截断。
 * 下面专门构造"切在任意位置"的场景。
 */

import { describe, expect, it } from 'vitest'
import { parseSSEBlock, parseSSEStream, splitSSEBuffer } from './sse'

describe('parseSSEBlock', () => {
  it('解析标准事件', () => {
    const ev = parseSSEBlock('event: progress\ndata: {"label":"① 信息抽取"}')
    expect(ev).toEqual({ event: 'progress', data: '{"label":"① 信息抽取"}' })
  })

  it('忽略以冒号开头的注释/心跳', () => {
    // 不少反代会定时发 `:` 保活，不忽略的话会被当成异常事件
    const ev = parseSSEBlock(': keep-alive\nevent: done\ndata: {"correlation_id":"abc"}')
    expect(ev?.event).toBe('done')
  })

  it('多行 data 按行拼接而不是覆盖', () => {
    const ev = parseSSEBlock('event: x\ndata: line1\ndata: line2')
    expect(ev?.data).toBe('line1\nline2')
  })

  it('冒号后仅去掉一个前导空格（协议规定），多余空格保留', () => {
    const ev = parseSSEBlock('event: y\ndata:  two-spaces')
    expect(ev?.data).toBe(' two-spaces')
  })

  it('缺 event 名时返回 null', () => {
    expect(parseSSEBlock('data: {"a":1}')).toBeNull()
  })

  it('缺 data 时返回 null', () => {
    expect(parseSSEBlock('event: progress')).toBeNull()
  })

  it('空块返回 null', () => {
    expect(parseSSEBlock('')).toBeNull()
    expect(parseSSEBlock('   \n  ')).toBeNull()
  })

  it('容忍 CRLF 行尾（某些代理会改写换行符）', () => {
    const ev = parseSSEBlock('event: progress\r\ndata: {"a":1}\r\n')
    expect(ev).toEqual({ event: 'progress', data: '{"a":1}' })
  })

  it('反向: 值与字段名之间无冒号的行不应被当成事件', () => {
    expect(parseSSEBlock('event progress\ndata {"a":1}')).toBeNull()
  })
})

describe('splitSSEBuffer —— 跨 chunk 分片', () => {
  it('完整消息全部切出，无残留', () => {
    const { blocks, rest } = splitSSEBuffer('event: a\ndata: 1\n\nevent: b\ndata: 2\n\n')
    expect(blocks.length).toBe(2)
    expect(rest).toBe('')
  })

  it('末段不完整时留在 rest 里等下一个 chunk', () => {
    const { blocks, rest } = splitSSEBuffer('event: a\ndata: 1\n\nevent: b\ndata: 2')
    expect(blocks.length).toBe(1)
    expect(rest).toBe('event: b\ndata: 2')
  })

  it('消息被切成两半后再拼接，能还原出完整事件', () => {
    // 模拟 TCP 把一条消息切在 JSON 中间
    const full = 'event: result\ndata: {"status":"done","correlation_id":"abc"}\n\n'

    for (let cut = 1; cut < full.length; cut++) {
      const first = full.slice(0, cut)
      const second = full.slice(cut)

      let buffer = first
      let { blocks, rest } = splitSSEBuffer(buffer)
      const collected: string[] = [...blocks]

      buffer = rest + second
      const r2 = splitSSEBuffer(buffer)
      collected.push(...r2.blocks)

      const evs = collected.map(parseSSEBlock).filter(Boolean)
      expect(evs.length, `切在第 ${cut} 位时丢失了事件`).toBe(1)
      expect(evs[0]!.data).toBe('{"status":"done","correlation_id":"abc"}')
    }
  })

  it('一个 chunk 含多条消息时全部切出', () => {
    const { blocks } = splitSSEBuffer(
      'event: progress\ndata: {"i":1}\n\nevent: progress\ndata: {"i":2}\n\nevent: progress\ndata: {"i":3}\n\n',
    )
    expect(blocks.length).toBe(3)
  })

  it('逐字节喂入也不丢事件（最严苛的分片）', () => {
    const full =
      'event: progress\ndata: {"label":"①"}\n\nevent: result\ndata: {"status":"done"}\n\nevent: done\ndata: {}\n\n'
    let buffer = ''
    const events: string[] = []

    for (const ch of full) {
      buffer += ch
      const { blocks, rest } = splitSSEBuffer(buffer)
      buffer = rest
      for (const b of blocks) {
        const ev = parseSSEBlock(b)
        if (ev) events.push(ev.event)
      }
    }

    expect(events).toEqual(['progress', 'result', 'done'])
  })

  it('CRLF 换行也能切出完整消息（某些代理会改写换行符）', () => {
    // 旧实现只按 '\n\n' 切：CRLF 流里一条消息都切不出来，整条流解析不出事件，
    // 前端表现为"进度一直不动、最后什么都没有"。
    // 而同一文件的 parseSSEBlock 早就专门处理了行尾 CR —— 属于自相矛盾。
    const crlf =
      'event: progress\r\ndata: {"i":1}\r\n\r\nevent: result\r\ndata: {"status":"done"}\r\n\r\n'

    const { blocks, rest } = splitSSEBuffer(crlf)

    expect(blocks.length).toBe(2)
    expect(rest).toBe('')
    expect(blocks.map((b) => parseSSEBlock(b)?.event)).toEqual(['progress', 'result'])
  })

  it('CRLF 与 LF 混用的流也能解析（代理只改写一部分换行）', () => {
    const mixed =
      'event: progress\r\ndata: {"i":1}\n\nevent: done\ndata: {}\r\n\r\n'

    const events = parseSSEStream(mixed)

    expect(events.map((e) => e.event)).toEqual(['progress', 'done'])
  })

  it('逐字节喂入 CRLF 流同样不丢事件', () => {
    const full =
      'event: progress\r\ndata: {"i":1}\r\n\r\nevent: done\r\ndata: {}\r\n\r\n'
    let buffer = ''
    const events: string[] = []

    for (const ch of full) {
      buffer += ch
      const { blocks, rest } = splitSSEBuffer(buffer)
      buffer = rest
      for (const b of blocks) {
        const ev = parseSSEBlock(b)
        if (ev) events.push(ev.event)
      }
    }

    expect(events).toEqual(['progress', 'done'])
  })
})

describe('parseSSEStream', () => {
  it('解析后端真实格式的三条事件', () => {
    const text =
      'event: progress\ndata: {"label":"🚀 启动多Agent协作诊断...","status":"start","debate_round":0}\n\n' +
      'event: result\ndata: {"status":"done","workorder":{"工单编号":"WO-1"}}\n\n' +
      'event: done\ndata: {"correlation_id":"abc123"}\n\n'

    const evs = parseSSEStream(text)
    expect(evs.map((e) => e.event)).toEqual(['progress', 'result', 'done'])
    expect(JSON.parse(evs[1]!.data).workorder.工单编号).toBe('WO-1')
  })

  it('中文与 emoji 不被破坏（ensure_ascii=False 的服务端输出）', () => {
    const evs = parseSSEStream('event: progress\ndata: {"label":"① 信息抽取：把口语化描述转成结构化字段"}\n\n')
    expect(JSON.parse(evs[0]!.data).label).toContain('信息抽取')
  })

  it('夹杂心跳注释时仍能正确解析', () => {
    const evs = parseSSEStream(': ping\n\nevent: done\ndata: {"correlation_id":"x"}\n\n')
    expect(evs.length).toBe(1)
    expect(evs[0]!.event).toBe('done')
  })

  it('空输入返回空数组', () => {
    expect(parseSSEStream('')).toEqual([])
  })
})
