"""`tools/check_doc_numbers.py` 自身的守卫（D8）。

为什么需要给一个核对脚本再写测试：这个脚本的存在意义是"文档数字会不会漂"。
它一旦悄悄失效（比如 vitest 改了输出格式、正则匹配不到），就退化成一个**永远打印
"全部一致"**的摆设 —— 那比没有它更糟，因为它给了人一个假的安心。

所以这里把它的两个关键解析函数钉住，并且**不跑 subprocess**（快、且离线）：
解析逻辑用固定的样本字符串验证。真实数量的采集由脚本本身负责。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import check_doc_numbers as checker


# ========== pytest --collect-only 输出解析 ==========

def test_parse_pytest_collect_standard_output():
    assert checker.parse_pytest_collect(
        "...............\n541 tests collected in 0.49s\n"
    ) == 541


def test_parse_pytest_collect_singular():
    assert checker.parse_pytest_collect("1 test collected in 0.1s") == 1


def test_parse_pytest_collect_raises_when_absent():
    """判别式：解析不到必须**报错**，绝不能静默返回 0（那会让核对永远"一致"）。"""
    with pytest.raises(RuntimeError):
        checker.parse_pytest_collect("ERROR: usage error\n")


# ========== ANSI 去色 ==========

def test_strip_ansi_removes_color_codes():
    assert checker.strip_ansi("\x1b[1m\x1b[32mTests\x1b[39m\x1b[22m  181 passed") == "Tests  181 passed"


def test_strip_ansi_leaves_plain_text_alone():
    assert checker.strip_ansi("Tests  181 passed (181)") == "Tests  181 passed (181)"


def test_parse_vitest_with_ansi_colors():
    """真实踩过：带颜色时 `Tests\\s+(\\d+)` 匹配不上，因为中间夹着转义序列。"""
    colored = "\x1b[1m\x1b[32mTests\x1b[39m\x1b[22m  \x1b[1m181 passed\x1b[22m (181)"
    assert checker.parse_vitest(colored) == 181


def test_parse_vitest_plain():
    assert checker.parse_vitest("Test Files  11 passed (11)\n     Tests  181 passed (181)\n") == 181


def test_parse_vitest_raises_when_absent():
    with pytest.raises(RuntimeError):
        checker.parse_vitest("No test files found\n")


# ========== 文档数字提取 ==========

def test_doc_numbers_picks_up_backend_count(tmp_path):
    doc = tmp_path / "GUIDE.md"
    doc.write_text(
        "venv/Scripts/python.exe -m pytest -q   # 541 项，全离线，约 9 秒\n"
        "├── tests/                       541 项离线单测\n",
        encoding="utf-8",
    )
    assert checker.doc_numbers(doc, "项") == [541, 541]


def test_doc_numbers_ignores_unrelated_numbers(tmp_path):
    """判别式：不该把 "12 设备类型""36 故障条目" 这类数字当成测试项数。"""
    doc = tmp_path / "README.md"
    doc.write_text("知识库：12 设备类型 / 36 故障条目 / 76 个向量块\n", encoding="utf-8")
    # 关键词不匹配 → 一行都不取
    assert checker.doc_numbers(doc, "项单测") == []


def test_doc_numbers_empty_when_file_missing(tmp_path):
    assert checker.doc_numbers(tmp_path / "nope.md", "项") == []


def test_doc_files_exist():
    """脚本里登记的文档必须真实存在——改名后不更新会静默少查一份。"""
    for p in checker.DOC_FILES:
        assert p.exists(), f"登记的文档不存在：{p}"
