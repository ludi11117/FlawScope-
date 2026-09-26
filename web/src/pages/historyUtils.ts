/**
 * 历史页的翻页与格式化工具。
 *
 * 与后端/Streamlit 口径保持一致的两个关键点：
 *   1. `total` 必须来自 `/records` 的响应字段（后端用 count_records 独立 COUNT 查询），
 *      **不能**用 `records.length` —— 那是被 LIMIT 截断的当前页条数，
 *      库里有 500 条时也会显示"共 20 条"，用户不知道还有更多。
 *   2. 页码越界必须夹回有效范围。换了筛选条件后结果变少（原来第 5 页，
 *      现在总共只剩 1 页），不夹的话用户停在空列表上，看起来像"一条记录都没有"，
 *      而真实原因是"页码超了"。
 */

import { labelFor } from '../api/statusMeta'

export interface PageWindow {
  page: number
  offset: number
  pageCount: number
  /** 页码是否被夹取过（用于提示"已回到第 1 页"之类的语境） */
  clamped: boolean
}

/** 把 (总数, 页码, 每页条数) 归一化成页码 / 偏移 / 总页数。 */
export function pageWindow(total: number, page: number, pageSize: number): PageWindow {
  const size = Math.max(1, Math.floor(pageSize))
  const count = Math.max(1, Math.ceil(Math.max(0, total) / size))

  // 关键：先原样记下"调用方请求的页码"，再做夹取。
  // 不能写成 `const requested = Math.max(1, ...)` 再比 —— 那样下界已被提前抹平，
  // clamped 永远检不出 page < 1 的情况，前端就失去了"页码被修正过"这个信号。
  const rawPage = Math.floor(page)
  const requested = Number.isFinite(rawPage) ? rawPage : 1
  const clampedPage = Math.min(Math.max(1, requested), count)

  return {
    page: clampedPage,
    offset: (clampedPage - 1) * size,
    pageCount: count,
    clamped: clampedPage !== requested,
  }
}

/** 列表计数说明。total > shown 时必须体现"还有更多"。 */
export function historySummary(total: number, shown: number): string {
  if (total > shown) {
    return `共找到 ${total} 条记录，当前显示 ${shown} 条`
  }
  return `共找到 ${total} 条记录`
}

/** 状态 → 展示标签与配色。口径与后端状态机一致。 */
export const STATUS_META: Record<
  string,
  { label: string; color: string; bg: string }
> = {
  done: { label: '诊断完成', color: '#04342C', bg: '#E1F5EE' },
  need_more_info: { label: '需要补充信息', color: '#412402', bg: '#FAEEDA' },
  insufficient_knowledge: { label: '知识库无依据', color: '#412402', bg: '#FAEEDA' },
  llm_failed: { label: '模型服务失败', color: '#791F1F', bg: '#FCEBEB' },
  pending_human_review: { label: '转人工复核', color: '#791F1F', bg: '#FCEBEB' },
}

export function statusMeta(status: string) {
  const local =
    STATUS_META[status] ?? { label: status || '未知', color: '#2C2C2A', bg: '#F1EFE8' }
  // 标签优先用后端下发的（`GET /meta/statuses`，唯一来源见 status_meta.py），
  // 拿不到时用上面这份内置兜底——后端没起来时界面不能变成一堆英文状态名。
  // 配色是渲染细节，始终由前端掌握。
  return { ...local, label: labelFor(status, local.label) }
}

/** ISO 时间串截断到秒，避免时区后缀把表格撑开。 */
export function formatTime(raw?: string): string {
  if (!raw) return '-'
  return raw.slice(0, 19).replace('T', ' ')
}

/**
 * 导出为 CSV。
 *
 * 前缀从 `agentdiag_` 改为 `flawscope_`——项目已改名，这里之前是唯一漏改的地方。
 *
 * CSV 注入防护：以 = + - @ 开头的内容会被 Excel 当公式执行，
 * 因此在字段前加单引号。工单编号与故障描述都来自模型输出，不可信。
 */
export function recordsToCsv(records: Record<string, unknown>[]): string {
  if (records.length === 0) return ''

  const headers = ['ID', '时间', '状态', '故障描述', '工单编号', '根因', '预计成本', '追踪ID']

  const esc = (v: unknown): string => {
    let s = v === null || v === undefined ? '' : String(v)
    if (/^[=+\-@]/.test(s)) s = `'${s}`
    return `"${s.replace(/"/g, '""')}"`
  }

  const rows = records.map((r) => {
    const wo = (r.workorder ?? {}) as Record<string, unknown>
    const dg = (r.diagnosis ?? {}) as Record<string, unknown>
    const cost = (r.cost ?? {}) as Record<string, unknown>
    return [
      r.id,
      formatTime(r.created_at as string | undefined),
      r.status,
      r.fault_description,
      wo.工单编号 ?? '',
      dg.根因判断 ?? wo.根因 ?? '',
      cost.预计成本 ?? '',
      r.correlation_id ?? '',
    ]
      .map(esc)
      .join(',')
  })

  // BOM 让 Excel 正确识别 UTF-8，否则中文列名会乱码
  return '\uFEFF' + [headers.map(esc).join(','), ...rows].join('\n')
}

export function csvFilename(now = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `flawscope_records_${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}_${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}.csv`
}
