# 批次 B 完成报告（省钱与提速）

日期：2026-09-25 ｜ 基线 `409 passed` → 本批后 **431 passed**（ruff 0 问题）

> ⚠️ 本批包含一项**提前完成的批次 D 工作**：`tests/conftest.py` 的模块级状态隔离
> （D2 的第 2、3 条）。原因：B3 要跑真实 lifespan、B4 引入新的模块级缓存，
> 没有隔离夹具时新测试会读到前一个用例的残留状态——那正是本仓库反复踩过的
> "假绿测试"来源。D2 的剩余部分（强制哨兵 key、mtime 证据）也已一并完成，
> 详见「批次 D 报告」。

---

## 改动清单

| # | 文件:行 | 改了什么 | 为什么 |
|---|---|---|---|
| B1 | `orchestrator.py:46-54` | 新增 `LOW_CONFIDENCE_THRESHOLD = 60` | 终审"通过但置信度 30"此前以**正常**面貌交付 |
| B1 | `orchestrator.py:594-609` | 新增 `route_after_rebuttal`：`行动 == "维持"` → `cost`，否则 → `final_review` | 维持原判时审核师没有新材料，再跑一次终审纯属烧配额（终审占 6.0% token / 4764 ms） |
| B1 | `orchestrator.py:695-702` | `add_edge("rebuttal","final_review")` → `add_conditional_edges(...)` | 同上 |
| B1 | `orchestrator.py:486-512` | `workorder_node` 新增两条风险标记：①`行动 == "维持"` → 「中风险（辩论未改变结论）」；②`终审通过且置信度 < 60` → 「中风险（终审置信度偏低）」 | 省一次调用不能变成悄悄降低把关强度；低置信度通过不能当正常交付 |
| B2 | `agents.py:1780-1812` | `agent_review_final` 新增 `evidence` / `disagreements` 参数并传进提示词 | 终审的对照物此前比初审还少（初审刚被补上 evidence/fault/disagreements），而终审才是决定终态的那一步 |
| B2 | `prompts/agent_review_final.j2` | 补「知识库检索资料」「知识库中的经验分歧」两节 + 越界/分歧两项检查 + **禁止超范围质疑**的边界 | 只加资料不写边界，审核师会开始要求诊断引入资料之外的原因 |
| B2 | `orchestrator.py:518-548` | `final_review_node` 传 `evidence` 与 `extract_kb_disagreements(evidence)`，并记分歧日志 | 「函数改对了 ≠ 调用点接对了」 |
| B3 | `api.py:18-24`、`:108-122` | lifespan 新增一步 `("构建检索索引", _init_bm25)`，放在「加载向量库」之后 | BM25 此前是**首次检索时懒建**；`/health/ready` 已宣告就绪，用户点下诊断却要先等建索引（`retrieve` 均耗时 2309 ms 的大头） |
| B4 | `agents.py:63-76` | 新增 `_retrieval_cache`（OrderedDict + LRU + 锁），上限 64 | `retrieve_evidence` 一次诊断最多调 4 次，每次都打一次 embedding |
| B4 | `agents.py:611-690` | `retrieve_evidence` 加缓存，key = `(规范化 query, k, 知识库块数)`；抽出 `_normalize_query` | 块数进 key 让"重建库"天然失效 |
| B4 | `agents.py:529-542` | `rebuild_bm25_index()` 显式清检索缓存 | 覆盖"重建后块数恰好没变"的场景 |
| B5 | `agents.py:1131-1158` | 抽出 `_deterministic_exclusion_map`，返回 `(命中编号, 漏网项下标)`；`_deterministic_exclusion_hits` 保留原签名作为包装 | 需要"哪些排除项还没被确定性覆盖"才能决定要不要叫 LLM |
| B5 | `agents.py:1174-1240` | `map_excluded_causes` **反转顺序**：先确定性；全部命中 → 直接返回（0 次 LLM）；有漏网 → 只把漏网项交给 LLM，再取并集 | 诊断与每轮辩论各调一次，而大多数排除描述都能被 token 重叠直接命中 |
| B7 | `api.py:42-49` | `_health_cache` 加 `_health_cache_lock` | 裸 dict 的缓存击穿：并发调用同时读到 `payload is None`，各自真探一次 |
| B7 | `api.py:479-508` | `health()` 持锁做"判断 + 真探 + 写回" | 同上 |
| B7 | `api.py:511-560` | `_collect_health` 调整顺序：先算 probe 向量 → Chroma 用 `similarity_search_by_vector(probe_vector)` 复用；嵌入不可用时才退回文本检索 | 此前 `similarity_search("test")` 内部嵌一次 + 组件再 `embed_query("test")` 一次，两个向量逐字节相同 |

