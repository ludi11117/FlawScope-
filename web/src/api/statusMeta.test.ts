/**
 * 状态标签的运行时来源测试（D7）。
 *
 * 守的是"后端加了一个状态、前端却显示成未知"这类**不报错、只误导**的漂移。
 * 后端侧另有一组集合比对（`tests/test_status_meta.py` 会读这两个前端源文件），
 * 两边合起来才算把口径钉住。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { applyStatusMeta, labelFor, resetStatusMeta } from './statusMeta'
import { statusMeta } from '../pages/historyUtils'

beforeEach(() => {
  resetStatusMeta()
})

describe('applyStatusMeta', () => {
  it('合并后端下发的标签', () => {
    const n = applyStatusMeta({
      version: '1.3.0',
      statuses: {
        done: { label: '诊断完成', level: 'success', terminal: true, failure: false, persisted: true, followup: false },
        llm_failed: { label: '模型服务失败', level: 'error', terminal: true, failure: true, persisted: true, followup: false },
      },
    })

    expect(n).toBe(2)
    expect(labelFor('done', '兜底')).toBe('诊断完成')
    expect(labelFor('llm_failed', '兜底')).toBe('模型服务失败')
  })

  it('缺字段/空标签的条目被跳过，不污染覆盖表', () => {
    const n = applyStatusMeta({
      statuses: {
        done: { label: '诊断完成' },
        broken: { level: 'error' },          // 没有 label
        blank: { label: '   ' },             // 空白标签
        nope: null,
      },
    })

    expect(n).toBe(1)
    expect(labelFor('broken', '兜底')).toBe('兜底')
    expect(labelFor('blank', '兜底')).toBe('兜底')
  })

  it('载荷形状不对时返回 0 且不抛异常', () => {
    // 网络来的数据，一个字段缺失不该让页面崩掉
    for (const bad of [null, undefined, 'text', 42, {}, { statuses: null }, { statuses: [] }]) {
      expect(applyStatusMeta(bad)).toBe(0)
    }
  })

  it('列表形状的 statuses 不被当成字典（数组也是 object）', () => {
    expect(applyStatusMeta({ statuses: ['done', 'llm_failed'] })).toBe(0)
  })
})

describe('labelFor 的兜底', () => {
  it('没有覆盖时用传入的兜底标签', () => {
    expect(labelFor('done', '诊断完成')).toBe('诊断完成')
    expect(labelFor('完全没见过的状态', '兜底')).toBe('兜底')
  })

  it('resetStatusMeta 清掉覆盖（避免用例互相污染）', () => {
    applyStatusMeta({ statuses: { done: { label: '覆盖过的' } } })
    expect(labelFor('done', '兜底')).toBe('覆盖过的')

    resetStatusMeta()
    expect(labelFor('done', '兜底')).toBe('兜底')
  })
})

describe('接线：列表/统计用的 statusMeta 会采用后端标签', () => {
  it('拿到后端标签后，历史页的标签跟着变', () => {
    // 改的是后端文案的场景：前端不该等到发版才跟上
    expect(statusMeta('done').label).toBe('诊断完成')

    applyStatusMeta({ statuses: { done: { label: '后端改过的文案' } } })

    expect(statusMeta('done').label).toBe('后端改过的文案')
  })

  it('颜色仍由前端掌握，不被后端覆盖', () => {
    const before = statusMeta('llm_failed')
    applyStatusMeta({ statuses: { llm_failed: { label: '模型服务失败' } } })
    const after = statusMeta('llm_failed')

    expect(after.label).toBe('模型服务失败')
    expect(after.bg).toBe(before.bg)
    expect(after.color).toBe(before.color)
  })

  it('未知状态仍回退成状态名本身', () => {
    expect(statusMeta('weird_state').label).toBe('weird_state')
  })
})
