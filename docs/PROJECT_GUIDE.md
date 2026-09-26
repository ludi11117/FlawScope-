# FlawScope 项目精读指南

> 目标：读完这一份，你能独立讲清这个系统的每一层，能自己改、自己扩展、自己排错。
> 所有结论都对着代码核对过（提交 `61f87f1`）。

---

## 0. 怎么用这份文档

三种读法，按你的时间选：

- **15 分钟抓全貌** → 只读第 1、4、7 章
- **半天上手改** → 第 1–4 章 + 第 14 章（操作手册）+ 第 15 章（坑）
- **彻底吃透** → 从头到尾，配合第 17 章的读码顺序

### 另有一份你自己维护的材料

根目录的 **`AgentDiag-代码全解.html`**（浏览器直接打开）是**按文件、逐函数**的走查，
**由项目作者自行维护，本指南不负责更新它**，只在读法上做个分工：

| 你想知道 | 看哪份 |
|---|---|
| 某个函数到底怎么写的、逐行在干什么 | `AgentDiag-代码全解.html`（你自己维护） |
| 为什么这么设计、各机制之间怎么配合 | 本指南 |
| 我要动手改一处，要同步哪些地方 | 本指南第 14 章 |
| 这个坑是怎么踩出来的 | 本指南第 15 章 |

本指南中的事实均与提交 `61f87f1` 的代码逐项核对过（文件行数、Agent 数、路由数、
模型数、模板数、节点数）。**代码改动后本指南需要同步更新**，否则会与实际行为脱节。

---

## 1. 这个项目到底在做什么

### 一句话

用户用大白话描述设备故障（可以带一张报警照片），系统自动走完
**信息抽取 → 知识检索 → 故障诊断 → 方案审核 → 辩论仲裁 → 成本精算 → 工单生成**，
最后给出一张能直接派工的维修工单；**证据不够时它会承认"我不知道"，而不是编一个根因。**

### 为什么不能"把问题丢给大模型就完事"

单次调用大模型有三个绕不开的问题，这个项目每一层都在对付它们：

| 问题 | 表现 | 本项目怎么治 |
|---|---|---|
| **幻觉** | 编一个听起来很专业但知识库里根本没有的根因 | 三重防线：检索无结果就降级 + 设备/报警代码确定性比对 + 相关性语义核验（第 6 章） |
| **过度自信** | 一次回答就当成结论，错了也没人拦 | 独立的审核角色 + 辩论仲裁，最多 3 轮（第 4 章） |
| **算术不可靠** | 报价算错，用户拿到的成本是假的 | 模型只负责"提取备件和工时"，钱由 Python 规则函数算（第 8 章） |

### 一次诊断的完整旅程（跟着真实数据流走一遍）

用户输入：`"那台数控机床主轴转起来一顿一顿的，还有怪声，温度也高得离谱"`

```
① extract_info   调 LLM 抽取 → {"设备类型":"数控机床主轴电机", "报警代码":null,
                                 "故障现象":["转速不稳","异响","温升异常"], "排除条件":[]}
② check_info     纯规则判断：有设备类型 + 有现象 → 信息够，放行
③ retrieve       把上面三个字段拼成查询串 → BM25 关键词检索 + 向量语义检索
                 → RRF 融合排名 → 拼成"【资料1】…【资料2】…"的证据文本
④ diagnose       先过三重护栏，再让 LLM 基于证据诊断 → {"根因判断":"…","依据":"…","排查建议":[…]}
⑤ review         另一个 LLM 扮演审核师：通过 / 不通过
                    ├─ 通过 ────────────────→ ⑥
                    └─ 不通过 → 辩论（最多 3 轮，第 4 章）
⑥ cost           LLM 只提取 ["主轴轴承"] 和 2.0 小时 → Python 算出 930+300=1230 元
⑦ workorder      LLM 汇总成结构化工单
                 ↓
            落库 SQLite（历史页可查、可导出 CSV）
```

关键点：**每一步的产物都写进同一个共享字典 `AgentState`**，下一步从它读。这是理解整个项目的钥匙。

---

## 2. 五分钟跑起来

```bash
# 1) 依赖
python -m venv venv
venv\Scripts\activate                # Windows
pip install -r requirements.txt

# 2) 密钥（config.py 里 SILICONFLOW_API_KEY 是必填，缺了 import 就报错）
copy .env.example .env
# 编辑 .env，填入 SILICONFLOW_API_KEY=你的密钥

# 3) 构建知识库（首次必须做，否则检索为空 → 所有诊断都会降级）
python build_knowledge_base.py

# 4) 三种入口任选
streamlit run app.py                 # 前端，浏览器开 http://localhost:8501
uvicorn api:app --port 8000          # API，文档在 http://localhost:8000/docs
python orchestrator.py               # 命令行直接跑一次诊断
```

**只跑测试不需要密钥**（`tests/conftest.py` 会兜底塞一个假 key）：

```bash
venv/Scripts/python.exe -m pytest -q        # 563 项，全离线，约 9 秒
```

---

## 3. 目录与文件职责

