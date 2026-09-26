"""知识库 metadata 与检索过滤开关（C2 —— 只做了"无行为变化"的半步）。

## 做了什么

1. **入库时写 metadata**：`build_knowledge_base.parse_chunk_metadata()` 给每个块
   补上 `{section, chapter, alarm, device}`，由 `db.add_texts(chunks, metadatas=...)`
   写入。**切分本身一个字都没动**（按位置回溯，块边界与以前逐字节相同），
   所以对现有检索行为零影响。
2. **检索过滤的开关**：`settings.RETRIEVAL_DEVICE_FILTER` + `agents.device_filter()`。
   **默认关闭**，`retrieve_evidence(..., device=...)` 只有在开关打开时才会把
   `filter={"device": ...}` 传给 Chroma。

## 没做什么（待决策）

按交办书 §5.2 的要求，"是否默认开启过滤"牵动误降级指标，先不动。
另外：**现有 `chroma_db/` 是没写 metadata 的旧库**，而重建向量库要真调 Embedding
（花钱），所以这套 metadata 要等下一次真实重建才会生效。在那之前开启过滤 =
召回为空 = 全线误降级。测试里用假 Chroma 只验证"开关接线正确"，不碰真实库。

全部离线：不联网、不碰真实 `chroma_db/`。
"""

import pytest

import agents
from config import settings


# ========== metadata 解析 ==========

@pytest.fixture
def kb_module():
    """懒导入：该模块在 import 时会 `configure_logging()`，放进夹具避免影响其他测试。"""
    import build_knowledge_base

    return build_knowledge_base


@pytest.fixture
def real_chunks(kb_module):
    text = kb_module.KB_PATH.read_text(encoding="utf-8")
    body = kb_module.strip_preamble(text)
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    chunks = RecursiveCharacterTextSplitter(
        chunk_size=kb_module.CHUNK_SIZE, chunk_overlap=kb_module.CHUNK_OVERLAP
    ).split_text(body)
    return chunks, body, kb_module.parse_chunk_metadata(chunks, body)


def test_every_chunk_gets_a_section(real_chunks):
    """所有块都必须能溯源到设备章节——包括没有条目标题的续块。"""
    chunks, _, metadatas = real_chunks
    assert len(metadatas) == len(chunks)
    missing = [i for i, m in enumerate(metadatas) if not m["section"]]
    assert not missing, f"第 {missing[:5]} 块没有 section（切分与回溯对不上）"


def test_first_chunk_maps_to_the_first_section(real_chunks):
    """第一块就是【数控机床主轴电机常见故障与排查】那一行。"""
    _, _, metadatas = real_chunks
    assert metadatas[0]["section"] == "数控机床主轴电机常见故障与排查"
    assert metadatas[0]["device"] == "数控机床主轴电机"
    # 章节头本身不属于任何条目
    assert metadatas[0]["chapter"] == ""
    assert metadatas[0]["alarm"] == ""


def test_chunk_carrying_a_chapter_title_gets_alarm_code(real_chunks):
    """含「一、主轴电机报警代码E-203」的块要解析出报警代码与条目标题。"""
    chunks, _, metadatas = real_chunks
    idx = next(i for i, c in enumerate(chunks) if "报警代码E-203" in c)
    meta = metadatas[idx]
    assert meta["alarm"] == "E-203"
    assert meta["chapter"] == "主轴电机报警代码E-203"
    assert meta["device"] == "数控机床主轴电机"


def test_continuation_chunk_inherits_the_preceding_chapter(real_chunks):
    """接在条目中间的续块（没有标题）要继承它前面最近的条目标题。

    这正是"按位置回溯"要解决的问题：一个条目会被切成 2~3 块，
    只有第一块含标题，其余块必须靠回溯拿到来源。
    """
    chunks, _, metadatas = real_chunks
    idx = next(i for i, c in enumerate(chunks) if "报警代码E-203" in c)
    # 紧跟着的那一块通常就是同一条目的续块
    nxt = metadatas[idx + 1]
    assert nxt["chapter"] == "主轴电机报警代码E-203"
    assert nxt["alarm"] == "E-203"


def test_metadata_switches_section_across_device_boundaries(real_chunks):
    """跨设备章节后，metadata 必须跟着切换（不能一直沿用第一个章节）。"""
    _, _, metadatas = real_chunks
    devices = {m["device"] for m in metadatas}
    assert len(devices) >= 8, f"只解析出 {len(devices)} 个设备：{sorted(devices)}"
    assert "空气压缩机" in devices
    assert "离心泵" in devices


