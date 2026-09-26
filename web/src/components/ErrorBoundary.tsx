/**
 * 顶层错误边界。
 *
 * 为什么必须有：React 在渲染期间抛出的异常会**卸载整棵树**，页面变成一片空白，
 * 控制台里的报错用户看不到。本项目真实踩过一次——后端返回 422 时
 * `state.error` 被赋成了数组，`{state.error}` 渲染对象直接抛错，整页白屏，
 * 用户完全不知道发生了什么。
 *
 * 兜底页只做两件事：**说清"界面出错"**、**给一个能自愈的动作**。
 * 不展示 error.stack —— 那是给开发者的，塞给用户只会让人以为系统崩得很彻底。
 * 真正的堆栈走 console.error，开发时 F12 就能看到。
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 保留到控制台：兜底页刻意不展示堆栈，所以这里是唯一的排查入口
    console.error('界面渲染出错', error, info.componentStack)
  }

  private reload = (): void => {
    window.location.reload()
  }

  private reset = (): void => {
    // 只清掉边界自身的错误态，让子树重新挂载。渲染是幂等的，
    // 若错误来自一次性数据（比如上一次的响应），这一步就能恢复。
    this.setState({ error: null })
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div
        role="alert"
        data-testid="error-boundary"
        style={{
          maxWidth: 560,
          margin: '80px auto',
          padding: '22px 24px',
          border: '0.5px solid var(--danger, #d9534f)',
          borderRadius: 12,
          background: 'var(--danger-soft, #fdf2f2)',
          color: '#791f1f',
          fontFamily: 'var(--font-sans, system-ui, sans-serif)',
          lineHeight: 1.7,
        }}
      >
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 6 }}>界面出错</div>
        <div style={{ fontSize: 13 }}>
          页面渲染时遇到未预期的数据，已经停止渲染以免显示错误的信息。
          刷新页面通常可以恢复；如果反复出现，请把浏览器控制台的报错一并反馈。
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
          <button
            type="button"
            onClick={this.reload}
            style={{
              padding: '7px 16px',
              fontSize: 13,
              borderRadius: 8,
              border: 'none',
              background: 'var(--accent, #534ab7)',
              color: '#fff',
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            刷新页面
          </button>
          <button
            type="button"
            onClick={this.reset}
            style={{
              padding: '7px 16px',
              fontSize: 13,
              borderRadius: 8,
              border: '0.5px solid currentColor',
              background: 'transparent',
              color: 'inherit',
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            重试
          </button>
        </div>
      </div>
    )
  }
}