```
FlawScope/
├── orchestrator.py            ★ 状态机（调度中心）：定义 AgentState、十个节点、条件路由
├── agents.py                  ★ 所有"能力"：LLM 调用、检索、护栏、成本、Schema 校验
├── app.py                       Streamlit 前端（**仅作对照**，容器不再包含它；日常入口是 web/ 的 React 前端）
├── api.py                       FastAPI 接口层
├── database.py                  SQLite 读写 + 自动迁移
├── workorder_export.py          工单 → 可打印 Markdown（降级工单不补空章节）
├── config.py                    所有配置项（pydantic-settings，可被 .env 覆盖）
├── logging_config.py            structlog 配置 + TokenTracker（用量与耗时统计）
├── prompt_loader.py             Jinja2 模板加载器 + PromptTemplates 常量表
├── prompts/*.j2                 12 个提示词模板（不在 Python 里拼长字符串）
├── build_knowledge_base.py      构建/重建向量库（切分 data/knowledge_base.txt → ChromaDB，幂等）
├── eval_test.py                 自动化评估（LLM 判官 + 程序化指标），会真实花钱
├── compare_single_vs_multi.py   单 Agent vs 多 Agent 对比实验
├── test_cases.json              评估用例集（含对抗案例）
├── tests/                       563 项离线单测
├── data/knowledge_base.txt      原始知识库（示例级数据）
├── chroma_db/                   向量库持久化目录（gitignored）
├── diagnosis_history.db         诊断历史（gitignored）
├── legacy/                      早期原型脚本，已冻结，不参与 lint
└── Dockerfile / docker-compose.yml / entrypoint.sh
```

**读代码的入口顺序**：`config.py` → `orchestrator.py`（看骨架）→ `agents.py`（看血肉）→ `database.py` → `api.py` → `app.py`。

---

## 4. 核心一：状态机（`AgentState` 是跨层契约）

### 4.1 共享状态长什么样

`orchestrator.py` 顶部：

```python
class AgentState(TypedDict):
    user_input: str              # 用户原始输入
    image_description: str       # 图片经视觉模型转成的文字
    fault_info: Optional[dict]   # ① 抽取结果
    followup_question: str       # 信息不足时的追问
    evidence: str                # ③ 检索到的证据文本
    diagnosis: Optional[dict]    # ④ 诊断
    review: Optional[dict]       # ⑤ 初审
    rebuttal: Optional[dict]     # 辩论
    final_review: Optional[dict] # ⑦ 终审
    cost: Optional[dict]         # ⑥ 成本
    workorder: Optional[dict]    # ⑦ 工单
    debate_round: int            # 已辩论轮数
    max_debate_rounds: int       # 上限（默认 3）
    status: str                  # ★ 状态值，见 4.4
    correlation_id: str          # 全链路追踪 ID
```

**每个节点做的事就三件**：从 state 读 → 干活 → 返回一个**只含增量字段**的 dict。
LangGraph 负责把它 merge 回 state。所以你会看到节点都是 `return {"status": "xxx"}` 这种写法。

### 4.2 十个节点逐个讲

| # | 节点 | 输入 | 输出 | 调 LLM？ | 失败怎么办 |
|---|---|---|---|---|---|
| ① | `extract_info` | user_input + 图片描述 | fault_info | ✅ | 返回 `None` → 状态 `llm_failed`，**不追问用户** |
| ② | `check_info` | fault_info | followup_question / status | ❌ 纯规则 | — |
| ③ | `retrieve` | fault_info 拼成的查询 | evidence | ❌ 但调嵌入 API | 空知识库 → 退化成仅向量检索 |
| ④ | `diagnose` | evidence + fault_info | diagnosis | ✅ | 三重护栏降级 / 空结果 → `llm_failed` |
| ⑤ | `review` | diagnosis | review | ✅ | 空结果 → `llm_failed` |
| ⑥ | `rebuttal` | diagnosis + 驳回理由 | rebuttal + 新 evidence | ✅ | 空结果 → `llm_failed` |
| ⑦ | `final_review` | diagnosis + rebuttal | final_review | ✅ | 空结果 → `llm_failed` |
| ⑧ | `cost` | 有效诊断 + 有效审核 | cost | ✅ | 空结果 → `llm_failed`；已是终态则跳过 |
| ⑨ | `workorder` | 有效诊断 + 有效审核 + cost | workorder | ✅ | 空结果 → 兜底工单 + `llm_failed` |
| ⑩ | `human_review` | — | status | ❌ | — |

### 4.3 图结构（谁连谁）

```python
extract_info → check_info
check_info   → retrieve | need_more_info(END) | cost      # 三岔
retrieve     → diagnose
diagnose     → review | cost                              # 降级/失败直接跳去出工单
review       → cost | rebuttal | human_review             # 三岔
rebuttal     → final_review
final_review → cost | rebuttal                            # 不通过就回去再辩
cost         → workorder
workorder    → END
human_review → cost                                       # ★ 不是死胡同
```

三条设计意图，务必记住：

1. **降级/失败路径全部汇到 `cost → workorder`**，所以任何情况用户都能拿到一张工单（带风险标记），
   不会出现"只看到一句报错、什么都没有"。这就是 `human_review → cost` 的原因。
2. **辩论是个环**：`final_review → rebuttal → final_review`，靠 `debate_round` 计数收敛，
   到上限强制出结果。有上限防死循环。
