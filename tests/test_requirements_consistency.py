"""依赖清单的一致性守卫（D6）。

## 守的是什么

`requirements.txt`（全量）与 `requirements-runtime.txt`（镜像用）是**手抄**的两份清单。
手抄的代价不是"多打几个字"，而是**漂移**：某次只在一个文件里升了版本，
于是"本地测过的"和"镜像里跑的"变成两个东西，而所有测试都发现不了
——镜像用的那份清单**从来没被测试验证过**。

这里不断言"两份文件逐字相同"（它们的用途不同，本来就不该相同），
而是断言真正的不变式：

  **镜像清单里的每个包，必须在全量清单里存在且版本完全一致。**

外加两条：
  - 全量清单里不得出现已确认零使用的包（requests / pytest-asyncio）；
  - `tools/parse_manual_pdf.py` 依赖的 pypdf 必须有明确落点（不能再靠传递依赖）。

全部离线：只读文件。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL = ROOT / "requirements.txt"
RUNTIME = ROOT / "requirements-runtime.txt"
BASE = ROOT / "requirements-base.txt"
DEV = ROOT / "requirements-dev.txt"

# `包名==版本`，允许行尾注释与 `-r other.txt` 之类的行（后者不参与比对）
_PIN = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([^\s#]+)")
_INCLUDE = re.compile(r"^\s*-r\s+(\S+)")


def _pins(path: Path) -> dict:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN.match(line)
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def _resolve(path: Path, seen: set | None = None) -> dict:
    """跟着 `-r` 把整条依赖链展开成"最终会装上的 pin 集合"。

    必须有这一步：拆分之后某个文件里可能**一个 pin 都没有**（只剩 `-r`），
    只看单文件会得出"这份清单是空的"这种错误结论。
    """
    seen = seen or set()
    resolved = path.resolve()
    if resolved in seen:
        return {}
    seen.add(resolved)

    out = _pins(path)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _INCLUDE.match(line)
        if m:
            child = (path.parent / m.group(1)).resolve()
            assert child.exists(), f"{path.name} 引用了不存在的 {m.group(1)}"
            out.update(_resolve(child, seen))
    return out


def test_runtime_pins_are_a_consistent_subset_of_full():
    """核心断言：镜像清单的每个 pin 都必须在全量清单里同版本存在。

    这条正是"手抄漂移"的守门人。若在 runtime 里单独升一个版本（或改个包名），
    它会立刻红，而不是等到镜像里跑出问题时才发现。
    """
    full = _resolve(FULL)
    runtime = _resolve(RUNTIME)

    assert runtime, "requirements-runtime.txt 没解析出任何依赖，切分可能已经失效"

    mismatched = {
        name: (ver, full.get(name))
        for name, ver in runtime.items()
        if full.get(name) != ver
    }
    assert not mismatched, (
        f"镜像清单与全量清单不一致（包名: (镜像版本, 全量版本)）：{mismatched}"
    )


# ========== base / dev / runtime 的拆分结构 ==========

def test_base_is_the_only_place_with_versions():
    """拆分的核心不变式：版本只写在 base 里，dev 与 runtime 都不重复声明。"""
    assert _pins(BASE), "base 里必须有实际版本"
    assert not _pins(RUNTIME), "runtime 不该再声明版本，只用 -r base"
    # dev 只允许声明 dev 专属的（pytest / pypdf / streamlit / pandas）
    dev_pins = set(_pins(DEV))
    assert dev_pins <= {"pytest", "pypdf", "streamlit", "pandas"}, (
        f"dev 里声明了本该在 base 的包：{sorted(dev_pins - {'pytest', 'pypdf', 'streamlit', 'pandas'})}"
    )


def test_include_chain_is_wired():
    for path, expected in ((FULL, "requirements-dev.txt"),
                           (DEV, "requirements-base.txt"),
                           (RUNTIME, "requirements-base.txt")):
        includes = [
            m.group(1)
            for m in (_INCLUDE.match(l.strip()) for l in path.read_text(encoding="utf-8").splitlines())
            if m
        ]
        assert expected in includes, f"{path.name} 没有 -r {expected}"


def test_resolved_sets_match_expectations():
    """展开后的最终集合必须与拆分前逐包一致（这次重构不能改变装什么）。"""
    base = _resolve(BASE)
    full = _resolve(FULL)
    runtime = _resolve(RUNTIME)

    # base ⊆ runtime ⊆ full，且 base 与 runtime 完全相等
    assert runtime == base
    assert set(base) <= set(full)

    # dev 专属的包必须出现在 full 里、且不出现在 runtime 里
    for name in ("pytest", "pypdf", "streamlit", "pandas"):
        assert name in full, f"{name} 缺失：dev 依赖没进全量清单"
        assert name not in runtime, f"{name} 不该进镜像清单"

    # 运行期核心包必须在 runtime 里
    for name in ("fastapi", "uvicorn", "langchain-core", "chromadb", "jieba", "tenacity"):
        assert name in runtime, f"{name} 是运行期依赖，不该从镜像清单里消失"


def test_full_list_has_no_known_unused_packages():
    """已确认零使用的包不得留在清单里。

    `requests` 只被 `legacy/test_api.py` 用（legacy/ 已冻结、不进镜像）；
    `pytest-asyncio` 全仓零 async 测试。留着它们的代价是每次装依赖都多一份
    攻击面与解析时间，而收益是零。
    """
    full = _pins(FULL)
    assert "requests" not in full, "requests 只有 legacy/ 在用，不该占运行期依赖"
    assert "pytest-asyncio" not in full, "全仓没有 async 测试，pytest-asyncio 是死依赖"


def test_pypdf_has_an_explicit_landing_spot():
    """pypdf 必须被某个清单显式声明。

    它此前是"动态 import + 无任何声明"：在本机因为别的包把它带进来而能用，
    换台机器就在跑脚本时才炸。这种"靠传递依赖碰巧可用"的状态，
    比直接报缺失更难排查。
    """
    # 注意用 _resolve：拆分后 requirements.txt 自己一个 pin 都没有（只剩 -r dev）
    assert "pypdf" in _resolve(FULL), "pypdf 没有在任何依赖清单里声明"
    # 镜像里**不该**有它：tools/ 不进镜像，pypdf 也就没有运行期用途
    assert "pypdf" not in _resolve(RUNTIME), "pypdf 是开发依赖，不该进运行时镜像"


def test_runtime_does_not_ship_streamlit_or_pandas():
    """对偶：镜像清单要继续保持"不含旧前端依赖"。

    容器入口只起 FastAPI（entrypoint.sh），streamlit / pandas 只服务于
    app.py（保留作对照的旧前端）。它们进镜像等于白背几十 MB。
    """
    runtime = _resolve(RUNTIME)
    assert "streamlit" not in runtime
    assert "pandas" not in runtime
    # 全量清单里则必须保留——本地起旧前端对照时要用
    full = _resolve(FULL)
    assert "streamlit" in full and "pandas" in full


def test_parser_script_degrades_readably_without_pypdf():
    """`tools/parse_manual_pdf.py` 缺 pypdf 时必须给指引，而不是裸 traceback。"""
    src = (ROOT / "tools" / "parse_manual_pdf.py").read_text(encoding="utf-8")
    assert "except ModuleNotFoundError" in src, "缺 pypdf 时会直接抛 traceback"
    assert "pip install" in src, "缺依赖时没有告诉用户怎么装"
