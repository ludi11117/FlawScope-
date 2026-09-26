"""启动就绪契约的回归测试（`startup_status.py` + `/health/ready`）。

## 这组测试守的是什么

`/health/live` **在启动过程中也会返回 200**——进程确实活着，但向量库、
BM25 索引都还没加载，此时发起诊断必然失败。前端顶栏状态灯原先只看 liveness，
于是冷启动的十几秒里显示"服务正常"，用户据此点"开始诊断"、拿到一个失败，
而失败原因（服务还没起来）从错误信息里读不出来。

这是"探针在真实可用之前就宣告可用"，属于诚实性问题，与项目一贯立场冲突。

所以这里断言的核心是**两个探针的语义必须不同**：
  - `/health/live`  启动期间就该是 200（liveness 不能因为依赖没就绪就重启容器）
  - `/health/ready` 启动期间必须是 503（readiness 回答"请求现在能不能成功"）

把两者混用是部署里极常见的一类错误，所以下面既有"该放行的"也有"该拦下的"。

全部离线：不调 LLM、不联网、不碰真实数据库。
"""

import pytest
from fastapi.testclient import TestClient

import agents as agents_module
import api
import startup_status


@pytest.fixture(autouse=True)
def _reset_startup_state():
    """每个用例前后都把启动状态复位。

    必须有这个夹具：`startup_status` 是模块级状态，同一 pytest 进程里只初始化
    一次，前一个用例 `mark_ready()` 之后，后一个用例就再也测不到"未就绪"分支——
    测试会全绿但什么都没验到。这类"状态跨用例泄漏"是假绿测试的常见来源。
    """
    startup_status.reset_for_tests()
    yield
    startup_status.reset_for_tests()


def _client() -> TestClient:
    # 不用 with 语句：那样会触发 lifespan（真去加载向量库/模型），
    # 单测必须离线。我们直接操纵 startup_status 来构造两种状态。
    return TestClient(api.app)


# ---------- 第一组：startup_status 状态机 ----------

def test_initial_state_is_not_ready():
    """刚启动时必须报未就绪。初始为 ready 会让探针在初始化完成前放行。"""
    st = startup_status.snapshot()
    assert st["ready"] is False
    assert st["error"] is None
    assert st["elapsed_ms"] is None
    assert st["warnings"] == []


def test_set_step_records_progress_but_does_not_claim_ready():
    """推进步骤只更新进度，绝不能顺带把 ready 置真。

    这是"进度条走到 6/6 就等于可用"这类偷懒实现的判别式：
    进度走完 ≠ 初始化成功，就绪必须由 mark_ready 显式宣告。
    """
    startup_status.set_step("校验知识库", 6, 6)
    st = startup_status.snapshot()
    assert st["step"] == "校验知识库"
    assert st["step_index"] == 6
    assert st["step_total"] == 6
    assert st["ready"] is False


def test_mark_ready_sets_flag_and_elapsed():
    startup_status.mark_ready()
    st = startup_status.snapshot()
    assert st["ready"] is True
    assert st["elapsed_ms"] is not None
    assert st["error"] is None


def test_mark_failed_keeps_not_ready_and_records_reason():
    """启动失败时 ready 必须保持 False。

    半初始化的服务对外宣告"就绪"比直接崩溃更危险——
    调用方会以为请求失败是自己的问题。
    """
    startup_status.mark_failed("ConnectionError: 无法连接向量库")
    st = startup_status.snapshot()
    assert st["ready"] is False
    assert "无法连接向量库" in st["error"]


def test_snapshot_is_a_copy_not_a_live_reference():
    """快照必须是拷贝——标量和列表都要。

    返回内部 dict 的引用会让调用方（或测试）无意间改写全局状态，
    而且这种 bug 只在并发下才暴露。

    列表要单独再拷一层：`dict(_state)` 是浅拷贝，`warnings` 还是同一个对象，
    `snap["warnings"].append(...)` 就能污染模块状态。所以这里对列表也断言一遍，
    只测标量会让"浅拷贝漏了列表"这种情况照样绿。
    """
    snap = startup_status.snapshot()
    snap["ready"] = True
    assert startup_status.snapshot()["ready"] is False

    startup_status.add_warning("原始警告")
    snap = startup_status.snapshot()
    snap["warnings"].append("被调用方塞进去的")
    assert startup_status.snapshot()["warnings"] == ["原始警告"]


def test_reset_restores_initial_state():
    """复位必须把 ready 与 error 一起清掉，否则用例之间会互相污染。"""
    startup_status.mark_failed("boom")
    startup_status.reset_for_tests()
    st = startup_status.snapshot()
    assert st["ready"] is False
    assert st["error"] is None
    assert st["elapsed_ms"] is None


# ---------- 第二组：/health/ready 的语义 ----------

