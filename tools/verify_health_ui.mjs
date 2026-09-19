/**
 * 前端真实渲染验证（顶栏状态灯 + 诊断页未就绪横幅）。
 *
 * ## 为什么必须真渲染
 *
 * 本项目踩过一次：`btnStyle` 的危险按钮变成**红底红字**（删除按钮整个隐形），
 * 而 `tsc` 通过、`vite build` 通过、肉眼 review 也通过——只有真的渲染出来才看得见。
 * 类型检查只保证"声明自洽"，不保证"渲染正确"。
 *
 * 所以改了前端的**样式与状态渲染**之后要跑一次这个脚本，
 * 而不是只跑 `vitest` + `tsc`（那两者都测不到"颜色到底有没有生效"）。
 *
 * ## 做法
 *
 * 用 Playwright 的路由拦截构造三种后端状态，而不是去折腾真实后端：
 *   - 只截 `/api/health/ready` / `/api/health/live` 两个请求，
 *     其余请求（页面资源）照常放行。
 *   - 这样三种状态都**确定性地**可复现，不依赖"后端此刻恰好处于启动中"
 *     （而启动中只有 1 秒窗口，靠真实后端根本抓不到）。
 *
 * 断言的是**计算后的样式**（getComputedStyle 拿到的 rgb），
 * 不是内联字符串——只有前者能证明颜色真的生效了。
 *
 * ## 运行
 *
 *   # 1) 起前端（后端无所谓，探活会被拦截）
 *   cd web && npm run dev
 *
 *   # 2) 装 playwright（可选依赖，不进 package.json：CI 里没有浏览器会直接跳过）
 *   npm install playwright
 *   npx playwright install chromium
 *
 *   # 3) 跑
 *   node tools/verify_health_ui.mjs
 *
 * 需要改 `EXECUTABLE` 指向本机的 chromium；本机若用 ms-playwright 默认路径则
 * 可直接删掉这个参数让 playwright 自己找。
 *
 * 断言依赖 `App.tsx` 上的两个 data-testid：`health-indicator` / `health-dot`
 * / `health-label`（还有 `data-health-state` 暴露当前状态）。
 * 改这三个属性名时必须同步改这里。
 */

import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const EXECUTABLE =
  'C:/Users/余梓岳/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe'

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? '  PASS' : '  FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`)
}

/** 把页面导航到诊断页并等 React 挂载完 */
async function open(page) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="health-indicator"]', { timeout: 15000 })
  // 等第一轮探活落地（hook 挂载后立刻探一次）
  await page.waitForTimeout(1200)
}

async function snapshot(page) {
  const dot = page.locator('[data-testid="health-dot"]')
  const label = page.locator('[data-testid="health-label"]')
  const indicator = page.locator('[data-testid="health-indicator"]')
  return {
    color: await dot.evaluate((el) => getComputedStyle(el).backgroundColor),
    size: await dot.evaluate((el) => {
      const s = getComputedStyle(el)
      return { w: s.width, h: s.height }
    }),
    label: (await label.textContent())?.trim(),
    state: await indicator.getAttribute('data-health-state'),
    title: await indicator.getAttribute('title'),
  }
}

