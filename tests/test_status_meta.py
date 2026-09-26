"""状态值单一来源（D7）——"加状态漏改"的守门人。

## 守的是什么

状态值散在 6 处时，加一个状态的典型故障是"漏改一处"：界面上显示成未知、
或者该落库的没落库。这类 bug **不报错**，只是悄悄误导，而且只有那条分支被走到
才暴露（可能就是几个月后）。

所以这里做两件事：
  1. 把 `orchestrator.py` 里出现的状态字面量**grep 出来**，断言它们都被
     `status_meta.STATUS_META` 覆盖；
  2. 断言各消费点（api 的落库、orchestrator 的终态集合）确实从表里派生，
     而不是各自又写了一份。

判别式取"grep 字面量"而不是"手工列一份清单"：后者在有人加了新状态时会**一起漏**，
测试照样绿。
"""

import re
from pathlib import Path

import pytest

import orchestrator
import api
import status_meta
from status_meta import STATUS_META, TERMINAL_FAILURE_STATUSES, should_persist, status_meta as lookup

ROOT = Path(__file__).resolve().parent.parent
ORCH_SRC = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "api.py").read_text(encoding="utf-8")

# `"status": "xxx"` 形式的赋值
#
# ⚠️ 字符类必须含数字：第一版写成 `[a-z_]+`，于是 `"status": "reviewed_v2"`
# 这条**根本匹配不上**（`v` 后面是 `2`，不满足结尾的引号），守门人静默失效。
# 退化验证时发现的——把 `reviewed` 改成 `reviewed_v2`，本该变红的两条测试里
# 只有一条红了。
_STATUS_CHARS = r"[a-z0-9_]+"
_ASSIGN = re.compile(r'"status"\s*:\s*"(' + _STATUS_CHARS + r')"')
# `status == "xxx"` / `status != "xxx"` / `state.get("status") == "xxx"`
_COMPARE = re.compile(r'status"\)?\s*(?:==|!=)\s*"(' + _STATUS_CHARS + r')"')
# `else "xxx"`（例如 `status if status in TERMINAL_FAILURE_STATUSES else "done"`）
_ELSE_LITERAL = re.compile(r'else\s+"(' + _STATUS_CHARS + r')"')


def _orchestrator_status_literals() -> set:
    found = set(_ASSIGN.findall(ORCH_SRC))
    found |= set(_COMPARE.findall(ORCH_SRC))
    # `else "done"` 只取看起来像状态的（避免把普通英文词也吸进来）
    found |= {s for s in _ELSE_LITERAL.findall(ORCH_SRC) if s in STATUS_META}
    return found


def test_every_orchestrator_status_is_registered():
    """核心断言：orchestrator 里出现的每个状态字面量都必须在 STATUS_META 里。

    加了一个新状态却忘了登记 → 这里红。这正是"加状态要改 6 处"变成"改 1 处"的保证。
    """
    found = _orchestrator_status_literals()
    assert found, "没从 orchestrator.py 里 grep 到任何状态字面量，正则可能已经失效"

    missing = sorted(found - set(STATUS_META))
    assert not missing, f"这些状态没有登记到 STATUS_META：{missing}"


def test_status_meta_has_no_orphan_entries():
    """对偶：表里不该有 orchestrator 从不产生的状态。

    只测单向覆盖会让"把见过的词全塞进表里"也算通过。
    唯一允许的例外是 `unknown`：它只出现在 api.py 的兜底（读不到 status 时）。
    """
    orphans = set(STATUS_META) - _orchestrator_status_literals() - {"unknown"}
    assert not orphans, f"STATUS_META 里有 orchestrator 不会产生的状态：{sorted(orphans)}"


def test_unknown_is_reachable_from_api():
    """`unknown` 的例外要成立：api.py 里确实会用它兜底。"""
    assert '"unknown"' in API_SRC or "'unknown'" in API_SRC


# ========== 各消费点必须从表里派生 ==========

