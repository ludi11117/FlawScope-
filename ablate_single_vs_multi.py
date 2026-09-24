"""消融实验：单 Agent vs 多 Agent 辩论（同一批用例、同一套指标口径）。

这个脚本存在的唯一理由，是回答一个面试官一定会追问的问题：
**"你凭什么说多 Agent 比单 Agent 好？"**

此前 `compare_single_vs_multi.py` 只能给"诊断准确率"一个数，且用的是
`evaluate_semantic`（更宽松的 LLM 判官），与主评估 `eval_test.py` 的
8 项指标口径不一致——两边数字不可比，等于白跑。

本脚本的做法：
  1. 用 `eval_test` 的**同一个** `judge_root_cause` / `is_hallucinated` /
     `check_exclusion` / `is_unknown_response` 判两边的输出；
  2. 单 Agent 只做「抽取 → 检索 → 一次诊断」，**不给审核、不给辩论、不给 schema 回灌**；
  3. 两侧用同一次检索得到的**同一份 evidence**（否则幻觉率差异可能只是检索差异）；
  4. 结果落盘 JSON + Markdown，含逐用例对照表，便于人工复核。

诚实性约束（写死在代码里，防止我自己事后粉饰）：
  · 单 Agent 与多 Agent 的判定函数完全相同，不允许给单侧更松的口径；
  · 单 Agent 调用失败（返回 None）必须计为「失败」，不能静默跳过——
    否则等于把单 Agent 的崩溃从分母里删掉，人为抬高它的成绩。

用法：
    python ablate_single_vs_multi.py                # 全量 30 例
    python ablate_single_vs_multi.py --limit 5      # 只跑前 5 例（先验证流程、省额度）
    python ablate_single_vs_multi.py --skip-judge   # 只跑确定性指标，不花判官的钱
"""

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import eval_test  # noqa: E402
from agents import agent_diagnose, extract_fault_info, retrieve_evidence  # noqa: E402
from orchestrator import run_diagnosis  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "eval_reports"


def _force_utf8_stdout() -> None:
    """把 stdout 换成 UTF-8。**只能在 main() 里调用，不能放模块级。**

    放模块级会让 import 也带副作用：pytest 捕获输出时已经替换过 sys.stdout，
    这里再往 `sys.stdout.buffer` 上套一层 TextIOWrapper，就把 pytest 的捕获流
    套住了，teardown 阶段报 "I/O operation on closed file"——只要测试里
    import 这个模块就会中招，而报错位置在 pytest 内部，极难定位。
    """
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if enc.startswith("utf-8"):
        return
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def build_single_query(fault_text: str, fault_info: dict) -> str:
    """把抽取结果拼成检索 query。单 Agent 侧独立实现，不借 orchestrator 的节点。"""
    parts = []
    if fault_info.get("设备类型"):
        parts.append(str(fault_info["设备类型"]))
    code = fault_info.get("报警代码")
    if code and str(code).lower() not in ("null", "none", ""):
        parts.append(str(code))
    symptom = fault_info.get("故障现象")
    if isinstance(symptom, (list, tuple)):
        parts.extend(str(s) for s in symptom)
    elif symptom:
        parts.append(str(symptom))
    return " ".join(parts) if parts else fault_text


def single_agent_run(fault_text: str, evidence: str = None) -> dict:
    """单 Agent 基线：抽取 → 检索 → 一次诊断。无审核、无辩论、无 schema 回灌。

    刻意**不**复用 orchestrator 的任何节点，否则就变成"多 Agent 减去一点"
    而不是真正的单 Agent 基线。

    evidence 传入时直接复用（保证两侧面对同一份资料，幻觉率差异才归因于生成
    而非检索）；为 None 时自行检索。
    """
    fault_info = extract_fault_info(fault_text)
    if fault_info is None:
        # 调用失败必须如实返回失败，不能当成"信息不足"继续跑
        return {"ok": False, "reason": "extract_fault_info 调用失败", "root": "", "evidence": ""}

    if evidence is None:
        evidence = retrieve_evidence(build_single_query(fault_text, fault_info), k=3)

    diagnosis = agent_diagnose(evidence, json.dumps(fault_info, ensure_ascii=False), [])
    if not diagnosis:
        return {"ok": False, "reason": "agent_diagnose 返回空", "root": "", "evidence": evidence}

    return {
        "ok": True,
        "reason": "",
        "root": diagnosis.get("根因判断", "") or "",
        "evidence": evidence,
    }


