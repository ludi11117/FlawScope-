# 批次 A 完成报告（必须修：真 bug + 免鉴权烧配额）

日期：2026-09-25 ｜ 基线：`403 passed` → 本批后 `409 passed`（后端）、前端 `141` → `175`

---

## 改动清单

| # | 文件:行 | 改了什么 | 为什么 |
|---|---|---|---|
| A1 | `web/src/hooks/diagnosisContext.ts`（新增） | 新增纯函数模块：`buildContext` / `composePayload` / `appendUserTurn` / `attachFollowup`，轮数上限 5、上下文预算 3000 字、整段上限 4000 字（对齐后端 `max_length`） | 上下文自我嵌套的根因是「turns 里存了本轮实际发出去的 payload」。把「拼上下文」与「存历史」拆成两件事，并加两道闸 |
| A1 | `web/src/hooks/useDiagnosisStream.ts:81` | reducer 的 `start` 改为 `appendUserTurn(state.turns, action.userInput)`（只存原文） | 同上；存原文后下一轮才不会把整段 payload 再套一层 `第N轮：` |
| A1 | `web/src/hooks/useDiagnosisStream.ts:172-198` | `start()` 内部用 `composePayload(turnsRef, userInput)` 临时组装，不再写回 state | 上下文只在发请求那一刻存在，state 里永远只有原话 |
| A1 | `web/src/pages/DiagnosePage.tsx:71-80` | `onSubmit` 不再自己拼上下文，只 `start(text, imageBase64)` | 消除「两处都能拼上下文」的双份实现 |
| A2 | `web/src/api/errorDetail.ts`（新增） | `extractDetail(body)` 兼容 string / 数组（拼 `loc.msg`）/ 缺失，数组时去掉开头的 `body` 段 | 422 的 `detail` 是数组，旧代码直接赋值 → `state.error` 变成对象 → 渲染抛错 → 白屏 |
| A2 | `web/src/api/client.ts:63`、`:198-208` | 两处错误分支都改走 `extractDetail` | 业务端点与 SSE 端点共用一份口径 |
| A2 | `web/src/pages/DiagnosePage.tsx:198-250` | `textarea` 加 `maxLength={4000}`、`id`、视觉隐藏 `label`、接近上限时的实时字数提示 | 让用户「写不超」比事后报错便宜 |
| A2 | `web/src/components/ErrorBoundary.tsx`（新增）、`web/src/main.tsx` | 最外层加 ErrorBoundary，兜底页显示「界面出错」+ 刷新/重试 | React 渲染期抛错会卸载整棵树；没有边界就是一片白 |
| A3 | `api.py:479-501` | `health(fresh, x_api_key)`：`fresh=true` 时调用 `require_api_key(x_api_key)` | `/health?fresh=true` 真调 1 次 LLM + 2 次 Embedding，文档承诺的鉴权在代码里不存在 |
| A4 | `web/src/hooks/streamLifecycle.ts`（新增） | `createStreamController`：启动 / 中断 / 卸载中断；`start` 会重新武装（兼容 StrictMode） | 无 jsdom 也要能测「卸载时 abort 被调用」 |
| A4 | `web/src/hooks/useDiagnosisStream.ts:159-166` | 加 `useEffect(() => () => controllerRef.current.dispose(), [])` | 卸载不中断 = 后端继续烧完剩余节点 |
| A4 | `web/src/App.tsx:33-40`、`:197-205` | 诊断状态提升到 `App`（`useDiagnosisStream()`），透传给 `DiagnosePage`；去掉 `key={route}` | 切 tab 不再丢结果；同时消除了强制重挂载 |
| A5 | `web/src/api/client.ts` | ① 校验 `content-type` 含 `text/event-stream`；② 流结束若未收到 result/error → `onError('连接中断，未收到诊断结果')`；③ `onDone` 接线 + reducer 新增 `done` 收敛；④ 普通请求 15s 超时、SSE 60s 静默超时 | 旧代码读到 `done` 就 `break`，`running` 永远为 true；代理返回 HTML 200 也被当流处理 |
| A6 | `web/nginx.conf:9-36` | `client_max_body_size 12m;`、`gzip on;` + `gzip_proxied off;`（`gzip_types` 不含 `text/event-stream`） | 默认 1MB 会把带图请求挡成 413；压缩必须避开 SSE，否则流式退化成一次性 |
| A6 | `web/vite.config.ts:31-37` | `sourcemap: false` | nginx 公开托管 `/assets/`，带 map 等于公开源码 |
| A6 | `web/Dockerfile:15-21` | `npm install` → `npm ci`，`COPY package.json package-lock.json*` → 显式列出 lock | `ci` 严格按 lock 装；通配符在 lock 缺失时静默跳过，`ci` 会直接失败（这是想要的） |

---

## 新增/修改的测试

