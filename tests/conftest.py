"""测试基础设施：真离线 + 模块级状态隔离。

## 为什么这里的东西不是"可选的整洁度"

### 1. 假 key 必须**强制赋值**，不能靠顺序巧合

原来写的是 `os.environ.setdefault("SILICONFLOW_API_KEY", "test-key-for-unit-tests")`。
它之所以"生效"，只是因为「conftest 先设 + `load_dotenv()` 默认不覆盖」这个顺序巧合——
而 `.env` 里放的是**真实 key**。任何绕过 conftest 的入口（单独 import agents、
别的测试框架、直接跑脚本）都会打到线上，并且**没有任何提示**。

改成强制赋值，并在导入后断言哨兵真的在位：`load_dotenv(override=True)` 或
`.env` 被提前加载时，这里必须立刻炸，而不是让测试悄悄拿真实 key 去联网。

### 2. 模块级可变状态必须逐用例快照/还原

`agents.py` / `api.py` 里有大量懒加载单例与进程内缓存：LLM 客户端、Chroma 句柄、
BM25 索引、排除映射缓存、检索缓存、健康检查缓存。它们**跨用例存活**，于是：

  - 前一个用例缓存下来的证据会被后一个用例读到 → 测试"碰巧通过"；
  - 前一个用例桩出来的假 DB 会被后一个用例当成真 DB 用；
  - 只在全量运行时才复现的偶发失败，几乎都出自这里。

同一组状态此前在两处被手工复位（`test_guards_and_pagination.py` 只复位
`_bm25_initialized`，`test_resilience_and_retrieval.py` 复位三个字段），
口径不一致本身就是 bug 的温床。集中到 autouse 夹具里，新增缓存不必再逐个补。

### 3. 向量库必须指到 tmp_path

实测：跑一次 pytest 前后 `chroma_db/chroma.sqlite3` 的 mtime 会变 ——
"离线单测"名不副实，测试真的打开了真实向量库。把 `CHROMA_PERSIST_DIR`
指向 tmp_path 之后，即使某个用例漏了桩，也碰不到真实数据。
"""

import os
import sys
from pathlib import Path

import pytest

# 哨兵 key：测试进程里只允许存在这一个值（见模块头第 1 节）
SENTINEL_API_KEY = "test-key-for-unit-tests"
os.environ["SILICONFLOW_API_KEY"] = SENTINEL_API_KEY

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在 sys.path 就绪之后再导入
import agents  # noqa: E402
import api  # noqa: E402
import database  # noqa: E402

assert os.environ["SILICONFLOW_API_KEY"] == SENTINEL_API_KEY, (
    "测试进程里的 SILICONFLOW_API_KEY 不是哨兵值 —— .env 的真实 key 已经漏进来了"
)

# 需要逐用例隔离的 agents 模块级单例
_AGENTS_SINGLETONS = (
    "_llm",
    "_vision_llm",
    "_embeddings",
    "_db",
    "_bm25_index",
    "_bm25_corpus",
    "_doc_texts_cache",
    "_bm25_initialized",
)


@pytest.fixture(autouse=True)
def isolate_module_state(tmp_path, monkeypatch):
    """逐用例快照 / 还原所有模块级可变状态，并把向量库指到 tmp_path。"""
    for name in _AGENTS_SINGLETONS:
        monkeypatch.setattr(agents, name, getattr(agents, name))

    # 缓存是"就地改写"的 dict，必须换成一个副本，否则用例内的写会直接改到快照上。
    # 用 `type(...)` 取原类型而不是写死 dict：检索缓存是 OrderedDict（LRU 要 move_to_end），
    # 换成普通 dict 会在运行期抛 AttributeError —— 这正是第一版夹具踩到的坑。
    monkeypatch.setattr(agents, "_exclusion_cache", type(agents._exclusion_cache)(agents._exclusion_cache))
    monkeypatch.setattr(agents, "_retrieval_cache", type(agents._retrieval_cache)(agents._retrieval_cache))
    monkeypatch.setattr(api, "_health_cache", dict(api._health_cache))

    # 向量库目录：宁可让用例落到空的 tmp 库上失败，也不要它悄悄读真实数据
    monkeypatch.setattr(agents.settings, "CHROMA_PERSIST_DIR", str(tmp_path / "chroma_db"))

    yield


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把数据库指到临时文件，避免污染真实的 diagnosis_history.db。

    连接是 thread-local 缓存的，所以切换路径前必须先关掉当前线程的连接。
    """
    database.close_db_connections()
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(database, "_db_initialized", False)
    yield database
    database.close_db_connections()