def multi_agent_run(fault_text: str) -> dict:
    """多 Agent：走真实 LangGraph 全流程。"""
    result = run_diagnosis(fault_text)
    diagnosis = result.get("diagnosis") or {}
    rebuttal = result.get("rebuttal") or {}
    initial = diagnosis.get("根因判断", "") or ""
    final = (rebuttal.get("最终根因") or initial) if rebuttal.get("最终根因") else initial
    return {
        "ok": True,
        "reason": "",
        "initial_root": initial,
        "root": final,
        "evidence": result.get("evidence") or "",
        "rounds": int(result.get("debate_round", 0) or 0),
        "missing_nodes": eval_test._missing_nodes(result),
        "token_usage": result.get("token_usage", {}),
    }


def score_one(root: str, evidence: str, case: dict, args) -> dict:
    """用 eval_test 的口径给单个输出打分。两侧必须走这一个函数。"""
    expected = eval_test.extract_expected_root_causes(case)
    excluded = case.get("excluded_keywords", [])
    expect_unknown = bool(case.get("expect_unknown", False))

    degraded = eval_test.is_unknown_response(root) if root else False

    hallucinated, fake = (False, [])
    if eval_test.KB_TEXT and not expect_unknown and not degraded and root:
        hallucinated, fake = eval_test.is_hallucinated(root, evidence)

    violation, violated_kw = eval_test.check_exclusion(root, excluded)

    judgment = None
    if expected and not degraded and root:
        judgment = eval_test.judge_root_cause(root, expected, args.judge_model, args.skip_judge)

    return {
        "degraded": degraded,
        "unknown_honest": degraded if expect_unknown else None,
        "false_degraded": (not expect_unknown and degraded and bool(expected)) or None,
        "hallucinated": hallucinated if (eval_test.KB_TEXT and not expect_unknown and not degraded) else None,
        "hallucinated_causes": fake,
        "exclusion_violated": violation if excluded else None,
        "exclusion_violated_keywords": violated_kw,
        "judgment": judgment,
    }


def summarize(records: list, side: str) -> dict:
    """按 eval_test.aggregate 的同名指标汇总一侧结果。"""
    n = len(records)

    def pick(field):
        return [r[side][field] for r in records]

    judged = [r for r in pick("judgment") if r is not None]
    core_ok = sum(1 for j in judged if j["core"])
    coverage_ok = sum(1 for j in judged if j["coverage"])

    halluc = [v for v in pick("hallucinated") if v is not None]
    halluc_bad = sum(1 for v in halluc if v)

    excl = [v for v in pick("exclusion_violated") if v is not None]
    excl_bad = sum(1 for v in excl if v)

    unknown = [r for r in records if r["expect_unknown"]]
    unknown_honest = sum(1 for r in unknown if r[side]["unknown_honest"])

    known = [r for r in records if r["expected_root_causes"]]
    false_deg = sum(1 for r in known if r[side]["false_degraded"])

    failed = sum(1 for r in records if not r[side]["ok"])

    def rate(a, b):
        return (a / b) if b else None

    return {
        "total": n,
        "call_failures": failed,
        "core_accuracy": {"pass": core_ok, "total": len(judged), "rate": rate(core_ok, len(judged))},
        "coverage_rate": {"pass": coverage_ok, "total": len(judged), "rate": rate(coverage_ok, len(judged))},
        # 严格口径：弃权（有依据却降级）留在分母里。
        # 现行口径的分母 len(judged) 会把降级样本整个剔出去，于是多降级 = 分母变小
        # = 覆盖率更高。两侧分母不同时，百分比根本不可比（详见 eval_test.aggregate 同名项）。
        "coverage_rate_strict": {"pass": coverage_ok, "total": len(known),
                                 "abstained": len(known) - len(judged),
                                 "rate": rate(coverage_ok, len(known))},
        "hallucination_rate": {"bad": halluc_bad, "total": len(halluc), "rate": rate(halluc_bad, len(halluc))},
        "exclusion_compliance": {"violated": excl_bad, "total": len(excl),
                                 "rate": rate(len(excl) - excl_bad, len(excl))},
        "honest_degradation": {"pass": unknown_honest, "total": len(unknown), "rate": rate(unknown_honest, len(unknown))},
        "false_degradation": {"bad": false_deg, "total": len(known), "rate": rate(false_deg, len(known))},
    }


