/**
 * 系统统计页（从 Streamlit 的 `app.py::render_stats()` 迁来）。
 *
 * 相比 Streamlit 版：
 *   - **状态分布改成横向条形 + 降序**。原版 `st.bar_chart` 用的是字典序，
 *     且柱子细、数量少时挤在一角；这里每行带状态色、条数、占比，一眼能读出主次。
 *   - **数据库大小改由后端返回**。原版在前端 `Path(settings.DB_PATH).stat()`，
 *     React 拿不到（也不该知道）DB 路径，所以挪到了 `get_stats()`。
 *   - 加了手动刷新按钮。
 *
 * **刻意不轮询**：统计只在跑完一次诊断后才变，
 * 每几秒刷一次纯属浪费（后端每次都要做 4 次聚合查询）。
 * 想看最新数据点一下刷新即可。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { getStats, ApiError } from '../api/client'
import type { Stats } from '../types/contracts'
import { btnStyle } from '../ui/button'
import { statusMeta } from './historyUtils'
import { formatBytes, formatCount, formatPercent, isEmptyStats, statusRows } from './statsUtils'

export function StatsPage() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setStats(await getStats())
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `加载失败：${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const rows = useMemo(
    () => statusRows(stats?.by_status, stats?.total_records ?? 0),
    [stats?.by_status, stats?.total_records],
  )

  const empty = isEmptyStats(stats)

  return (
    <div style={{ maxWidth: 1120, margin: '0 auto', padding: '32px 24px 60px' }}>
      <header
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 12,
          marginBottom: 20,
        }}
      >
        <div style={{ flex: 1 }}>
          <h1 style={{ fontSize: 21, fontWeight: 600, margin: 0, letterSpacing: -0.3 }}>
            系统统计
          </h1>
          <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', margin: '6px 0 0' }}>
            所有诊断记录的汇总视图。数据只在完成一次诊断后变化，需要最新数据时点右侧刷新。
          </p>
        </div>
        <button onClick={() => void load()} disabled={loading} style={btnStyle(false, loading)}>
          {loading ? '加载中…' : '刷新'}
        </button>
      </header>

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
            marginBottom: 14,
          }}
        >
          {error}
        </div>
      )}

      {/* 四个概览指标 */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <MetricCard label="总记录数" value={stats ? formatCount(stats.total_records) : '—'} accent />
        <MetricCard
          label="平均辩论轮数"
          value={stats ? String(stats.avg_debate_rounds) : '—'}
        />
        <MetricCard
          label="总 Token 消耗"
          value={stats ? formatCount(stats.total_tokens) : '—'}
        />
        <MetricCard label="数据库大小" value={stats ? formatBytes(stats.db_size_bytes) : '—'} />
      </div>

      {/* 状态分布 */}
      <div className="fs-card" style={{ padding: '14px 16px 16px' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginBottom: 12,
            paddingBottom: 8,
            borderBottom: '0.5px solid var(--color-border-tertiary)',
          }}
        >
          <span
            aria-hidden
            style={{
              width: 3,
              height: 13,
              borderRadius: 2,
              background: 'var(--accent)',
              flexShrink: 0,
            }}
          />
          <span style={{ fontSize: 13, fontWeight: 600 }}>按状态分布</span>
          {!loading && !empty && (
            <span style={{ marginLeft: 'auto', fontSize: 11.5, color: 'var(--color-text-tertiary)' }}>
              共 {formatCount(stats?.total_records ?? 0)} 条
            </span>
          )}
        </div>

        {loading ? (
          <div style={{ fontSize: 13, color: 'var(--color-text-secondary)', padding: '18px 0' }}>
            <span className="fs-spin" /> 加载中…
          </div>
        ) : empty ? (
          <div
            data-testid="stats-empty"
            style={{
              padding: 34,
              textAlign: 'center',
              fontSize: 13,
              color: 'var(--color-text-secondary)',
              border: '1px dashed var(--color-border-secondary)',
              borderRadius: 'var(--radius-md)',
            }}
          >
            <div style={{ fontSize: 24, opacity: 0.3, marginBottom: 8 }} aria-hidden>
              ◔
            </div>
            暂无数据。先进行一次诊断，记录会自动计入统计。
          </div>
        ) : (
          <div data-testid="stats-rows" style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
            {rows.map((r) => {
              const meta = statusMeta(r.status)
              return (
                <div
                  key={r.status}
                  data-testid="stats-row"
                  data-status={r.status}
                  style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 13 }}
                >
                  <span
                    style={{
                      width: 118,
                      flexShrink: 0,
                      padding: '2.5px 9px',
                      borderRadius: 20,
                      background: meta.bg,
                      color: meta.color,
                      fontSize: 11.5,
                      fontWeight: 500,
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      textAlign: 'center',
                    }}
                  >
                    {meta.label}
                  </span>

                  {/* 条形轨道 */}
                  <span
                    style={{
                      flex: 1,
                      height: 9,
                      borderRadius: 5,
                      background: 'var(--color-background-tertiary)',
                      overflow: 'hidden',
                      minWidth: 60,
                    }}
                  >
                    <span
                      style={{
                        display: 'block',
                        height: '100%',
                        // 用 barPercent 而不是 percent：极小占比也要看得见
                        width: `${r.barPercent}%`,
                        borderRadius: 5,
                        background: meta.color,
                        opacity: 0.85,
                        transition: 'width 0.35s ease',
                      }}
                    />
                  </span>

                  <span
                    style={{
                      width: 52,
                      textAlign: 'right',
                      fontWeight: 600,
                      fontVariantNumeric: 'tabular-nums',
                      flexShrink: 0,
                    }}
                  >
                    {formatCount(r.count)}
                  </span>
                  <span
                    style={{
                      width: 54,
                      textAlign: 'right',
                      fontSize: 11.5,
                      color: 'var(--color-text-tertiary)',
                      fontVariantNumeric: 'tabular-nums',
                      flexShrink: 0,
                    }}
                  >
                    {formatPercent(r.percent)}
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </div>

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

function MetricCard({
  label,
  value,
  accent,
}: {
  label: string
  value: string
  accent?: boolean
}) {
  return (
    <div
      className="fs-card"
      data-testid="stats-metric"
      style={{
        flex: '1 1 150px',
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
