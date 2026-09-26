/**
 * 诊断流的生命周期控制器。
 *
 * 抽出来的唯一理由：**没有 jsdom 也要能测「卸载时中断」**。
 * React 的 useEffect 清理在本仓库的测试环境里跑不起来（不装 jsdom / testing-library），
 * 而「切页不中断」正是一个真实发生过的 bug —— 诊断中途切 tab，组件卸载、
 * 旧 fetch 无人 abort，后端继续跑完 6~9 次 LLM 调用，用户切回来结果已经没了。
 *
 * 所以把「启动一条流 / 记住中断函数 / 卸载时中断」做成一个不依赖 React 的对象，
 * hook 只负责在 useEffect 的 cleanup 里调 `dispose()`。
 * 这样「中断到底有没有被调用」就是一条可以在单测里断言的纯逻辑。
 */

/** 启动一条流，返回它的中断函数。`diagnoseStream` 就是这个形状。 */
export type StreamStarter<TReq, THandlers> = (
  payload: TReq,
  handlers: THandlers,
) => () => void

export interface StreamController<TReq, THandlers> {
  start(payload: TReq, handlers: THandlers): void
  /** 主动中断（用户点「中断诊断」） */
  abort(): void
  /** 卸载时的兜底中断。与 abort 的区别只在语义，行为一致。 */
  dispose(): void
  readonly disposed: boolean
}

export function createStreamController<TReq, THandlers>(
  streamFn: StreamStarter<TReq, THandlers>,
): StreamController<TReq, THandlers> {
  let cancel: (() => void) | null = null
  let disposed = false

  return {
    start(payload, handlers) {
      // 上一条还没结束就先中断，避免两条流同时往同一份 state 里写
      cancel?.()
      cancel = streamFn(payload, handlers)
      // 重新武装。React 18 的 StrictMode 会「挂载 → 卸载 → 再挂载」，
      // 若把 disposed 当成一次性开关，开发模式下第二次挂载就再也起不了流。
      disposed = false
    },
    abort() {
      cancel?.()
      cancel = null
    },
    dispose() {
      disposed = true
      cancel?.()
      cancel = null
    },
    get disposed() {
      return disposed
    },
  }
}
