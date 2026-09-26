/**
 * 流生命周期控制器测试（A4）。
 *
 * 真实 bug：诊断中途切 tab，组件卸载、旧 fetch 无人 abort，
 * 后端继续跑完 6~9 次 LLM 调用；用户切回来结果已经丢了。
 *
 * 本仓库不装 jsdom，useEffect 的清理跑不起来，所以把「启动 / 中断 / 卸载中断」
 * 抽成纯对象（`streamLifecycle.ts`），这里测它的行为，再用源码断言守住"接线"
 * ——函数写对 ≠ 调用点接对，两件事要分开证明。
 */

import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it, vi } from 'vitest'
import { createStreamController } from './streamLifecycle'

const HERE = dirname(fileURLToPath(import.meta.url))

describe('createStreamController', () => {
  it('dispose() 会调用在途流的 abort —— 这是「切页不中断」的修复点', () => {
    const abort = vi.fn()
    const controller = createStreamController<{ q: string }, { onResult?: () => void }>(() => abort)

    controller.start({ q: 'a' }, {})
    expect(abort).not.toHaveBeenCalled()

    controller.dispose()
    expect(abort).toHaveBeenCalledTimes(1)
    expect(controller.disposed).toBe(true)
  })

  it('dispose() 之后再 dispose() 不会重复中断', () => {
    const abort = vi.fn()
    const controller = createStreamController<unknown, unknown>(() => abort)
    controller.start({}, {})
    controller.dispose()
    controller.dispose()
    expect(abort).toHaveBeenCalledTimes(1)
  })

  it('上一轮还在跑时再 start：先中断旧流，避免两条流写同一份 state', () => {
    const first = vi.fn()
    const second = vi.fn()
    const fn = vi.fn().mockReturnValueOnce(first).mockReturnValueOnce(second)
    const controller = createStreamController<unknown, unknown>(fn)

    controller.start({}, {})
    controller.start({}, {})

    expect(first).toHaveBeenCalledTimes(1)
    expect(second).not.toHaveBeenCalled()
  })

  it('abort() 后可以重新 start（StrictMode 会挂载→卸载→再挂载）', () => {
    const fn = vi.fn(() => () => {})
    const controller = createStreamController<unknown, unknown>(fn)

    controller.start({}, {})
    controller.dispose()
    expect(controller.disposed).toBe(true)

    controller.start({}, {})
    expect(controller.disposed).toBe(false)
    expect(fn).toHaveBeenCalledTimes(2)
  })
})

/**
 * 接线断言。没有 jsdom 时无法渲染 hook，退而求其次：确认清理逻辑真的被接上，
 * 以及诊断状态真的挂在 App 上（否则"切页丢结果"的根因还在）。
 * 这类断言只保证"接线存在"，具体行为由上面的纯对象测试负责。
 *
 * ⚠️ 必须先去掉注释行再匹配：第一版断言直接搜全文，结果把**被注释掉的清理**
 * 也当成了接线成功（退化验证时发现的）。源码断言不剥注释就是自欺欺人。
 */
function stripComments(src: string): string {
  return src
    .split('\n')
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
    .join('\n')
}

describe('卸载清理已接线', () => {
  const hookCode = stripComments(readFileSync(resolve(HERE, 'useDiagnosisStream.ts'), 'utf8'))
  const appCode = stripComments(readFileSync(resolve(HERE, '..', 'App.tsx'), 'utf8'))

  it('hook 在 useEffect 清理里中断在途的流', () => {
    expect(hookCode).toContain('useEffect')
    expect(hookCode).toMatch(/useEffect\([\s\S]{0,200}controllerRef\.current\.dispose\(\)/)
  })

  it('诊断状态挂在 App 上并透传给诊断页，且不再用 route 作 key 强制重挂载', () => {
    expect(appCode).toContain('useDiagnosisStream()')
    expect(appCode).toContain('<DiagnosePage diagnosis={diagnosis} />')
    expect(appCode).not.toContain('key={route}')
  })

  it('历史页的请求带取消器与序号（防"旧响应覆盖新结果"）', () => {
    // 同一个"卸载/切换时不清理在途请求"的家族问题：
    // 历史页翻页时旧请求若不被取消，它先回来就会把新结果覆盖掉，
    // 表现是"点了下一页，列表闪一下又跳回上一页的内容"。
    const historyCode = stripComments(
      readFileSync(resolve(HERE, '..', 'pages', 'HistoryPage.tsx'), 'utf8'),
    )
    expect(historyCode).toContain('AbortController')
    expect(historyCode).toContain('controller.signal')
    // 序号兜底：abort 是尽力而为的，光有它挡不住"已经在返程路上"的响应
    expect(historyCode).toMatch(/seqRef\.current/)
    // 卸载时中断
    expect(historyCode).toMatch(/return \(\) => abortRef\.current\?\.abort\(\)/)
    // 用夹取后的 offset，而不是 (page - 1) * pageSize
    expect(historyCode).toContain('winRef.current.offset')
  })
})