def test_ready_endpoint_returns_503_while_initializing():
    """核心断言：未就绪必须用 503 表达，不能是 200。

    用状态码而不是 body 里的 ready:false，是因为部署脚本与负载均衡
    大多只看状态码——放行一个还在初始化的实例，等于把冷启动耗时
    转嫁成用户的第一次请求失败。
    """
    startup_status.set_step("加载向量库", 4, 5)
    resp = _client().get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False


def test_503_body_still_carries_progress():
    """503 的响应体必须带进度。

    否则前端只能显示"正在启动"，用户不知道还要等多久、也判断不出是不是卡住了。
    状态码给机器看，body 给人看，两者都要有。
    """
    startup_status.set_step("加载向量库", 5, 6)
    body = _client().get("/health/ready").json()
    assert body["step"] == "加载向量库"
    assert body["step_index"] == 5
    assert body["step_total"] == 6


def test_ready_endpoint_returns_200_after_mark_ready():
    startup_status.mark_ready()
    resp = _client().get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["elapsed_ms"] is not None


def test_ready_endpoint_exposes_version():
    """版本号要走 API_VERSION 这个唯一来源。

    历史上出现过 `/health/live` 谎报旧版本：改版本时只改了 FastAPI 元数据那一处。
    这类不一致在"探活只返回 200 就算过"的脚本里根本看不出来，
    所以这里显式断言 ready 也带上了当前版本。
    """
    startup_status.mark_ready()
    assert _client().get("/health/ready").json()["version"] == api.API_VERSION


def test_ready_endpoint_reports_failure_reason():
    """启动失败的原因要能被前端读到，否则界面会永远停在"正在启动"。"""
    startup_status.mark_failed("RuntimeError: chroma 目录不可写")
    body = _client().get("/health/ready").json()
    assert body["ready"] is False
    assert "chroma 目录不可写" in body["error"]


def test_ready_endpoint_does_not_touch_external_dependencies(monkeypatch):
    """就绪探针必须只读内存状态，不能去探 LLM / Embedding / ChromaDB。

    这条防的是"顺手复用 _collect_health()"式的实现：那个会真调模型，
    而前端状态灯 1.5~5 秒轮询一次，等于持续烧配额
    （后端给 /health 加 TTL 缓存正是为了这个原因）。

    做法：把三个依赖 getter 全换成会抛异常的桩——真去探就必然失败。
    """
    def _boom(*_a, **_k):
        raise AssertionError("就绪探针不应触碰外部依赖")

    monkeypatch.setattr(api, "get_llm", _boom)
    monkeypatch.setattr(api, "get_embeddings", _boom)
    monkeypatch.setattr(api, "get_chroma_db", _boom)

    startup_status.mark_ready()
    assert _client().get("/health/ready").status_code == 200


# ---------- 第三组：与 /health/live 的语义必须不同（本文件的核心） ----------

def test_live_stays_200_while_not_ready():
    """liveness 在启动期间就该是 200——它回答的是"进程要不要被重启"。

    断言这条是为了把"两个探针语义不同"这件事**钉在测试里**。
    如果哪天有人把 /health/live 也改成"未就绪就 503"，
    编排层会在冷启动时反复重启容器，永远起不来。
    """
    startup_status.set_step("加载向量库", 1, 5)
    resp = _client().get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"


def test_live_and_ready_disagree_during_startup():
    """判别式：启动期间两个探针的结论必须不同。

    这是本文件存在的唯一理由。若有人把 /health/ready 实现成 /health/live 的
    别名（"都是探活，合并一下"），这条会立刻变红——而那正是原始缺陷的形态。
    """
    startup_status.set_step("加载向量库", 2, 5)
    client = _client()
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503


def test_ready_and_live_agree_once_ready():
    """对偶：就绪之后两者必须一致。

    只测"启动期不同"会让实现退化成"ready 永远 503"——
    那样用户永远等不到可用，测试却全绿。
    """
    startup_status.mark_ready()
    client = _client()
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


# ---------- 第四组：lifespan 的接线（防"函数写对了但没接上"） ----------