3. **`diagnose` 可以直接跳过审核和辩论**：诊断本身就诚实降级了，再让审核去驳它、让辩论去翻它，
   只会凭空编造根因。

### 4.4 状态值全景（这是最容易改错的地方）

| 状态 | 含义 | 由谁产生 | 是终态？ | 前端显示 |
|---|---|---|---|---|
| `start` / `extracted` / `info_sufficient` / `retrieved` / `diagnosed` / `reviewed` / `rebutted` / `final_reviewed` / `costed` | 过程态 | 各节点 | ❌ | 不直接显示 |
| `need_more_info` | 信息不足，需要用户补充 | `check_info` | 终态（流程结束） | 蓝色提示 + 追问气泡 |
| `done` | 全流程正常完成 | `workorder` | ✅ | 绿色"诊断流程完成" |
| `insufficient_knowledge` | 知识库无依据 / 设备不匹配 / 资料不相关 | `diagnose` / `rebuttal` | ✅ 降级 | 黄色"知识库无相关依据" |
| `llm_failed` | 模型调用失败 / 输出无法校验 | 任意节点 | ✅ 失败 | 红色"模型服务调用失败" |
| `pending_human_review` | 审核判定结论不可采信，转人工 | `human_review` | ✅ 降级 | 红色"已转人工审核" |

代码里对应的常量：

```python
TERMINAL_FAILURE_STATUSES = ("insufficient_knowledge", "llm_failed", "pending_human_review")
```

**铁律：这三个状态在 `cost` / `workorder` 里不许被覆盖成 `costed` / `done`。**
否则用户会看到"✅ 诊断流程完成"，而实际上系统压根没诊断出来——这是修过的一个真实 bug。

### 4.5 改状态要同步哪几处（漏一处就出 bug）

1. `orchestrator.py`：产生它的节点 + 相关的 `route_after_*` 路由 + （如果是终态）加进 `TERMINAL_FAILURE_STATUSES`
2. `app.py` 第 194 行附近的 `if/elif` 状态横幅
3. `app.py` 第 339 行附近的 `status_color` 颜色字典
4. `api.py` 的响应模型（一般不用改，但确认字段能透出）
5. `README.md` 的流程图

> 注意第 2、3 处在 `app.py` 里是**两个分开的地方**（一个 if/elif、一个字典），
> 这正是最容易漏改的结构。想省事可以把它合并成一张表——见第 14 章的练习。

---

## 5. 核心二：混合检索与 RRF 融合

### 为什么要两路检索

| 通道 | 擅长 | 例子 |
|---|---|---|
| **BM25**（关键词） | 精确 token：报警代码、型号 | 用户说 `E-203`，资料里也写 `E-203` → 直接命中 |
| **向量**（语义） | 语义改写、同义表达 | 用户说"一顿一顿"，资料写"转速不稳定" → 也能命中 |

单用任何一个都会漏。所以两路都跑，再融合。

### 为什么融合不能用"拼接 + 截断"

BM25 的分数是 0~几十的无界值，余弦相似度是 -1~1，**量纲完全不同**。
如果简单地把 BM25 结果排在前面、再取前 k 条，向量通道会被整体挤掉——混合检索名存实亡。

**RRF（Reciprocal Rank Fusion）只用排名，不用分数**：

```
score(文档) = Σ  1 / (rrf_k + rank_i(文档))
                     i∈通道
```

`rrf_k` 默认 60（`settings.RRF_K`）。排名第 1 的文档贡献 `1/61`，第 2 名贡献 `1/62`……
两路各自的高排名文档都能进最终结果，且天然完成去重（同一文档出现在两路则分数累加，排名更靠前）。

代码在 `agents.py` 的 `_reciprocal_rank_fusion()`（纯函数，好单测）。

### 检索的关键参数

| 参数 | 默认 | 含义 |
|---|---|---|
| `RETRIEVAL_K` | 3 | 最终给诊断师的资料条数 |
| `BM25_K` | 3 | BM25 通道自己的候选数 |
| `RRF_K` | 60 | 融合平滑系数，越大越弱化高排名优势 |

> 两路**各取各的 k**，再由融合算法决定最终名额——这才是 RRF 的标准用法。

---

## 6. 核心三：三重防幻觉

幻觉 = 模型编造知识库里没有的根因。三道防线按顺序拦：

### 第一道：检索层——查不到就说查不到

`retrieve_evidence()` 融合后为空 → 返回固定标记 `"【知识库无相关依据】"`。
`diagnose_node` 一看到这个标记就直接降级，**根本不调诊断模型**。

### 第二道：确定性护栏——设备和报警代码必须在资料里出现

`is_equipment_in_evidence(fault_info, evidence)`：

- 报警代码：先归一化（去掉 `-`/`_`/空格/点、转小写）再比对。
  这样 `E-203` / `E203` / `e 203` / `E_203` 被视为**同一个代码**。
  （此前用精确子串比对，写法差异会被判成"资料里没有" → 误降级。）
  过短的代码（归一化后 < 3 字符）跳过代码级判定，否则 `"1"` 会命中任意文本。
- 设备类型：jieba 分词后取长度 ≥2 的词，只要有**任一**出现在资料里就算通过。

**为什么用规则而不是再问一次模型**：这是确定性判断，规则零成本、可复现、可单测。

### 第三道：语义相关性核验——三态返回

