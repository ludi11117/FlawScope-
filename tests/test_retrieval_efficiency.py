"""检索与排除映射的省钱回归测试（B4 / B5）。

## B4：检索结果与查询嵌入无缓存

`retrieve_evidence` 每次调用都会打一次 embedding API。一次诊断里它最多被调 4 次
（初检、每轮辩论、降级时的 `find_cross_device_hints`），而辩论重检索的 query
常与初检高度重叠。缓存键 = (规范化 query, k, 知识库块数)。

判别式刻意用「`similarity_search` 被调用的次数」而不是「返回内容相同」——
后者在"没有缓存、但每次都返回一样"的实现下同样成立，测不出退化。

## B5：排除条件映射每次都花一次 LLM

`map_excluded_causes` 现在是"先确定性、只有漏网项才叫 LLM"。
这里既测"全命中时不调 LLM"，也测"有漏网项时仍要调、且并集语义不变"。

全部离线：`get_db` / `invoke_and_parse_json` 全部打桩。
"""

import agents


class _Doc:
    def __init__(self, text):
        self.page_content = text


class _CountingChroma:
    """记录检索次数的假向量库。

    只实现检索路径真正会用到的方法；`get()` 返回的文档数决定缓存键里的
    "知识库块数"，因此可以被用来模拟"库扩容了"。
    """

    def __init__(self, docs=("主轴电机 轴承损坏 异响",)):
        self._docs = list(docs)
        self.search_calls = 0
        self.vector_calls = 0

    def get(self):
        return {"documents": list(self._docs)}

    def similarity_search(self, query, k):
        self.search_calls += 1
        return [_Doc("【命中】主轴电机 轴承损坏 异响")][:k]

    def similarity_search_by_vector(self, vector, k):
        self.vector_calls += 1
        return [_Doc("【命中】主轴电机 轴承损坏 异响")][:k]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(agents, "get_db", lambda: fake)
    # 强制重建 BM25，让它去读假库的文档数（= 缓存键里的块数）
    monkeypatch.setattr(agents, "_bm25_initialized", False)
    agents._retrieval_cache.clear()


# ========== B4：检索缓存 ==========

def test_same_query_hits_cache(monkeypatch):
    """同一 query 调两次，向量检索只应发生一次。"""
    fake = _CountingChroma()
    _wire(monkeypatch, fake)

    first = agents.retrieve_evidence("主轴异响", k=3)
    second = agents.retrieve_evidence("主轴异响", k=3)

    assert first == second
    assert fake.search_calls == 1, f"同一 query 重复检索了 {fake.search_calls} 次（缓存没生效）"


def test_different_query_is_not_served_from_cache(monkeypatch):
    """对偶：不同 query 必须各自检索，不能拿别人的证据回答。

    只测"相同 query 命中缓存"会让实现退化成"永远返回第一次的结果"——
    那是最坏的一种"省"：拿别的故障的资料回答本次故障。
    """
    fake = _CountingChroma()
    _wire(monkeypatch, fake)

    agents.retrieve_evidence("主轴异响", k=3)
    agents.retrieve_evidence("液压压力不足", k=3)

    assert fake.search_calls == 2


def test_cache_key_includes_k(monkeypatch):
    """k 不同 = 要的名额不同，不能复用。"""
    fake = _CountingChroma()
    _wire(monkeypatch, fake)

    agents.retrieve_evidence("主轴异响", k=3)
    agents.retrieve_evidence("主轴异响", k=5)

    assert fake.search_calls == 2


def test_cache_key_includes_knowledge_base_size(monkeypatch):
    """知识库块数变化后缓存必须自然失效。

    这正是"扩库后还拿旧证据回答"的守门人：块数是缓存键的一部分，
    库变了键就变了，不需要任何人记得手动清缓存。
    """
    fake = _CountingChroma(docs=["一条"])
    _wire(monkeypatch, fake)

    agents.retrieve_evidence("主轴异响", k=3)
    assert fake.search_calls == 1

    # 模拟知识库扩容
    fake._docs = ["一条", "两条"]
    monkeypatch.setattr(agents, "_bm25_initialized", False)
    agents.retrieve_evidence("主轴异响", k=3)

    assert fake.search_calls == 2, "知识库块数变了却仍命中旧缓存"