def test_terminal_failure_statuses_derives_from_the_table():
    """终态集合不能再是手写的字符串元组。"""
    assert set(TERMINAL_FAILURE_STATUSES) == {
        name for name, meta in STATUS_META.items() if meta.failure
    }
    assert orchestrator.TERMINAL_FAILURE_STATUSES is TERMINAL_FAILURE_STATUSES, (
        "orchestrator 没有引用 status_meta 的那一份，而是自己又写了一遍"
    )
    # 三个失败终态的具体取值不能变（跨层契约）
    assert set(TERMINAL_FAILURE_STATUSES) == {
        "insufficient_knowledge", "llm_failed", "pending_human_review",
    }


def test_api_persistence_decision_comes_from_the_table():
    """api.py 的两处落库判断都改成了 should_persist()，不再各写一遍 != "need_more_info"。"""
    assert API_SRC.count("should_persist(") >= 2, "api.py 的落库判断没有走统一口径"
    assert '"need_more_info"' not in API_SRC, "api.py 里还留着硬编码的状态比较"


def test_should_persist_matches_old_behaviour():
    """落库口径必须与旧实现逐值等价（否则历史列表会多出/少掉记录）。"""
    for name in STATUS_META:
        assert should_persist(name) == (name != "need_more_info"), name
    # 未知状态宁可多存一条，也不要丢掉用户等了数十秒的结果
    assert should_persist("some_future_status") is True
    assert should_persist(None) is True


def test_followup_is_the_only_non_persisted_state():
    followups = {n for n, m in STATUS_META.items() if m.followup}
    assert followups == {"need_more_info"}


def test_terminal_and_failure_are_consistent():
    """失败终态必须是终态；终态与过程态不能重叠。"""
    for name, meta in STATUS_META.items():
        if meta.failure:
            assert meta.terminal, f"{name} 是失败状态却不是终态"
    process = {"start", "extracted", "info_sufficient", "retrieved", "diagnosed",
               "reviewed", "rebutted", "final_reviewed", "costed"}
    for name in process:
        assert not STATUS_META[name].terminal, f"{name} 是过程态，不该标成终态"


def test_banner_levels_are_known():
    """横幅级别必须是 app.py 能映射到 st.* 的那四种。"""
    allowed = {"error", "warning", "success", "info"}
    for name, meta in STATUS_META.items():
        assert meta.level in allowed, f"{name} 的 level={meta.level} 不在 {allowed}"


def test_every_terminal_status_has_a_banner():
    """终态都必须有横幅文案——用户看到的最终结论不能被静默吞掉。"""
    for name, meta in STATUS_META.items():
        if meta.terminal:
            assert meta.banner, f"{name} 是终态却没有横幅文案"


@pytest.mark.parametrize("status,expected_label", [
    ("done", "诊断完成"),
    ("insufficient_knowledge", "知识库无依据"),
    ("llm_failed", "模型服务失败"),
    ("pending_human_review", "转人工复核"),
    ("need_more_info", "需要补充信息"),
])
def test_known_labels_are_stable(status, expected_label):
    """标签是界面文案，也是跨层契约的一部分（前端另有自己的一份，两边都要稳）。"""
    assert lookup(status).label == expected_label


def test_unknown_status_falls_back_without_raising():
    meta = lookup("完全没见过的状态")
    assert meta is status_meta._FALLBACK
    assert meta.label == "未知状态"


def test_app_py_no_longer_hardcodes_status_text():
    """app.py 的横幅与颜色都取自表，不再各写一份。"""
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "status_meta(" in app_src
    for hardcoded in ("诊断流程完成，工单已生成", "知识库无相关依据，建议人工介入"):
        assert hardcoded not in app_src, f"app.py 里还硬编码着横幅文案：{hardcoded}"
    # 颜色字典已经删掉，不该再出现
    assert '"done": "🟢"' not in app_src


# ========== GET /meta/statuses（把口径暴露给前端） ==========

