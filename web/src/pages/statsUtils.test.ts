/**
 * 统计页纯逻辑（`src/pages/statsUtils.ts`）。
 *
 * 按项目惯例：每条"该放行的"配一条"该拦下的"，
 * 且每条断言都要是**只有正确实现才满足**的——
 * 不然就是"测了个寂寞"（见 MEMORY I-34）。
 */

import { describe, expect, it } from 'vitest'
import type { Stats } from '../types/contracts'
import {
  formatBytes,
  formatCount,
  formatPercent,
  isEmptyStats,
  statusRows,
} from './statsUtils'

const stats = (over: Partial<Stats> = {}): Stats => ({
  total_records: 0,
  by_status: {},
  avg_debate_rounds: 0,
  total_tokens: 0,
  ...over,
})

describe('formatCount', () => {
  it('大数加千分位', () => {
    expect(formatCount(1234567)).toBe('1,234,567')
  })

  it('小数不显示（统计口径是整数条数）', () => {
    expect(formatCount(3.7)).toBe('4')
  })

  it('非有限值显示占位而不是 NaN', () => {
    expect(formatCount(NaN)).toBe('—')
    expect(formatCount(Infinity)).toBe('—')
  })
})

describe('formatBytes', () => {
  it('小于 1KB 用 B', () => {
    expect(formatBytes(512)).toBe('512 B')
  })

  it('KB 与 MB 用 1024 进制（和 Windows 资源管理器一致）', () => {
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })

  it('**undefined 与 0 必须区分**——前者是"没统计到"', () => {
    // 判别式：若实现写成 `bytes || 0` 或 `?? 0`，
    // 缺字段会显示成 "0 B"，等于把一个"未知"显示成了"零字节"。
    expect(formatBytes(undefined)).toBe('—')
    expect(formatBytes(0)).toBe('0 B')
  })

  it('边界：1023 仍是 B，1024 进 KB', () => {
    expect(formatBytes(1023)).toBe('1023 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
  })
})

describe('statusRows', () => {
  it('按数量降序（字典序看不出主次）', () => {
    const rows = statusRows({ done: 3, llm_failed: 10, pending_human_review: 5 }, 18)
    expect(rows.map((r) => r.status)).toEqual(['llm_failed', 'pending_human_review', 'done'])
  })

  it('占比按总数计算', () => {
    const rows = statusRows({ done: 3, failed: 1 }, 4)
    const done = rows.find((r) => r.status === 'done')
    expect(done?.percent).toBe(75)
  })

  it('总数为 0 时占比是 0，不能是 NaN', () => {
    // NaN 会一路进到 style 里变成 "width: NaN%"，浏览器直接忽略，
    // 条形消失且没有任何报错——最难受的一类 bug。
    const rows = statusRows({ done: 0 }, 0)
    expect(rows[0]?.percent).toBe(0)
    expect(Number.isNaN(rows[0]?.percent)).toBe(false)
  })

  it('非零但极小的占比要有可见下限，不能是 0 宽', () => {
    // 判别式：1/1000 = 0.1% → 像素宽度约等于 0，
    // 用户会以为这一项"没有数据"。所以必须给下限。
    //
    // ⚠ 断言必须写成 `barPercent > percent`（下限**真的被抬高**了），
    // 不能只写 `barPercent > 0` —— 后者在 MIN_BAR_PERCENT 被改成 0 时
    // 依然成立（真实占比 0.1 本身就 > 0），等于测了个寂寞。
    // 这条踩过一次：改常量做退化验证时 21 项全绿，什么都没验到。
    const rows = statusRows({ rare: 1 }, 1000)
    const rare = rows.find((r) => r.status === 'rare')
    expect(rare?.percent).toBeCloseTo(0.1, 5) // 真实占比仍然准确
    expect(rare!.barPercent).toBeGreaterThan(rare!.percent) // 下限确实生效
  })

  it('数量为 0 的状态不给下限——它确实没有数据', () => {
    // 与上面一条对偶：别把"没有"也画出来。
    const rows = statusRows({ none: 0, some: 5 }, 5)
    expect(rows.find((r) => r.status === 'none')?.barPercent).toBe(0)
  })

  it('空对象返回空数组而不是抛异常', () => {
    expect(statusRows({}, 0)).toEqual([])
  })

  it('by_status 缺失（undefined）时不崩', () => {
    expect(statusRows(undefined as unknown as Record<string, number>, 0)).toEqual([])
  })

  it('同数量时按状态名稳定排序，避免每次渲染顺序跳动', () => {
    const rows = statusRows({ b: 2, a: 2, c: 2 }, 6)
    expect(rows.map((r) => r.status)).toEqual(['a', 'b', 'c'])
  })
})

describe('formatPercent', () => {
  it('保留一位小数并带百分号', () => {
    expect(formatPercent(62.5)).toBe('62.5%')
  })

  it('非有限值显示占位', () => {
    expect(formatPercent(NaN)).toBe('—')
  })
})

describe('isEmptyStats', () => {
  it('null 视为空', () => {
    expect(isEmptyStats(null)).toBe(true)
  })

  it('有记录就不算空', () => {
    expect(isEmptyStats(stats({ total_records: 3, by_status: { done: 3 } }))).toBe(false)
  })

  it('对偶：total_records 为 0 但 by_status 有内容时**不能**判为空', () => {
    // 后端这两个字段是独立聚合的（COUNT(*) 与 GROUP BY），
    // 万一不一致，至少要把 by_status 里的数据显示出来，
    // 判成空态就等于把仅有的信息也藏了。
    expect(isEmptyStats(stats({ total_records: 0, by_status: { done: 2 } }))).toBe(false)
  })

  it('两者都空才算空', () => {
    expect(isEmptyStats(stats())).toBe(true)
  })
})