def test_rebuild_bm25_index_clears_retrieval_cache(monkeypatch):
    """重建索引后必须清缓存 —— 覆盖"块数恰好没变"的重建场景。"""
    fake = _CountingChroma(docs=["一条", "两条"])
    _wire(monkeypatch, fake)

    agents.retrieve_evidence("主轴异响", k=3)
    assert fake.search_calls == 1

    agents.rebuild_bm25_index()
    agents.retrieve_evidence("主轴异响", k=3)

    assert fake.search_calls == 2, "重建索引后仍读到旧库的证据"


def test_normalized_query_shares_cache_entry(monkeypatch):
    """只差空白的 query 视为同一条（规范化只压空白，不做语义改写）。"""
    fake = _CountingChroma()
    _wire(monkeypatch, fake)

    agents.retrieve_evidence("主轴  异响", k=3)
    agents.retrieve_evidence("主轴 异响", k=3)

    assert fake.search_calls == 1


# ========== B5：排除映射先确定性、后 LLM ==========

_ENTRIES = [
    ("变频器报警代码F-014", "电机绕组对地绝缘破损。"),
    ("变频器报警代码F-014", "动力电缆在桥架转弯处磨损露出导体。"),
    ("变频器报警代码F-014", "变频器内部电流互感器故障造成误报。"),
]


def test_all_items_deterministically_matched_skips_llm(monkeypatch):
    """每条排除项都能确定性命中时，LLM 调用次数必须是 0。"""
    def _boom(*a, **kw):
        raise AssertionError("全部命中时不该调用 LLM 做排除映射")

    monkeypatch.setattr(agents, "invoke_and_parse_json", _boom)
    agents._exclusion_cache.clear()

    idx = agents.map_excluded_causes(
        ["电机绕组对地绝缘测量正常", "电缆护套完好"], _ENTRIES, scope=[0, 1, 2]
    )

    assert idx == [0, 1]


def test_unmatched_item_still_calls_llm_and_unions(monkeypatch):
    """有漏网项时仍要调 LLM，且结果与确定性命中取并集（并集语义不变）。"""
    captured = {}

    def _fake(messages, llm=None, correlation_id=None):
        captured["messages"] = messages
        # invoke_and_parse_json 的契约是"返回已解析的 dict"，桩必须遵守，
        # 否则会走进兜底解析分支、LLM 结果被当成无效（第一版返回了 JSON 字符串，踩过）
        return {"排除编号": [3]}   # 条目 [2]

    monkeypatch.setattr(agents, "invoke_and_parse_json", _fake)
    agents._exclusion_cache.clear()

    idx = agents.map_excluded_causes(
        ["电机绕组对地绝缘测量正常", "润滑油已更换"], _ENTRIES, scope=[0, 1, 2]
    )

    # 确定性通道给出 [0]（电机绕组），LLM 给出 [2]（电流互感器）
    assert idx == [0, 2], f"并集语义被破坏：{idx}"


def test_llm_only_receives_unmatched_items(monkeypatch):
    """只把**漏网的**排除项交给 LLM，命中项不进提示词（提示词更短 = 更便宜）。"""
    captured = {}

    def _fake(messages, llm=None, correlation_id=None):
        captured["messages"] = messages
        return {"排除编号": []}

    monkeypatch.setattr(agents, "invoke_and_parse_json", _fake)
    agents._exclusion_cache.clear()

    agents.map_excluded_causes(
        ["电机绕组对地绝缘测量正常", "润滑油已更换"], _ENTRIES, scope=[0, 1, 2]
    )

    text = "".join(m.content for m in captured["messages"])
    assert "润滑油已更换" in text, "漏网项没有交给 LLM，它会永远漏掉"
    assert "电机绕组对地绝缘测量正常" not in text, "已命中的排除项不该再占提示词"


def test_union_still_covers_every_exclusion_item(monkeypatch):
    """回归 TC018：LLM 只答一部分时，确定性通道必须补上剩下的。

    B5 改了调用顺序，但这条保障不能丢——排除条件是用户的硬约束，
    漏掉一条等于把用户明确排除的原因又写回根因里。
    """
    monkeypatch.setattr(
        agents, "invoke_and_parse_json",
        lambda *a, **kw: {"排除编号": [1]},     # LLM 只答了条目 [0]
    )
    agents._exclusion_cache.clear()

    idx = agents.map_excluded_causes(
        ["电机绕组对地绝缘测量正常", "电缆护套完好"], _ENTRIES, scope=[0, 1, 2]
    )
    assert idx == [0, 1], f"LLM 漏掉的排除项未被补救：{idx}"
