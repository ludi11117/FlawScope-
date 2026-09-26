# 批次 C 完成报告（护栏正确性与接口契约）

日期：2026-09-25 ｜ 基线 `431 passed` → 本批后 **480 passed**（ruff 0 问题，前端 175 passed）

---

## 改动清单

| # | 文件:行 | 改了什么 | 为什么 |
|---|---|---|---|
| C3 | `orchestrator.py:806-812` | `run_diagnosis_stream` 的两次 yield 改成 `dict(merged)`（浅拷贝） | 此前所有快照指向同一个 dict；SSE 端立刻序列化看不出问题，但"把每步快照收进列表"的消费方会拿到 N 份终态 |
| C4 | `api.py:164-186` | `DiagnosisResponse` 新增 `record_id`；`/diagnose` 落库包 try/except，失败时 `logger.exception` 并仍返回完整结果 | 此前同步版丢掉 `record_id`，且 SQLite 一抖就把用户等了数十秒的结果变成 500；`app.py` 反而包了 try/except，两处口径不一致 |
| C5 | `agents.py:2098-2130` | 新增 `detect_image_mime()`（按魔数判 PNG/JPEG/WebP/GIF），`extract_image_info` 用它拼 data URL | 此前硬编码 `data:image/jpeg`，而前端允许 png——PNG 字节流被贴 jpeg 标签会被视觉服务拒收或解出坏图 |
| C5 | `orchestrator.py:23-33`、`:768-786` | `AgentState` 新增 `image_warning`；`_build_initial_state` 在视觉失败时置「图片未能识别，本轮仅依据文字描述诊断」 | 视觉失败此前静默返回空串，用户不知道照片没被用上 |
| C5 | `api.py:164-186`、`:238`、`:330`；`web/src/types/contracts.ts`、`DiagnosePage.tsx` | `image_warning` 贯通两个端点 + 前端琥珀色提示条 | 同上（前端是用户唯一能看到的地方） |
| C6 | `agents.py:411-500` | `invoke_and_validate` 新增 `invoke_fn` 参数；**JSON 解析失败也回灌** Schema + 原始输出 + 报错 | 此前解析失败只 `continue`，第二次调用拿到一模一样的消息，模型没有任何新信息 |
| C6 | `agents.py:554-605` | `extract_fault_info` 接进统一通道，用 `_tracking_invoke` 探针保住 `None`/`{}` 两义返回值 | 旧实现自己重写了一遍重试循环，且没有 schema 回灌 |
| C7 | `agents.py:220-260` | 新增 `_RETRYABLE_LLM_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)`，`@retry` 只对它生效 | 此前 `retry_if_exception_type((Exception,))`：401/400 白等 3 次退避（1.5s / 2.25s / 3.375s）才失败 |
| C1 | `agents.py:807-830` | 新增 `_GENERIC_DEVICE_TOKENS` 泛词黑名单；`is_equipment_in_evidence` 的设备 token 判定前先滤掉它 | 「系统」这类词几乎在任何资料里都能命中；实测 21 次跨设备误放行里 **14 次**只靠「系统」过关 |
| C2 | `build_knowledge_base.py:64-160` | 新增 `parse_chunk_metadata()`：按位置回溯给每块补 `{section, chapter, alarm, device}`，`add_texts(chunks, metadatas=...)` 写入 | 库里的块没有 metadata，检索层既无法按设备过滤、也无法溯源到"哪条原因"（README「条目级切分」的前置条件） |
| C2 | `config.py:127-142`、`agents.py:683-706` | 新增 `settings.RETRIEVAL_DEVICE_FILTER`（默认 False）+ `device_filter()`；`retrieve_evidence(..., device=)` 在开关打开时才把 `filter` 传给 Chroma；filter 进缓存键 | 按交办书 §5.2，只做"入库写 metadata"半步，过滤**默认关闭** |

---

## 新增/修改的测试

