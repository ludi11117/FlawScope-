/**
 * 后端可用性探活的纯逻辑（`src/api/health.ts`）。
 *
 * 单独抽出来的理由与 `sse.ts` / `historyUtils.ts` 一致：组件只负责渲染，
 * 判定逻辑要能被直接测。这里的判定有两条容易被写错、且写错之后
 * **在页面上看不出来**（顶栏那个点永远绿着）：
 *
 *   1. fetch 不会因为 4xx/5xx 抛错，必须显式检查 `resp.ok`——
 *      否则后端返回 500 时会被当成健康。
 *   2. 网络层失败（后端没起、被防火墙挡）走的是 catch 分支，
 *      与"返回了非 200"是两种不同的失败，但**对用户而言都是"连不上"**。
 *      两者都必须收敛成 down，不能被漏掉一种。
 *
 * ## 为什么有四个状态而不是三个
 *
 * 原先只有 up / degraded / down，于是**冷启动窗口被误报成 up**：
 * `/health/live` 只要进程活着就返回 200，而初始化要花十几秒
 * （import 链 + 向量库 + BM25 分词），这期间它照样 200。
 * 状态灯显示"服务正常"、用户点下"开始诊断"、第一次请求必然失败——
 * 探针在真实可用之前就宣告可用。
 *
 * 现在拆成两级探针，状态灯以 readiness 为准：
 *   - `/health/ready` 200 → up       可以诊断
 *   - `/health/ready` 503 → starting 初始化中（**不是故障**，别报红吓人）
 *   - live 通、ready 不通 → starting
 *   - live 都不通        → down
 *
 * `starting` 与 `down` 必须分开：前者是"稍等一下"，后者是"后端根本没起来"，
 * 让用户对同一件事采取不同行动（等待 / 去启动后端）。
 */

/** 探活结果。degraded 预留给"活着但依赖异常"。 */
export type HealthState = 'up' | 'starting' | 'down' | 'degraded'

export interface HealthPayload {
  status: string
  version?: string
}

/** `/health/ready` 的响应体。未就绪时后端返回 503 + 同样的结构。 */
export interface ReadyPayload {
  ready: boolean
  step: string
  step_index: number
  step_total: number
  elapsed_ms: number | null
  error: string | null
  version: string
}

/** 一次探活的原始观测结果，由 `useHealth` 采集、交给下面的纯函数判定。 */
export interface ProbeObservation {
  /**
   * 进程是否活着（`/health/live` 成功）。网络层失败时为 false。
   * 为 null 表示"live 这一步没探"（不该出现，留作显式表达）。
   */
  live: boolean
  /**
   * `/health/ready` 的状态码；网络层失败为 null。
   * 200 = 已就绪，503 = 初始化中，其他 = 异常。
   */
  readyStatus: number | null
  /** ready 就绪时的响应体，用于取版本号与步骤文案。 */
  ready: ReadyPayload | null
}

/**
 * 把一次探活观测归一成状态。
 *
 * 判定顺序有讲究：**先看 ready 再看 live**。
 * 反过来的话，一个"活着但没就绪"的后端会被判成 up——
 * 正是这次要修的缺陷。
 */
export function healthStateFromProbe(obs: ProbeObservation): HealthState {
  if (obs.readyStatus === 200) return 'up'
  // 503 是后端"有意表达的未就绪"，不是错误
  if (obs.readyStatus === 503) return 'starting'
  // ready 端点不存在（404，旧后端 / 网关没配路由）时回退到 liveness 判定：
  // 活着但问不出就绪状态 → 保守当作 starting，而不是乐观当作 up。
  if (obs.readyStatus === 404) return obs.live ? 'starting' : 'down'
  // 其余非 200/503 的状态码（500 等）是**真故障**，不能当成"还在进行"。
  // 否则启动崩溃后状态灯会永远显示"正在启动"，用户一直等下去。
  if (obs.readyStatus !== null) return 'down'
  // ready 根本没探到（网络层失败）
  return obs.live ? 'starting' : 'down'
}

/** 兼容入口：只看一次 HTTP 响应是否成功。被 `nextHealthValue` 与测试使用。 */
export function healthStateFromResponse(resp: { ok: boolean } | null): HealthState {
  if (!resp) return 'down'
  return resp.ok ? 'up' : 'down'
}

