/**
 * 探活判定（`src/api/health.ts`）。
 *
 * 这些断言之所以值得写，是因为它们守的东西**在页面上看不出来**：
 * 状态灯只要判定逻辑退化，就会永远显示"服务正常"——绿着，
 * 而绿着是所有失败里最容易被忽略的一种（用户会以为后端好着）。
 *
 * 本文件经历过一次真实的教训：第一版只看 `/health/live`，
 * 而那个端点**在启动过程中也返回 200**（进程确实活着）。
 * 于是冷启动的十几秒里状态灯显示"服务正常"，用户点下"开始诊断"必然失败。
 * 测试当时是绿的——因为断言本身写的是"live 通即为 up"，
 * **与错误实现完全一致**。所以下面针对 starting 的用例不是补充覆盖率，
 * 是这个缺陷的判别式。
 *
 * 按项目惯例，每条"该放行的"都配一条"该拦下的"。
 */

import { describe, expect, it } from 'vitest'
import {
  HEALTH_META,
  HEALTH_STARTUP_POLL_MS,
  healthStateFromProbe,
  healthStateFromResponse,
  nextHealthValue,
  nextPollInterval,
  shouldPoll,
  startupHint,
  type HealthState,
  type ProbeObservation,
} from './health'

/** 造一个观测结果，只传关心的字段，避免每条用例都写全五个字段。 */
function obs(partial: Partial<ProbeObservation>): ProbeObservation {
  return { live: true, readyStatus: null, ready: null, ...partial }
}

const READY_OK: ProbeObservation = obs({
  live: true,
  readyStatus: 200,
  ready: {
    ready: true,
    step: '启动完成',
    step_index: 5,
    step_total: 5,
    elapsed_ms: 12000,
    error: null,
    version: '1.3.0',
  },
})

describe('healthStateFromProbe —— 启动窗口（本文件存在的理由）', () => {
  it('ready 返回 503 时必须判为 starting，而不是 up', () => {
    // 判别式：后端初始化中，live 已经是 200，ready 还是 503。
    // 只看 live 的实现会给出 up —— 那正是原始缺陷。
    const state = healthStateFromProbe(obs({ live: true, readyStatus: 503 }))
    expect(state).toBe('starting')
    expect(state).not.toBe('up')
  })

  it('ready 200 才判为 up', () => {
    expect(healthStateFromProbe(READY_OK)).toBe('up')
  })

  it('live 通但 ready 端点不存在（旧后端）→ 保守判 starting，不判 up', () => {
    // 回退策略的方向性取舍：宁可让用户多等一次轮询，
    // 也不能在没确认就绪的情况下宣告"服务正常"。
    const state = healthStateFromProbe(obs({ live: true, readyStatus: 404 }))
    expect(state).toBe('starting')
    expect(state).not.toBe('up')
  })

  it('live 也不通 → down（这时才该提示"去启动后端"）', () => {
    expect(healthStateFromProbe(obs({ live: false, readyStatus: null }))).toBe('down')
  })

  it('starting 与 down 必须分开——一个该等，一个该去启动进程', () => {
    const starting = healthStateFromProbe(obs({ live: true, readyStatus: 503 }))
    const down = healthStateFromProbe(obs({ live: false, readyStatus: null }))
    expect(starting).not.toBe(down)
  })

  it('判定顺序：先 ready 后 live，不能反', () => {
    // 反过来的实现（先看 live）会把"活着但没就绪"判成 up。
    // 这条用例用"live=true + ready=503"钉住顺序依赖。
    expect(healthStateFromProbe(obs({ live: true, readyStatus: 503 }))).toBe('starting')
  })

  it('ready 返回 500（真故障）不能当成 starting', () => {
    // 500 是初始化**失败**而不是"还在进行"，必须与 503 区分，
    // 否则启动崩了之后状态灯会永远显示"正在启动"，用户一直等下去。
    expect(healthStateFromProbe(obs({ live: true, readyStatus: 500 }))).not.toBe('starting')
    expect(healthStateFromProbe(obs({ live: true, readyStatus: 500 }))).toBe('down')
  })
})

describe('healthStateFromResponse', () => {
  it('2xx 视为正常', () => {
    expect(healthStateFromResponse({ ok: true })).toBe('up')
  })

  it('网络层失败（resp 为 null）视为不可达，而不是"未知"', () => {
    // 后端起不来时走的是 fetch 抛异常的路径，这里必须收敛成 down。
    // 若漏掉这一支，"后端没起"会变成无状态 → 灯不亮但不报错。
    expect(healthStateFromResponse(null)).toBe('down')
  })

  it('非 2xx 视为不可达', () => {
    // fetch 不会因 4xx/5xx 抛错，必须显式检查 ok。
    expect(healthStateFromResponse({ ok: false })).toBe('down')
  })

  it('只有 ok 为真才算正常——不能把"拿到了响应"当成健康', () => {
    const up = healthStateFromResponse({ ok: true })
    const notOk = healthStateFromResponse({ ok: false })
    expect(up).not.toBe(notOk)
  })
})