const run = async () => {
  const browser = await chromium.launch({ executablePath: EXECUTABLE, headless: true })
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
  page.on('pageerror', (e) => console.log(`  [页面异常] ${e.message}`))

  // ---------- 场景 1：后端正常（live 200 + ready 200） ----------
  console.log('\n[场景 1] 后端已就绪')
  await page.route('**/api/health/live', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'alive', version: '1.3.0' }) }),
  )
  await page.route('**/api/health/ready', (r) =>
    r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ready: true, step: '启动完成', step_index: 5, step_total: 5,
        elapsed_ms: 6000, error: null, version: '1.3.0',
      }),
    }),
  )
  await open(page)
  let s = await snapshot(page)
  check('就绪时状态为 up', s.state === 'up', `state=${s.state}`)
  check('就绪时文案为"服务正常"', s.label === '服务正常', `label=${s.label}`)
  check('就绪时点是绿色（success）', s.color === 'rgb(15, 110, 86)', `color=${s.color}`)
  check('就绪时点真的渲染出了尺寸（不是被压成 0）', s.size.w === '6px' && s.size.h === '6px', `size=${s.size.w}x${s.size.h}`)
  const upColor = s.color
  const upLabel = s.label
  await page.screenshot({ path: 'D:/AgentDiag/_shot_up.png', fullPage: false })

  // ---------- 场景 2：后端正在启动（live 200 + ready 503） ----------
  console.log('\n[场景 2] 后端启动中（这是原缺陷的场景）')
  await page.unroute('**/api/health/ready')
  await page.route('**/api/health/ready', (r) =>
    r.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        ready: false, step: '加载向量库', step_index: 5, step_total: 5,
        elapsed_ms: null, error: null, version: '1.3.0',
      }),
    }),
  )
  await open(page)
  s = await snapshot(page)
  check('启动中状态为 starting（不能是 up）', s.state === 'starting', `state=${s.state}`)
  check('启动中文案为"正在启动"', s.label === '正在启动', `label=${s.label}`)
  check('启动中颜色与就绪不同', s.color !== upColor, `${s.color} vs ${upColor}`)
  check('启动中不能是警示色（那是正常过程）', s.color !== 'rgb(163, 45, 45)', `color=${s.color}`)
  check('启动中 hover 提示带上了后端上报的步骤', (s.title ?? '').includes('加载向量库'), `title=${s.title}`)
  // 顶栏里应显示步骤进度
  const navText = await page.locator('[data-testid="health-indicator"]').textContent()
  check('顶栏显示了启动步骤进度', navText.includes('加载向量库') && navText.includes('5/5'), `text=${navText?.replace(/\s+/g, ' ').trim()}`)
  // 诊断页横幅
  const banner = page.locator('text=后端正在启动，请稍候')
  check('诊断页出现"正在启动"横幅', (await banner.count()) > 0)
  const startBtn = page.locator('button', { hasText: '后端启动中…' })
  check('开始按钮被禁用并改了文案', (await startBtn.count()) > 0 && (await startBtn.first().isDisabled()))
  await page.screenshot({ path: 'D:/AgentDiag/_shot_starting.png', fullPage: false })

  // ---------- 场景 3：后端不可达（两个请求都失败） ----------
  console.log('\n[场景 3] 后端不可达')
  await page.unroute('**/api/health/live')
  await page.unroute('**/api/health/ready')
  await page.route('**/api/health/**', (r) => r.abort('connectionrefused'))
  await open(page)
  s = await snapshot(page)
  check('不可达状态为 down', s.state === 'down', `state=${s.state}`)
  check('不可达文案为"服务不可达"', s.label === '服务不可达', `label=${s.label}`)
  check('不可达是红色（danger）', s.color === 'rgb(163, 45, 45)', `color=${s.color}`)
  check('不可达与就绪的颜色不同', s.color !== upColor)
  check('不可达与启动中的颜色不同', s.label !== upLabel)
  const downBanner = page.locator('text=后端服务不可达')
  check('诊断页出现"不可达"横幅', (await downBanner.count()) > 0)
  const downBtn = page.locator('button', { hasText: '后端不可达' })
  check('开始按钮被禁用并提示不可达', (await downBtn.count()) > 0 && (await downBtn.first().isDisabled()))
  await page.screenshot({ path: 'D:/AgentDiag/_shot_down.png', fullPage: false })

  // ---------- 场景 4：页面隐藏时暂停轮询 ----------
  console.log('\n[场景 4] 轮询次数（验证"隐藏即暂停"真的生效）')
  await page.unroute('**/api/health/**')
  let polls = 0
  await page.route('**/api/health/**', (r) => {
    polls += 1
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ready: true, step: '启动完成', step_index: 5, step_total: 5, elapsed_ms: 1, error: null, version: '1.3.0' }) })
  })
  await open(page)
  const before = polls
  // 模拟标签页切到后台
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.waitForTimeout(6000)
  const hiddenDelta = polls - before
  check('页面隐藏后停止轮询（6 秒内新增 0 次）', hiddenDelta === 0, `隐藏期间新增 ${hiddenDelta} 次`)

  // 切回前台应立刻补探
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.waitForTimeout(1500)
  const resumeDelta = polls - before - hiddenDelta
  check('切回前台后立刻补探一次', resumeDelta >= 1, `恢复后新增 ${resumeDelta} 次`)

  await browser.close()

  const failed = results.filter((r) => !r.ok)
  console.log(`\n合计 ${results.length} 项，通过 ${results.length - failed.length}，失败 ${failed.length}`)
  if (failed.length) {
    console.log('失败项：')
    for (const f of failed) console.log(`  - ${f.name}  (${f.detail})`)
    process.exit(1)
  }
}

run().catch((e) => {
  console.error('验证脚本异常：', e)
  process.exit(2)
})