`check_relevance(fault_description, evidence)` 返回 **`True` / `False` / `None`**：

| 返回 | 含义 | 调用方行为 |
|---|---|---|
| `True` | 资料相关 | 继续诊断 |
| `False` | 资料不相关 | 降级，理由写"检索资料与故障不相关" |
| `None` | **核验没做成**（模型不可用，或输出无法解读） | 降级，理由写"相关性校验未能完成" |

**为什么要区分 `False` 和 `None`**：这两件事给用户的理由完全不同。
模型挂了却说成"资料不相关"，是把服务故障甩锅给知识库，用户会去查一个不存在的问题。

**一个必记的陷阱**：判定模型输出时不能写 `"不相关" not in content`——
"**不确定是否一致**"这类回答**不包含连续子串**"不相关"，会被当成"相关"放行。
凡是这种判定，必须同时挡掉"无法 / 不能 / 不确定 / 难以"这类含糊措辞。
`check_relevance` 和 `evaluate_semantic` 都栽过这个坑。

---

## 7. 核心四：失败与降级（本项目最重要的设计原则）

### 原则

> **出错就明说，绝不装作成功；证据不够就承认，绝不编造。**

### 短路矩阵

| 哪一步失败 | 后果（修复前） | 现在的行为 |
|---|---|---|
| 抽取 LLM 失败 | 返回 `{}` → 被当成"信息不足" → **追问用户补充设备类型** | 返回 `None` → 状态 `llm_failed` → 直接出降级工单 |
| 诊断 LLM 失败 | 空诊断进审核 → 判"不通过" → **辩论空转 3 轮**（白烧 6+ 次调用）→ 空工单 | 状态 `llm_failed` → 跳过审核与辩论 |
| 审核 LLM 失败 | 没有驳回理由却进辩论 → 继续空转 | 跳过整段辩论，直接出结果 |
| 复审 LLM 失败 | 同上 | 跳过，直接出结果 |
| 成本 LLM 失败 | `{}` 也置 `costed` → 最终报"✅ 诊断流程完成"，**用户拿到没有报价的工单** | 状态 `llm_failed` + 占位成本（"待人工核算"） |
| 工单 LLM 失败 | `workorder["风险等级"]=...` 写进空 dict → 残缺工单却报成功 | 兜底工单 + 状态 `llm_failed` |

### `extract_fault_info` 的两义返回值（最容易搞错）

```python
extract_fault_info(...) -> Optional[dict]

None  → 模型调用彻底失败（熔断/重试耗尽）→ 必须短路，绝不能追问用户
{}    → 模型正常响应，但抽不出结构化信息 → 这才是真正的"信息不足"，走追问
```

**合并处理等于把服务故障甩锅给用户。** 这条在 `MEMORY.md` 里是硬约束。

### 熔断器

`agents.py` 的 `CircuitBreaker(failure_threshold=5, recovery_timeout=60)`：

- 连续失败 5 次 → 进入 `open`，后续调用直接抛 `CircuitBreakerOpenError`（不再打上游）
- 60 秒后进入 `half-open`，放一次请求试探：成功 → `closed`，失败 → 继续 `open`
- **成功必须清零 `failure_count`**。只在 half-open 里清零的话，语义会从"连续失败"
  退化成"累计失败"——成功夹在中间也不重置，几天攒够 5 次偶发抖动照样熔断
- **必须加锁**：它是模块级单例，Streamlit 多会话 + FastAPI 线程池都会碰它。
  锁只包住状态读写，**不能包住 `func()` 本身**，否则所有 LLM 调用会被串行化

---

## 8. 核心五：成本规则（模型绝不算数）

### 分工

```
LLM  →  只提取两个参数：备件清单 ["主轴轴承"]、预计工时 2.0
Python →  calculate_cost(parts, hours) 算钱（纯函数、已单测）
```

`calculate_cost()` 用 `config.py` 里的价格表：

```python
PARTS_PRICE = {"主轴轴承": 850, "润滑脂": 80, "冷却风扇": 300,
               "液压泵": 2500, "溢流阀": 600, "液压油": 200}
LABOR_RATE_PER_HOUR = 150
```

返回 `{"备件费用", "工时费用", "总费用", "未知备件"}`。

**关键细节**：价格表里**没有**的备件不计费，但必须显式写进 `计费提示`
（"以下备件不在价格表中，未计入报价，需人工核价：…"）。
否则用户会以为拿到的报价是完整的。

> 想改价格：改 `config.py`，或用环境变量 `PARTS_PRICE_JSON='{"主轴轴承":900}'` 覆盖，不用改代码。

---

## 9. 核心六：Schema 校验与"自修重试"

### 问题

模型经常把 `"排查建议": ["换轴承"]` 写成 `"排查建议": "换轴承"`，或者漏一个字段。
如果这算"模型故障"，整条链路就白跑了——但模型其实好好的，只是格式没对齐。

### 方案

所有 Agent 统一走 `agents.invoke_and_validate()`：

```
调模型 → 解析 JSON → Pydantic 校验
   ├─ 通过 → 返回模型对象
   └─ 校验失败 → 把「Schema + 你的原始输出 + 具体报错」拼成一条消息追加到对话里
                → 让模型自己改一次（模板 prompts/fix_schema.j2）
                → 改完仍不合规才返回 None（这才算真失败）
```