---

## 新增/修改的测试

| 测试名 | 断言什么 | 退化验证结果 |
|---|---|---|
| `test_debate_efficiency.py :: test_maintain_skips_final_review` | `行动=维持` 时 `agent_review_final` 调用次数 == 0，且工单风险等级含「未改变结论」 | 把边改回无条件 `add_edge` → **失败**：`assert 1 == 0`（日志可见 `final_review_done verdict=通过`） |
| `test_debate_efficiency.py :: test_revise_still_runs_final_review` | 对偶：`行动=修正` 时调用次数 == 1 | — |
| `test_debate_efficiency.py :: test_low_confidence_pass_marks_risk` | `置信度=30 + 通过` → 风险等级 ≠ 正常，且说明里带 `30` | 去掉低置信度分支 → **失败**：`KeyError`（日志 `workorder_done risk=正常`） |
| `test_debate_efficiency.py :: test_high_confidence_pass_stays_normal` | 对偶：`置信度=90 + 通过` → 风险等级 == 正常 | — |
| `test_debate_efficiency.py :: test_route_after_rebuttal_is_the_discriminator` | 三个分支各自的返回值 | — |
| `test_debate_efficiency.py :: test_final_review_prompt_contains_evidence` | 终审提示词含证据文本 | 不把 evidence 传进模板 → **失败**：`终审提示词里没有证据文本` |
| `test_debate_efficiency.py :: test_final_review_prompt_keeps_out_of_scope_boundary` | 补资料的同时保留「不得要求诊断引入…」边界 | — |
| `test_debate_efficiency.py :: test_final_review_node_forwards_evidence` | 节点真的把 `evidence` 传下去（接线） | — |
| `test_startup_readiness.py :: test_lifespan_reports_steps_and_marks_ready`（改） | 步骤数 6→7；`构建检索索引` 在列；`agents._bm25_initialized is True` | 去掉该步骤 → **失败**：`assert 6 == 7`（`_bm25_initialized` 断言同样会红） |
| `test_retrieval_efficiency.py :: test_same_query_hits_cache` | 同一 query 两次 → `similarity_search` 只 1 次 | 停用缓存读取 → **失败**：`同一 query 重复检索了 2 次` |
| `test_retrieval_efficiency.py :: test_different_query_is_not_served_from_cache` | 对偶：不同 query 各自检索 | — |
| `test_retrieval_efficiency.py :: test_cache_key_includes_k` | k 不同不复用 | — |
| `test_retrieval_efficiency.py :: test_cache_key_includes_knowledge_base_size` | 块数变化后缓存失效 | — |
| `test_retrieval_efficiency.py :: test_rebuild_bm25_index_clears_retrieval_cache` | 重建后重新检索 | — |
| `test_retrieval_efficiency.py :: test_normalized_query_shares_cache_entry` | 只差空白视为同一条 | 同上（共 2 条变红） |
| `test_retrieval_efficiency.py :: test_all_items_deterministically_matched_skips_llm` | 全部命中 → LLM 调用 0 次 | 改回"无条件叫 LLM" → **失败**：`全部命中时不该调用 LLM 做排除映射` |
| `test_retrieval_efficiency.py :: test_unmatched_item_still_calls_llm_and_unions` | 漏网项仍调 LLM，结果与确定性取并集 == `[0,2]` | — |
| `test_retrieval_efficiency.py :: test_llm_only_receives_unmatched_items` | 提示词只含漏网项 | — |
| `test_retrieval_efficiency.py :: test_union_still_covers_every_exclusion_item` | 回归 TC018：LLM 只答一部分时确定性通道补上 | — |
| `test_health_probe.py :: test_probe_embeds_the_query_only_once` | `embed_query` 调用 1 次、`by_vector` 1 次、`by_text` 0 次 | 改回 `similarity_search("test")` → **失败**：`assert 0 == 1`（`by_vector`） |
| `test_health_probe.py :: test_probe_falls_back_to_text_search_when_embedding_unavailable` | 对偶：嵌入挂了仍能探 Chroma | — |
| `test_health_probe.py :: test_concurrent_probes_trigger_only_one_real_probe` | 8 线程并发 → `_collect_health` 只被调 1 次 | 去掉缓存锁 → **失败**：`并发探活真探了 8 次（缓存击穿）` |
| `test_health_probe.py :: test_fresh_always_reprobes` | 对偶：`fresh=true` 绕开缓存 | — |

