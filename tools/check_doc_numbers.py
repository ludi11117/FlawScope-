"""核对文档里"写了多少项测试"与实际情况是否一致（D8 的验收命令）。

用法：
    venv/Scripts/python.exe tools/check_doc_numbers.py            # 全查（后端 + 前端）
    venv/Scripts/python.exe tools/check_doc_numbers.py --no-frontend   # 只查后端

为什么需要它：文档里的测试数字是**手抄**的，改完代码几乎不会有人回来同步。
抄错不算 bug，但它会让"这份文档可信吗"这件事整体打折——而本项目的很多结论
（误降级率、幻觉率、覆盖率）都建立在"文档写的是真的"这个前提上。

它的判别式是双向的：
  - 文档里提到的每个数字，必须与实测一致（漏同步 → 报错）；
  - 实测数字必须**至少在一处文档里出现**（加了一堆测试却没人知道 → 报错）。

全部离线：只跑 `pytest --collect-only`（不执行用例）与 `npx vitest run`，不联网。
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 文档里"测试项数"的写法。注意前端那句是 "86 项，全部离线"，后端是 "157 项离线单测" / "403 项单测"。
# 用"紧邻 项 的整数"做匹配，能覆盖这些变体而不必逐个硬编码。
_NUMBER_BEFORE_项 = re.compile(r"(\d+)\s*项")

DOC_FILES = [
    ROOT / "README.md",
    ROOT / "docs" / "PROJECT_GUIDE.md",
    ROOT / "web" / "README.md",
]


def parse_pytest_collect(output: str) -> int:
    """从 `pytest --collect-only -q` 的输出里取 "N tests collected"。"""
    m = re.search(r"(\d+)\s+tests?\s+collected", output)
    if not m:
        raise RuntimeError(f"没解析出收集数量，输出尾部：{output[-300:]}")
    return int(m.group(1))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """去掉终端颜色控制符。

    vitest 即使 stdout 不是 TTY 也会着色（实测踩到过：带颜色时 `Tests\\s+(\\d+)` 匹配不上，
    因为 "Tests" 与实际数字之间夹着 `\\x1b[39m` 之类的转义序列）。
    """
    return _ANSI.sub("", text)


def parse_vitest(output: str) -> int:
    """从 `npx vitest run` 的输出里取 "Tests  N passed (N)"。"""
    m = re.search(r"Tests\s+(\d+)\s+(?:passed|failed)", strip_ansi(output))
    if not m:
        raise RuntimeError(f"没解析出前端测试数量，输出尾部：{output[-300:]}")
    return int(m.group(1))


def collect_backend() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return parse_pytest_collect(proc.stdout or "")


def collect_frontend() -> int:
    proc = subprocess.run(
        ["npx", "vitest", "run"],
        cwd=str(ROOT / "web"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=(sys.platform == "win32"),
    )
    if proc.returncode != 0 and "Tests" not in (proc.stdout or ""):
        raise RuntimeError(f"vitest 跑不起来：{(proc.stdout or proc.stderr)[-300:]}")
    return parse_vitest(proc.stdout or "")


def doc_numbers(path: Path, keyword: str) -> list:
    """取出某份文档里"说了多少项测试"的那些数字。

    只取**同一行里含关键词**（如"离线单测""全部离线""项单测"）的数字，
    避免把"12 设备类型""36 故障条目"这类完全无关的数字吸进来。
    """
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if keyword not in line:
            continue
        for n in _NUMBER_BEFORE_项.findall(line):
            out.append(int(n))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-frontend", action="store_true", help="跳过前端（npx 不可用时）")
    args = ap.parse_args()

    try:
        backend = collect_backend()
    except Exception as e:
        print(f"✗ 无法统计后端测试数：{e}")
        return 1
    print(f"后端实测：{backend} 项")

    frontend = None
    if not args.no_frontend:
        try:
            frontend = collect_frontend()
            print(f"前端实测：{frontend} 项")
        except Exception as e:
            print(f"⚠ 无法统计前端测试数（跳过）：{e}")

    checks = [
        ("后端", backend, ROOT / "README.md", "项单测"),
        ("后端", backend, ROOT / "docs" / "PROJECT_GUIDE.md", "项"),
        ("前端", frontend, ROOT / "web" / "README.md", "全部离线"),
    ]

    fails = []
    for label, actual, path, keyword in checks:
        if actual is None:
            continue
        numbers = doc_numbers(path, keyword)
        if not numbers:
            fails.append(f"{path.name} 里找不到任何「{keyword}」附近的项数")
            continue
        bad = sorted({n for n in numbers if n != actual})
        if bad:
            fails.append(
                f"{path.name}（{label}）写的是 {bad} 项，实测 {actual} 项"
            )
        else:
            print(f"  OK  {path.name}（{label}）与实测一致：{actual}")

    if fails:
        print("\n✗ 文档数字与实测不一致：")
        for f in fails:
            print("   -", f)
        return 1

    print("\n全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
