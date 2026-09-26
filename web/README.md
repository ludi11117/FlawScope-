# FlawScope 前端

React 18 + TypeScript + Vite。从"Streamlit 直连业务模块"改为真正的前后端分离：
前端只经 HTTP 调用 FastAPI，`api.py` 从"被绕过的摆设"变成唯一入口。

## 快速开始

```bash
# 终端 1：后端
cd ..
venv/Scripts/python.exe -m uvicorn api:app --port 8000

# 终端 2：前端
cd web
npm install
npm run dev          # http://localhost:5173
```

Vite 会把 `/api/*` 代理到 `http://127.0.0.1:8000`，因此**不需要**在后端配 CORS 也能开发。
前端代码里所有请求都写 `/api` 前缀，不出现后端主机名——开发期由 Vite 代理，
生产期由 nginx 反代承担同样的角色（见 `nginx.conf`）。

若后端不在默认端口，用 `VITE_API_PROXY_TARGET=http://127.0.0.1:9000 npm run dev` 覆盖。

## 命令

| 命令 | 说明 |
|---|---|
| `npm run dev` | 开发服务器（5173） |
| `npm run build` | 类型检查 + 生产构建 → `dist/` |
| `npm run preview` | 预览构建产物 |
| `npm run typecheck` | 只做类型检查，不产出文件 |
| `npm test` | 跑单元测试（单次） |
| `npm run test:watch` | 单元测试 watch 模式 |

## 目录结构

```
src/
  api/
    client.ts              # HTTP 客户端（含 SSE 订阅）
    sse.ts                 # SSE 字节流解析（纯函数，单独抽出以便测试）
  types/contracts.ts       # 与后端 Pydantic 对齐的类型，逐字段标注契约来源
  state/machine.ts         # 9 节点状态机的前端镜像，用于进度可视化
  hooks/useDiagnosisStream.ts  # 诊断流程状态管理（useReducer）
  components/
    StateMachineView.tsx   # 状态机 + 辩论环可视化
    ResultView.tsx         # 结果渲染（严格按"没有的字段就不渲染"）
  pages/
    DiagnosePage.tsx       # 诊断页
    HistoryPage.tsx        # 历史页
    historyUtils.ts        # 历史页的纯逻辑（分页夹取 / 状态配色 / CSV 序列化）
  App.tsx                  # hash 路由外壳
```

## 单元测试

190 项，全部离线、不依赖后端：

| 文件 | 项数 | 覆盖什么 |
|---|---|---|
| `api/health.test.ts` | 50 | 探活三态判定、失败时保留版本号、隐藏时暂停轮询 |
| `pages/historyUtils.test.ts` | 27 | 分页夹取与 `clamped` 信号、CSV 注入防护、时间格式化 |
| `api/sse.test.ts` | 21 | 跨 chunk 分片、多行 data、注释心跳、半截消息、**CRLF 换行** |
| `pages/statsUtils.test.ts` | 21 | 状态分布排序与占比、空表边界 |
| `state/machine.test.ts` | 19 | 节点映射（含"不该映射"的反向用例）、状态归约、转人工的 active 态 |
| `hooks/diagnosisContext.test.ts` | 11 | 多轮上下文**线性增长**（不自我嵌套）、轮数与字符预算 |
| `api/statusMeta.test.ts` | 9 | 后端状态表合并、脏载荷不崩、标签覆盖但颜色不被覆盖 |
| `ui/button.test.ts` | 8 | 危险按钮不能红底红字（真实 bug 的回归守卫） |
| `hooks/streamLifecycle.test.ts` | 7 | 卸载时中断在途流、接线断言（源码级） |
| `api/errorDetail.test.ts` | 7 | 422 的数组 detail 提成可读字符串（白屏的守门人） |
| `api/client.test.ts` | 5 | Content-Type 校验、流提前关闭兜底、422 detail |
| `hooks/diagnosisReducer.test.ts` | 5 | error 必为字符串、`done` 让 running 收敛、turns 推进 |

> 项数用 `cd web && npm test` 核对（输出里每个文件一行）。改完测试记得回来同步这张表——
> 它是**手抄**的，没有自动校验（后端那份有 `tools/check_doc_numbers.py`，前端暂时没有）。