**新增 Agent 请沿用这条通道**，不要自己用 `invoke_and_parse_json` + `validate_and_parse` 裸拼。

另外 `agent_workorder` 还挂了个 `preprocess=_normalize_workorder_types`：
像"维修方案写成数组"这种**确定性**格式问题，用规则修比再花一次 LLM 调用便宜得多。

---

## 10. 可观测性：Token 与耗时归因

### 两个曾经失真、现在修好的点

1. **Token 用量**：优先读服务端返回的 `response.usage_metadata`（真实值）；
   取不到才回退到本地 tiktoken 估算，并记进 `estimated_calls` 计数。
   —— 只看总数无法判断这个数字准不准，所以把"其中多少次是估算的"一并暴露。
2. **按节点归因**：`orchestrator._tracked(node_name)` 装饰器在节点执行前
   `tracker.start_node(name)`，于是该节点内的 LLM 调用都记在它名下。
   没有这个装饰器，所有用量会全部落在初始化那一次 `start_node("init")` 上。

`TokenTracker` 还记录每个节点的 `duration_ms`——Token 只能回答"花了多少钱"，
回答不了"为什么这次这么慢"。

### 快照必须是深拷贝

`get_summary()` 返回的 `by_node` 必须**深一层拷贝**。
浅拷贝的话调用方拿到的是 tracker 内部那个 dict，而这份快照会被放进结果、
存进 session_state、写进数据库——后续再发生的 LLM 调用会改写这份"历史快照"。

### 全链路追踪

`correlation_id` 贯穿日志与数据库（`diagnosis_records.correlation_id` 列）。
拿到一条历史记录，就能用这个 ID 去日志里捞出那一轮的完整过程。

---

## 11. 数据层：SQLite 设计

### 表结构

```sql
CREATE TABLE diagnosis_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    fault_description TEXT NOT NULL,
    status TEXT,
    diagnosis TEXT, review TEXT, rebuttal TEXT, final_review TEXT,
    cost TEXT, workorder TEXT,               -- 全部存 JSON 字符串
    debate_round INTEGER,
    token_usage TEXT,                        -- 完整用量明细（含按节点归因）
    total_tokens INTEGER DEFAULT 0,          -- 冗余列，专供聚合统计
    correlation_id TEXT                      -- 追踪 ID
);
```

### 三个值得学的设计

1. **JSON 存半结构化结果**：诊断/审核/工单的字段会随提示词演进而变，
   用 TEXT 存 JSON 比频繁改表结构灵活。读取时统一反解析为 dict。
2. **`total_tokens` 冗余列**：统计页只要总和。如果每次 `SELECT token_usage` 全表取回再逐行
   `json.loads`，打开一次统计页就要把全部历史读进内存解析一遍。加个冗余列后直接 `SUM()`。
3. **自动迁移**：`_ensure_columns()` 用 `PRAGMA table_info` 查现有列，缺什么补什么
   （`ALTER TABLE ... ADD COLUMN`）。`_backfill_total_tokens()` 再给老记录回填。
   **新增写库字段时，记得同步 `_ensure_columns` 列表**，否则旧库会报 `no such column`。

### 连接管理

- 连接是 **thread-local** 的（每线程一条），因为 Streamlit/FastAPI 都是多线程
- 开了 **WAL 模式**：读写不阻塞；代价是会生成 `-wal` / `-shm` 伴生文件
  （这就是为什么 Docker 里必须挂载**目录**而不是单个 `.db` 文件）
- 测试要切库时：先 `close_db_connections()`，再 monkeypatch `DB_PATH` 和 `_db_initialized`

---

## 12. 接口层：API 与前端

### API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/diagnose` | 提交故障描述（可带 base64 图片）执行诊断；并发超上限返回 503 |
| POST | `/diagnose/stream` | **流式诊断（SSE）**：逐节点推 `progress`，最后推 `result` + `done`。前端默认走这条 |
| GET | `/records` | 查历史，支持 `keyword` / `status` / `limit` / `offset` |
| GET | `/records/{id}` | 单条详情 |
| GET | `/records/{id}/workorder.md` | 导出该条记录的工单（可打印 Markdown） |
| DELETE | `/records/{id}` | 删除 |
| GET | `/stats` | 统计 |
| GET | `/meta/statuses` | **状态值口径**（label / level / 是否终态 / 是否落库 / 是否可追问）。静态元数据、无需鉴权，前端启动时拉一次覆盖内置兜底 |
| GET | `/health/live` | **存活探针**：只确认进程在，毫秒级，不碰外部依赖，**启动期间也是 200** |
| GET | `/health/ready` | **就绪探针**：初始化完成没，**未就绪返回 503**；只读内存状态，可高频轮询 |
| GET | `/health` | **依赖探针**：真探 LLM/Embedding/ChromaDB/SQLite，结果缓存 60s |

三个探针的分工不能混用：`live` 回答"进程要不要被重启"，`ready` 回答"请求现在能不能成功"，
`health` 回答"依赖全不全"（**只有它会烧配额**）。

三个容易踩的点：