| 测试名 | 断言什么 | 退化验证结果 |
|---|---|---|
| `test_interface_contracts.py :: test_stream_yields_independent_snapshots` | 每份快照是独立对象；倒数第二个与最后一个的 `status` 不同 | 改回共享 dict → **失败**：`多个快照是同一个 dict 对象 assert 2 == 8`（8 份快照只有 2 个对象） |
| `test_interface_contracts.py :: test_stream_keeps_early_stage_status` | 第一份快照仍是 `start`、`diagnosis is None` | — |
| `test_interface_contracts.py :: test_diagnose_returns_record_id` | 落库成功时 `record_id == 42` | 去掉返回值 → **失败**：`assert None == 42` |
| `test_interface_contracts.py :: test_diagnose_survives_persistence_failure` | 落库抛异常时仍 200 + 完整工单 + 日志痕迹 | 去掉 try/except → **失败**：`assert 500 == 200` |
| `test_interface_contracts.py :: test_followup_round_is_not_persisted` | 追问轮次不落库 | — |
| `test_interface_contracts.py :: test_detect_image_mime_by_magic_bytes` / `..._falls_back_conservatively` | PNG/JPEG/WebP/GIF 魔数识别 + 认不出时保守回退 | — |
| `test_interface_contracts.py :: test_image_data_url_uses_detected_mime` | PNG 被贴 `image/png` | 硬编码回 jpeg → **失败**：`实际用了：data:image/jpeg;base64,iVBORw0KGgo...` |
| `test_interface_contracts.py :: test_image_warning_set_when_vision_fails` | 视觉失败时 `image_warning` 非空 | 还原静默失败 → **失败**：`图片没识别出来却没有可见提示` |
| `test_interface_contracts.py :: test_no_image_warning_when_vision_succeeds` / `..._when_no_image` | 对偶：成功时/没传图时不得有提示 | — |
| `test_interface_contracts.py :: test_extract_returns_none_when_model_unavailable` / `..._empty_dict_when_model_answers_garbage` | 两义返回值（`None` vs `{}`） | — |
| `test_interface_contracts.py :: test_extract_second_attempt_carries_schema_errors` | 第二次调用带 Schema + 原始输出 + 校验报错 | 去掉回灌 → **失败**：`没有发生第二次调用：1` |
| `test_interface_contracts.py :: test_extract_second_attempt_carries_parse_error` | 解析失败同样回灌 | 同上 |
| `test_interface_contracts.py :: test_extract_does_not_loop_forever` | 重试收敛在 2 次 | 同上 |
| `test_interface_contracts.py :: test_bad_request_is_not_retried` / `test_unauthorized_is_not_retried` | 400 / 401 只调 1 次 | 改回 `(Exception,)` → **失败**：`4xx 被重试了 3 次` |
| `test_interface_contracts.py :: test_rate_limit_is_retried` / `test_timeout_and_connection_error_are_retried` | 对偶：429 / 超时 / 连接错误按次数重试 | — |
| `test_equipment_guard_rate.py`（14 条） | 跨设备误放行率 ≤3%（实测 6.4% → **2.1%**）；同义放行 5 例；真不匹配拦下 5 例；护栏不是常量 | 去掉泛词黑名单 → **失败**：`跨设备误放行 21/327 = 6.4%，超过阈值 3%` |
| `test_kb_metadata.py`（14 条） | 每块都有 section；章节头块的 chapter 为空；含条目标题块解析出 alarm；续块继承前一章；跨设备边界切换；无报警代码条目 alarm 为空串；**切分结果与纯切分逐字节一致**；`--dry-run` 不构造 Chroma；`device_filter` 三态；默认不带 filter / 开关打开带 filter / filter 进缓存键 | 不认块自身标题 → **失败 4 条**（`第 [0] 块没有 section` 等）；不传 filter → **失败**：`assert [{}] == [{'filter': {...}}]` |

### 被改动的既有测试

- `tests/conftest.py`：本批**没有**改动它（D2 已提前完成）。
- 本批未修改任何既有断言。C6 的实现换了通道，但 `extract_fault_info` 的两义语义由**新增**的测试守住，既有相关测试（`test_graph_short_circuits_when_extraction_fails` 等）保持全绿。

---

## C1 的离线探针数字（DoD 第 6 项）

探针构造：`data/knowledge_base.txt` → 按 `【设备章节】` / `一、条目` 两级切分 →
8 类设备 × 全部跨设备条目（同设备条目不计入）→ 调 `is_equipment_in_evidence`。

| 分组 | 旧实现 | 新实现 |
|---|---:|---:|
| 无报警代码 | 21/327 = **6.4%** | 7/327 = **2.1%** |
| 带报警代码 | 0/327 = **0.0%** | 0/327 = **0.0%** |

同设备条目误伤（潜在误降级）：旧 **10/33** → 新 **10/33**（**未增加**）。

### 与交办书的差异（重要）

1. 交办书写的是「无报警代码 7.7% / 带报警代码 1.2%」，我复现不出那组数字
   （差异应在设备清单与条目切分上）。上面以**本探针自己的构造与数字**为准。
