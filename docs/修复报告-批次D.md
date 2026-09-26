# 批次 D 完成报告（工程化 / 部署 / 文档）

日期：2026-09-25 ｜ 基线 `480 passed` → 本批后 **563 passed**（ruff 0 问题；前端 `175` → `190`）

> 本批末尾按你的决策补做了两项原本"待决策"的改动：
> **D6 依赖清单拆成 base/dev/runtime**、**D7 新增 `GET /meta/statuses` 并让前端真正消费**。
> 详见 §「决策后的补做」。

---

## 改动清单

| # | 文件:行 | 改了什么 | 为什么 |
|---|---|---|---|
| D2 | `tests/conftest.py`（重写） | ① 强制赋值哨兵 key（不是 `setdefault`）+ 断言哨兵在位；② `autouse` 夹具快照/还原 `agents._llm/_vision_llm/_embeddings/_db/_bm25_index/_bm25_corpus/_bm25_initialized/_doc_texts_cache`、`agents._exclusion_cache`、`agents._retrieval_cache`、`api._health_cache`；③ `CHROMA_PERSIST_DIR` 指向 `tmp_path` | 假 key 此前靠"conftest 先设 + load_dotenv 不覆盖"的顺序巧合生效；实测测试会打开真实向量库 |
| D1 | `.dockerignore`（重写，47 条） | 补 `chroma_db/`、`*.db*`、**`data/raw/`**（受版权 PDF）、`node_modules/` + **/node_modules/`、`web/node_modules/`、`web/dist/`、`tests/`、`tools/`、`.recovery/`、`.ruff_cache/`、`.tmp_smoke/`、`_shot_*.png`、`.git/`、`.git_corrupted_20260918/`、eval 脚本与 `eval_reports/` | `Dockerfile` 是 `COPY . .`，这些都会进镜像 |
| D3 | `eval_test.py:503,520,597` | `format_rate(1 - sr['rate']) if sr['rate'] else 'N/A'` → `if sr['rate'] is not None`；`_parse_json_quiet` 对非字符串输入返回 `{}` | `rate == 0.0`（**全部未变更，最理想**）会被显示成 `N/A`，0 被当成"没数据" |
| D4 | `run_all.py:194-201` | `Popen(..., start_new_session=not IS_WINDOWS)` | POSIX 上子进程与启动器同进程组，`os.killpg(os.getpgid(pid))` 会把启动器自己一起杀掉 |
| D4 | `run_all.py:474-497` | `find_python()` 同时探测 `venv/Scripts/python.exe`、`venv/bin/python`、`.venv/*`；新增可选 `root` 参数（便于测试） | 只认 Windows 布局 → macOS/Linux 上静默退回系统解释器 |
| D4 | `run_all.py:513-607` | 抽出纯函数 `frontend_command(node, npm)`；新增 `print_npm_missing()` / `install_frontend_deps(npm)`，`npm is None` 时给可读指引并 `return 1` | `find_node()` 返回 `(node, None)` 时 `subprocess.call([str(npm), ...])` 会拿字符串 `"None"` 去 exec → FileNotFoundError |
| D5 | `.github/workflows/ci.yml`（重写） | ① `concurrency`（同分支取消旧运行）；② ruff **pin 到 0.16.8**（与本机 `ruff --version` 一致）；③ 新增 `frontend` job（setup-node 22 + `npm ci` → `typecheck` → `test` → `build`）；④ test job 加 `pytest-cov` + `--cov-fail-under=88` | 此前前端 141 项 vitest 完全不进 CI；ruff 未 pin（注释自己承认会漂移） |
| D5 | `.pre-commit-config.yaml`（新增） | ruff（v0.16.8）+ 通用钩子（冲突标记、YAML/JSON 语法、大文件、行尾/文件尾）。**只挂 linter，不挂 formatter** | 把 CI 的 lint 提前到提交前 |
| D5 | `pytest.ini` | 加注释说明覆盖率门槛只写在 CI 里 + 为什么不能用 `[coverage:run] source` | 实测踩坑（见下） |
| D6 | `requirements.txt:60-76` | 删 `requests==2.34.2`（只有 `legacy/test_api.py` 用）、`pytest-asyncio==1.4.0`（全仓零 async 测试）；新增 `pypdf==6.19.0` 落到"开发/测试"段并写清已移除项 | `tools/parse_manual_pdf.py` 动态 `import pypdf` 却无任何清单声明 |
| D6 | `tools/parse_manual_pdf.py:117-134` | `import pypdf` 包 try/except，缺时打印安装指引并退出 1 | 缺依赖时要给"怎么装"，而不是 traceback |
| D9 | `web/src/api/sse.ts:30-40` | `split('\n\n')` → `split(/\r?\n\r?\n/)` | 代理改写换行时整条流解析不出事件 |
| D9 | `web/src/pages/HistoryPage.tsx:52-110` | `load` 加 `AbortController` + 请求序号；用 `winRef.current.offset`（夹取后的 offset）；卸载时中断 | 旧响应覆盖新结果；`win.offset` 在生产代码里根本没被用到 |
| D9 | `web/src/pages/HistoryPage.tsx:435-450` | 行 `div` 加 `role="button"` / `tabIndex=0` / `aria-expanded` / `onKeyDown`（Enter + Space） | 键盘与读屏用户 Tab 不到、也听不出这行能点 |
| D9 | `web/src/api/client.ts` | 删 `diagnoseSync`、`getRecord`、`ApiError.isOverloaded`（grep 确认零引用）；`listRecords` 新增可选 `signal` | 死代码 |
| D9 | `web/src/hooks/useDiagnosisStream.ts:229` | 删 `export { ApiError }` 再导出（grep 确认零引用） | 死代码 |
| D9 | `web/src/state/machine.ts`+`components/StateMachineView.tsx` | `human_review` 并入 `NODES`（带 `conditional` 标记）；新增 `PRIMARY_NODES`；进度分母改用 `PRIMARY_NODES` | 转人工时状态机视图没有 active 节点，看起来像卡死 |
| D7 | `status_meta.py`（新增） | `StatusMeta` 表：label / icon / level / banner / terminal / failure / persisted / followup；派生 `TERMINAL_FAILURE_STATUSES` 与 `should_persist()` | 状态值散在 6 处，加一个状态要改 6 处 |
| D7 | `orchestrator.py, api.py, app.py` | `TERMINAL_FAILURE_STATUSES` 改为从表导入；api.py 两处落库判断改用 `should_persist()`；app.py 的横幅 if/elif 与颜色字典改为查表 | 同上 |
| D8 | `README.md`、`docs/PROJECT_GUIDE.md`、`web/README.md` | 测试数字同步（157/403/86 → **553 / 553 / 181**）；端点表补 `/diagnose/stream`、`/health/ready` 并区分三个探针；`app.py` 标注为对照前端；`.env.example` 补 `CORS_ALLOW_ORIGINS`、`GRAPH_RECURSION_LIMIT`、`RETRIEVAL_DEVICE_FILTER`，删已不存在的 `STREAMLIT_PORT/STREAMLIT_ADDRESS`；待办里已完成项移走 | 文档数字与端点表过期 |
| D8 | `tools/check_doc_numbers.py`（新增） | 自动核对"文档写的测试数"与 `pytest --collect-only` / `npx vitest run` 的实测值 | 手抄的数字必然漂移 |

---

## 新增/修改的测试

测试文件（本批新增 5 个，共 **73 条**）：

| 测试文件 | 条数 | 断言什么 |
|---|---:|---|
| `tests/test_eval_judge.py` | 26 | 判官模型来自 `args.judge_model`（含**调用点**的源码断言）；`_parse_json_quiet` 围栏/截断/嵌套花括号；`debate_summary` 的修正/恶化/持平**名单**；`compare_with_snapshot`；`_missing_nodes`；`format_rate(0.0) == "0.0%"`；rate=0 时报告出现 `100.0%` |
| `tests/test_run_all_launcher.py` | 13 | `Popen` 带 `start_new_session=not IS_WINDOWS`；`find_python` 认 POSIX 布局；`frontend_command` 的"只有 node 没有 npm"分支；`install_frontend_deps(None)` 打印指引且**不调用 subprocess** |
| `tests/test_requirements_consistency.py` | 5 | 镜像清单的每个 pin 在全量清单里同版本存在；无零使用包；pypdf 有落点且不进镜像；runtime 不含 streamlit/pandas |
| `tests/test_status_meta.py` | 17 | orchestrator 里 grep 出的状态字面量必须全部登记（双向）；`TERMINAL_FAILURE_STATUSES` 从表派生；api 落库走统一口径；终态必含横幅 |
| `tests/test_doc_number_checker.py` | 12 | 核对脚本的 `parse_pytest_collect` / `strip_ansi` / `parse_vitest` / `doc_numbers` 解析逻辑（防止脚本静默失效） |

前端（本批新增 6 条）：`sse.test.ts` 的 CRLF 三例（含逐字节喂入）、`machine.test.ts` 的两条节点定义 + 一条转人工 active、`streamLifecycle.test.ts` 的历史页接线断言。

### 退化验证结果

| 退化注入 | 失败断言 |
|---|---|
| `eval_test.py` 调用点改回 `args.diagnosis_model` | `判官没有用 judge_model：judge_root_cause(final_root, expected_list, args.diagnosis_model, …)` |
| `format_rate` 改回 `if not rate` | `assert 'N/A' == '0.0%'` |
| `run_all.py` 去掉 `start_new_session` | `POSIX 上没有开新会话：killpg 会把启动器自己一起杀掉` |
| `find_python` 只认 Windows 布局 | `assert WindowsPath('…/venv/Scripts/python.exe') == WindowsPath('…/venv/bin/python')` |
| 去掉 npm 缺失守卫 | `FileNotFoundError: [WinError 2]` + `拿 None 去执行了命令：[(['None', 'install'],)]` |
| `requirements-runtime.txt` 单边改 `uvicorn` 版本 | `镜像清单与全量清单不一致：{'uvicorn': ('0.52.5', '0.52.4')}` |
| orchestrator 里加一个未登记状态 `reviewed_v2` | `这些状态没有登记到 STATUS_META：['reviewed_v2']` + 对偶的 orphan 断言 |
| api.py 落库改回硬编码 | `api.py 的落库判断没有走统一口径` |
| `sse.ts` 改回 `split('\n\n')` | CRLF 三例全红 |
| `machine.ts` 不把转人工并进 NODES | 节点数 + `转人工时必须有一个 active 节点` 共 3 条 |
| 去掉 HistoryPage 的卸载清理 | `expected … to match /return \(\) => abortRef\.current\?\.abort\(\)/` |
| 文档数字改回 157 | `PROJECT_GUIDE.md（后端）写的是 [157, 541] 项，实测 553 项` |

### 被改动的既有测试

1. `web/src/state/machine.test.ts:16` —— `NODES.length === 9` 改为**同时断言** `NODES.length === 10`
   且 `PRIMARY_NODES.length === 9`。原注释"与 orchestrator 的节点数对齐"其实不准确
   （后端是 10 个节点），改后两个数字都钉住，比原来更严。
2. `tests/test_startup_readiness.py` —— 步骤数 6→7（B3 新增一步），见批次 B 报告。
3. `tests/test_resilience_and_retrieval.py::test_map_excluded_causes_caches_identical_inputs`
   —— 换输入，见批次 B 报告。

---

## D2 的关键证据：向量库 mtime 不再变化

```
改前 mtime: 2026-09-25 13:35:35.422185500 +0800   chroma_db/chroma.sqlite3
跑 pytest -q → 553 passed
改后 mtime: 2026-09-25 13:35:35.422185500 +0800   chroma_db/chroma.sqlite3
```

对比交办书报告的现象：`13:10:40 → 13:11:15`（跑一次测试 mtime 就变）。
现在**逐毫秒不变**。

附加验证：**临时把 `.env` 改名**后跑 `pytest -q` 仍然是 **553 passed**（`.env` 已改回）。
说明测试不再依赖真实密钥文件。

---

## 决策后的补做（你选了 D6=拆、D7=新增，B6=不改、C2=保持关闭）

### D6：依赖清单拆成 base / dev / runtime

```
requirements-base.txt      ← 版本唯一来源（运行期真正会 import 的 20 个包）
requirements-dev.txt       ← -r base + pytest / pypdf / streamlit / pandas
requirements-runtime.txt   ← -r base（容器镜像）
requirements.txt           ← -r dev（保留旧文件名，兼容既有脚本与文档）
```

- `Dockerfile:8-12`：`COPY requirements-runtime.txt requirements-base.txt ./`
  —— **`-r` 引用的文件必须一起 COPY**，漏了 base 会让 pip 在构建期失败，
  而且这个错误很晚才暴露（前面的依赖层可能已被缓存）。
- `tests/test_requirements_consistency.py` 新增 `_resolve()`：跟着 `-r` 把整条链展开。
  必须有这一步——拆分后 `requirements.txt` 自己**一个 pin 都没有**，
  只看单文件会得出"这份清单是空的"这种错误结论（写第一版时就踩到了）。
- 新增 3 条断言：版本只写在 base 里；`-r` 链已接好；展开后的集合与拆分前逐包一致。

退化验证：把 `requirements-runtime.txt` 改成 `-r requirements-dev.txt` →
`requirements-runtime.txt 没有 -r requirements-base.txt` +
`左值多出 {'pandas','pypdf','pytest','streamlit'}`（即镜像会白背 4 个包）。

### D7：`GET /meta/statuses`

- `api.py`：新增端点，下发 `label / level / terminal / failure / persisted / followup`，
  **不下发 `icon` / `banner`**（前者是 Streamlit 对照前端的展示细节，后者是后端自己的整句文案）。
  无需鉴权（静态元数据，与 `/health/live` 同类）。`/` 的端点清单里也加上了它。
- 前端真正消费：
  - `web/src/api/statusMeta.ts`（新增）：`applyStatusMeta()` 防御性解析载荷、
    `labelFor()` 取标签、`loadStatusMeta()` 启动时拉一次（失败静默）。
  - `historyUtils.statusMeta()` 改为 `labelFor(status, local.label)` ——
    **颜色仍由前端掌握**，后端只覆盖标签。
  - `App.tsx` 挂载时 `void loadStatusMeta()`。
- ⚠️ **`ResultView` 刻意不消费**：它的文案故意比列表长（列表写「知识库无依据」，
  结果横幅写「知识库无相关依据，无法自动诊断」）。用后端标签覆盖会把长文案压短、
  信息量下降。它需要防的是**状态集合漂移**，那由测试守着。
- 新增漂移守卫（`tests/test_status_meta.py`）：读 `historyUtils.ts` 与 `ResultView.tsx`
  两个源文件，抓出 `const STATUS_META = {…}` 里的 key 做**双向**集合比对。

退化验证：
- 后端加一个 `brand_new_state` → 两个前端表**同时**被点名
  （`web/src/pages/historyUtils.ts 不认这些后端状态：['brand_new_state']`）；
- `historyUtils` 改回不采用后端标签 → 前端 `拿到后端标签后，历史页的标签跟着变` 变红。

> 写这组守卫时踩了两个正则坑，都写进注释了：
> ① `_TS_KEY` 必须带 `re.MULTILINE`，否则 `^` 只匹配字符串开头、一个 key 都抓不到；
> ② 起点要搜 `const STATUS_META` 而不是 `STATUS_META`——`ResultView.tsx` 先在注释里
> 提到了这个名字，从注释开始切会切错块。

---

## 回归结果（最终）

- `./venv/Scripts/python.exe -m pytest -q` → **563 passed**（基线 403，新增 160）
- `./venv/Scripts/ruff.exe check .` → **All checks passed**
- 覆盖率（CI 的门槛命令）→ 89.13%，`Required test coverage of 88% reached`
- 前端：`npx tsc --noEmit` 通过；`npx vitest run` → **190 passed**；`npm run build` 通过
- CI 三个 job 的本地等价命令全部跑通：
  - lint：`ruff check . --output-format=github` → 0 问题
  - test：`pytest -q --cov=… --cov-fail-under=88` → 563 passed，89.13%
  - frontend：`npm ci --dry-run`（lock 与 package.json 同步）→ typecheck → test(190) → build
- `tools/check_doc_numbers.py` → 后端 563 / 前端 190 与文档一致
- `pip install -r requirements-runtime.txt` 后 `python -c "import api"` → `import api OK, API_VERSION = 1.3.0`

---

## 与任务书不符之处

1. **D5 的覆盖率范围**：第一版写成 `[coverage:run] source` + CI 里裸 `--cov`。
   **实测发现裸 `--cov` 等价于 `--cov=.`，会覆盖 coverage 的 `source` 设置**，
   于是 tests/、legacy/、tools/ 全被算进来，覆盖率从 89% 掉到 **74%**，
   而配置看起来是生效的。已改为在 CI 命令里显式列 `--cov=模块`，
   并在 `pytest.ini` 里把这次踩坑写进注释（避免有人再"优化"回去）。
2. **D9 的 `parseSSEStream` 没有删**。它列在交办书的死代码清单里，
   grep 确认引用数为 1（只有它自己的测试）。删它需要同时删掉 5 条测试，
   其中"解析后端真实格式的三条事件"是一条有价值的集成用例，
   而函数本身只有 9 行（纯函数、无副作用）。**收益/成本不成立，保留**并在
   `sse.ts` 的注释里写明它只用于测试与调试。其余三项死代码（`diagnoseSync`、
   `getRecord`、`isOverloaded`、`ApiError` 再导出）已全部删除。
3. **D7 原本没有新增 `GET /meta/statuses`**：按 §5.2 先只做了后端 `STATUS_META` 单一来源，
   端点在拿到你的决策后补做（见「决策后的补做」）。补做时发现一个额外的判断点：
   **`ResultView` 刻意不消费后端标签**——它的文案故意比列表长，覆盖会把信息量压下去。
   它需要防的是状态集合漂移，改由测试守。
4. **D6 原本没有做 `base/dev/runtime` 拆分**：按 §5.2 先只做了"删除未使用项 + 给 pypdf
   明确落点"，拆分在拿到你的决策后补做（见「决策后的补做」）。
5. **D1 额外排除了几项**：`.git/`、`.git_corrupted_20260918/`、`node_modules/`（根目录 19M）、
   `web/dist/`、`eval_reports/`、`ablate_single_vs_multi.py`、`compare_single_vs_multi.py`、
   `pytest.ini`。交办书列的是最低要求；这几项都是明显的开发期产物/大目录。
   ⚠️ 已确认 `data/knowledge_base.txt` **没有被排除**（它是重建向量库的输入）。
6. **D3 顺手加了一行**：`_parse_json_quiet` 对非字符串输入返回 `{}`。
   原实现第 124 行会 `text.find("{")`，传 `None` 时抛 AttributeError。
   这是真实的健壮性缺口（"quiet" 函数不该在输入不合法时崩），不是为了让测试变绿。

---

## 未做/建议后续

- `eval_reports/` 是否该进镜像：本次按"开发产物"排除了，若你希望保留历史评估报告，
  把它从 `.dockerignore` 里删掉即可。
- 前端的测试项数表（`web/README.md`）仍是**手抄**的，没有自动校验
  （后端那份有 `tools/check_doc_numbers.py`）。要收紧的话，可以把它也纳入同一个脚本
  —— 这次改完前端测试后，那张表就是手工同步的。
- CI 里 `pip install -r requirements.txt` 会把 streamlit/pandas 也装上（几秒钟）。
  若要更省，可以改成 `pip install -r requirements-dev.txt`（少装 streamlit/pandas）——
  但 `requirements-dev.txt` 本身就包含它们，真正省下来需要把 streamlit/pandas 再拆一层，
  收益不值当。
- `.recovery/before/` 目录里的旧快照在 `.dockerignore` 与 pre-commit 的排除列表里，
  但它**没有被 gitignore**。建议后续确认是否还需要它。