三个刻意的组织决定：

1. **把纯逻辑抽成独立模块**（`api/sse.ts`、`api/health.ts`、`hooks/diagnosisContext.ts`、
   `hooks/streamLifecycle.ts`），而不是塞在组件里。组件只负责渲染，逻辑能被直接测——
   这不是为了凑测试数量，`pageWindow` 的 `clamped` 恒假 bug、以及多轮上下文**指数膨胀**
   那个 bug，都是抽出来之后才被测到的。
2. **每个"该拦的"都配"该放的"**。比如 `nodeFromLabel` 既测"转人工→human_review"，
   也测"随便一句话→null"。只测前者的话，实现退化成"永远返回第一个节点"也能通过。
3. **没有 jsdom 时的退路是"源码接线断言"**，不是不测。`streamLifecycle.test.ts` 里
   会读 `useDiagnosisStream.ts` / `App.tsx` / `HistoryPage.tsx` 的源码，
   断言 `useEffect` 清理、`AbortController`、`winRef.current.offset` 确实被接上。
   ⚠️ 这类断言**必须先剥掉注释再匹配**——第一版直接搜全文，把"被注释掉的清理"
   也当成了接线成功（退化验证时抓出来的）。

**本项目没有 jsdom 与 @testing-library**，所有测试都是纯逻辑的、不渲染组件。
这不是疏漏而是取舍：为了测一个组件而引入整套 DOM 环境，成本高于把逻辑抽出来。
`api/health.ts` 里的 `shouldPoll(hidden)` 就是为此而生的——它本来是 hook 里的
一个 `if (document.hidden)`，抽出来才成为可直测的布尔函数。
**代价要如实承认**：接线是否正确（比如 `App.tsx` 有没有真的把状态灯接到
`HEALTH_META[health.state]`）测不到，只能靠人工开页面看一次。

## 几个刻意的技术选择

### SSE 用 fetch + ReadableStream 手写，不用 EventSource

三个原因，缺一不可：

1. **EventSource 只支持 GET** —— 而 `/diagnose/stream` 是 POST。故障描述可能很长，
   还可能带 base64 图片，塞进 URL 既不现实也会撞上代理的 URL 长度限制；
2. **EventSource 无法自定义请求头** —— 带不了 `X-API-Key`，后端开了鉴权就用不了；
3. **EventSource 断线会自动重连** —— 对诊断这种"重连就重跑一遍、每次烧 6~9 次 LLM 调用"
   的场景，自动重连是有害的。

手写解析时有一个最容易踩的坑：**一个 SSE 消息可能被 TCP 切成多个 chunk，
一个 chunk 里也可能含多条消息**。所以必须在循环外累积 buffer、以空行分隔，
不能按 chunk 直接解析。`client.ts` 里对此有注释。

### 类型手写而不是用 openapi-typescript 生成

后端用的是中文键名（`根因判断` / `工单编号` …），这些是**跨层契约**。
自动生成会把键名原样搬过来但丢掉语义，反而看不出哪个字段是"降级时可能整体缺失"的。
手写一份并在每个类型上标注「契约来源：agents.py :: XXXOutput」，
改后端时能一眼找出前端要同步的地方。

### 不引入状态管理库

状态是单一的、由 reducer 收敛的，`useReducer` 已经足够表达。
引入 Redux/Zustand 只会让"状态从哪来"更难讲清。

### 降级工单不补空章节

`ResultView` 里所有工单字段都是可选的，缺什么就不渲染什么。
这是项目级硬约束（与后端 `workorder_export` 口径一致）：
没有依据的字段补出空的"维修方案"，比没有这个章节更危险——操作工可能照着一张
内容为空的工单去拆机。

## 已知限制

- **诊断 / 历史 / 统计三个页面都已迁移到 React**；Streamlit 旧前端（8501）仅作对照保留。
- 路由是极简的 hash 路由（`#/diagnose`、`#/history`、`#/stats`），没引 react-router。
  三个页面不值得为它加一个依赖。
- 移动端适配只做了基础响应式，未针对触屏优化。