describe('HEALTH_META', () => {
  const states: HealthState[] = ['up', 'starting', 'down', 'degraded']

  it('四态都有文案与颜色', () => {
    for (const s of states) {
      expect(HEALTH_META[s].label).toBeTruthy()
      expect(HEALTH_META[s].color).toBeTruthy()
    }
  })

  it('不同状态的文案必须不同（否则看不出区别）', () => {
    const labels = states.map((s) => HEALTH_META[s].label)
    expect(new Set(labels).size).toBe(states.length)
  })

  it('不同状态的颜色必须不同', () => {
    const colors = states.map((s) => HEALTH_META[s].color)
    expect(new Set(colors).size).toBe(states.length)
  })

  it('不可达不能用成功色', () => {
    expect(HEALTH_META.down.color).not.toBe(HEALTH_META.up.color)
  })

  it('starting 不能用警示色——启动是正常过程，用警示色会误导用户以为出错了', () => {
    expect(HEALTH_META.starting.color).not.toBe(HEALTH_META.degraded.color)
    expect(HEALTH_META.starting.color).not.toBe(HEALTH_META.down.color)
  })
})

describe('nextPollInterval —— 启动期加密轮询', () => {
  it('starting 时用更短的间隔', () => {
    expect(nextPollInterval('starting', 5000)).toBe(HEALTH_STARTUP_POLL_MS)
  })

  it('就绪后用调用方给的基准间隔', () => {
    expect(nextPollInterval('up', 5000)).toBe(5000)
  })

  it('启动间隔必须真的比基准短，否则这个优化是假的', () => {
    // 判别式：如果实现写成 `return baseMs`，上面两条里至少这条会红。
    expect(nextPollInterval('starting', 5000)).toBeLessThan(5000)
  })

  it('down 时不该用启动间隔（后端都没起，刷再快也没用）', () => {
    expect(nextPollInterval('down', 5000)).toBe(5000)
  })
})

describe('startupHint', () => {
  it('没有 ready 信息时给出通用文案', () => {
    expect(startupHint(null)).toBeTruthy()
  })

  it('带步骤信息时显示步骤名与序号', () => {
    const hint = startupHint({
      ready: false,
      step: '加载向量库',
      step_index: 5,
      step_total: 5,
      elapsed_ms: null,
      error: null,
      version: '1.3.0',
    })
    expect(hint).toContain('加载向量库')
    expect(hint).toContain('5/5')
  })

  it('启动失败时优先显示错误，而不是继续报"正在加载"', () => {
    // 这条防的是"启动崩了但界面一直显示进度"——用户会无限等下去。
    const hint = startupHint({
      ready: false,
      step: '启动失败',
      step_index: 0,
      step_total: 0,
      elapsed_ms: null,
      error: 'ConnectionError: 无法连接向量库',
      version: '1.3.0',
    })
    expect(hint).toContain('无法连接向量库')
  })

  it('step_total 为 0 时不显示"0/0"这种无意义序号', () => {
    const hint = startupHint({
      ready: false,
      step: '进程已启动，等待初始化',
      step_index: 0,
      step_total: 0,
      elapsed_ms: null,
      error: null,
      version: '1.3.0',
    })
    expect(hint).not.toContain('0/0')
    expect(hint).toContain('进程已启动')
  })
})

describe('nextHealthValue', () => {
  it('就绪 → up，并更新版本号', () => {
    const next = nextHealthValue({ version: null }, READY_OK)
    expect(next.state).toBe('up')
    expect(next.version).toBe('1.3.0')
  })

  it('探活失败 → down，但**保留**已知版本号', () => {
    // 这是这个函数存在的唯一理由。直觉写法会把版本号刷成 null，
    // 于是后端抖一下之后界面上就再也看不到版本了。
    const next = nextHealthValue(
      { version: '1.3.0' },
      obs({ live: false, readyStatus: null }),
    )
    expect(next.state).toBe('down')
    expect(next.version).toBe('1.3.0')
  })

  it('启动中（503）不更新版本号，因为前端不解析 503 的 body', () => {
    // 这里刻意把 ready 传成 null：client.ts 在非 200 时不回填 body。
    // 若实现写成"只要有 version 就更新"，把 503 的 body 也传进来就会出错，
    // 所以这条钉住的是"version 只来自 200 的 body"这个约定。
    const next = nextHealthValue(
      { version: '1.3.0' },
      obs({ live: true, readyStatus: 503, ready: null }),
    )
    expect(next.state).toBe('starting')
    expect(next.version).toBe('1.3.0')
  })

  it('就绪 body 里没有 version 时保留旧值', () => {
    const next = nextHealthValue(
      { version: '1.3.0' },
      obs({
        readyStatus: 200,
        ready: {
          ready: true,
          step: '启动完成',
          step_index: 5,
          step_total: 5,
          elapsed_ms: 1,
          error: null,
          version: '',
        },
      }),
    )
    expect(next.state).toBe('up')
    expect(next.version).toBe('1.3.0')
  })

  it('只有就绪才变 up——启动中绝不能是 up（这是原 bug 的形态）', () => {
    const starting = nextHealthValue({ version: null }, obs({ live: true, readyStatus: 503 }))
    const up = nextHealthValue({ version: null }, READY_OK)
    expect(up.state).toBe('up')
    expect(starting.state).not.toBe(up.state)
  })

  it('空版本号首次就绪后能被填上', () => {
    const next = nextHealthValue({ version: null }, READY_OK)
    expect(next.version).toBe('1.3.0')
  })
})

describe('shouldPoll', () => {
  it('页面可见时轮询', () => {
    expect(shouldPoll(false)).toBe(true)
  })

  it('页面隐藏时停止轮询（后台标签页没理由每 5 秒发请求）', () => {
    expect(shouldPoll(true)).toBe(false)
  })

  it('可见与隐藏必须给出相反结论', () => {
    expect(shouldPoll(true)).not.toBe(shouldPoll(false))
  })
})