2. 在我这套构造下，"带报警代码"那组**改前就已经是 0.0%**——真正在拦的是报警代码的
   归一化子串检查，不是设备名。所以本次收紧的收益全部在「无报警代码」那一组。
   交办书说"报警代码那条 `len>=3` 的归一化子串检查才是真正在拦的"，这一点与我一致。
3. 交办书建议的两种改法我都实测了：
   - **"≥2 个不同 token 命中才放行"**：误放行降到 1.5%，但**同设备误伤从 10/33 涨到 12/33**，
     包括旗舰用例所在的「一、主轴电机报警代码E-203」——数控机床章节的**条目标题写的是
     "主轴电机"**，正文也不提"数控"，过严的规则会把它判成"无依据"。**弃用。**
   - **"规范化全名或同义词命中"**：同样会误伤上面那 5 条数控机床条目。**弃用。**
   - 最终采用**泛词黑名单**：2.1% 的误放行，且同设备误伤数与旧实现完全一致（10/33）。

---

## 回归结果

- `./venv/Scripts/python.exe -m pytest -q` → **480 passed**（431 → 480，新增 49 条）
- `./venv/Scripts/ruff.exe check . --statistics` → **0 问题**
- 前端：`npx tsc --noEmit` 通过；`npx vitest run` → **175 passed**；`npm run build` 通过
- `build_knowledge_base.py --dry-run` → 76/76 块解析出来源，未写库、未调模型

---

## 与任务书不符之处

1. **C1 的实现方式**：见上"与交办书的差异"。交办书给了两个候选改法，实测两个都会
   明显推高同设备误降级（10/33 → 12/33），所以改用泛词黑名单。阈值仍取 ≤3%。
2. **C2 的 metadata 字段**：任务书列了 `{section, alarm, device}` 三个键，我加了
   `chapter`（条目标题）。理由：README 的「条目级切分」待办需要条目名，
   而"章节"在本仓库的口径里指的是 `一、` 条目（`knowledge_base_health` 的
   `section_count` = 36 就是条目数）；不加 `chapter` 就得在检索层重新解析文本。
   另外 `device` 从**设备章节标题**解析而不是条目标题——数控机床的条目标题里没有设备名。
3. **C2 的过滤没有默认开启**：按 §5.2 执行。但要说清一件事：
   **现有 `chroma_db/` 里没有 metadata，而重建库要真调 Embedding（花钱），
   所以本次改动不会让线上库立刻具备 metadata**——过滤开关在重建之前**必须保持关闭**，
   否则 `where` 条件对不上任何数据，召回直接为空、全线误降级。
4. **C5 的"工单提示"**：任务书说"让前端/工单提示"。我做到了 API 两个端点 + 前端提示条，
   **没有**往工单 dict 里加字段——加字段要同步 React 渲染与 Markdown 导出两处消费端
   （硬约束 20b 踩过的坑），与本条修复的体量不成比例。`image_warning` 已在结果载荷里，
   任何消费方都能用。
5. **C6 的 `invoke_and_validate` 改动范围**：为了让 `extract_fault_info` 能区分
   "模型没答"和"答了但格式不合规"，给 `invoke_and_validate` 加了 `invoke_fn` 参数。
   同时**顺手补上了 JSON 解析失败的回灌**——任务书只提了"接进统一通道"，
   但旧通道对解析失败也只是空转一次，不补这条，"非法 JSON 时第二次调用带上校验报错"
   这条验收根本无法成立。
6. **C7 的测试提速**：`@retry` 的退避在 import 时就固化了，测试里用
   `monkeypatch.setattr(agents._llm_invoke_with_retry.retry, "wait", tenacity.wait_none())`
   把等待改成 0，否则每条重试用例要真等 3.75 秒。

---

## 未做/建议后续

- **C2 的过滤默认是否开启**：待决策（见 `docs/修复报告-B6评估.md` 同类格式的说明）。
- C1 残余的 2.1% 是**真·跨设备词面重合**（某台设备的"经验分歧"里提到"变频器"），
  词面匹配消不掉，要等检索层带上设备 metadata 后按来源过滤才能根治。
- ⚠️ **C1/C6/C5 都触及"LLM 抽取的语义"与"护栏的字面匹配"的接缝**（硬约束 10c），
  离线全绿不等于线上正确。建议把以下三条纳入下一次真机端到端验证清单：
  1. 数控机床 E-203（TC001 类）是否仍能正常诊断而不是降级；
  2. 注塑机 E-203（TC029）是否仍诚实降级；
  3. 带图诊断时 PNG 上传的 `image_warning` 是否为空（即识别成功）。