def test_chapter_without_alarm_code_gets_empty_string(real_chunks):
    """「五、主轴运转异响（无报警代码）」这类条目的 alarm 必须是空串而不是 None。"""
    chunks, _, metadatas = real_chunks
    idx = next(i for i, c in enumerate(chunks) if "主轴运转异响" in c)
    assert metadatas[idx]["alarm"] == ""
    assert metadatas[idx]["chapter"] == "主轴运转异响（无报警代码）"


def test_chunk_boundaries_are_unchanged(real_chunks):
    """**核心约束**：加 metadata 不能改变切分结果。

    判别式取"块数与逐块文本都与纯切分一致"——只要有人在解析 metadata 时
    顺手改了切分（例如按章节分别切），这条会立刻红。
    """
    chunks, body, metadatas = real_chunks
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    import build_knowledge_base as kb

    plain = RecursiveCharacterTextSplitter(
        chunk_size=kb.CHUNK_SIZE, chunk_overlap=kb.CHUNK_OVERLAP
    ).split_text(body)
    assert chunks == plain
    assert len(metadatas) == len(plain)


# ========== --dry-run 不写库 ==========

def test_dry_run_never_constructs_a_chroma_client(monkeypatch, kb_module):
    """`--dry-run` 不写库的判别式：连 Chroma 客户端都不该构造。

    只断言"返回 0"是不够的——它完全可能先把库连上再决定不写。
    """
    def _boom(*a, **kw):
        raise AssertionError("--dry-run 不该构造 Chroma 客户端")

    monkeypatch.setattr(kb_module, "Chroma", _boom)
    assert kb_module.build(dry_run=True) == 0


# ========== 检索过滤开关 ==========

def test_device_filter_is_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_DEVICE_FILTER", False)
    assert agents.device_filter("数控机床") is None


def test_device_filter_returns_condition_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_DEVICE_FILTER", True)
    assert agents.device_filter("数控机床") == {"device": "数控机床"}


def test_device_filter_ignores_empty_device(monkeypatch):
    """开关开着但设备名为空时也不能过滤——空条件会把所有证据滤掉。"""
    monkeypatch.setattr(settings, "RETRIEVAL_DEVICE_FILTER", True)
    assert agents.device_filter("") is None
    assert agents.device_filter(None) is None


class _RecordingChroma:
    """记录每次 similarity_search 收到的 kwargs。"""

    def __init__(self, docs=("主轴电机 轴承损坏",)):
        self._docs = list(docs)
        self.calls = []

    def get(self):
        return {"documents": list(self._docs)}

    def similarity_search(self, query, k, **kwargs):
        self.calls.append(kwargs)

        class _D:
            page_content = "【命中】主轴电机 轴承损坏"

        return [_D()][:k]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(agents, "get_db", lambda: fake)
    monkeypatch.setattr(agents, "_bm25_initialized", False)
    agents._retrieval_cache.clear()


def test_retrieval_does_not_filter_by_default(monkeypatch):
    """默认路径必须**不带** filter —— 旧库没有 metadata，带上就是召回为空。"""
    monkeypatch.setattr(settings, "RETRIEVAL_DEVICE_FILTER", False)
    fake = _RecordingChroma()
    _wire(monkeypatch, fake)

    agents.retrieve_evidence("主轴异响", k=3, device="数控机床")

    assert fake.calls == [{}], f"默认路径传了过滤条件：{fake.calls}"


def test_retrieval_filters_when_switch_is_on(monkeypatch):
    """对偶：开关打开时 filter 必须真的传下去（否则这个开关是假的）。"""
    monkeypatch.setattr(settings, "RETRIEVAL_DEVICE_FILTER", True)
    fake = _RecordingChroma()
    _wire(monkeypatch, fake)

    agents.retrieve_evidence("主轴异响", k=3, device="数控机床")

    assert fake.calls == [{"filter": {"device": "数控机床"}}]


def test_filter_is_part_of_the_cache_key(monkeypatch):
    """带过滤与不带过滤是两次不同的检索，不能共用缓存条目。"""
    fake = _RecordingChroma()
    _wire(monkeypatch, fake)

    monkeypatch.setattr(settings, "RETRIEVAL_DEVICE_FILTER", False)
    agents.retrieve_evidence("主轴异响", k=3, device="数控机床")
    monkeypatch.setattr(settings, "RETRIEVAL_DEVICE_FILTER", True)
    agents.retrieve_evidence("主轴异响", k=3, device="数控机床")

    assert len(fake.calls) == 2, "带过滤的检索命中了不带过滤的缓存"