- **`/records` 的 `total` 必须用独立的 `COUNT(*)`**（`database.count_records()`）。
  用返回行数当总数的话，`limit=10` 时 `total` 恒为 10，调用方无法判断"还有没有更多"。
  ⚠️ **这条规则对前端同样成立**：`app.py` 的历史页一度还在用 `len(records)`，
  于是库里 500 条也只显示"共找到 20 条记录"。凡是"列表 + 总数"，总数一律走 `count_records()`。
- **`total` 修对 ≠ 能翻页**：`get_records` 早期只有 `limit`，调用方知道"还有更多"却取不出第二页。
  现在 `limit` 与 `offset` 成对出现，前端也有翻页控件与页码越界夹取
  （换筛选条件后结果变少，停在第 5 页会看到空列表，用户会误以为"一条都没有"）。
- **`/health` 会真调 LLM + Embedding**，所以加了 TTL 缓存。
  容器 healthcheck 请用 `/health/live`，否则每 30s 一次持续烧配额。
  `?fresh=true` 强制真探，属于"会花钱"的操作：**配了 `API_KEY` 时强制校验 `X-API-Key`**
  （未配置 API_KEY 的本地开发环境保持放行）。命中缓存时不校验——它只读内存，没有烧配额的风险。
  缓存读写有锁：并发探活只会真探一次，不会各自烧一份配额。

### 并发闸门

`/diagnose` 用 `threading.BoundedSemaphore(settings.MAX_CONCURRENT_DIAGNOSES)` 限并发。
一次诊断要串行发 6~9 次 LLM 调用、耗时数十秒；不限并发时突发流量会占满 FastAPI 的线程池
（连 `/health` 都得排队），并把模型配额成倍烧掉。超限**明确返回 503 + `Retry-After`**，
而不是无声排队——排队只会让调用方挂到超时，还一直占着线程不放。

用 `BoundedSemaphore` 而不是普通 `Semaphore`：多 release 一次会直接抛错，
让"release 次数写错"在测试里就暴露。`release()` 放在 `finally` 里，
否则诊断中途抛异常会永久占掉一格，连续几次失败后整个接口就再也不接请求了。

### 鉴权

`require_api_key` 是**可选**的：`settings.API_KEY` 留空则完全放行（本地开发默认），
设置了就要求请求头 `X-API-Key`。用 `secrets.compare_digest` 而不是 `==`，避免比较耗时泄露密钥信息。

### 前端

三个页面：诊断页 / 历史页 / 统计页。几个关键点：

- 诊断过程用 `run_diagnosis_stream()` 逐节点 `yield`，前端实时更新进度
- 诊断完成后**必须落库**（`save_diagnosis_record`），否则"诊断历史"对网页用户永远是空的
- `download_button` 必须**直接渲染**，不能嵌在 `if st.button(...)` 里
  （点击后页面重跑、按钮状态复位，下载按钮根本来不及出现）
- 上一轮已出结论时，再次提交要**清空累积上下文**，否则新故障会被旧描述污染

---

## 13. 测试体系：怎么写出"能失败的测试"

### 铁律

> **单测必须离线可跑**：不调 LLM、不联网、不碰真实数据库。
> 做不到这一点，测试就会变成"要花钱才能跑"的东西，慢慢就没人跑了。

三个手法：

1. **LLM 打桩**：`monkeypatch.setattr(agents_module, "safe_llm_invoke", lambda *a, **kw: "…")`
2. **检索打桩**：伪造一个只有 `get()` 和 `similarity_search()` 的 Chroma 替身
3. **数据库隔离**：`close_db_connections()` → monkeypatch `DB_PATH` 到 `tmp_path` → monkeypatch `_db_initialized = False`

### 最重要的一条：验证你的测试真的能失败

写完一个"修复并发问题"的测试，**必须**把修复临时去掉、确认测试变红，再装回来。

真实例子（BM25 索引加锁）：

```
把锁换成空实现 → db.get() 被调用 8 次
用真锁         → db.get() 被调用 1 次
```

真实例子（懒加载单例加锁，`agents.get_llm / get_vision_llm / get_embeddings`）：

```
把 _singleton_lock 换成空实现 → 8 个线程各构造一次（构造 8 次）
用真锁                        → 只构造 1 次
```

⚠️ 模拟"无锁"时有个坑：别用 `contextlib.contextmanager` 生成的实例去替换锁——
那是一次性的，跨线程复用会抛 `AttributeError`，于是所有线程都失败退出，
构造次数恰好变成 1，你会得出"测试无效"的**假结论**。要写一个线程安全的空
`__enter__` / `__exit__` 类。

不做这一步，测试很可能是装饰——线程压根没重叠、断言恒为真。

### 状态机端到端冒烟

`tests/test_resilience_and_retrieval.py` 里把用到的 Agent **全部 monkeypatch 成桩**
（`_stub_happy_agents()` 一次替换 8 个：抽取、检索、设备护栏、相关性、诊断、审核、成本、工单），
然后跑**真实的 LangGraph**。这样能验证节点装饰、条件路由、状态流转是自洽的，且一分钱不花。

覆盖的分支：happy path 到 `done`、诊断失败短路（断言审核/辩论调用次数为 0）、
信息不足走追问且不进检索、辩论在上限处收敛并标高风险。

### 别把函数名当证据

