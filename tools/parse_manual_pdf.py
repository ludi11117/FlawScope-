"""从厂家诊断手册 PDF 里抽取"报警代码 → 含义 → 处理"结构化条目。

**这是"把权威手册变成可用知识库"的管道验证**，不是最终产物。
先回答三个问题：
  1. 抽得出多少条？
  2. 字段完整度如何（说明/处理 都有吗）？
  3. 抽出来的东西能直接用吗，还是需要人工清洗？

用法：
    venv\\Scripts\\python.exe tools/parse_manual_pdf.py                 # 跑统计
    venv\\Scripts\\python.exe tools/parse_manual_pdf.py --dump 20       # 看前 20 条
    venv\\Scripts\\python.exe tools/parse_manual_pdf.py --json          # 导出 JSON

⚠️ 重要免责：
  - 抽出来的是**厂家手册原文**，版权归西门子所有。**不要直接塞进公开仓库的知识库**。
  - 本脚本只做"管道可行性验证"，产出仅供评估；真要用于生产需自行确认授权。
  - 手册里含大量 `%1`/`%2` 占位符（参数化报警），这些**不能**直接当故障现象用。
"""

import argparse
import json
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "data" / "raw" / "siemens_DA_1106_cs.pdf"

# 报警条目的起始行：6 位数字 + 空格 + 正文
# 注意正文里也有"报警 300500,"这种引用，用行首锚定 + 要求后面不是逗号来排除。
ENTRY_START = re.compile(r"^\s*(\d{6})\s+(?!,)(.+)$")
# 字段行
FIELD = re.compile(r"^\s*(参数|说明|反应|处理|程序继续|报警清除)\s*[：:]\s*(.*)$")
# 页眉页脚噪声
NOISE = re.compile(
    r"(© Siemens AG|SINUMERIK, SIMODRIVE 诊断手册|All Rights Reserved|^爆进鼇鏆|^\d+-\d+$)"
)


def clean(text: str) -> str:
    """清掉页眉页脚与多余空白。"""
    lines = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or NOISE.search(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def parse_pages(pages_text: list) -> list:
    """逐页扫，把报警条目攒成结构化记录。

    状态机：遇到 ENTRY_START 就开一条新记录；遇到 FIELD 归属到当前记录；
    其余行追加到当前字段（手册里说明/处理经常折行）。
    """
    entries = []
    cur = None
    cur_field = None

    for page_no, raw in enumerate(pages_text, 1):
        text = clean(raw)
        for line in text.splitlines():
            m = ENTRY_START.match(line)
            if m:
                if cur:
                    entries.append(cur)
                cur = {
                    "code": m.group(1),
                    "title": m.group(2).strip(),
                    "page": page_no,
                    "说明": "",
                    "处理": "",
                    "反应": "",
                    "参数": "",
                }
                cur_field = None
                continue
            if cur is None:
                continue
            f = FIELD.match(line)
            if f:
                cur_field = f.group(1)
                key = "说明" if cur_field == "说明" else (
                    "处理" if cur_field in ("处理", "程序继续", "报警清除") else
                    ("反应" if cur_field == "反应" else "参数")
                )
                cur[key] = (cur[key] + " " + f.group(2)).strip() if cur.get(key) else f.group(2).strip()
                continue
            # 续行：归到最近一个字段
            if cur_field:
                key = "说明" if cur_field == "说明" else (
                    "处理" if cur_field in ("处理", "程序继续", "报警清除") else
                    ("反应" if cur_field == "反应" else "参数")
                )
                cur[key] = (cur.get(key, "") + " " + line).strip()

    if cur:
        entries.append(cur)
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", type=int, default=0, help="打印前 N 条")
    ap.add_argument("--json", action="store_true", help="导出 JSON")
    args = ap.parse_args()

    if not PDF.exists():
        print(f"找不到 PDF：{PDF}")
        print("先下载：curl -sL -o data/raw/siemens_DA_1106_cs.pdf <西门子官方附件地址>")
        sys.exit(1)

    # pypdf 只给开发用（见 requirements.txt 的"开发/测试"段），容器镜像里没有它。
    # 所以这里**不能裸 import**：真缺的时候要给出"怎么装"，而不是一个
    # ModuleNotFoundError traceback —— 那个脚本的使用者往往是来验证数据管道的，
    # 不是来排查 Python 依赖的。
    try:
        import pypdf
    except ModuleNotFoundError:
        print("缺少 pypdf（PDF 抽取用，只给开发环境装）。")
        print()
        print("  安装：  venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        print("  或单独：venv\\Scripts\\python.exe -m pip install pypdf")
        print()
        print("  说明：容器镜像（requirements-runtime.txt）**不含** pypdf，")
        print("        这个脚本是「把厂家手册变成知识库」的管道验证工具，运行期用不到。")
        sys.exit(1)

    reader = pypdf.PdfReader(str(PDF))
    print(f"手册：{PDF.name}  共 {len(reader.pages)} 页")

    pages = [(p.extract_text() or "") for p in reader.pages]
    entries = parse_pages(pages)

    codes = [e["code"] for e in entries]
    uniq = sorted(set(codes))
    with_desc = [e for e in entries if len(e["说明"]) >= 10]
    with_action = [e for e in entries if len(e["处理"]) >= 10]
    with_both = [e for e in entries if len(e["说明"]) >= 10 and len(e["处理"]) >= 10]
    param_heavy = [e for e in entries if "%1" in e["title"] or "%2" in e["title"]]

    print()
    print("=" * 70)
    print("  抽取统计")
    print("=" * 70)
    print(f"  条目总数        : {len(entries)}")
    print(f"  唯一报警代码    : {len(uniq)}")
    print(f"  有「说明」(≥10字): {len(with_desc)}  ({len(with_desc)*100//max(len(entries),1)}%)")
    print(f"  有「处理」(≥10字): {len(with_action)}  ({len(with_action)*100//max(len(entries),1)}%)")
    print(f"  「说明」+「处理」都有: {len(with_both)}  ({len(with_both)*100//max(len(entries),1)}%)")
    print(f"  含 %N 占位符的  : {len(param_heavy)}  ({len(param_heavy)*100//max(len(entries),1)}%)")

    if uniq:
        print(f"\n  代码区间: {uniq[0]} ~ {uniq[-1]}")

    # 按前缀分布 —— 判断这是几个子系统混在一起
    from collections import Counter
    pref = Counter(c[:3] for c in uniq)
    print("\n  代码前缀分布（前 10）:")
    for p, n in pref.most_common(10):
        print(f"    {p}xxx : {n}")

    if args.dump:
        print()
        print("=" * 70)
        print(f"  前 {args.dump} 条明细")
        print("=" * 70)
        for e in entries[:args.dump]:
            print(f"\n  [{e['code']}] {e['title'][:60]}  (p.{e['page']})")
            print(f"      说明: {e['说明'][:100]}")
            print(f"      处理: {e['处理'][:100]}")

    if args.json:
        out = ROOT / "manual_parse_report.json"
        payload = {
            "note": "由 tools/parse_manual_pdf.py 生成；内容版权归西门子，仅供可行性评估，勿入公开仓库",
            "source_pdf": PDF.name,
            "total_entries": len(entries),
            "unique_codes": len(uniq),
            "entries": entries[:500],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已导出：{out}")


if __name__ == "__main__":
    main()