def fmt(metric: dict, key: str, as_pct=True) -> str:
    m = metric.get(key) or {}
    r = m.get("rate")
    if r is None:
        return "n/a"
    return f"{r * 100:.0f}%" if as_pct else f"{r:.3f}"


# 指标清单：(键, 显示名, 说明)。控制台与 Markdown 报告共用一份，
# 避免两处各写一遍、加指标时漏改一处导致两个输出对不上。
METRIC_KEYS = [
    ("core_accuracy", "核心一致率", "LLM 判官：根因是否抓对主因"),
    ("coverage_rate", "全覆盖率", "LLM 判官：是否覆盖全部期望根因（分母 = 判官判过的用例）"),
    ("coverage_rate_strict", "全覆盖率·严格", "同上，但**弃权留在分母**（没测到 ≠ 测得好）"),
    ("hallucination_rate", "幻觉率", "程序化：引用检索片段之外的根因（越低越好）"),
    ("exclusion_compliance", "排除遵守率", "程序化：未泄漏被排除原因（越高越好）"),
    ("honest_degradation", "诚实降级率", "知识库外用例题是否正确降级"),
    ("false_degradation", "误降级率", "能答的题是否被误判为答不了（越低越好）"),
]


def cross_run_spread(runs: list, side: str) -> dict:
    """多轮重复运行时，每个指标的分布（min / mean / max / 摆动幅度）。

    为什么需要：同一份代码、同一批用例跑两次，多 Agent 的"是否降级"会翻转
    ——实测 30 个共同用例里翻了 5 例，而单 Agent 0 例。翻转意味着**单次运行的
    差值本来就落在噪声里**：不跑多轮，分不清"真的变好了"和"这次运气好"。
    """
    summaries = [summarize(recs, side) for recs in runs]
    out = {}
    for key, _, _ in METRIC_KEYS:
        rates = [s[key]["rate"] for s in summaries if (s.get(key) or {}).get("rate") is not None]
        if not rates:
            out[key] = {"rates": [], "min": None, "mean": None, "max": None, "spread_pp": None}
            continue
        out[key] = {
            "rates": rates,
            "min": min(rates),
            "mean": sum(rates) / len(rates),
            "max": max(rates),
            "spread_pp": (max(rates) - min(rates)) * 100,
        }
    return out


