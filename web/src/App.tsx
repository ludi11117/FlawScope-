/**
 * 应用外壳：顶部导航 + 页面切换。
 *
 * 用 location.hash 做路由而不是引入 react-router：
 * 只有两个页面、不需要嵌套路由 / 路由守卫 / 数据预取，
 * 引入一个 30KB 的库换来 `useState` 就能做到的事，不划算。
 * 换成 hash 而非 pushState 是因为 hash 路由不需要服务端配合
 * ——刷新 /history 时 nginx 不必特殊配置（虽然我们也配了 try_files）。
 */

import { useEffect, useState } from 'react'
import { DiagnosePage } from './pages/DiagnosePage'
import { HistoryPage } from './pages/HistoryPage'
import { StatsPage } from './pages/StatsPage'
import { useHealth } from './hooks/useHealth'
import { HEALTH_META, startupHint } from './api/health'

type Route = 'diagnose' | 'history' | 'stats'

const ROUTES: { key: Route; label: string; icon: string }[] = [
  { key: 'diagnose', label: '故障诊断', icon: '◈' },
  { key: 'history', label: '诊断历史', icon: '▤' },
  { key: 'stats', label: '系统统计', icon: '◔' },
]

function parseHash(): Route {
  const h = window.location.hash.replace(/^#\/?/, '')
  if (h === 'history') return 'history'
  if (h === 'stats') return 'stats'
  return 'diagnose'
}

export function App() {
  const [route, setRoute] = useState<Route>(parseHash)
  const health = useHealth()

  // 支持浏览器前进/后退：不监听的话，用户按后退键 URL 变了但页面不切
  useEffect(() => {
    const onHashChange = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const go = (r: Route) => {
    window.location.hash = `#/${r}`
    setRoute(r)
  }

  return (
    <div>
      <nav
        style={{
          borderBottom: '0.5px solid var(--color-border-tertiary)',
          padding: '0 24px',
          display: 'flex',
          alignItems: 'center',
          gap: 2,
          position: 'sticky',
          top: 0,
          background: 'rgba(255, 255, 255, 0.85)',
          backdropFilter: 'blur(12px)',
          WebkitBackdropFilter: 'blur(12px)',
          zIndex: 10,
        }}
      >
        {/* 品牌标识：图标 + 名称 + 副标题 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginRight: 22 }}>
          <span
            aria-hidden
            style={{
              width: 26,
              height: 26,
              borderRadius: 7,
              display: 'grid',
              placeItems: 'center',
              fontSize: 14,
              color: '#fff',
              background: 'linear-gradient(135deg, #6a60d0 0%, #4b3fa8 100%)',
              boxShadow: '0 2px 6px rgba(83, 74, 183, 0.28)',
            }}
          >
            ⌬
          </span>
          <span style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.15 }}>
            <span style={{ fontSize: 14, fontWeight: 600, letterSpacing: 0.2 }}>FlawScope</span>
            <span style={{ fontSize: 10.5, color: 'var(--color-text-tertiary)', letterSpacing: 0.3 }}>
              多智能体故障诊断
            </span>
          </span>
        </div>

        {ROUTES.map((r) => {
          const active = route === r.key
          return (
            <button
              key={r.key}
              onClick={() => go(r.key)}
              style={{
                padding: '13px 13px',
                fontSize: 13,
                border: 'none',
                background: 'transparent',
                cursor: 'pointer',
                color: active ? 'var(--accent)' : 'var(--color-text-secondary)',
                fontWeight: active ? 600 : 400,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                // 用下边框而不是背景色做选中态：这个位置背景色会显得很重
                borderBottom: active ? '2px solid var(--accent)' : '2px solid transparent',
                fontFamily: 'inherit',
                transition: 'color var(--transition), border-color var(--transition)',
              }}
            >
              <span aria-hidden style={{ fontSize: 11, opacity: active ? 1 : 0.7 }}>
                {r.icon}
              </span>
              {r.label}
            </button>
          )
        })}

        {/* 运行状态灯：轮询 /health/live + /health/ready，真实反映后端可用性。
            此前这里是写死的绿点，后来改成只看 liveness——两种都会撒谎：
            冷启动的十几秒里 liveness 已是 200，但初始化没完成，
            此时点"开始诊断"必然失败。现在以 readiness 为准，
            并把"正在启动"与"不可达"分开（一个该等，一个该去启动进程）。 */}
        <button
          type="button"
          onClick={health.refresh}
          data-testid="health-indicator"
          data-health-state={health.state}
          title={
            health.state === 'starting'
              ? startupHint(health.ready)
              : health.version
                ? `后端版本 ${health.version}，点击重新探活`
                : '点击重新探活'
          }
          style={{
            marginLeft: 'auto',
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            fontSize: 11.5,
            color: 'var(--color-text-tertiary)',
            background: 'transparent',
            border: 'none',
            padding: 0,
            cursor: 'pointer',
            fontFamily: 'inherit',
          }}
        >
          <span
            aria-hidden
            data-testid="health-dot"
            style={{
              width: 6,
              height: 6,
              borderRadius: '50%',
              background: HEALTH_META[health.state].color,
              boxShadow: `0 0 0 3px ${HEALTH_META[health.state].halo}`,
              transition: 'background var(--transition), box-shadow var(--transition)',
              // 启动中让点"呼吸"，传达"有事情正在发生，不是卡住了"
              animation: health.state === 'starting' ? 'fs-pulse-soft 1.4s ease-in-out infinite' : 'none',
            }}
          />
          <span data-testid="health-label">{HEALTH_META[health.state].label}</span>
          {/* 启动中把后端上报的步骤一并显示——十几秒的等待没有进度反馈，
              用户会以为页面坏了 */}
          {health.state === 'starting' && health.ready?.step_total ? (
            <span style={{ color: 'var(--color-text-tertiary)', opacity: 0.85 }}>
              · {health.ready.step}
              {health.ready.step_index ? ` ${health.ready.step_index}/${health.ready.step_total}` : ''}
            </span>
          ) : null}
        </button>

        <a
          href="/api/docs"
          target="_blank"
          rel="noreferrer"
          style={{
            marginLeft: 14,
            fontSize: 11.5,
            color: 'var(--color-text-tertiary)',
            textDecoration: 'none',
            borderBottom: '1px dotted var(--color-border-secondary)',
          }}
        >
          API 文档
        </a>
      </nav>

      <main key={route} className="fs-rise">
        {route === 'stats' ? (
          <StatsPage />
        ) : route === 'history' ? (
          <HistoryPage />
        ) : (
          <DiagnosePage />
        )}
      </main>
    </div>
  )
}
