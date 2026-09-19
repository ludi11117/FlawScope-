/**
 * 后端可用性轮询 hook。
 *
 * 探两个端点，分工明确：
 *   - `/health/live`  ：进程活着吗（毫秒级，不碰依赖）
 *   - `/health/ready` ：能接诊断请求吗（初始化完成没）
 *
 * **状态灯以 ready 为准**。只用 live 的话，冷启动的十几秒里它已经返回 200，
 * 页面会显示"服务正常"，用户点下"开始诊断"必然失败——
 * 探针在真实可用之前就宣告可用，这正是要消除的假象。
 *
 * 不用 `/health`：那个会真去探 LLM / Embedding / ChromaDB，每次调用都烧配额。
 *
 * 三个刻意的行为：
 *   - **启动中加密轮询**（1.5s），就绪后放宽到 5s。启动窗口只有十几秒，
 *     5 秒一次会让用户多等最多 5 秒；放大间隔则纯属浪费。
 *   - **页面不可见时暂停轮询**。用户在别的标签页工作时，这个页面没有任何理由
 *     继续发请求；`visibilitychange` 恢复时立刻补探一次，
 *     这样切回来看到的是刚更新的状态而不是几秒前的陈旧值。
 *   - **卸载时清掉定时器与在途请求**。在 Streamlit 时代这是常见泄漏源，
 *     在 React 里表现为"切页面后还继续发请求"。
 *
 * 判定与状态更新规则都在 `api/health.ts` 里做成纯函数
 * （`healthStateFromProbe` / `nextHealthValue` / `nextPollInterval` / `shouldPoll`），
 * 本文件只负责把它们接到 React 生命周期上。
 * 这样在**没有 jsdom / testing-library** 的前提下，逻辑部分依然是可测的。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { getHealthLive, getHealthReady } from '../api/client'
import {
  HEALTH_POLL_MS,
  nextHealthValue,
  nextPollInterval,
  shouldPoll,
  type HealthState,
  type ProbeObservation,
  type ReadyPayload,
} from '../api/health'

export interface UseHealthResult {
  state: HealthState
  /** 最近一次成功探活拿到的后端版本号 */
  version: string | null
  /** 启动中时的后端进度（步骤名与序号），就绪后也为最新值 */
  ready: ReadyPayload | null
  /** 手动重新探活一次（用于"重试"按钮） */
  refresh: () => void
}

export function useHealth(pollMs: number = HEALTH_POLL_MS): UseHealthResult {
  // 初始为 down：在第一次探活回来之前，不能假设后端是好的。
  // 若初始成 up，后端没起时页面会先闪一下绿灯——正是要消除的假象。
  const [state, setState] = useState<HealthState>('down')
  const [version, setVersion] = useState<string | null>(null)
  const [ready, setReady] = useState<ReadyPayload | null>(null)

  // 组件是否还挂载着：避免卸载后 setState
  const aliveRef = useRef(true)
  // 版本号的权威副本。用 ref 而不是读 state，是为了让 probe 在不依赖
  // `version` 的情况下也能拿到最新值——否则 probe 每次重建，effect 会跟着重跑。
  const versionRef = useRef<string | null>(null)
  // 定时器句柄。放 ref 是因为下次间隔取决于本次探活结果，
  // 需要在 probe 内部重建定时器，而不是交给 useEffect 的依赖数组。
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 上一次探活得到的 state。schedule 需要它来决定下次间隔，
  // 但直接读 `state` 会把 state 塞进 effect 依赖、导致定时器被反复重建。
  const currentStateRef = useRef<HealthState>('down')

  const probe = useCallback(async () => {
    // 两个请求并行：ready 的结果决定状态，live 只作为"网线通不通"的兜底。
    // 串行发会让每次轮询多一个 RTT，在启动期（1.5s 一次）尤其不划算。
    const [liveOk, readyResp] = await Promise.all([
      getHealthLive().then(
        () => true,
        () => false,
      ),
      getHealthReady(),
    ])

    if (!aliveRef.current) return

    const obs: ProbeObservation = {
      live: liveOk,
      readyStatus: readyResp ? readyResp.status : null,
      ready: readyResp?.body ?? null,
    }

    const next = nextHealthValue({ version: versionRef.current }, obs)
    versionRef.current = next.version
    currentStateRef.current = next.state
    setState(next.state)
    // 版本号没变就不触发重渲染（轮询密集时，无意义的 setState 会白白渲染）
    setVersion((prev) => (prev === next.version ? prev : next.version))
    setReady((prev) => {
      // 同样只在内容变化时换引用：ready 对象每次 fetch 都是新的，
      // 直接 setState 会让下游 useMemo/useEffect 反复失效
      if (prev && obs.ready && JSON.stringify(prev) === JSON.stringify(obs.ready)) return prev
      return obs.ready
    })

    return next.state
  }, [])

  useEffect(() => {
    aliveRef.current = true
    let cancelled = false

    // 用 setTimeout 自排下一轮而不是 setInterval：
    // 轮询间隔取决于本轮结果（启动中要更快），setInterval 的间隔在创建时就固定了。
    const schedule = () => {
      if (cancelled) return
      timerRef.current = setTimeout(() => {
        void probe().then((st) => {
          if (cancelled || !st) return
          schedule()
        })
      }, nextPollInterval(currentStateRef.current, pollMs))
    }

    const stop = () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }

    const kick = () => {
      void probe().then((st) => {
        if (cancelled || !st) return
        stop()
        schedule()
      })
    }

    const onVisibility = () => {
      if (shouldPoll(document.hidden)) {
        kick() // 切回来先补一次，别让用户盯着旧状态
      } else {
        stop()
      }
    }

    kick()
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      cancelled = true
      aliveRef.current = false
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [probe, pollMs])

  return { state, version, ready, refresh: probe }
}