/**
 * 顶栏状态灯的展示元数据。
 *
 * `label` 刻意用能独立成句的措辞（"服务正常 / 正在启动 / 服务不可达"）
 * 而不是"绿 / 红"——状态灯是给色盲用户和截图看的，
 * 只靠颜色区分是不合格的。
 *
 * starting 用蓝色（accent）而不是琥珀色：它不是一种"警告"，
 * 是正常的启动过程，用警示色会误导用户以为出了问题。
 */
export const HEALTH_META: Record<
  HealthState,
  { label: string; color: string; halo: string }
> = {
  up: { label: '服务正常', color: 'var(--success)', halo: 'rgba(15, 110, 86, 0.12)' },
  starting: { label: '正在启动', color: 'var(--accent)', halo: 'rgba(83, 74, 183, 0.14)' },
  degraded: { label: '服务降级', color: 'var(--warning)', halo: 'rgba(186, 117, 23, 0.14)' },
  down: { label: '服务不可达', color: 'var(--danger)', halo: 'rgba(163, 45, 45, 0.14)' },
}

/**
 * 启动中时状态灯的**hover 提示**：把后端上报的具体步骤显示出来。
 *
 * 没有它就只剩"正在启动"四个字，用户不知道还要等多久、
 * 也不知道卡住了没有——十几秒的等待如果没有任何进度反馈，
 * 用户会以为页面坏了。
 */
export function startupHint(ready: ReadyPayload | null): string {
  if (!ready) return '后端进程已就绪，正在初始化…'
  if (ready.error) return `启动失败：${ready.error}`
  const total = ready.step_total
  const idx = ready.step_index
  return total > 0 ? `${ready.step}（${idx}/${total}）` : ready.step
}

/** 探活轮询间隔。5s 是在"及时发现后端掉了"与"不要白刷请求"之间的取舍。 */
export const HEALTH_POLL_MS = 5000

/**
 * 启动期间的轮询间隔。比稳态更密，理由：启动窗口只有十几秒，
 * 5 秒一次意味着用户最多要等 5 秒才看到"可以用了"；
 * 1.5 秒一次能把等待感压下去，而这段时间本来就在等，多几次内存态请求可忽略。
 */
export const HEALTH_STARTUP_POLL_MS = 1500

/**
 * 本次探活后，下一次轮询应该用多长的间隔。
 *
 * 抽成函数是为了可测：这个"启动时加密轮询、就绪后放宽"的行为
 * 如果埋在 hook 里，就只能靠渲染组件来验证，而本项目没有 jsdom。
 */
export function nextPollInterval(state: HealthState, baseMs: number): number {
  return state === 'starting' ? HEALTH_STARTUP_POLL_MS : baseMs
}

/**
 * 探活成功后如何更新内部状态。
 *
 * 单独抽出来是因为这里有一个**非平凡的正确性要求**：
 * 版本号只在真正拿到值时更新，失败时保持原值。
 * 直觉写法（每次都 `setVersion(payload?.version ?? null)`）会在后端
 * 抖动一下之后把已知版本号擦成 null——那不是"状态更新"，是信息丢失。
 *
 * 注意"拿到值"的判据是 `ready` 对象存在且带 version，
 * **不是**"这次探活成功"：启动中（503）时后端也回了 version，
 * 那时同样应该把它记下来。
 *
 * @param prev 之前的版本号
 * @param obs  本次探活观测
 */
export function nextHealthValue(
  prev: { version: string | null },
  obs: ProbeObservation,
): { state: HealthState; version: string | null } {
  const state = healthStateFromProbe(obs)
  // 只有 200（up）才带完整 payload；503 时前端不解析 body，
  // 所以 version 的更新只在真正就绪时发生。
  // 用 `||` 而不是 `??`：后端若返回空字符串的 version，也算"没给"，
  // 应该保留旧值（空串会把界面上已知的版本号擦掉）。
  const version = obs.ready?.version || prev.version
  return { state, version }
}

/**
 * 是否应当轮询。页面隐藏时不轮询。
 *
 * 抽成函数的理由：这个判断原本藏在 effect 里的 `if (document.hidden)`，
 * 于是"页面隐藏时停止轮询"这条行为只能靠渲染组件才能测——
 * 而本项目**没有 jsdom / testing-library**，所有测试都是纯逻辑的。
 * 抽出来之后它就是一个可以直测的布尔函数。
 */
export function shouldPoll(hidden: boolean): boolean {
  return !hidden
}
