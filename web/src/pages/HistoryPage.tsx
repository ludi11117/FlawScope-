/**
 * 诊断历史页。
 *
 * 相比 Streamlit 版的两处实质改进：
 *   1. **列表用表格而不是逐条 st.json**。原版每展开一条就渲染 7 个 st.json 块，
 *      记录一多整个页面卡住；这里改成紧凑表格 + 按需展开详情。
 *   2. 查询条件变化时自动回第 1 页（原版靠 session_state 的签名字段，逻辑分散）。
 *
 * 保留的能力：关键词搜索、状态筛选、每页条数、翻页（含越界夹取）、
 * 单条展开、CSV 导出、工单 Markdown 下载。搜索做了防抖，避免每敲一个字都打后端。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { listRecords, deleteRecord, workorderUrl, ApiError } from '../api/client'
import type { Diagnosis, DiagnosisRecord, RecordListResponse, WorkOrder } from '../types/contracts'
import { btnStyle } from '../ui/button'
import {
  pageWindow,
  historySummary,
  statusMeta,
  formatTime,
  recordsToCsv,
  csvFilename,
} from './historyUtils'

const PAGE_SIZE_OPTIONS = [20, 50, 100, 200]

export function HistoryPage() {
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [status, setStatus] = useState('')
  const [pageSize, setPageSize] = useState(20)
  const [page, setPage] = useState(1)
  const [data, setData] = useState<RecordListResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<number | null>(null)
  const debounceRef = useRef<number | null>(null)
  // 在途请求的取消器。翻页/改筛选会立刻发新请求，旧请求如果不取消，
  // 它**先回来**时会把新结果覆盖掉（网络快慢不由我们决定）——
  // 表现是"点了下一页，列表闪一下又跳回上一页的内容"。
  const abortRef = useRef<AbortController | null>(null)
  // 请求序号兜底：abort 是"尽力而为"的（请求可能已经在返程路上），
  // 序号则能确定性地丢弃过期响应。
  const seqRef = useRef(0)

  // 搜索防抖：每敲一个字就打一次后端，既浪费也会让输入感觉卡顿
  useEffect(() => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    debounceRef.current = window.setTimeout(() => {
      setKeyword(keywordInput.trim())
      setPage(1) // 换关键词必须回第 1 页，否则停在第 3 页大概率是空的
    }, 300)
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [keywordInput])

  const total = data?.total ?? 0
  const records = data?.records ?? []
  const win = useMemo(() => pageWindow(total, page, pageSize), [total, page, pageSize])

  // `win` 的实时副本：`load` 需要读夹取后的 offset，但不能把 `win` 放进它的依赖数组
  // ——`win` 依赖 `data.total`，而 `load` 会 setData，依赖成环后每次响应都会
  // 触发新一轮请求（无限循环）。用 ref 读最新值既拿到 offset，又断开这条环。
  const winRef = useRef(win)
  winRef.current = win

  const load = useCallback(async () => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const seq = ++seqRef.current

    setLoading(true)
    setError(null)
    try {
      const resp = await listRecords(
        {
          keyword,
          status,
          limit: pageSize,
          // 用**夹取后**的 offset，而不是 (page-1)*pageSize。
          // 页码越界时（例如删掉了本页最后一条）前者一次就取到正确的页，
          // 后者会先发一个注定为空的请求、再靠下面的 effect 纠正后重发。
          offset: winRef.current.offset,
        },
        controller.signal,
      )
      if (seq !== seqRef.current) return // 过期响应，丢弃
      setData(resp)
    } catch (e) {
      if ((e as Error).name === 'AbortError') return
      if (seq !== seqRef.current) return
      setError(e instanceof ApiError ? e.message : `加载失败：${(e as Error).message}`)
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
  }, [keyword, status, pageSize, page])

  useEffect(() => {
    void load()
    // 卸载时中断在途请求：否则切页后还会继续打后端（与 SSE 那条是同一类问题）
    return () => abortRef.current?.abort()
  }, [load])

  // 后端返回的 total 可能让当前页码越界（例如删掉了本页最后一条），
  // 检测到就夹回去——不夹的话用户会停在空列表上，误以为"没有记录"。
  useEffect(() => {
    if (win.clamped && win.page !== page) setPage(win.page)
  }, [win.clamped, win.page, page])

  const onStatusChange = useCallback((v: string) => {
    setStatus(v)
    setPage(1)
  }, [])

  const onPageSizeChange = useCallback((v: number) => {
    setPageSize(v)
    setPage(1)
  }, [])

  const onExportCsv = useCallback(() => {
    const csv = recordsToCsv(records as unknown as Record<string, unknown>[])
    if (!csv) return
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = csvFilename()
    a.click()
    URL.revokeObjectURL(url)
  }, [records])

  const onDelete = useCallback(
    async (id: number) => {
      if (!window.confirm(`确定删除记录 #${id}？此操作不可撤销。`)) return
      try {
        await deleteRecord(id)
        if (expanded === id) setExpanded(null)
        await load()
      } catch (e) {
        setError(e instanceof ApiError ? e.message : `删除失败：${(e as Error).message}`)
      }
    },
    [expanded, load],
  )

  const statuses = data?.statuses ?? []

  // 状态分布概览：只统计后端返回的状态枚举，不额外请求
  const distribution = useMemo(() => {
    const map = new Map<string, number>()
    for (const r of records) map.set(r.status, (map.get(r.status) ?? 0) + 1)
    return map
  }, [records])

  return (
    <div style={{ maxWidth: 1120, margin: '0 auto', padding: '32px 24px 60px' }}>
      <header style={{ marginBottom: 20 }}>
        <h1 style={{ fontSize: 21, fontWeight: 600, margin: 0, letterSpacing: -0.3 }}>
          诊断历史
        </h1>
        <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', margin: '6px 0 0' }}>
          每次诊断的结果、工单与追踪 ID 都落库在这里，可搜索、筛选、导出。
        </p>
      </header>

      {/* 统计概览 */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <StatCard label="记录总数" value={total} accent />
        <StatCard label="本页状态种类" value={distribution.size} />
        <StatCard label="当前页" value={`${win.page} / ${win.pageCount}`} />
      </div>

      {/* 筛选栏 */}
      <div
        className="fs-card"
        style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap', padding: 13 }}
      >
        <input
          value={keywordInput}
          onChange={(e) => setKeywordInput(e.target.value)}
          placeholder="搜索故障描述关键词，如：液压、E-203、主轴"
          style={{
            flex: '1 1 260px',
            padding: '8px 12px',
            fontSize: 13,
            borderRadius: 'var(--radius-md)',
            border: '1px solid var(--color-border-secondary)',
            background: 'var(--color-background-primary)',
            color: 'var(--color-text-primary)',
            fontFamily: 'inherit',
            outline: 'none',
          }}
        />
        <select value={status} onChange={(e) => onStatusChange(e.target.value)} style={selectStyle}>
          <option value="">全部状态</option>
          {statuses.map((s) => (
            <option key={s} value={s}>
              {statusMeta(s).label}
            </option>
          ))}
        </select>
        <select
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value))}
          style={selectStyle}
        >
          {PAGE_SIZE_OPTIONS.map((n) => (
            <option key={n} value={n}>
              每页 {n} 条
            </option>
          ))}
        </select>
        <button
          onClick={onExportCsv}
          disabled={records.length === 0}
          style={btnStyle(false, records.length === 0)}
        >
          导出当前页 CSV
        </button>
      </div>

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          fontSize: 12,
          color: 'var(--color-text-secondary)',
          marginBottom: 10,
        }}
      >
        {loading ? (
          <>
            <span className="fs-spin" /> 加载中…
          </>
        ) : (
          <>
            {historySummary(total, records.length)}
            {win.pageCount > 1 && ` · 第 ${win.page} / ${win.pageCount} 页`}
          </>
        )}
      </div>

      {error && (
        <div
          className="fs-banner"
          style={{
            padding: '10px 13px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--danger-soft)',
            border: '0.5px solid var(--danger)',
            color: '#791F1F',
            fontSize: 13,
            marginBottom: 12,
          }}
        >
          {error}
        </div>
      )}

      {/* 列表 */}
      {records.length > 0 ? (
        <div
          style={{
            background: 'var(--color-background-primary)',
            border: '1px solid var(--color-border-tertiary)',
            borderRadius: 'var(--radius-lg)',
            overflow: 'hidden',
            boxShadow: 'var(--shadow-sm)',
          }}
        >
          {/* 表头：桌面端提供列语义，窄屏自动换行 */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 12,
              padding: '8px 13px',
              background: 'var(--color-background-tertiary)',
              borderBottom: '1px solid var(--color-border-tertiary)',
              fontSize: 11.5,
              fontWeight: 600,
              color: 'var(--color-text-secondary)',
              letterSpacing: 0.3,
            }}
          >
            <span style={{ width: 34 }}>ID</span>
            <span style={{ width: 132 }}>时间</span>
            <span style={{ width: 84 }}>状态</span>
            <span style={{ flex: 1 }}>故障描述</span>
            <span style={{ width: 16 }} />
          </div>
          {records.map((r, i) => (
            <RecordRow
              key={r.id}
              record={r}
              isOpen={expanded === r.id}
              isLast={i === records.length - 1}
              onToggle={() => setExpanded(expanded === r.id ? null : r.id)}
              onDelete={() => void onDelete(r.id)}
            />
          ))}
        </div>
      ) : (
        !loading && (
          <div
            style={{
              padding: 40,
              textAlign: 'center',
              fontSize: 13,
              color: 'var(--color-text-secondary)',
              background: 'var(--color-background-primary)',
              border: '1px dashed var(--color-border-secondary)',
              borderRadius: 'var(--radius-lg)',
            }}
          >
            <div style={{ fontSize: 26, opacity: 0.3, marginBottom: 8 }} aria-hidden>
              ▤
            </div>
            {total > 0
              ? '当前页没有记录，请回到第 1 页查看。'
              : '没有符合条件的记录。先进行一次诊断，记录会自动保存。'}
          </div>
        )
      )}

      {/* 翻页 */}
      {win.pageCount > 1 && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 10,
            marginTop: 18,
          }}
        >
          <button onClick={() => setPage(1)} disabled={win.page <= 1} style={btnStyle(false, win.page <= 1)}>
            首页
          </button>
          <button
            onClick={() => setPage(win.page - 1)}
            disabled={win.page <= 1}
            style={btnStyle(false, win.page <= 1)}
          >
            上一页
          </button>
          <span
            style={{
              fontSize: 13,
              color: 'var(--color-text-secondary)',
              minWidth: 96,
              textAlign: 'center',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            第 {win.page} / {win.pageCount} 页
          </span>
          <button
            onClick={() => setPage(win.page + 1)}
            disabled={win.page >= win.pageCount}
            style={btnStyle(false, win.page >= win.pageCount)}
          >
            下一页
          </button>
          <button
            onClick={() => setPage(win.pageCount)}
            disabled={win.page >= win.pageCount}
            style={btnStyle(false, win.page >= win.pageCount)}
          >
            末页
          </button>
        </div>
      )}

      <style>{`
        .fs-spin {
          width: 9px; height: 9px; border-radius: 50%;
          border: 1.5px solid var(--accent-border);
          border-top-color: var(--accent);
          display: inline-block; vertical-align: middle; margin-right: 6px;
          animation: fs-rot 0.7s linear infinite;
        }
        @keyframes fs-rot { to { transform: rotate(360deg); } }
      `}</style>
    </div>
  )
}

