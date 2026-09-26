/**
 * 诊断流程的状态管理 hook。
 *
 * 这是前端最复杂的一块：需要同时管理
 *   - SSE 流的生命周期（启动 / 中断 / 卸载清理）
 *   - 节点进度（用于状态机可视化）
 *   - 多轮追问的上下文累积（与 Streamlit 版行为对齐）
 *   - 并发闸门返回 503 时的重试语义
 *
 * 刻意不引入状态管理库：状态是单一的、由 reducer 收敛的，
 * 用 useReducer 已经足够表达。引入 Redux/Zustand 只会让"状态从哪来"更难讲清。
 *
 * **状态的归属**：本 hook 的实例挂在 `App` 上（见 App.tsx），不是挂在诊断页里。
 * 切 tab 会卸载诊断页，若状态随组件走，跑了一半的诊断就会被丢掉；
 * 而按 `key={route}` 强制重挂载更糟——旧 fetch 无人 abort，后端继续烧配额。
 */

import { useCallback, useEffect, useReducer, useRef } from 'react'
import { diagnoseStream } from '../api/client'
import type { DiagnosisRequest, DiagnosisResult, ProgressEvent } from '../types/contracts'
import { nodeFromLabel, type NodeId } from '../state/machine'
import {
  appendUserTurn,
  attachFollowup,
  composePayload,
  type Turn,
} from './diagnosisContext'
import { createStreamController } from './streamLifecycle'

export type { Turn }

interface StreamState {
  /** 是否正在跑诊断 */
  running: boolean
  /** 已走过的节点 */
  visited: Set<NodeId>
  /** 当前正在执行的节点 */
  active: NodeId | null
  /** 实时进度文案（直接用后端 label，里面带序号，可读性好） */
  progressLog: string[]
  /** 辩论轮数（后端实时推） */
  debateRound: number
  /** 最终结果，null 表示还没出 */
  result: DiagnosisResult | null
  /** 错误信息，null 表示无错误 */
  error: string | null
  /** 被并发闸门挡下时的建议重试秒数 */
  retryAfter: number | null
  /** 本次累计的对话轮次，只存用户原话（拼上下文见 diagnosisContext.ts） */
  turns: Turn[]
  /** 当前 correlation_id（诊断开始后即有） */
  correlationId: string | null
}

type Action =
  | { type: 'start'; userInput: string }
  | { type: 'progress'; payload: ProgressEvent }
  | { type: 'result'; payload: DiagnosisResult }
  | { type: 'done'; correlationId?: string }
  | { type: 'error'; message: string; retryAfter?: number }
  | { type: 'reset' }

const initialState: StreamState = {
  running: false,
  visited: new Set(),
  active: null,
  progressLog: [],
  debateRound: 0,
  result: null,
  error: null,
  retryAfter: null,
  turns: [],
  correlationId: null,
}

/**
 * 导出 reducer 是为了让它可测：状态推进（尤其是 `turns` 里存什么）是本项目
 * 踩过坑的地方，而 hook 本身在没有 jsdom 的环境里跑不起来。
 * 测试驱动真实 reducer，而不是重抄一遍逻辑。
 */
