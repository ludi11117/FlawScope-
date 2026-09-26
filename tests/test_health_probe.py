"""`/health` 真探的开销与缓存击穿（B7）。

## 两个问题

1. **一次真探打两次 Embedding**：`db.similarity_search("test", k=1)` 内部会
   `embed_query("test")`，紧接着 Embedding 组件又 `embed_query("test")` 一次。
   两个向量逐字节相同，纯粹是白花一次 API 调用。现在只嵌一次，
   用 `similarity_search_by_vector` 把它喂给 Chroma。

2. **缓存击穿**：`_health_cache` 原本是无锁裸 dict，并发调用会同时读到
   `payload is None`，于是各自真探一次。探活是高频动作，几个人同时刷就等于配额翻倍。
   现在持锁做"判断 + 真探 + 写回"。

判别式都用**调用次数**：`embed_query` 被调几次、`_collect_health` 被调几次。
只断言"返回了 healthy"是测不出来的——两种实现都会返回 healthy。

全部离线：LLM / Embedding / Chroma 全部打桩，SQLite 走 `isolated_db`。
"""

import threading
import time

import agents
import api


class _FakeEmbeddings:
    def __init__(self):
        self.calls = 0

    def embed_query(self, text):
        self.calls += 1
        return [0.1, 0.2, 0.3]


class _FakeChroma:
    def __init__(self):
        self.by_text = 0
        self.by_vector = 0

    def similarity_search(self, query, k):
        self.by_text += 1
        return []

    def similarity_search_by_vector(self, vector, k):
        self.by_vector += 1
        return []


class _FakeLLM:
    def invoke(self, messages):
        class _Resp:
            content = "pong"

        return _Resp()


def _stub_dependencies(monkeypatch, emb, db):
    monkeypatch.setattr(api, "get_embeddings", lambda: emb)
    monkeypatch.setattr(api, "get_chroma_db", lambda: db)
    monkeypatch.setattr(api, "get_llm", lambda: _FakeLLM())
    # `_collect_health` 里是函数内 import，所以桩 agents 模块的属性即可
    monkeypatch.setattr(agents, "get_vision_llm", lambda: object())


def test_probe_embeds_the_query_only_once(monkeypatch, isolated_db):
    """真探只应嵌一次 "test"。

    去掉修复（改回 `similarity_search("test")` + 独立的 `embed_query("test")`）
    后这里会变成 2。
    """
    emb, db = _FakeEmbeddings(), _FakeChroma()
    _stub_dependencies(monkeypatch, emb, db)

    payload = api._collect_health()

    assert emb.calls == 1, f"一次真探嵌了 {emb.calls} 次，重复调用 Embedding API"
    assert db.by_vector == 1, "Chroma 探活没有复用已算出的向量"
    assert db.by_text == 0, "不应再走会重新嵌入的文本检索"
    assert payload["components"]["embedding"]["dimension"] == 3
    assert payload["components"]["chromadb"]["status"] == "healthy"


def test_probe_falls_back_to_text_search_when_embedding_unavailable(monkeypatch, isolated_db):
    """对偶：嵌入挂掉时 Chroma 探活仍要给出结论，而不是直接判不健康。

    降级路径允许它再嵌一次（反正整体已经是 degraded），但必须**还能探**——
    否则 embedding 一坏，chromadb 的健康状况就永远看不到。
    """
    db = _FakeChroma()

    def _boom():
        raise RuntimeError("embedding 服务不可达")

    monkeypatch.setattr(api, "get_embeddings", _boom)
    monkeypatch.setattr(api, "get_chroma_db", lambda: db)
    monkeypatch.setattr(api, "get_llm", lambda: _FakeLLM())
    monkeypatch.setattr(agents, "get_vision_llm", lambda: object())

    payload = api._collect_health()

    assert payload["components"]["embedding"]["status"] == "unhealthy"
    assert payload["components"]["chromadb"]["status"] == "healthy"
    assert db.by_text == 1


def test_concurrent_probes_trigger_only_one_real_probe(monkeypatch):
    """并发命中空缓存时，只能有一个线程真探。

    用 8 个线程 + 带 sleep 的计数桩把竞态窗口放大：无锁实现下
    几乎所有线程都会读到 `payload is None`，`calls` 会接近 8。
    """
    calls = {"n": 0}
    lock = threading.Lock()

    def _fake_collect():
        with lock:
            calls["n"] += 1
        # 放大竞态窗口：没有锁的话，其余线程会在这段时间里全部涌入真探
        time.sleep(0.05)
        return {"status": "healthy", "components": {}}

    monkeypatch.setattr(api, "_collect_health", _fake_collect)
    monkeypatch.setattr(api, "_health_cache", {"ts": 0.0, "payload": None})
    monkeypatch.setattr(api.settings, "HEALTH_CACHE_TTL", 60)

    results = []

    def _worker():
        results.append(api.health(fresh=False))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert calls["n"] == 1, f"并发探活真探了 {calls['n']} 次（缓存击穿）"
    assert all(r["status"] == "healthy" for r in results)


def test_fresh_always_reprobes(monkeypatch):
    """对偶：`fresh=true` 必须绕开缓存，否则"强制真探"这个开关是假的。"""
    calls = {"n": 0}

    def _fake_collect():
        calls["n"] += 1
        return {"status": "healthy", "components": {}}

    monkeypatch.setattr(api, "_collect_health", _fake_collect)
    monkeypatch.setattr(api, "_health_cache", {"ts": 0.0, "payload": None})
    monkeypatch.setattr(api.settings, "API_KEY", None)

    api.health(fresh=True)
    api.health(fresh=True)

    assert calls["n"] == 2