本仓库被多个会话编辑过，两边可能实现了**同名但语义不同**的函数。
判断某处是否真的修好，必须**把函数体读出来逐行看**，并为它写一条"未修复时会失败"的断言。

---

## 14. 操作手册：常见改动怎么做

### ① 加一个状态

1. 产生它的节点里 `return {"status": "新状态"}`
2. `route_after_*` 里加上对应分支（并在 `add_conditional_edges` 的映射表里注册）
3. 如果是失败/降级终态 → 加进 `TERMINAL_FAILURE_STATUSES`
4. `app.py` 的 `if/elif` 横幅 + `status_color` 字典
5. README 流程图
6. 补一条测试（至少断言路由走向）

### ② 加一个 Agent

1. `prompts/新agent.j2` 写提示词
2. `prompt_loader.PromptTemplates` 加常量
3. `agents.py` 定义 Pydantic 输出模型 + 写 `agent_xxx()`，**用 `invoke_and_validate`**
4. `orchestrator.py` 写节点函数，`graph.add_node("xxx", _tracked("xxx")(xxx_node))`
   —— **`_tracked` 别忘了包**，否则 Token 归因不到这个节点
5. 接上边：`add_edge` 或 `add_conditional_edges`
6. 加进 `NODE_DESCRIPTIONS`（前端进度展示要用）
7. 补测试

### ③ 换模型 / 调参数

改 `.env` 或 `config.py`：`DIAGNOSIS_MODEL`、`JUDGE_MODEL`、`VISION_MODEL`、`EMBEDDING_MODEL`。
编排参数：`MAX_DEBATE_ROUNDS`、`RETRIEVAL_K`、`BM25_K`、`RRF_K`。
**新增配置项要同步补进 `.env.example`**（它是配置项的权威清单）。

### ④ 扩知识库

1. 把新资料追加进 `data/knowledge_base.txt`
2. 重新执行 `python build_knowledge_base.py`（默认**清空重建**，可反复执行；
   不加 `--append` 就不会出现重复条目）
3. 重启服务（BM25 是进程内缓存，`rebuild_bm25_index()` 可强制重建）

**知识库条目的写法直接影响排除条件功能**：`extract_kb_causes()` 靠
"`一、二级标题` + `1. 条目`"这个格式解析，条目要一条一个原因，不要把多个原因捆在一行。

### ⑤ 练习（想真正吃透就做这个）

- 把 `app.py` 里那两处状态映射（if/elif 横幅 + 颜色字典）**合并成一张表**，
  顺便把 `STATUS_BANNER` 抽到 `config.py` 或独立模块，让"改状态只改一处"
- 把 `/diagnose` 的**全局**并发闸门改成按调用方（IP / API Key / 租户）隔离，
  这样一个人打满配额时不会把其他人一起 503
- 把历史页的逐条 `st.json`（200 条 × 7 个）改成表格 + 按需展开
- 把 `/diagnose` 从同步阻塞改成"任务队列 + 轮询/SSE"
- 给 `build_knowledge_base.py` 的切分参数（`chunk_size` / `chunk_overlap`）加个 `--chunk-size` 开关，
  并对比不同切分粒度下的检索命中率

---

## 15. 这个仓库特有的坑（血泪清单）

| 坑 | 后果 | 正确做法 |
|---|---|---|
| 用 `len(get_records(...))` 当分页 `total` | `total` 恒等于 `limit`，翻页逻辑以为已到底 | 用 `database.count_records()`（**前端同理**） |
| 只修 `total` 不加 `offset` | 调用方知道"还有更多"却取不出第二页，等于只修一半 | `limit` 与 `offset` 成对出现，前端也要有翻页控件 |
| 裸的 `if _x is None: _x = 构造()` 做单例 | 并发首次调用会重复构造；Chroma 重复打开同一 persist 目录会争抢 SQLite 锁 | 双重检查 + 锁（本项目统一用 `_singleton_lock`） |
| 用普通 `Lock` 保护"会互相调用"的单例 | `get_db()` 内部要调 `get_embeddings()`，同线程二次 acquire 直接自锁死 | 用 `RLock`（可重入） |
| 重跑 `build_knowledge_base.py` | `Chroma.from_texts` 是**追加**语义，库里块数翻倍，检索全是近重复条目 | 默认清空重建（`reset_collection()` 后再写） |
| 用 `contextlib.contextmanager` 的实例模拟"无锁"来验测试 | 上下文管理器实例一次性，跨线程复用抛 `AttributeError`，反而得出"测试无效"的假结论 | 写一个线程安全的空 `__enter__/__exit__` 类 |
| 单独挂载 `diagnosis_history.db` 到 Docker | 宿主机没这文件时 Docker 建**同名目录**，SQLite 打不开；WAL 伴生文件也留不住 | 挂载 `data/` 目录 + 设 `DB_PATH=/app/data/...` |
| 容器 healthcheck 用 `/health` | 每 30s 真调一次 LLM，持续烧配额 | 用 `/health/live` |
| `"不一致" not in content` 这类否定判定 | 漏掉"不确定是否一致"（不含连续子串），指标注水 | 同时挡掉"无法/不能/不确定/难以" |
| 直接调用 FastAPI 端点函数做单测 | 绕过参数解析，拿到的是 `Query` 对象本身而不是默认值 | 显式传全部参数；HTTP 契约另用 OpenAPI schema 断言 |
| 用 `git checkout -b fix/xxx`（带斜杠的分支名） | 沙箱打印成功但不建引用，HEAD 变 unborn、文件全变 untracked | 用不带斜杠的名字，或 `git branch <name>` |
| 对同一文件并行发多个 Edit | 出现过"两个都返回成功但其中一个没落盘" | 串行改，改完 grep 复核 |
| `git fetch` 后直接用 `origin/main` | 沙箱下 `refs/remotes/origin/main` 不落盘，报 unknown revision | 用 `FETCH_HEAD` |
| 多个会话同时编辑本仓库 | 两边互相覆盖，得到语义混杂的半成品 | 开工前先检测并发写者（见下） |
| 提示词里拼长字符串 | 无法维护、无法复用、改一处漏一处 | 全部放 `prompts/*.j2` |

