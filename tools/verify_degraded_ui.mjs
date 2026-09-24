/**
 * 降级工单真实渲染验证（`#/history` → 展开降级记录 → 风险说明）。
 *
 * ## 为什么必须真渲染
 *
 * 与 `verify_health_ui.mjs` / `verify_stats_ui.mjs` 同样的理由：`tsc` 绿 +
 * `vitest` 绿 + `build` 成功 ≠ 界面正确。本项目踩过 `btnStyle` 变成红底红字
 * （按钮整个隐形）的事故，那三项检查一个都没拦住。
 *
 * 本次要验的是一个**更隐蔽**的同类问题：`whiteSpace: 'pre-line'` 的缺失。
 * 降级工单的「风险说明」是多行文本（末段是"参考方向"的逐条排查动作）：
 *
 *     知识库未覆盖该故障的相关依据……可尝试：……
 *     以下排查动作来自知识库中「数控机床」的同类症状条目——非本设备根因，执行前请现场确认
 *     - 停机后手动盘车，判断异响是否随转速升高而加剧。
 *     - 检查主轴轴承润滑脂状态，必要时清洗后重新加注。
 *
 * 少了 `pre-line`，HTML 会把换行折叠成空格，几条动作挤成一坨 —— 字符串里
 * 明明有 `\n`（单测能过），用户看到的却是一行。**只有量渲染后的行数才能发现。**
 *
 * ## 判据（而不是"看起来对"）
 *
 * 用 `Range.getClientRects()` 数文本的**视觉行数**（按 rect.top 去重）：
 *   · 现状行数 ≥ 3  → 换行真的生效了
 *   · 把 whiteSpace 临时改回 `normal` 再量 → 行数应当**明显变少**
 * 两条一起才构成证据：只测前者，无法排除"这文本本来就短、一行也够"。
 * 断言一律走 `getComputedStyle` / 几何量，不看内联字符串。
 *
 * 前置条件：
 *   1) 后端已起：`venv/Scripts/python.exe -m uvicorn api:app --port 8000`
 *   2) 前端已起：`cd web && npm run dev`
 *   3) 库里**有降级记录**（`status = insufficient_knowledge` 且有参考方向）。
 *      没有就先用一条知识库外的故障跑一次诊断。
 *
 * 运行（playwright 装在隔离工作区，ESM 不读 NODE_PATH，所以脚本要放在该目录下跑）：
 *   cp D:/AgentDiag/tools/verify_degraded_ui.mjs <node-workspace>/
 *   cd <node-workspace> && node verify_degraded_ui.mjs
 */

import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const EXECUTABLE =
  'C:/Users/余梓岳/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe'
const SHOT = 'D:/AgentDiag/_shot_degraded_note.png'

// 参考方向里那句免责说明是全项目独一份的措辞，拿它定位最稳
const ANCHOR = '非本设备根因'

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? '  PASS' : '  FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`)
}

/** 在浏览器里定位「风险说明」那个 div，并量出它的视觉行数。
 *  `ws` 传入时临时改掉 whiteSpace，用来做对照（量完还原，不污染页面）。
 *  注意：这个函数会被序列化后丢进浏览器执行，所以**不能引用外部变量**。 */
const measure = ({ anchor, ws }) => {
  const all = [...document.querySelectorAll('div')]
  const hit = all.filter((d) => d.textContent.includes(anchor))
  // 取最内层：子元素里不再包含该文本的那个
  const inner = hit.filter((d) => ![...d.children].some((c) => c.textContent.includes(anchor)))
  if (!inner.length) return null
  const el = inner[inner.length - 1]
  const original = el.style.whiteSpace
  if (ws) el.style.whiteSpace = ws

  const cs = getComputedStyle(el)
  const range = document.createRange()
  range.selectNodeContents(el)
  const rects = [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0)
  const visualLines = new Set(rects.map((r) => Math.round(r.top))).size

  const out = {
    whiteSpace: cs.whiteSpace,
    lineHeight: parseFloat(cs.lineHeight),
    boxHeight: Math.round(el.getBoundingClientRect().height),
    visualLines,
    text: el.textContent,
    newlineCount: (el.textContent.match(/\n/g) || []).length,
  }
  if (ws) el.style.whiteSpace = original
  return out
}

const run = async () => {
  const browser = await chromium.launch({
    executablePath: EXECUTABLE,
    headless: true,
    // 本机 ~/.gitconfig 配了 http.proxy=127.0.0.1:7897，不绕过的话
    // 连 http://127.0.0.1:5173 都会被代理拦掉。
    args: ['--no-proxy-server'],
  })
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('pageerror', (e) => console.log(`  [页面异常] ${e.message}`))

  await page.goto(`${BASE}/#/history`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=诊断历史', { timeout: 20000 })
  await page.waitForTimeout(1200) // 等 /api/records 回来并渲染

  console.log('\n[1] 找到降级记录并展开')
  const rows = page.locator('div', { hasText: '注塑机' })
  const rowCount = await rows.count()
  check('历史页渲染出记录', rowCount > 0, `含"注塑机"的 div 数=${rowCount}`)
  if (!rowCount) {
    await page.screenshot({ path: SHOT, fullPage: true })
    await browser.close()
    console.log('\n⚠ 没找到降级记录，无法继续。请先跑一条知识库外的故障。')
    process.exit(1)
  }

  // 点最内层的记录行把它展开（默认是折叠的）
  const row = page.getByText('注塑机主轴异响').first()
  await row.click()
  await page.waitForTimeout(600)

  console.log('\n[2] 风险说明的渲染结果')
  const m = await page.evaluate(measure, { anchor: ANCHOR })
  check('页面上找得到「风险说明」（含参考方向）', !!m)
  if (!m) {
    await page.screenshot({ path: SHOT, fullPage: true })
    await browser.close()
    process.exit(1)
  }

  console.log(`       whiteSpace=${m.whiteSpace}  lineHeight=${m.lineHeight}px  ` +
    `盒高=${m.boxHeight}px  视觉行数=${m.visualLines}  文本内换行符=${m.newlineCount}`)
  console.log('       渲染文本：')
  for (const ln of m.text.split('\n')) console.log(`         | ${ln}`)

  check('风险说明确实含换行符（数据侧是多行的）', m.newlineCount >= 2, `\\n 数=${m.newlineCount}`)
  check("样式解析为 pre-line（不是 normal / 空）", m.whiteSpace === 'pre-line', `实际=${m.whiteSpace}`)
  check(
    '渲染成 3 行以上（换行真的生效，不是被折叠成一坨）',
    m.visualLines >= 3,
    `视觉行数=${m.visualLines}`,
  )
  check(
    '盒高与行数相符（排除"行数对但被 overflow 截断"）',
    m.boxHeight >= m.lineHeight * m.visualLines * 0.9,
    `盒高=${m.boxHeight} 行高=${m.lineHeight} 行数=${m.visualLines}`,
  )

  console.log('\n[3] 对照实验：把 whiteSpace 改回 normal 应当被折叠')
  const collapsed = await page.evaluate(measure, { anchor: ANCHOR, ws: 'normal' })
  console.log(`       whiteSpace=normal 时视觉行数=${collapsed?.visualLines}`)
  check(
    '改成 normal 后行数明显减少（证明 pre-line 是必需的，不是可有可无）',
    collapsed && collapsed.visualLines < m.visualLines,
    `pre-line=${m.visualLines} 行 → normal=${collapsed?.visualLines} 行`,
  )

  await page.screenshot({ path: SHOT, fullPage: true })
  console.log(`\n截图已保存：${SHOT}`)
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
