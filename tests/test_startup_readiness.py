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


def test_set_step_records_progress_but_does_not_claim_ready():
    """推进步骤只更新进度，绝不能顺带把 ready 置真。

    这是"进度条走到 5/5 就等于可用"这类偷懒实现的判别式：
    进度走完 ≠ 初始化成功，就绪必须由 mark_ready 显式宣告。
    """
    startup_status.set_step("加载向量库", 5, 5)
    st = startup_status.snapshot()
    assert st["step"] == "加载向量库"
    assert st["step_index"] == 5
    assert st["step_total"] == 5
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
    """快照必须是拷贝。

    返回内部 dict 的引用会让调用方（或测试）无意间改写全局状态，
    而且这种 bug 只在并发下才暴露。这里断言改快照不影响真实状态。
    """
    snap = startup_status.snapshot()
    snap["ready"] = True
    assert startup_status.snapshot()["ready"] is False


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
    startup_status.set_step("加载向量库", 4, 5)
    body = _client().get("/health/ready").json()
    assert body["step"] == "加载向量库"
    assert body["step_index"] == 4
    assert body["step_total"] == 5


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

    # 把五个初始化步骤全换成桩：单测不能真去加载向量库
    monkeypatch.setattr(api, "configure_logging", lambda: None)
    monkeypatch.setattr(api, "init_db", lambda: None)
    monkeypatch.setattr(api, "get_llm", lambda: None)
    monkeypatch.setattr(api, "get_embeddings", lambda: None)

    real_set_step = startup_status.set_step

    def spy_set_step(step, index, total):
        steps_seen.append(step)
        real_set_step(step, index, total)

    monkeypatch.setattr(api, "set_step", spy_set_step)

    with TestClient(api.app):
        # 进入 with 即跑完 lifespan 的启动段
        assert startup_status.snapshot()["ready"] is True

    assert len(steps_seen) == 5
    assert "加载向量库" in steps_seen


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