### 被改动的既有测试（逐条说明，均未弱化断言语义）

1. `test_startup_readiness.py :: test_lifespan_reports_steps_and_marks_ready`
   —— `len(steps_seen) == 6` → `== 7`。**语义未变**（仍要求"上报步骤数 == 实际步骤数"），
   只是实际步骤数因 B3 多了一步；同时**新增**两条更强的断言（步骤名在列 +
   `agents._bm25_initialized is True`）。
2. `test_resilience_and_retrieval.py :: test_map_excluded_causes_caches_identical_inputs`
   —— 排除条件由「轴承没问题」改为「润滑油已更换」。原因：B5 之后前者能被确定性
   通道命中，LLM 根本不会被调用，`calls["n"] == 1` 这条断言就测不到缓存了
   （全量回归实测 `assert 0 == 1`）。断言语义与数值（`== 1`）**均未改动**，
   只把输入换成"必须靠 LLM"的那一类，让它继续测它本来要测的东西。

---

## 回归结果

- `./venv/Scripts/python.exe -m pytest -q` → **431 passed**（409 → 431，新增 22 条）
- `./venv/Scripts/ruff.exe check . --statistics` → **0 问题**
- 前端本批无改动，未重跑

---

## 与任务书不符之处

1. **B4 的缓存键取值**：任务书建议 `key = (规范化 query, k, 知识库块数)`。我按此实现，
   但补充了一条：`rebuild_bm25_index()` 会**显式清缓存**，因为"块数恰好没变"的重建
   （改内容不增删）不会让键失效——那正是最需要失效的场景。
2. **B5 的"只把未命中的排除项交给 LLM"**：我选了这个更省的做法，但要注意一个副作用——
   若某条排除项一个有效 token 都没有（如被停用词吃光的短句），确定性通道必然判它"漏网"，
   于是仍会叫一次 LLM。这符合"宁可多排除不可漏排除"，但意味着**不是所有输入都能省下调用**。
3. **B6**：按任务书要求只做评估、未动手。评估见 `docs/修复报告-B6评估.md`。
   结论是**不建议**按方案 A 合并：在"资料不相关"这一支上反而更贵（现在该分支不调
   `agent_diagnose`），且会让判据从"独立预检"退化成"事后自评"。待你决策。
4. **B7 的锁粒度**：任务书说"给缓存读写加锁（或改成原子替换）"。我选了**持锁覆盖真探**
   ——并发调用会排队等同一个结果，而不是各自真探。代价是真探期间其他 `/health`
   调用要等；收益是"并发只烧一次配额"这条能被测试直接断言（实测无锁时 8 个线程
   真探 8 次）。
5. **B3 的测试桩方式**：任务书说"用桩避免真实全库分词"。我没有桩 `_init_bm25` 本身，
   而是让它**真实执行**、落到 conftest 指向的 tmp 空库上（走"空库短路"分支）。
   这样既不做任何分词，又能真正验证"接线是真的"——只桩掉函数本身的话，
   名字加进 steps 列表而函数没接上也能通过。

---

## 未做/建议后续

- B6 待决策（见上）。
- `workorder` 节点 token 占比 22.0%、均耗时 7617 ms，是本批未动的大头；
  它的输入是诊断 + 审核 + 成本三份 JSON，收窄空间比 `check_relevance` 大，
  且不涉及护栏形态。建议作为下一轮提速的首选目标。
- 本批把 `tests/conftest.py` 的隔离夹具提前落地（见文件头说明），
  同时**新增了一条"既有测试必须随之调整"的先例**：凡是改变"是否会调用 LLM"的优化，
  都可能让原本靠"调用次数"断言的测试失去靶子。这类测试应当**换输入**而不是删断言。