def test_lifespan_reports_steps_and_marks_ready(monkeypatch):
    """驱动真实的 lifespan，确认它真的会上报进度并最终置为就绪。

    不测这一条的话，`startup_status` 的单元测试全绿、端点测试也全绿，
    但只要 lifespan 忘了调 `mark_ready()`，线上就永远停在"正在启动"——
    而所有测试都发现不了。这是"函数改对了 ≠ 调用点接对了"的典型。
    """
    steps_seen: list[str] = []

    # 把初始化步骤全换成桩：单测不能真去加载向量库。
    # 注意「构建检索索引」（B3 新增）用的是**真实** `agents._init_bm25`：
    # 它内部会调 `agents.get_db()`，而 conftest 的 autouse 夹具已把
    # CHROMA_PERSIST_DIR 指到 tmp_path，落到一个空库上 —— 于是走"空库短路"
    # 分支，不做任何分词，同时把 `_bm25_initialized` 置真。
    # 这正是我们要测的：**接线是真的**，而不只是"步骤列表里有个名字"。
    monkeypatch.setattr(api, "configure_logging", lambda: None)
    monkeypatch.setattr(api, "init_db", lambda: None)
    monkeypatch.setattr(api, "get_llm", lambda: None)
    monkeypatch.setattr(api, "get_embeddings", lambda: None)
    # 桩到 agents 层的取数函数（而不是桩 `_check_knowledge_base` 本身），
    # 这样"校验知识库"这一步的函数体仍会被真实执行，接线断了能测出来。
    monkeypatch.setattr(api, "get_knowledge_base_size", lambda: 73)

    real_set_step = startup_status.set_step

    def spy_set_step(step, index, total):
        steps_seen.append(step)
        real_set_step(step, index, total)

    monkeypatch.setattr(api, "set_step", spy_set_step)

    with TestClient(api.app):
        # 进入 with 即跑完 lifespan 的启动段
        assert startup_status.snapshot()["ready"] is True

    # 步骤数随 B3 新增的「构建检索索引」由 6 变 7 —— 断言语义未变：
    # 仍然要求"上报的步骤数 == 实际执行的步骤数"，多一步少一步都会红。
    assert len(steps_seen) == 7
    assert "加载向量库" in steps_seen
    assert "校验知识库" in steps_seen
    # B3 的判别式：检索索引必须在启动期就建好。
    # 只断言"步骤列表里有这个名字"是不够的 —— 名字可以加进去而函数不接上；
    # 这里直接查 agents 模块的真实状态位。
    assert "构建检索索引" in steps_seen
    assert agents_module._bm25_initialized is True, (
        "lifespan 走完后 BM25 仍未初始化 —— 首次检索还得现场建索引，"
        "/health/ready 宣告的就绪是假的"
    )
    # 库非空时不该产生警告——否则"知识库为空"的提示会天天挂在界面上，
    # 用户很快就学会忽略它。
    assert startup_status.snapshot()["warnings"] == []


def test_lifespan_marks_failed_when_a_step_raises(monkeypatch):
    """某一步抛异常时必须记 failed 并把异常继续抛出。

    吞掉异常会让进程带着半初始化的状态"假装启动成功"——
    那是最坏的结果：探针说可用，实际每个请求都失败。
    """
    monkeypatch.setattr(api, "configure_logging", lambda: None)
    monkeypatch.setattr(api, "init_db", lambda: None)
    monkeypatch.setattr(api, "get_llm", lambda: None)
    monkeypatch.setattr(api, "get_embeddings", lambda: None)

    def _boom():
        raise RuntimeError("向量库目录不存在")

    monkeypatch.setattr(api, "get_chroma_db", _boom)

    with pytest.raises(RuntimeError, match="向量库目录不存在"):
        with TestClient(api.app):
            pass

    st = startup_status.snapshot()
    assert st["ready"] is False
    assert "向量库目录不存在" in st["error"]


# ---------- 第五组：启动阶段的知识库健康检查 ----------
#
# 守的是什么：首次克隆下来没跑 build_knowledge_base.py 时，此前只有**首次检索**
# 才会发现库是空的——而那时用户已经点下"开始诊断"，白等一轮 9 次 LLM 调用，
# 最后拿到一张降级工单，看不出根因是"库没建"。
#
# 这里同样要**对偶**：既测"空库要提示"，也测"查不到不能报成空库"。
# 只测前者的话，把 -1 和 0 合并实现（都报"知识库为空"）照样能过。

class _FakeCollection:
    def __init__(self, n: int = 73, boom: bool = False):
        self._n, self._boom = n, boom

    def count(self) -> int:
        if self._boom:
            raise RuntimeError("sqlite 数据库被锁住")
        return self._n


class _FakeDb:
    """假的 Chroma：`get()` 一律抛异常，用来钉住"必须走 count() 而不是 get()"。"""

    def __init__(self, n: int = 73, boom: bool = False):
        self._collection = _FakeCollection(n, boom)

    def get(self, *args, **kwargs):  # pragma: no cover - 只在实现走错时触发
        raise AssertionError(
            "get_knowledge_base_size() 不该调用 get()：它会把全部文档 materialize 出来"
        )


def test_kb_size_uses_count_not_get(monkeypatch):
    """必须走 `_collection.count()`，不能 `len(get()["ids"])`。

    判别式：假 db 的 `get()` 直接抛异常，只要实现走了 get() 就会失败。
    只断言"返回值是 73"是不够的——两种写法都能返回 73。
    """
    import agents

    monkeypatch.setattr(agents, "get_db", lambda: _FakeDb(73))
    assert agents.get_knowledge_base_size() == 73


