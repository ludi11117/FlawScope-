/**
 * 统计页真实渲染验证（`#/stats`）。
 *
 * 与 `verify_health_ui.mjs` 同样的理由：`tsc` 绿 + `build` 成功 ≠ 界面正确。
 * 本页最容易出的三类"只有渲染出来才看得见"的 bug：
 *   1. 状态色胶囊 background/color 取自 `statusMeta()`，取不到时是透明底 → 胶囊隐形；
 *   2. 条形宽度若为 0 或 NaN%，浏览器静默忽略 → 数据"看起来不存在"；
 *   3. 指标卡 flex 布局在窄视口被压成 0 宽。
 * 所以断言一律走 `getComputedStyle`，不看内联字符串。
 *
 * 与 health 脚本的差别：**这里打真实后端**（`/api/stats` 不拦），
 * 因为要验的正是"真实数据 → 真实条形"这条链路。前置条件：
 *   1) 后端已起：`venv/Scripts/python.exe -m uvicorn api:app --port 8000`
 *   2) 前端已起：`cd web && npm run dev`
 *   3) 库里**有记录**（否则走空态，验不到条形图）
 *
 * 运行（playwright 装在隔离工作区，ESM 不读 NODE_PATH，所以脚本要放在该目录下跑）：
 *   cd C:/Users/余梓岳/.workbuddy-ai/binaries/node/workspace
 *   node verify_stats_ui.mjs
 */

import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const EXECUTABLE =
  'C:/Users/余梓岳/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe'
const SHOT = 'D:/AgentDiag/_shot_stats.png'

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? '  PASS' : '  FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`)
}

const run = async () => {
  const browser = await chromium.launch({
    executablePath: EXECUTABLE,
    headless: true,
    // 本机 ~/.gitconfig 配了 http.proxy=127.0.0.1:7897，若不绕过，
    // 连 http://127.0.0.1:5173 都会被代理拦掉。
    args: ['--no-proxy-server'],
  })
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('pageerror', (e) => console.log(`  [页面异常] ${e.message}`))

  // 直接走真实后端；只记录，不拦截
  let statsResp = null
  page.on('response', async (r) => {
    if (r.url().includes('/api/stats')) {
      statsResp = { status: r.status(), body: await r.json().catch(() => null) }
    }
  })

  await page.goto(`${BASE}/#/stats`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="stats-metric"]', { timeout: 20000 })
  await page.waitForTimeout(1200) // 等 /api/stats 回来并重渲染

  console.log('\n[1] 数据链路')
  check('/api/stats 返回 200', statsResp?.status === 200, `status=${statsResp?.status}`)
  const total = statsResp?.body?.total_records
  check('后端返回了 total_records', typeof total === 'number', `total_records=${total}`)
  check(
    '后端返回了 db_size_bytes（本次迁移新增字段）',
    typeof statsResp?.body?.db_size_bytes === 'number',
    `db_size_bytes=${statsResp?.body?.db_size_bytes}`,
  )
  if (!total) {
    console.log('\n⚠ 库里没有记录，本次只验空态；要验条形图请先造数据。')
  }

  console.log('\n[2] 指标卡')
  const metrics = page.locator('[data-testid="stats-metric"]')
  const metricCount = await metrics.count()
  check('渲染出 4 个指标卡', metricCount === 4, `count=${metricCount}`)
  const metricBoxes = []
  for (let i = 0; i < metricCount; i++) {
    const el = metrics.nth(i)
    metricBoxes.push({
      label: (await el.locator('div').first().textContent())?.trim(),
      value: (await el.locator('div').nth(1).textContent())?.trim(),
      width: await el.evaluate((n) => getComputedStyle(n).width),
    })
  }
  for (const m of metricBoxes) console.log(`      · ${m.label} = ${m.value}  (w=${m.width})`)
  check('指标卡都有非零宽度', metricBoxes.every((m) => parseFloat(m.width) > 0))
  check('指标卡都渲染出了值（不是占位 —）', metricBoxes.every((m) => m.value && m.value !== '—'))
  check(
    `总记录数卡片显示 ${total}`,
    metricBoxes.some((m) => m.label === '总记录数' && m.value === String(total)),
    `实际=${JSON.stringify(metricBoxes.map((m) => [m.label, m.value]))}`,
  )

  console.log('\n[3] 状态分布条形')
  const emptyShown = await page.locator('[data-testid="stats-empty"]').count()
  const rows = page.locator('[data-testid="stats-row"]')
  const rowCount = await rows.count()
  if (total > 0) {
    check('有数据时不显示空态', emptyShown === 0)
    check('渲染出状态行', rowCount > 0, `count=${rowCount}`)

    const parsed = []
    for (let i = 0; i < rowCount; i++) {
      const row = rows.nth(i)
      const status = await row.getAttribute('data-status')
      const chip = row.locator('span').first()
      const bar = row.locator('span > span').first()
      const track = row.locator('span').nth(1)
      parsed.push({
        status,
        chipBg: await chip.evaluate((n) => getComputedStyle(n).backgroundColor),
        chipColor: await chip.evaluate((n) => getComputedStyle(n).color),
        barW: await bar.evaluate((n) => getComputedStyle(n).width),
        trackW: await track.evaluate((n) => getComputedStyle(n).width),
        texts: (await row.textContent())?.replace(/\s+/g, ' ').trim(),
      })
    }
    for (const p of parsed) {
      console.log(`      · ${p.status}  chip=${p.chipBg}  bar=${p.barW}/${p.trackW}  "${p.texts}"`)
    }

    check('状态行数与后端 by_status 项数一致', rowCount === Object.keys(statsResp?.body?.by_status ?? {}).length)
    check(
      '按数量降序（字典序看不出主次）',
      JSON.stringify(parsed.map((p) => p.status)) ===
        JSON.stringify(
          Object.entries(statsResp?.body?.by_status ?? {})
            .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
            .map(([s]) => s),
        ),
      `渲染=${parsed.map((p) => p.status).join(',')}`,
    )
    check(
      '状态胶囊不是透明底（隐形 bug）',
      parsed.every((p) => p.chipBg !== 'rgba(0, 0, 0, 0)' && p.chipBg !== 'transparent'),
      parsed.map((p) => p.chipBg).join(' | '),
    )
    check(
      '状态胶囊文字与底色不同（红底红字同类 bug）',
      parsed.every((p) => p.chipColor !== p.chipBg),
    )
    check(
      '每根条形都有非零像素宽度（0 宽 = 数据看起来不存在）',
      parsed.every((p) => parseFloat(p.barW) > 0),
      parsed.map((p) => p.barW).join(' | '),
    )
    check(
      '条形宽度不超过轨道',
      parsed.every((p) => parseFloat(p.barW) <= parseFloat(p.trackW) + 1),
    )
    check('条形宽度不是 NaN%（NaN 会被浏览器静默忽略）', parsed.every((p) => !p.barW.includes('NaN')))

    // 占比文案
    const pctTexts = parsed.map((p) => p.texts.match(/(\d+\.\d)%/)?.[1])
    check(
      '每行都渲染出占比文案（保留一位小数）',
      pctTexts.every((t) => t !== undefined),
      pctTexts.join(' | '),
    )
  } else {
    check('空库时显示空态', emptyShown === 1)
  }

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
