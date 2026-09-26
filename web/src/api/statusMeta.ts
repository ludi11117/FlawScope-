/**
 * 状态标签的运行时来源。
 *
 * ## 为什么要有它
 *
 * 状态值是跨层契约：后端产生它，前端决定标签与配色。此前**三处各抄了一份**
 * 中文标签——`api.py`/`app.py` 一份、`pages/historyUtils.ts` 一份、
 * `components/ResultView.tsx` 一份。改一处漏一处就会出现"界面显示成未知状态"，
 * 而且不报错、只是悄悄误导。
 *
 * 后端已收敛到 `status_meta.py`（唯一来源），并经 `GET /meta/statuses` 暴露。
 * 这里在启动时拉一次，用后端给的 `label` 覆盖内置兜底。
 *
 * ## 分工
 *
 * - **后端**：语义来源（label / 是否终态 / 是否落库 / 是否可追问 / 级别）
 * - **前端**：颜色等渲染细节仍然只在前端；内置表作为**离线兜底**
 *   （后端没起来时界面不能变成一堆英文状态名）
 * - 拉取失败**静默忽略**：这只是一层文案优化，不该影响诊断主流程，
 *   更不该在顶栏多出一条"状态表加载失败"的噪音
 */

import { getStatusMeta } from './client'

/** 后端下发的单条状态元信息（字段与 `api.py :: /meta/statuses` 一一对应）。 */
export interface RemoteStatusMeta {
  label: string
  level: string
  terminal: boolean
  failure: boolean
  persisted: boolean
  followup: boolean
}

export interface StatusMetaPayload {
  version: string
  statuses: Record<string, RemoteStatusMeta>
}

/** 后端给的标签覆盖。只存 label —— 颜色由前端决定，后端不该管。 */
const overrides: Record<string, string> = {}

/**
 * 把后端返回的载荷合并进覆盖表，返回合并了多少条。
 *
 * 对形状做**防御性解析**而不是直接断言类型：这是网络来的数据，
 * 一个字段缺失就让整个页面崩掉是不划算的（有 ErrorBoundary 也不该这么用）。
 */
export function applyStatusMeta(payload: unknown): number {
  if (!payload || typeof payload !== 'object') return 0
  const statuses = (payload as { statuses?: unknown }).statuses
  if (!statuses || typeof statuses !== 'object') return 0

  let applied = 0
  for (const [name, meta] of Object.entries(statuses as Record<string, unknown>)) {
    if (!name || !meta || typeof meta !== 'object') continue
    const label = (meta as { label?: unknown }).label
    if (typeof label === 'string' && label.trim()) {
      overrides[name] = label
      applied += 1
    }
  }
  return applied
}

/** 取状态标签：后端给过就用后端的，否则用内置兜底。 */
export function labelFor(status: string, fallback: string): string {
  return overrides[status] ?? fallback
}

/** 拉一次状态表。失败静默（见文件头说明）。 */
export async function loadStatusMeta(): Promise<number> {
  try {
    return applyStatusMeta(await getStatusMeta())
  } catch {
    return 0
  }
}

/** 仅测试用：清掉覆盖，避免用例之间互相污染。 */
export function resetStatusMeta(): void {
  for (const key of Object.keys(overrides)) delete overrides[key]
}
