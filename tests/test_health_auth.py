"""`/health?fresh=true` 的鉴权回归测试（A3）。

## 这组测试守的是什么

`/health` 会**真调 1 次 LLM + 2 次 Embedding**（见 `_collect_health`），
而 `docs/PROJECT_GUIDE.md` 一直写着「`?fresh=true` … 配了 `API_KEY` 时需带
`X-API-Key`」——承诺的保护在代码里根本不存在（`health()` 是全仓少数没挂
`Depends(require_api_key)` 的端点之一）。任何能访问到端口的人都能拿它烧配额。

所以这里要**成对**断言：
  - 该拦的：配了 API_KEY 时，`fresh=true` 无 key / 错 key 都必须 401；
  - 该放的：带对 key 要 200；未配 API_KEY 时（本地开发）保持放行；
    命中缓存（`fresh=false`）不鉴权——它不碰外部依赖，没有烧配额的风险；
  - 分工不变：`/health/live`、`/health/ready` 仍然无鉴权、可高频轮询。

只测"该拦的"会让实现退化成"什么都要求带 key"，
那样 Docker healthcheck 与前端状态灯会立刻 401，是过度反应。

全部离线：`_collect_health` 被桩掉，不会真的调 LLM / Embedding。
"""

import time

import pytest
from fastapi.testclient import TestClient

import api
import startup_status
from config import settings

SENTINEL_KEY = "unit-test-api-key"


@pytest.fixture(autouse=True)
def _isolate():
    """每个用例都复位启动状态与健康缓存，避免用例之间互相污染。"""
    startup_status.reset_for_tests()
    yield
    startup_status.reset_for_tests()


@pytest.fixture
def stub_probe(monkeypatch):
    """把真探换成假实现，并清空缓存。

    真探会调 LLM + Embedding，单测绝不能碰。清缓存是必须的：
    `fresh=false` 的分支只有在"缓存过期/为空"时才会走到真探。
    """
    monkeypatch.setattr(
        api, "_collect_health", lambda: {"status": "healthy", "components": {}}
    )
    monkeypatch.setattr(api, "_health_cache", {"ts": 0.0, "payload": None})


@pytest.fixture
def key_configured(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", SENTINEL_KEY)


@pytest.fixture
def key_absent(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", None)


def _client() -> TestClient:
    # 不用 with：那样会跑 lifespan，真去加载向量库/模型
    return TestClient(api.app)


# ---------- 该拦的 ----------

def test_fresh_without_key_is_rejected(stub_probe, key_configured):
    """核心断言：配了 API_KEY 时，`?fresh=true` 没带 key 必须 401。

    去掉修复（`health()` 里那两行）后这条会返回 200 —— 那就是"任何人都能烧配额"。
    """
    resp = _client().get("/health", params={"fresh": "true"})
    assert resp.status_code == 401
    assert "API Key" in resp.json()["detail"]


def test_fresh_with_wrong_key_is_rejected(stub_probe, key_configured):
    """错 key 同样 401：校验必须真的比对，而不是"只要带了头就放行"。"""
    resp = _client().get(
        "/health", params={"fresh": "true"}, headers={"X-API-Key": "wrong"}
    )
    assert resp.status_code == 401


def test_fresh_with_correct_key_succeeds(stub_probe, key_configured):
    """对偶：带对 key 要能真探，不能把合法的运维调用一起拦掉。"""
    resp = _client().get(
        "/health", params={"fresh": "true"}, headers={"X-API-Key": SENTINEL_KEY}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ---------- 该放的 ----------

def test_cached_health_does_not_require_key(stub_probe, key_configured, monkeypatch):
    """命中缓存时不鉴权。

    语义要写清：`fresh=false` 且缓存有效时，端点只读内存里的旧结果，
    不碰 LLM / Embedding，因此没有烧配额的风险。Docker healthcheck 走的就是这条路径，
    要求它配密钥只会让部署变脆。
    """
    monkeypatch.setattr(
        api,
        "_health_cache",
        {"ts": time.time(), "payload": {"status": "healthy", "components": {}}},
    )
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_fresh_allowed_when_api_key_not_configured(stub_probe, key_absent):
    """未配置 API_KEY（本地开发默认）时保持放行，与全仓的可选鉴权口径一致。"""
    resp = _client().get("/health", params={"fresh": "true"})
    assert resp.status_code == 200


def test_live_and_ready_stay_unauthenticated(stub_probe, key_configured):
    """两个探针不能被顺手加上鉴权。

    它们要能高频轮询（前端状态灯 1.5~5 秒一次），且不碰任何外部依赖。
    给它们加 key 等于把"服务还活着吗"这个问题也变成需要凭据的操作。
    """
    startup_status.mark_ready()
    client = _client()
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
