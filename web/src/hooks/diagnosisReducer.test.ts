/**
 * reducer 的行为测试（A2 的状态层 + A5 的收敛语义）。
 *
 * 为什么直接测 reducer 而不是渲染 hook：本仓库不装 jsdom，
 * 而这里要守的恰恰是「状态里存的东西形状对不对」——那正是 reducer 的职责。
 * 测真实 reducer 而不是重抄一遍逻辑，退化时才会真的红。
 */

import { describe, expect, it } from 'vitest'
import { initialState, reducer } from './useDiagnosisStream'
import type { DiagnosisResult } from '../types/contracts'

const baseResult: DiagnosisResult = {
  status: 'done',
  followup_question: '',
  diagnosis: {},
  review: {},
  rebuttal: {},
  final_review: {},
  cost: {},
  workorder: {},
  debate_round: 0,
  correlation_id: 'abc',
  token_usage: {},
}

describe('error 一定是字符串', () => {
  it('error action 写入的 message 是字符串，渲染时不会抛错', () => {
    // 这条对应「422 的 detail 是数组 → 整页白屏」。
    // 提取层负责把数组拼成字符串（见 api/errorDetail.test.ts），
    // 这里守住"进了 state 的就必须是字符串"这层契约。
    const state = reducer(initialState, { type: 'error', message: '字段超长' })
    expect(typeof state.error).toBe('string')
    expect(state.running).toBe(false)
  })
})

describe('done 让 running 收敛', () => {
  it('只收到 done（result 丢了）也要结束 running', () => {
    const running = reducer(initialState, { type: 'start', userInput: '设备异响' })
    expect(running.running).toBe(true)

    const done = reducer(running, { type: 'done', correlationId: 'abc' })
    expect(done.running).toBe(false)
    expect(done.active).toBeNull()
    expect(done.correlationId).toBe('abc')
  })
})

describe('turns 的推进', () => {
  it('start 只追加用户原话', () => {
    const s1 = reducer(initialState, { type: 'start', userInput: '第一轮' })
    const s2 = reducer(s1, { type: 'start', userInput: '第二轮' })
    expect(s2.turns.map((t) => t.userInput)).toEqual(['第一轮', '第二轮'])
  })

  it('result 里的 followup_question 挂到最后一轮上', () => {
    const s1 = reducer(initialState, { type: 'start', userInput: '设备异响' })
    const s2 = reducer(s1, {
      type: 'result',
      payload: { ...baseResult, status: 'need_more_info', followup_question: '报警代码是多少？' },
    })
    expect(s2.turns[0]!.followupQuestion).toBe('报警代码是多少？')
    expect(s2.running).toBe(false)
  })

  it('reset 清掉结果但保留对话历史（清空重来不该失忆）', () => {
    const s1 = reducer(initialState, { type: 'start', userInput: '第一轮' })
    const s2 = reducer(s1, { type: 'result', payload: baseResult })
    const s3 = reducer(s2, { type: 'reset' })
    expect(s3.result).toBeNull()
    expect(s3.turns.map((t) => t.userInput)).toEqual(['第一轮'])
  })
})
