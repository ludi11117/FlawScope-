/**
 * 统计页的纯计算与格式化（`src/pages/statsUtils.ts`）。
 *
 * 与 `historyUtils.ts` 同样的理由：组件只负责渲染，
 * 换算与排序逻辑要能脱离 React 直接测（本项目没有 jsdom）。
 */

import type { Stats } from '../types/contracts'

/** 千分位分隔。Token 动辄上万，不分隔根本数不清位数。 */
export function formatCount(n: number): string {
  if (!Number.isFinite(n)) return '—'
  return Math.round(n).toLocaleString('en-US')
}

/**
 * 字节数 → 人类可读。
 *
 * 用 1024 而不是 1000：Windows 资源管理器也用 1024（显示为 KB/MB），
 * 跟用户在本机"右键属性"看到的大小对得上。对不上会让人以为数据错了。
 */
export function formatBytes(bytes: number | undefined): string {
  // undefined 与 0 必须区分：前者是"后端没给这个字段"，后者是"文件真是 0 字节"。
  // 混成一回事会让界面显示一个假的 "0 B"。
  if (bytes === undefined || !Number.isFinite(bytes)) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export interface StatusRow {
  status: string
  count: number
  /** 占比（0–100）。总数为 0 时是 0，不能是 NaN。 */
  percent: number
  /** 条形宽度百分比。与 percent 分开是为了给极小值一个可见下限。 */
  barPercent: number
}

/**
 * 把 by_status 摊平成用于渲染的行。
 *
 * 两个刻意的处理：
 *   - **按数量降序**。原 Streamlit 版 `st.bar_chart` 用的是字典序，
 *     读起来是"谁多谁少"要自己比；降序之后一眼能看出主次。
 *   - **极小值给可见下限**（`MIN_BAR_PERCENT`）。1 条记录 / 1000 条 = 0.1%，
 *     渲染出来是 0 像素宽的条，用户会以为"这一项没有数据"——
 *     而实际上有一条。宁可让条形比例稍微失真，也不能让存在的数据看起来不存在。
 */
const MIN_BAR_PERCENT = 1.5

export function statusRows(
  byStatus: Record<string, number> | undefined,
  total: number,
): StatusRow[] {
  const entries = Object.entries(byStatus ?? {})
  const rows: StatusRow[] = entries.map(([status, count]) => {
    // total 为 0 时不能做除法（0/0 = NaN，会一路污染到 style 里变成 "NaN%"）
    const percent = total > 0 ? (count / total) * 100 : 0
    return {
      status,
      count,
      percent,
      barPercent: count > 0 ? Math.max(percent, MIN_BAR_PERCENT) : 0,
    }
  })
  // 数量降序；同数量时按状态名排，避免顺序随机跳动
  rows.sort((a, b) => b.count - a.count || a.status.localeCompare(b.status))
  return rows
}

/** 占比文案，保留一位小数。用于条形右侧的 "62.5%"。 */
export function formatPercent(percent: number): string {
  if (!Number.isFinite(percent)) return '—'
  return `${percent.toFixed(1)}%`
}

/** 是否"完全没有数据"——用于决定显示空态还是图表。 */
export function isEmptyStats(stats: Stats | null): boolean {
  if (!stats) return true
  return stats.total_records === 0 && Object.keys(stats.by_status ?? {}).length === 0
}
