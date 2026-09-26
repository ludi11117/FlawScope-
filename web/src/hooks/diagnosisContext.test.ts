/**
 * 多轮追问上下文的测试（A1）。
 *
 * 这一条对应本项目最贵的一个前端 bug：上下文被当成「本轮输入」存进了 turns，
 * 下一轮又给整段套一层「第N轮：」，长度近似指数增长，三四轮就撞上后端
 * `fault_description` 的 4000 字上限。
 *
 * 判别式刻意写成「**长度线性增长** + **「第1轮：」只出现一次**」而不是
 * 「长度小于某个数」——后者在轮数少时对旧实现也成立，测不出退化。
 */

import { describe, expect, it } from 'vitest'
import {
  appendUserTurn,
  attachFollowup,
  buildContext,
  composePayload,
  MAX_CONTEXT_CHARS,
  MAX_CONTEXT_TURNS,
  MAX_PAYLOAD_CHARS,
  OMITTED_MARKER,
  type Turn,
} from './diagnosisContext'

/** 模拟真实的轮次推进：拼 payload → 只把原文存进历史。 */
function runRounds(texts: string[]): { payloads: string[]; turns: Turn[] } {
  let turns: Turn[] = []
  const payloads: string[] = []
  for (const text of texts) {
    payloads.push(composePayload(turns, text))
    turns = appendUserTurn(turns, text)
  }
  return { payloads, turns }
}

describe('多轮上下文不自我嵌套', () => {
  it('连续 4 轮长度线性增长，且「第1轮：」在整串里只出现一次', () => {
    const texts = [1, 2, 3, 4].map((n) => `第${n}次描述：主轴有异响`)
    const { payloads } = runRounds(texts)
    const lengths = payloads.map((p) => p.length)

    // 线性 = 相邻增量相等。旧实现下第 3 段的增量约是第 2 段的两倍（指数），
    // 这一条会直接失败。
    const d1 = lengths[2]! - lengths[1]!
    const d2 = lengths[3]! - lengths[2]!
    expect(d2, `长度序列 ${lengths.join(' → ')} 不是线性增长`).toBe(d1)

    // 再给一条更直白的上界：4 轮不该超过 2 轮的 3 倍
    expect(lengths[3]!).toBeLessThan(lengths[1]! * 3)

    // 嵌套的直接症状：旧实现里第 1 轮的整段（含「第1轮：」）会被塞进第 2 轮，
    // 再被塞进第 3 轮……于是「第1轮：」出现多次。
    const occurrences = (payloads[3]!.match(/第1轮：/g) ?? []).length
    expect(occurrences).toBe(1)
  })

  it('turns 里存的是用户原话，不是拼好的请求体', () => {
    const { turns, payloads } = runRounds(['第一轮说的', '第二轮说的'])
    expect(turns.map((t) => t.userInput)).toEqual(['第一轮说的', '第二轮说的'])
    // 请求体里带了上下文，历史里不该有
    expect(payloads[1]).toContain('第1轮：第一轮说的')
    expect(turns[1]!.userInput).not.toContain('第1轮：')
  })

  it('第一轮不加上下文前缀（没有历史就别加引导词）', () => {
    const { payloads } = runRounds(['只有一轮'])
    expect(payloads[0]).toBe('只有一轮')
  })
})

describe('buildContext 的两道闸', () => {
  it('轮数上限：只回带最近 N 轮，且保留全局轮次编号', () => {
    const turns: Turn[] = Array.from({ length: MAX_CONTEXT_TURNS + 2 }, (_, i) => ({
      userInput: `现象${i + 1}`,
    }))
    const ctx = buildContext(turns)

    expect(ctx).not.toContain('第1轮：')
    expect(ctx).not.toContain('第2轮：')
    // 最新的那一轮必须在（丢的是最老的，不是最新的）
    expect(ctx).toContain(`第${MAX_CONTEXT_TURNS + 2}轮：现象${MAX_CONTEXT_TURNS + 2}`)
  })

  it('字符上限：超预算时丢最老的轮次而不是最新的', () => {
    // 每轮 800 字，5 轮远超 3000 的预算
    const turns: Turn[] = Array.from({ length: 5 }, (_, i) => ({
      userInput: `第${i + 1}段` + 'x'.repeat(800),
    }))
    const ctx = buildContext(turns)

    expect(ctx.length).toBeLessThanOrEqual(MAX_CONTEXT_CHARS)
    expect(ctx).toContain('第5轮：')
    expect(ctx).not.toContain('第1轮：')
  })

  it('单轮就超预算时也保留内容并显式标注省略', () => {
    const ctx = buildContext([{ userInput: 'y'.repeat(MAX_CONTEXT_CHARS * 2) }])
    expect(ctx.startsWith(OMITTED_MARKER)).toBe(true)
    expect(ctx.length).toBeLessThanOrEqual(MAX_CONTEXT_CHARS)
  })

  it('空历史返回空串', () => {
    expect(buildContext([])).toBe('')
  })
})

describe('composePayload 不越过后端上限', () => {
  function longHistory(): Turn[] {
    let turns: Turn[] = []
    for (let i = 0; i < 6; i++) turns = appendUserTurn(turns, 'z'.repeat(700))
    return turns
  }

  it('长历史 + 短输入：上下文被闸住，整段仍不超 4000', () => {
    const payload = composePayload(longHistory(), '本轮的关键描述')
    expect(payload.length).toBeLessThanOrEqual(MAX_PAYLOAD_CHARS)
    // 短输入时上下文本身就已被 MAX_CONTEXT_CHARS 限制，不必再省略
    expect(payload.endsWith('本轮的关键描述')).toBe(true)
  })

  it('长历史 + 长输入：优先保本轮输入，历史尾部保留并标注省略', () => {
    // 输入本身接近上限，余量装不下整段上下文 —— 这是省略标记真正会出现的场景
    const input = 'w'.repeat(3500)
    const payload = composePayload(longHistory(), input)

    expect(payload.length).toBeLessThanOrEqual(MAX_PAYLOAD_CHARS)
    // 用户刚说的话不能被历史挤掉
    expect(payload.endsWith(input)).toBe(true)
    expect(payload).toContain(OMITTED_MARKER)
  })

  it('没有历史时原样返回输入', () => {
    expect(composePayload([], '一段描述')).toBe('一段描述')
  })

  it('追问是系统的输出，跟着上一轮一起回带', () => {
    const turns = attachFollowup([{ userInput: '设备有异响' }], '请问报警代码是多少？')
    const ctx = buildContext(turns)
    expect(ctx).toContain('（系统追问：请问报警代码是多少？）')
  })
})