export function reducer(state: StreamState, action: Action): StreamState {
  switch (action.type) {
    case 'start':
      return {
        ...state,
        running: true,
        visited: new Set(),
        active: null,
        progressLog: [],
        debateRound: 0,
        result: null,
        error: null,
        retryAfter: null,
        // 只存原文。存「本轮实际发出去的 payload」会让下一轮的上下文自我嵌套，
        // 长度指数膨胀 —— 见 diagnosisContext.appendUserTurn 的说明。
        turns: appendUserTurn(state.turns, action.userInput),
      }

    case 'progress': {
      const node = nodeFromLabel(action.payload.label)
      const visited = new Set(state.visited)
      // 上一次 active 的节点标记为已走过：后端推进到下一个节点时，
      // 前一个节点其实已经完成了。这样进度条才会持续向右。
      if (state.active && state.active !== node?.id) {
        visited.add(state.active)
      }
      if (node) visited.add(node.id)

      return {
        ...state,
        visited,
        active: node?.id ?? state.active,
        progressLog: [...state.progressLog, action.payload.label],
        debateRound: Math.max(state.debateRound, action.payload.debate_round),
        correlationId: action.payload.correlation_id || state.correlationId,
      }
    }

    case 'result':
      return {
        ...state,
        running: false,
        result: action.payload,
        // 收尾时把 active 清掉，否则最后一个节点会永远显示"进行中"
        active: null,
        debateRound: action.payload.debate_round,
        correlationId: action.payload.correlation_id || state.correlationId,
        // 追问轮次记进对话历史，下一轮把上下文带回去
        turns: attachFollowup(state.turns, action.payload.followup_question),
      }

    // 流的正常收尾信号。没有它时，只有 result/error 能让 running 归位；
    // 而「收到 done 却没有 result」（中间丢包）会让界面永远停在"诊断中"。
    case 'done':
      return {
        ...state,
        running: false,
        active: null,
        correlationId: action.correlationId || state.correlationId,
      }

    case 'error':
      return {
        ...state,
        running: false,
        error: action.message,
        retryAfter: action.retryAfter ?? null,
        active: null,
      }

    case 'reset':
      return { ...initialState, turns: state.turns }

    default:
      return state
  }
}

export { initialState }

export function useDiagnosisStream() {
  const [state, dispatch] = useReducer(reducer, initialState)
  // 流控制器。用 ref 而不是 state：它不参与渲染，
  // 且必须在组件重渲染之间保持同一个引用，否则中断按钮会失效。
  const controllerRef = useRef(createStreamController(diagnoseStream))

  // 卸载时中断在途的流。缺了这段，「切页」就等于把后端一次诊断的 6~9 次
  // LLM 调用扔进黑洞：用户拿不到结果，配额照烧。
  // 依赖数组为空：只需要在挂载/卸载这一对时机上跑，不该跟着任何 state 重建。
  useEffect(
    () => () => {
      controllerRef.current.dispose()
    },
    [],
  )

  // turns 的权威副本。start 需要读「本轮之前的历史」来拼上下文，
  // 但把 state.turns 放进依赖数组会让 start 每轮都换引用、进而重建整个回调链。
  // 用 ref 读最新值，start 保持稳定。
  const stateRef = useRef(state)
  stateRef.current = state

  const start = useCallback(
    (userInput: string, imageBase64?: string) => {
      // 上下文在这里临时组装、不写回 state：turns 里只有用户原话。
      const faultDescription = composePayload(stateRef.current.turns, userInput)

      dispatch({ type: 'start', userInput })

      const payload: DiagnosisRequest = { fault_description: faultDescription }
      if (imageBase64) payload.image_base64 = imageBase64

      controllerRef.current.start(payload, {
        onProgress: (e) => dispatch({ type: 'progress', payload: e }),
        onResult: (r) => dispatch({ type: 'result', payload: r }),
        // done 是服务端宣告"这一轮结束了"。即使 result 中途丢了，它也能让
        // running 收敛，不至于把界面永久钉在"诊断中"。
        onDone: (correlationId) => dispatch({ type: 'done', correlationId }),
        onError: (message) => {
          // 503 的语义是"系统忙"，不是"你的请求有问题"，
          // 要把 Retry-After 带出来让 UI 能给出可操作的建议
          const retryMatch = message.match(/建议 (\d+) 秒后重试/)
          dispatch({
            type: 'error',
            message,
            retryAfter: retryMatch?.[1] ? Number(retryMatch[1]) : undefined,
          })
        },
      })
    },
    [],
  )

  const abort = useCallback(() => {
    controllerRef.current.abort()
    dispatch({ type: 'error', message: '已手动中断本次诊断' })
  }, [])

  const reset = useCallback(() => {
    controllerRef.current.abort()
    dispatch({ type: 'reset' })
  }, [])

  return { state, start, abort, reset }
}

/** hook 的返回类型。诊断页通过 props 接收它（状态挂在 App 上，见文件头说明）。 */
export type DiagnosisStreamApi = ReturnType<typeof useDiagnosisStream>