**开工前先检测有没有别的会话在写**：

```bash
find . -maxdepth 2 -type f -newermt '-90 seconds' \
     -not -path './venv/*' -not -path './.git/*' -not -path './chroma_db/*' \
     -not -path './__pycache__/*' -printf '%TH:%TM:%TS %p\n' | sort -r
```

隔 20 秒采样两次。**有并发写者时不要改文件**——先停掉另一个会话。

---

## 16. 面试 / 答辩要点

被问到"这个项目有什么技术含量"，按这个顺序答：

1. **为什么用状态机而不是线性代码？**
   需要条件路由、循环（辩论）、提前终止（追问），且每步状态可追溯。LangGraph 把这三件事
   表达成图，路由逻辑可单测。

2. **为什么要多 Agent 辩论？**
   单次诊断容易过度自信。用独立审核角色制衡，并让诊断师基于**新检索到的证据**自我纠偏。
   有上限（3 轮）防死循环，有兜底（高风险标记）防卡死。

3. **为什么成本不让模型算？**
   模型算术不可靠。模型只做它擅长的"提取备件和工时"，价格用确定业务规则算。
   而且价格表没覆盖的备件会显式提示待核价，不静默漏报。

4. **为什么用 RRF 而不是简单拼接？**
   BM25 分与余弦相似度量纲不同，直接按分数排序会让一路垄断。RRF 只看排名
   （`Σ 1/(60+rank)`），两路头部都能进结果，还天然去重。

5. **怎么防幻觉？**
   三重：检索无结果降级 → 设备/报警代码确定性比对 → 相关性语义核验（三态返回）。
   再加提示词强约束"不得添加资料外原因"。

6. **失败怎么处理？**（最能体现工程素养的一题）
   任何一步 LLM 失败都短路并写进 `llm_failed` 终态；**区分"模型失败"和"信息不足"**，
   绝不把服务故障甩锅给用户；所有降级路径都产出**带风险标记的工单**，没有死胡同。

7. **怎么保证可观测？**
   `correlation_id` 全链路；Token 取服务端真实 usage 并区分估算；Token 与耗时都按节点归因。

---

## 17. 学习路径（按这个顺序读代码）

| 顺序 | 读什么 | 带着什么问题读 |
|---|---|---|
| 1 | `config.py` | 有哪些可调参数？默认值是什么？ |
| 2 | `orchestrator.py` 的 `AgentState` | 系统在哪些数据之间流转？ |
| 3 | `orchestrator.py` 的十个节点函数 | 每个节点读什么、写什么、失败怎么处理？ |
| 4 | `orchestrator.py` 的 `route_after_*` + 图构建 | 分支条件是什么？为什么这么分？ |
| 5 | `agents.py` 的 `safe_llm_invoke` / `invoke_and_validate` | LLM 调用是怎么被保护的？ |
| 6 | `agents.py` 的 `retrieve_evidence` / `_reciprocal_rank_fusion` | 检索怎么融合？ |
| 7 | `agents.py` 的 `is_equipment_in_evidence` / `check_relevance` | 幻觉是怎么被拦的？ |
| 8 | `agents.py` 的 `calculate_cost` | 钱是怎么算的？ |
| 9 | `database.py` | 数据怎么存？旧库怎么迁移？ |
| 10 | `api.py` → `app.py` | 对外怎么暴露？前端怎么用？ |
| 11 | `tests/` | 每个修复对应哪条断言？ |
| 12 | `prompts/*.j2` | 提示词是怎么约束模型的？ |

读完这 12 步，你就真的拥有这个项目了。

---

## 附：常用命令速查

```bash
# 测试与检查
venv/Scripts/python.exe -m pytest -q          # 563 项离线单测
ruff check .                                   # 静态检查
ruff check . --statistics                      # 看各类问题数量

# 运行
python build_knowledge_base.py                 # 重建知识库（首次必做）
streamlit run app.py                           # 前端 :8501
uvicorn api:app --port 8000                    # API :8000
python orchestrator.py                         # 命令行跑一次

# 评估（会真实花钱）
python eval_test.py                            # 完整评估，报告落 eval_reports/
python eval_test.py --skip-judge --limit 3     # 只跑程序化指标，快速验证

# 容器
docker compose up -d --build
docker compose logs -f

# 排查
git log --oneline -10                          # 最近提交
git show --stat HEAD                           # 本次提交改了哪些文件
```