def test_kb_size_returns_minus_one_when_count_fails(monkeypatch):
    """取不到时返回 -1，不是 0。"""
    import agents

    monkeypatch.setattr(agents, "get_db", lambda: _FakeDb(boom=True))
    assert agents.get_knowledge_base_size() == -1


def test_kb_size_returns_minus_one_when_db_unavailable(monkeypatch):
    """连向量库都拿不到时同样是 -1，而不是抛出去把启动搞挂。"""
    import agents

    def _boom():
        raise RuntimeError("向量库目录不存在")

    monkeypatch.setattr(agents, "get_db", _boom)
    assert agents.get_knowledge_base_size() == -1


def test_add_warning_accumulates_and_does_not_clear_previous():
    """警告是累积的。

    启动步骤会依次上报进度，若 `add_warning` 顺手清空，先记的警告会被后一步抹掉——
    表现为"偶发看不到提示"，很难复现。
    """
    startup_status.add_warning("第一条")
    startup_status.add_warning("第二条")
    assert startup_status.snapshot()["warnings"] == ["第一条", "第二条"]


def test_reset_clears_warnings():
    """复位必须清警告，否则前一个用例的警告会泄漏到后一个用例。"""
    startup_status.add_warning("脏数据")
    startup_status.reset_for_tests()
    assert startup_status.snapshot()["warnings"] == []


def _run_lifespan_with_kb(monkeypatch, size: int):
    """跑一遍真实 lifespan，把知识库块数桩成 size。"""
    monkeypatch.setattr(api, "configure_logging", lambda: None)
    monkeypatch.setattr(api, "init_db", lambda: None)
    monkeypatch.setattr(api, "get_llm", lambda: None)
    monkeypatch.setattr(api, "get_embeddings", lambda: None)
    monkeypatch.setattr(api, "get_chroma_db", lambda: None)
    monkeypatch.setattr(api, "get_knowledge_base_size", lambda: size)
    with TestClient(api.app):
        pass


def test_empty_knowledge_base_warns_but_still_reports_ready(monkeypatch):
    """库为空时：**要提示**，但**不能判成启动失败**。

    判成失败是过度反应——历史页、统计页都还能用，`/diagnose` 也会诚实地降级产出工单。
    真正要修的是"时机"：让用户在动手之前就看到，而不是事后从降级工单反推根因。
    """
    _run_lifespan_with_kb(monkeypatch, 0)
    st = startup_status.snapshot()
    assert st["ready"] is True
    assert st["error"] is None
    assert len(st["warnings"]) == 1
    assert "知识库为空" in st["warnings"][0]
    assert "build_knowledge_base.py" in st["warnings"][0]


def test_empty_knowledge_base_warning_reaches_the_ready_endpoint(monkeypatch):
    """警告必须真的走到 `/health/ready` 的响应体里。

    只断言 `startup_status` 里有警告是不够的——端点忘了带上它，
    前端就永远看不到，而所有单测照样绿（"函数改对了 ≠ 调用点接对了"）。
    """
    _run_lifespan_with_kb(monkeypatch, 0)
    body = _client().get("/health/ready").json()
    assert body["ready"] is True
    assert len(body["warnings"]) == 1
    assert "知识库为空" in body["warnings"][0]


def test_non_empty_knowledge_base_produces_no_warning(monkeypatch):
    """库正常时**不得**有警告——否则提示天天挂着，用户很快学会忽略它。"""
    _run_lifespan_with_kb(monkeypatch, 73)
    assert startup_status.snapshot()["warnings"] == []
    assert _client().get("/health/ready").json()["warnings"] == []


def test_count_failure_is_not_reported_as_empty_knowledge_base(monkeypatch):
    """对偶测试：查不到（-1）**不得**报成"知识库为空"。

    两者合并会把"路径/权限/依赖出问题"误报成"库没建"，把人引向错误的排查方向。
    这与项目的诚实性立场是同一条：说清是"没有"还是"不知道"，别用前者冒充后者。
    """
    _run_lifespan_with_kb(monkeypatch, -1)
    warnings = startup_status.snapshot()["warnings"]
    assert len(warnings) == 1
    assert "读取失败" in warnings[0]
    assert "知识库为空" not in warnings[0]
    assert "build_knowledge_base.py" not in warnings[0]


def test_warnings_field_present_even_when_not_ready():
    """未就绪时的 503 响应体也要带 `warnings` 字段（空列表）。

    少了这个键，前端解析 `body.warnings` 会拿到 undefined；
    在"启动中"这段最容易出问题的窗口里，恰恰不该再引入一种新的未定义状态。
    """
    body = _client().get("/health/ready").json()
    assert body["warnings"] == []