def build_markdown(meta: dict, single: dict, multi: dict, records: list,
                   spread: dict = None) -> str:
    L = []
    L.append("# 消融实验：单 Agent vs 多 Agent 辩论\n")
    L.append(f"- 生成时间：{meta['timestamp']}")
    L.append(f"- 用例数：{meta['n_cases']}")
    if meta.get("repeat", 1) > 1:
        L.append(f"- 重复轮数：{meta['repeat']}（同一份代码、同一批用例重跑）")
    L.append(f"- 判官模型：{meta['judge_model']}{'（已跳过，仅确定性指标）' if meta['skip_judge'] else ''}")
    L.append(f"- 知识库规模：{meta['kb_chars']} 字符 / {meta['kb_lines']} 行")
    L.append("")
    L.append("## 口径说明（可比性前提）\n")
    L.append("两侧使用 test/eval 的**同一套**判定函数；单 Agent 与多 Agent 共用同一次检索的 evidence。")
    L.append("单 Agent 调用失败计为失败，不从分母剔除。")
    L.append("")
    L.append("⚠️ **「全覆盖率」有两个口径，必须一起看。** 默认口径的分母是「判官实际判过的用例」")
    L.append("（`len(judged)`），而降级样本没有根因可判、`judgment` 为 `None`，于是**每弃权一例，")
    L.append("分母就少一** —— 多降级反而让覆盖率更好看。严格口径把弃权留在分母里。")
    L.append("两侧弃权数不同时，两个口径会给出**相反的结论**，所以并列展示。\n")
    L.append("## 指标对照\n")
    L.append("| 指标 | 单 Agent | 多 Agent | 差值 | 说明 |")
    L.append("|---|---|---|---|---|")
    for key, name, desc in METRIC_KEYS:
        s = (single.get(key) or {}).get("rate")
        m_ = (multi.get(key) or {}).get("rate")
        if s is None or m_ is None:
            diff = "n/a"
        else:
            diff = f"{(m_ - s) * 100:+.0f}pp"
        L.append(f"| {name} | {fmt(single, key)} | {fmt(multi, key)} | {diff} | {desc} |")
    L.append("")
    L.append(f"- 单 Agent 调用失败：{single['call_failures']} / {single['total']}")
    L.append(f"- 多 Agent 调用失败：{multi['call_failures']} / {multi['total']}")
    L.append(f"- 弃权（降级，被默认口径剔出分母）：单 Agent "
             f"{single['coverage_rate_strict']['abstained']} 例 / 多 Agent "
             f"{multi['coverage_rate_strict']['abstained']} 例")
    L.append("")
    if spread:
        L.append("## 跨轮稳定性（同一代码重复运行）\n")
        L.append(f"跑了 **{meta.get('repeat', 1)} 轮**。这里看的是**指标摆动幅度**："
                 "同一份代码重跑，多 Agent 的「是否降级」会翻转，"
                 "所以单次运行的差值可能只是噪声。\n")
        L.append("| 指标 | 单 Agent 区间 | 多 Agent 区间 | 多 Agent 摆动 |")
        L.append("|---|---|---|---|")

        def _rng(x):
            if x["min"] is None:
                return "n/a"
            return f"{x['min'] * 100:.0f}% ~ {x['max'] * 100:.0f}%"

        for key, name, _ in METRIC_KEYS:
            s = spread["single"][key]
            m_ = spread["multi"][key]
            sw = f"{m_['spread_pp']:.0f}pp" if m_["spread_pp"] is not None else "n/a"
            L.append(f"| {name} | {_rng(s)} | {_rng(m_)} | {sw} |")
        L.append("")

    L.append("## 逐用例对照\n")
    L.append("| 用例 | 类型 | 单 Agent 结论 | 单判定 | 多 Agent 结论 | 多判定 | 辩论轮 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in records:
        typ = "知识库外" if r["expect_unknown"] else ("排除条件" if r["excluded_keywords"] else "常规")
        L.append(
            f"| {r['id']} | {typ} | {r['single']['root'][:34]} | {mark(r['single'])} | "
            f"{r['multi']['root'][:34]} | {mark(r['multi'])} | {r['multi'].get('rounds', 0)} |"
        )
    L.append("")
    return "\n".join(L)


def mark(scored: dict) -> str:
    if not scored["ok"]:
        return "调用失败"
    if scored["judgment"] is None:
        return "降级" if scored["degraded"] else "未判"
    tags = []
    tags.append("对" if scored["judgment"]["core"] else "错")
    if scored["hallucinated"]:
        tags.append("幻觉")
    if scored["exclusion_violated"]:
        tags.append("排除泄漏")
    return "+".join(tags)


def run_once(cases: list, args) -> list:
    """跑一轮完整评估，返回逐用例记录。抽成函数是为了支持 `--repeat`。"""
    records = []
    for i, case in enumerate(cases, 1):
        tag = "（知识库外）" if case.get("expect_unknown") else ("（排除条件）" if case.get("excluded_keywords") else "")
        print(f"\n[{i}/{len(cases)}] {case['id']}{tag}")

        # 先跑多 Agent（它会做检索），单 Agent 复用同一份 evidence 保证可比
        multi_raw = multi_agent_run(case["fault_description"])
        single_raw = single_agent_run(case["fault_description"], multi_raw.get("evidence", ""))

        single_scored = score_one(single_raw["root"], single_raw["evidence"], case, args)
        single_scored["ok"] = single_raw["ok"]
        single_scored["reason"] = single_raw["reason"]

        multi_scored = score_one(multi_raw["root"], multi_raw["evidence"], case, args)
        multi_scored["ok"] = multi_raw["ok"]
        multi_scored["reason"] = multi_raw["reason"]

        rec = {
            "id": case["id"],
            "expect_unknown": bool(case.get("expect_unknown", False)),
            "excluded_keywords": case.get("excluded_keywords", []),
            "expected_root_causes": eval_test.extract_expected_root_causes(case),
            "single": {**single_scored, "root": single_raw["root"]},
            "multi": {**multi_scored, "root": multi_raw["root"],
                      "rounds": multi_raw.get("rounds", 0),
                      "initial_root": multi_raw.get("initial_root", ""),
                      "missing_nodes": multi_raw.get("missing_nodes", [])},
        }
        records.append(rec)

        print(f"  单Agent：{mark(rec['single'])}  {rec['single']['root'][:50]}")
        print(f"  多Agent：{mark(rec['multi'])}  {rec['multi']['root'][:50]}"
              f"  [辩论{rec['multi']['rounds']}轮]")
    return records


def main():
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="单 Agent vs 多 Agent 消融实验")
    ap.add_argument("--cases", default=str(BASE_DIR / "test_cases.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--judge-model", default=eval_test.JUDGE_MODEL)
    ap.add_argument("--skip-judge", action="store_true")
    ap.add_argument("--output", default=str(OUT_DIR))
    ap.add_argument("--repeat", type=int, default=1,
                    help="重复跑几轮；>1 时输出跨轮摆动幅度，用来区分真实变化与抖动")
    args = ap.parse_args()

    if not os.getenv("SILICONFLOW_API_KEY"):
        print("未配置 SILICONFLOW_API_KEY，无法执行诊断。")
        sys.exit(1)

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.limit > 0:
        cases = cases[: args.limit]

    print("=" * 88)
    print("消融实验：单 Agent（抽取→检索→一次诊断） vs 多 Agent（审核+辩论+复审）")
    print(f"用例 {len(cases)} 条 | 判官 {args.judge_model}{'（跳过）' if args.skip_judge else ''}")
    print("两侧共用同一次检索的 evidence，判定函数完全一致。")
    print("=" * 88)

    t_start = time.perf_counter()
    runs = []
    for r in range(max(1, args.repeat)):
        if args.repeat > 1:
            print("\n" + "#" * 88)
            print(f"# 第 {r + 1}/{args.repeat} 轮（同一份代码、同一批用例重跑）")
            print("#" * 88)
        runs.append(run_once(cases, args))

    # 逐用例明细取第一轮；跨轮稳定性另算
    records = runs[0]
    single = summarize(records, "single")
    multi = summarize(records, "multi")
    spread = None
    if len(runs) > 1:
        spread = {"single": cross_run_spread(runs, "single"),
                  "multi": cross_run_spread(runs, "multi")}

    print()
    print("=" * 88)
    print("▶ 指标对照")
    print("-" * 88)
    for key, name in [("core_accuracy", "核心一致率"), ("coverage_rate", "全覆盖率"),
                      ("coverage_rate_strict", "全覆盖率·严格"),
                      ("hallucination_rate", "幻觉率"), ("exclusion_compliance", "排除遵守率"),
                      ("honest_degradation", "诚实降级率"), ("false_degradation", "误降级率")]:
        s = (single[key] or {}).get("rate")
        m_ = (multi[key] or {}).get("rate")
        d = f"{(m_ - s) * 100:+.0f}pp" if (s is not None and m_ is not None) else "n/a"
        print(f"  {name:<12} 单 {fmt(single, key):>5}   多 {fmt(multi, key):>5}   {d}")
    print("-" * 88)
    print(f"  弃权(降级，被默认口径剔出分母) 单 {single['coverage_rate_strict']['abstained']}"
          f" | 多 {multi['coverage_rate_strict']['abstained']}")
    print(f"  单 Agent 调用失败 {single['call_failures']}/{single['total']}"
          f" | 多 Agent 调用失败 {multi['call_failures']}/{multi['total']}")
    print(f"  耗时 {time.perf_counter() - t_start:.0f}s")

    if spread:
        print()
        print("▶ 跨轮稳定性（同一代码重跑，看摆动幅度）")
        print("-" * 88)

        def _rng(x):
            if x["min"] is None:
                return "n/a"
            return f"{x['min'] * 100:.0f}~{x['max'] * 100:.0f}%"

        for key, name, _ in METRIC_KEYS:
            s = spread["single"][key]
            m_ = spread["multi"][key]
            sw = f"{m_['spread_pp']:.0f}pp" if m_["spread_pp"] is not None else "n/a"
            print(f"  {name:<12} 单 {_rng(s):>9}   多 {_rng(m_):>9}   多 Agent 摆动 {sw}")
        print("-" * 88)
        print("  摆动幅度大 → 该指标的差值落在噪声里，不能只看单次运行。")
    print("=" * 88)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    meta = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "n_cases": len(cases),
        "judge_model": args.judge_model,
        "skip_judge": args.skip_judge,
        "kb_chars": len(eval_test.KB_TEXT),
        "kb_lines": len(eval_test.KB_SENTENCES),
        "repeat": max(1, args.repeat),
    }
    payload = {"meta": meta, "single": single, "multi": multi, "records": records}
    if spread:
        payload["cross_run_spread"] = spread
    json_path = out_dir / f"ablation_{ts}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / f"ablation_{ts}.md"
    md_path.write_text(build_markdown(meta, single, multi, records, spread), encoding="utf-8")
    print(f"报告：{md_path}")
    print(f"原始：{json_path}")


if __name__ == "__main__":
    main()
