/**
 * 错误文案提取测试（A2）。
 *
 * 真实 bug：FastAPI 422 的 `detail` 是**数组**，旧代码直接把它赋给 error，
 * React 渲染数组里的对象时抛错，没有 ErrorBoundary → 整页白屏。
 * 这里的三条分别对应：string（业务错误）、数组（校验错误）、缺失（网关错误页）。
 */

import { describe, expect, it } from 'vitest'
import { extractDetail } from './errorDetail'

describe('extractDetail', () => {
  it('string：业务错误（HTTPException 的 detail）原样取出', () => {
    expect(extractDetail({ detail: '记录不存在' })).toBe('记录不存在')
  })

  it('数组：422 校验错误拼成可读文案，且结果是 string 而不是对象', () => {
    const body = {
      detail: [
        {
          type: 'string_too_long',
          loc: ['body', 'fault_description'],
          msg: 'String should have at most 4000 characters',
          input: 'x'.repeat(5000),
        },
      ],
    }
    const out = extractDetail(body)

    // 这两条是白屏的直接守门人：结果必须是字符串，才能被 React 当子节点渲染
    expect(typeof out).toBe('string')
    expect(out).toBe('fault_description: String should have at most 4000 characters')
    // loc 开头的 "body" 要去掉——用户不知道什么是 body
    expect(out).not.toContain('body')
  })

  it('数组：多条校验错误全部保留（只报第一条会让用户改一次错一次）', () => {
    const out = extractDetail({
      detail: [
        { loc: ['body', 'fault_description'], msg: '字段不能为空' },
        { loc: ['body', 'image_base64'], msg: '长度超限' },
      ],
    })
    expect(out).toBe('fault_description: 字段不能为空；image_base64: 长度超限')
  })

  it('数组元素是字符串时也能用', () => {
    expect(extractDetail({ detail: ['第一处', '第二处'] })).toBe('第一处；第二处')
  })

  it('缺失：没有 detail 时返回 null，让调用方保留自己的默认文案', () => {
    expect(extractDetail({})).toBeNull()
    expect(extractDetail({ message: 'Not Found' })).toBeNull()
    expect(extractDetail(null)).toBeNull()
    expect(extractDetail(undefined)).toBeNull()
    expect(extractDetail('plain text')).toBeNull()
  })

  it('数组里没有可读字段时返回 null，而不是拼出一串空串', () => {
    expect(extractDetail({ detail: [{ foo: 1 }, null] })).toBeNull()
  })

  it('空字符串 detail 视为没取到', () => {
    expect(extractDetail({ detail: '   ' })).toBeNull()
  })
})