| 测试名 | 断言什么 | 退化验证结果（去掉修复后失败的那条断言） |
|---|---|---|
| `diagnosisContext.test.ts :: 连续 4 轮长度线性增长，且「第1轮：」在整串里只出现一次` | 相邻增量相等；`第1轮：` 只出现 1 次 | 把 `appendUserTurn` 改回「存 payload」→ **失败**：`长度序列 11 → 34 → 73 → 151 不是线性增长: expected 78 to be 39`（正是交办书描述的近指数增长） |
| `diagnosisContext.test.ts :: turns 里存的是用户原话，不是拼好的请求体` | turns 只含原话，不含 `第N轮：` | 同上 → **失败**：`expected [ '第一轮说的', '第1轮：第一轮说的\n\n【本轮补充】第二轮说的' ] to deeply equal [...]` |
| `diagnosisContext.test.ts`（buildContext 两道闸 / composePayload 不越界，共 7 条） | 轮数上限保留全局编号、字符上限丢最老不丢最新、整段 ≤4000 且本轮输入完整保留 | 同组退化时同步变红 |
| `errorDetail.test.ts :: 数组：422 校验错误拼成可读文案，且结果是 string 而不是对象` | `typeof out === 'string'`，`out` 不含 `body` | 把数组分支还原成「直接返回数组」→ **失败**：`expected 'object' to be 'string'`（白屏根因） |
| `errorDetail.test.ts`（其余 6 条） | string / 多元素 / 字符串元素 / 缺失 / 空串 / 无可用字段 | 同上，共 5 条变红 |
| `client.test.ts :: 返回 text/html 时立即报错，不按事件流解析` | `onError` 文案含「不是事件流」 | 去掉 Content-Type 校验 → **失败**：`等待超时：onError 被调用`（根本不会报错，界面永远"诊断中"） |
| `client.test.ts :: 只收到 progress、没有 result 时兜底报错` | `errors === [STREAM_INCOMPLETE_MESSAGE]` | 去掉兜底 → **失败**：`等待超时：onError 被调用` |
| `client.test.ts`（收到 result 后不报错 / error 事件不重复报 / 422 detail 提成字符串） | 3 条对偶 | — |
| `streamLifecycle.test.ts :: dispose() 会调用在途流的 abort` | 卸载中断被调用且只调一次 | — |
| `streamLifecycle.test.ts :: hook 在 useEffect 清理里中断在途的流` | 剥注释后匹配 `useEffect(...) controllerRef.current.dispose()` | 注释掉清理 → **失败**。⚠️ 第一版断言直接搜全文，**把注释掉的代码也当成了接线成功**；已改为先剥注释再匹配 |
| `diagnosisReducer.test.ts`（5 条） | error 必为字符串；`done` 让 running 收敛；turns 推进；追问挂到最后一轮；reset 保留历史 | — |
| `tests/test_health_auth.py`（6 条） | 配 key 时 fresh 无 key/错 key → 401、带对 key → 200；命中缓存不鉴权；未配 key 放行；live/ready 无鉴权 | 去掉 `if fresh: require_api_key(...)` → **失败 2 条**：`assert 200 == 401` |

---

## 回归结果

- `SILICONFLOW_API_KEY=test-key ./venv/Scripts/python.exe -m pytest -q` → **409 passed in 20.18s**（403 基线 + 6 新增）
- `./venv/Scripts/ruff.exe check . --statistics` → **0 问题**
- 前端：`npx tsc --noEmit` 通过；`npx vitest run` → **175 passed**（141 基线 + 34 新增）；`npm run build` 通过（`dist/` 内 `.map` 文件数 **0**，确认 sourcemap 已关）
- `docker compose config` → 解析通过（本机 docker daemon 未运行，无法 `nginx -t`/`docker build`）

---

## 与任务书不符之处

1. **A1 的验收口径**：任务书写「`buildContext()` 的输出长度随轮次线性增长」。实测第一轮没有 `第N轮：` 前缀与 `【本轮补充】` 后缀，因此 `L2-L1 ≠ L3-L2` 在**修复后**也不成立。判别式改为「从第 2 轮起相邻增量相等」+「`第1轮：` 只出现一次」，两者在旧实现下都会失败（见上表实测数字）。
2. **A1 的 `MAX_CONTEXT_CHARS` 与后端上限的关系**：上下文预算 3000 字 < 4000 上限，因此**只有在本轮输入很长（>约 1000 字）时** `composePayload` 的省略分支才会触发。测试按这个真实边界构造了两条用例（短输入 / 长输入），而不是假装省略总会发生。
3. **A2「渲染 DiagnosePage 的逻辑路径」**：本仓库确实不装 jsdom，无法渲染组件。改为在**纯函数层**断言（`extractDetail` 返回值必为 string）+ 在 **reducer 层**断言（进入 `state.error` 的必为 string），覆盖了导致白屏的那条数据通路。
4. **A3 的 `fresh=false` 语义**：任务书给了两个选项，我选了「命中缓存不鉴权」并写清理由（只读内存、不碰依赖、没有烧配额风险，Docker healthcheck 不必配密钥），并加了对偶断言。
5. **A4 的「状态提升」**：任务书说「提升到 App/context，去掉 key={route}（或至少保证中断先发生）」。我做了完整版（提升到 App + 去掉 key + hook 卸载清理），三者一起才既保住结果又不留孤儿流。
6. **A5 的超时数值**：任务书只给了普通请求「建议 15s」。SSE 静默超时取 **60s**，依据是库里 `token_usage.by_node` 显示最慢节点（rebuttal）约 14.5s，60s 无任何 chunk 只可能是连接被挂住。
7. **A6 的验收**：本机 docker daemon 未运行（CLI 存在但 `npipe` 连不上），因此没能跑 `docker build`。改为给出**逐项静态检查**：nginx.conf 语句配平 / 新增指令在位 / `gzip_types` 不含 `text/event-stream` / SSE 反代保护未丢；Dockerfile 的 RUN、COPY 指令行逐条断言；vite sourcemap 关闭。

---

## 未做/建议后续

- 本批未触碰 `legacy/`、未改任何既有断言语义。
- A4 的「接线」只有源码级断言（无 jsdom 的客观限制）。若后续引入 jsdom/testing-library，应把它升级为真正的渲染测试。
- `web/dist/` 已随构建更新（gitignore 覆盖，未进版本库）。