/** 概览统计卡 */
function StatCard({
  label,
  value,
  accent,
}: {
  label: string
  value: React.ReactNode
  accent?: boolean
}) {
  return (
    <div
      className="fs-card"
      style={{
        flex: '1 1 130px',
        padding: '11px 14px',
        background: accent
          ? 'linear-gradient(135deg, #f1effb 0%, #e9e6f8 100%)'
          : 'var(--color-background-primary)',
        borderColor: accent ? 'var(--accent-border)' : 'var(--color-border-tertiary)',
      }}
    >
      <div style={{ fontSize: 11.5, color: 'var(--color-text-secondary)', marginBottom: 2 }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 20,
          fontWeight: 600,
          letterSpacing: -0.4,
          color: accent ? 'var(--accent)' : 'var(--color-text-primary)',
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </div>
    </div>
  )
}

function RecordRow({
  record,
  isOpen,
  isLast,
  onToggle,
  onDelete,
}: {
  record: DiagnosisRecord
  isOpen: boolean
  isLast: boolean
  onToggle: () => void
  onDelete: () => void
}) {
  const meta = statusMeta(record.status)
  // 不能用 `?? {}`：空对象字面量会被推断成 `{}`，与 Partial<WorkOrder> 组成联合后
  // 读取 `wo.根因` 就会报 "does not exist on type '{}'"。
  // 这里的兜底语义是"没有就当作空工作单"，本来就是 Partial 的零值。
  const wo: Partial<WorkOrder> = record.workorder ?? {}
  const dg: Partial<Diagnosis> = record.diagnosis ?? {}
  const rootCause = (dg.根因判断 || wo.根因 || '—').slice(0, 60)
  const hasWorkorder = !!wo.工单编号

  return (
    <div
      style={{ borderBottom: isLast ? 'none' : '0.5px solid var(--color-border-tertiary)' }}
    >
      <div
        onClick={onToggle}
        // 可访问性：这一行是"可点击的展开开关"，但它是个 div。
        // 没有 role/tabIndex 时，键盘用户 Tab 不到它、读屏软件也不会说它能点 ——
        // 表格的详情对这部分用户等于不存在。
        role="button"
        tabIndex={0}
        aria-expanded={isOpen}
        onKeyDown={(e) => {
          // 只认 Enter / Space（原生按钮的行为），不吞掉方向键与 Tab
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onToggle()
          }
        }}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '10px 13px',
          cursor: 'pointer',
          background: isOpen ? 'var(--color-background-secondary)' : 'transparent',
          fontSize: 13,
          transition: 'background var(--transition)',
        }}
        onMouseEnter={(e) => {
          if (!isOpen) e.currentTarget.style.background = 'var(--color-background-secondary)'
        }}
        onMouseLeave={(e) => {
          if (!isOpen) e.currentTarget.style.background = 'transparent'
        }}
      >
        <span
          style={{
            color: 'var(--color-text-tertiary)',
            width: 34,
            fontFamily: 'var(--font-mono)',
            fontSize: 11.5,
          }}
        >
          {record.id}
        </span>
        <span
          style={{
            color: 'var(--color-text-secondary)',
            width: 132,
            fontSize: 12,
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {formatTime(record.created_at)}
        </span>
        <span style={{ width: 84 }}>
          <span
            style={{
              display: 'inline-block',
              padding: '2.5px 9px',
              borderRadius: 20,
              background: meta.bg,
              color: meta.color,
              fontSize: 11.5,
              fontWeight: 500,
              whiteSpace: 'nowrap',
            }}
          >
            {meta.label}
          </span>
        </span>
        <span
          style={{
            flex: 1,
            color: 'var(--color-text-primary)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
          title={record.fault_description}
        >
          {record.fault_description}
        </span>
        <span
          style={{
            color: 'var(--color-text-tertiary)',
            fontSize: 11,
            width: 16,
            transition: 'transform var(--transition)',
            transform: isOpen ? 'rotate(90deg)' : 'none',
            display: 'inline-block',
          }}
        >
          ▸
        </span>
      </div>

      {isOpen && (
        <div
          className="fs-rise"
          style={{
            padding: '10px 15px 15px 59px',
            background: 'var(--color-background-secondary)',
            fontSize: 13,
            lineHeight: 1.65,
            borderTop: '0.5px solid var(--color-border-tertiary)',
          }}
        >
          <Field label="最终根因" value={rootCause} />
          <Field label="工单编号" value={wo.工单编号} />
          <Field label="追踪 ID" value={record.correlation_id} />

          {wo.风险等级 && (
            <div
              style={{
                marginTop: 8,
                padding: '8px 11px',
                borderRadius: 'var(--radius-sm)',
                background: 'var(--warning-soft)',
                border: '0.5px solid #EF9F27',
                color: '#412402',
              }}
            >
              ⚠ {wo.风险等级}
              {/* pre-line：风险说明含多行（降级时附"参考方向"的逐条排查动作），
                  不设的话换行会被折叠成空格 */}
              {wo.风险说明 && (
                <div style={{ marginTop: 3, whiteSpace: 'pre-line' }}>{wo.风险说明}</div>
              )}
            </div>
          )}

          {/* 按需展开原始 JSON：默认收起，不必为看一个字段渲染 7 个 JSON 块 */}
          <details style={{ marginTop: 10 }}>
            <summary
              style={{ cursor: 'pointer', fontSize: 12, color: 'var(--color-text-secondary)' }}
            >
              查看完整数据（诊断 / 审核 / 辩论 / 成本 / 工单）
            </summary>
            <pre
              style={{
                marginTop: 8,
                padding: 12,
                borderRadius: 'var(--radius-sm)',
                background: 'var(--color-background-primary)',
                border: '0.5px solid var(--color-border-tertiary)',
                fontSize: 11.5,
                lineHeight: 1.55,
                overflow: 'auto',
                maxHeight: 340,
                fontFamily: 'var(--font-mono)',
              }}
            >
              {JSON.stringify(record, null, 2)}
            </pre>
          </details>

          <div style={{ display: 'flex', gap: 8, marginTop: 11 }}>
            {hasWorkorder && (
              <a
                href={workorderUrl(record.id)}
                download
                style={{ ...btnStyle(true, false), textDecoration: 'none' }}
              >
                下载工单 Markdown
              </a>
            )}
            <button
              onClick={(e) => {
                e.stopPropagation()
                const blob = new Blob([JSON.stringify(record, null, 2)], { type: 'application/json' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = `record_${record.id}.json`
                a.click()
                URL.revokeObjectURL(url)
              }}
              style={btnStyle(false, false)}
            >
              导出 JSON
            </button>
            <button onClick={onDelete} style={btnStyle(false, false, 'var(--danger)')}>
              删除
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function Field({ label, value }: { label: string; value?: React.ReactNode }) {
  if (!value) return null
  return (
    <div style={{ display: 'flex', gap: 10, marginTop: 3 }}>
      <span style={{ color: 'var(--color-text-secondary)', minWidth: 64, flexShrink: 0, fontSize: 12 }}>
        {label}
      </span>
      <span style={{ color: 'var(--color-text-primary)' }}>{value}</span>
    </div>
  )
}

const selectStyle = {
  padding: '8px 11px',
  fontSize: 13,
  borderRadius: 'var(--radius-md)',
  border: '1px solid var(--color-border-secondary)',
  background: 'var(--color-background-primary)',
  color: 'var(--color-text-primary)',
  fontFamily: 'inherit',
} as const