def test_meta_statuses_endpoint_returns_the_table():
    from fastapi.testclient import TestClient

    resp = TestClient(api.app).get("/meta/statuses")
    assert resp.status_code == 200

    body = resp.json()
    assert body["version"] == api.API_VERSION
    assert set(body["statuses"]) == set(STATUS_META)

    for name, meta in STATUS_META.items():
        got = body["statuses"][name]
        assert got["label"] == meta.label
        assert got["level"] == meta.level
        assert got["terminal"] is meta.terminal
        assert got["failure"] is meta.failure
        assert got["persisted"] is meta.persisted
        assert got["followup"] is meta.followup


def test_meta_statuses_does_not_leak_backend_only_fields():
    """`icon` / `banner` 是后端（Streamlit 对照前端）的展示细节，不该下发。

    前端拿到 `label` + `level` 就够渲染了；多下发只会让"谁负责文案"重新变模糊。
    """
    from fastapi.testclient import TestClient

    statuses = TestClient(api.app).get("/meta/statuses").json()["statuses"]
    for got in statuses.values():
        assert "icon" not in got
        assert "banner" not in got


def test_meta_statuses_needs_no_api_key(monkeypatch):
    """静态元数据，与 /health/live 同类，不该要求鉴权。

    要鉴权的话前端启动时就得先有 key —— 而它是"连界面文案都还没拿到"的时刻。
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api.settings, "API_KEY", "secret")
    assert TestClient(api.app).get("/meta/statuses").status_code == 200


def test_root_advertises_the_meta_endpoint():
    from fastapi.testclient import TestClient

    assert TestClient(api.app).get("/").json()["meta_statuses"] == "/meta/statuses"


# ========== 前端状态集合的漂移守卫 ==========
#
# 前端有两处状态表（历史/统计用的短标签、结果横幅用的长文案），都是手写的。
# 后端加一个状态而前端不认 → 界面显示成"未知"，不报错、只是悄悄误导。
# 这里直接读那两个 .tsx/.ts 源文件，把 key 抓出来做集合比对。

_TS_KEY = re.compile(r"^  ([a-z_]+):\s*\{", re.MULTILINE)


def _frontend_status_keys(path: Path) -> set:
    """从 `const STATUS_META = { … }` 里抓出状态名（按缩进两空格的 key 行）。

    ⚠️ 两个坑（都踩过）：
      1. 必须带 `re.MULTILINE`，否则 `^` 只匹配字符串开头，一个 key 都抓不到；
      2. 起点要搜 `const STATUS_META` 而不是 `STATUS_META` —— 文件里可能先在
         注释中提到这个名字（ResultView.tsx 就提到了），从注释开始切会切错块。
    """
    src = path.read_text(encoding="utf-8")
    start = src.index("const STATUS_META")
    end = src.index("\n}", start)
    return set(_TS_KEY.findall(src[start:end]))


@pytest.mark.parametrize("rel", [
    "web/src/pages/historyUtils.ts",
    "web/src/components/ResultView.tsx",
])
def test_frontend_status_tables_cover_every_backend_status(rel):
    keys = _frontend_status_keys(ROOT / rel)
    assert keys, f"没从 {rel} 里解析出状态名，正则可能已经失效"

    missing = sorted(set(STATUS_META) - keys - {"start", "extracted", "info_sufficient",
                                                "retrieved", "diagnosed", "reviewed",
                                                "rebutted", "final_reviewed", "costed",
                                                "unknown"})
    assert not missing, f"{rel} 不认这些后端状态：{missing}"


def test_frontend_status_tables_have_no_unknown_status():
    """对偶：前端不该有后端不认识的状态（打错字也是这种形态）。"""
    for rel in ("web/src/pages/historyUtils.ts", "web/src/components/ResultView.tsx"):
        keys = _frontend_status_keys(ROOT / rel)
        extra = sorted(keys - set(STATUS_META))
        assert not extra, f"{rel} 里有后端不认识的状态：{extra}"
