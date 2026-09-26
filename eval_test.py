# -*- coding: utf-8 -*-
"""
FlawScope · 三类硬指标评估框架（V2）

核心思想：把"LLM 判 LLM 的单一准确率"升级为可复现、可归因、可审计的硬指标。

指标：
  ① 终诊根因-核心一致率  （LLM 判官，默认独立模型，宽松标准：命中至少一条黄金根因）
  ② 终诊根因-全覆盖率    （LLM 判官，严格标准：覆盖全部黄金根因）
  ③ 幻觉率              （程序化：诊断原因是否全部接地于知识库，确定性、零 LLM）
  ④ 排除条件遵守率       （程序化：被用户排除的关键词是否泄漏进终诊，确定性）
  ⑤ 完整输出率          （程序化：诊断/审核/成本/工单各节点是否全部产出有效结构）
  ⑥ 诚实降级            （程序化：知识库外对抗案例是否拒绝编造）
  ⑦ 误降级              （程序化：有依据的案例是否错误地拒绝诊断）
  ⑧ 辩论增益            （初诊 vs 终诊判官对比：修正 / 恶化 / 持平）

用法：
  python eval_test.py                                   # 全量评估 + 判官 + 报告
  python eval_test.py --cases test_cases.json --limit 5 # 只跑前 5 条
  python eval_test.py --judge-model deepseek-ai/DeepSeek-V3
  python eval_test.py --skip-judge --limit 3            # 免判官，只跑确定性指标（调试用）
  python eval_test.py --output eval_reports
  python eval_test.py --snapshot                        # 生成/更新快照（黄金标准输出）
  python eval_test.py --regression                      # 回归测试：对比当前输出与快照

判官独立性：诊断用的诊断模型用 14B，判官默认用 72B（可通过 JUDGE_MODEL 环境变量或
--judge-model 覆盖，建议换成不同厂商模型以彻底消除同源偏倚）。
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import jieba

jieba.setLogLevel(logging.WARNING)
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from orchestrator import run_diagnosis

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = BASE_DIR / "eval_snapshots"

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ========== 配置 ==========

JUDGE_MODEL = os.getenv("JUDGE_MODEL", "Qwen/Qwen2.5-72B-Instruct")
UNKNOWN_PATTERNS = ("无法诊断", "无法判断", "无相关依据", "不相关", "人工介入", "补充知识库")

_judge_llm = None


# ========== 通用工具 ==========

def _kb_text() -> str:
    kb_path = BASE_DIR / "data" / "knowledge_base.txt"
    if kb_path.exists():
        return kb_path.read_text(encoding="utf-8")
    return ""


KB_TEXT = _kb_text()
KB_SENTENCES = [ln.strip() for ln in KB_TEXT.splitlines() if ln.strip()]


def _grounded_in_corpus(candidate: str, corpus: str, sentences: list) -> bool:
    """判断候选原因是否接地于给定语料：先查原文子串，再按 jieba 分词做同句 token 接地，
    以容忍模型的细微改写（如"油液污染"改写成"液压油污染"）。"""
    if not candidate or not corpus:
        return False
    if candidate in corpus:
        return True
    tokens = [t.strip() for t in jieba.cut(candidate) if t.strip()]
    if not tokens:
        return True
    for sent in sentences:
        if len(sent) >= 4 and all(t in sent for t in tokens):
            return True
    return False


def _sentences_of(text: str) -> list:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _grounded_in_kb(candidate: str) -> bool:
    """接地于**整份知识库原文**。保留作为兜底口径（全库包含检索命中片段）。"""
    return _grounded_in_corpus(candidate, KB_TEXT, KB_SENTENCES)


def _grounded_in_evidence(candidate: str, evidence: str) -> bool:
    """接地于**本次实际检索命中的片段**。

    这是幻觉检测的严格口径：只有"从检索到的资料里推出来的原因"才算接地。
    此前只用整份知识库做子串匹配，库越小越容易命中——1.5 KB 的知识库下，
    模型随口说的原因也大概率能在全文里找到子串，幻觉率会虚高为 0。
    改成比对 evidence（retrieve_evidence 拼接的【资料N】块）后，
    指标才真正衡量"有无编造"。
    """
    if "【知识库无相关依据】" in (evidence or ""):
        return False
    return _grounded_in_corpus(candidate, evidence, _sentences_of(evidence))


def _parse_json_quiet(text: str) -> dict:
    # 非字符串输入（None / 数字）也当作"解析不出来"：这个函数的名字里写着 quiet，
    # 就不该在输入不合法时抛 AttributeError。调用方虽然有 None 判断，
    # 但那是一层隐性契约，不如在这里收口。
    if not isinstance(text, str):
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, TypeError):
                pass
    return {}


def _invoke_llm(llm, messages, max_retries=1):
    for attempt in range(max_retries + 1):
        try:
            resp = llm.invoke(messages)
            return resp.content
        except Exception as e:
            print(f"  [判官调用异常，第{attempt + 1}次] {e}")
            if attempt == max_retries:
                return None
    return None


def _ensure_judge(model: str):
    global _judge_llm
    if _judge_llm is None or getattr(_judge_llm, "model_name", None) != model:
        _judge_llm = ChatOpenAI(
            model=model,
            openai_api_key=os.getenv("SILICONFLOW_API_KEY"),
            openai_api_base=os.getenv("JUDGE_BASE_URL") or os.getenv("SILICONFLOW_BASE_URL"),
            temperature=0.0,
            request_timeout=90,
        )
    return _judge_llm


# ========== 确定性规则（零 LLM，可复现） ==========

def split_root_causes(text: str) -> list:
    """把根因判断文本拆成候选原因列表（兼容 '原因1：' 标签、'或/和' 前缀与分隔符）。"""
    parts = re.split(r"[;；,，]", text)
    out = []
    for p in parts:
        p = p.strip()
        p = re.sub(r"^原因\s*\d+\s*[:：、\s]*", "", p)
        p = re.sub(r"^(或|和)+\s*", "", p)
        p = p.strip("。）).。、;；,，:").strip()
        if p:
            out.append(p)
    return out


def is_unknown_response(text: str) -> bool:
    return any(p in text for p in UNKNOWN_PATTERNS)


def is_hallucinated(root_cause: str, evidence: str = "") -> tuple:
    """诊断引用的原因是否全部接地于**本次检索命中的资料**。

    返回 (是否幻觉, 幻觉原因列表)。evidence 为空时回退到整库口径（兼容旧调用），
    但正式评估一律传 evidence，否则指标会因"库小易命中"而虚高。
    """
    if not root_cause or is_unknown_response(root_cause):
        return False, []
    if evidence:
        grounded = lambda c: _grounded_in_evidence(c, evidence)  # noqa: E731
    elif KB_TEXT:
        grounded = _grounded_in_kb
    else:
        return False, []
    fake = [c for c in split_root_causes(root_cause) if c and not grounded(c)]
    return bool(fake), fake


def check_exclusion(root_cause: str, excluded_keywords: list) -> tuple:
    """被明确排除的关键词是否泄漏进终诊。返回 (是否违规, 违规关键词列表)。"""
    if not excluded_keywords:
        return False, []
    violated = [k for k in excluded_keywords if k and k in root_cause]
    return bool(violated), violated


def _missing_nodes(result: dict) -> list:
    """统计因解析/schema 校验失败而产生空输出的节点。"""
    if result.get("status") in ("need_more_info", "pending_human_review", "insufficient_knowledge"):
        return []
    diagnosis = result.get("diagnosis") or {}
    # 诚实降级场景：按设计直接出"待人工确认"工单，审核/辩论节点本就不执行
    if is_unknown_response(diagnosis.get("根因判断", "")):
        return [k for k in ("diagnosis", "cost", "workorder") if not result.get(k)]
    return [k for k in ("diagnosis", "review", "cost", "workorder") if not result.get(k)]


def extract_expected_root_causes(case: dict) -> list:
    """从黄金标注中取出根因列表；兼容旧版 expected_diagnosis 字符串。"""
    if case.get("expect_unknown"):
        return []
    explicit = case.get("expected_root_causes")
    if explicit is not None:
        return [x for x in explicit if x]
    legacy = case.get("expected_diagnosis", "")
    cleaned = []
    for p in re.split(r"[;；,，]", legacy):
        p = re.sub(r"^(或|和)+\s*", "", p).strip()
        if p and p not in ("知识库无相关依据，无法判断", "知识库无相关依据，无法诊断"):
            cleaned.append(p)
    return cleaned


# ========== 快照管理 ==========

SNAPSHOT_FIELDS = [
    "diagnosis", "review", "rebuttal", "final_review", "cost", "workorder",
    "status", "debate_round", "followup_question"
]


def load_snapshots() -> dict:
    """加载所有快照文件"""
    snapshots = {}
    if SNAPSHOT_DIR.exists():
        for f in SNAPSHOT_DIR.glob("*.json"):
            case_id = f.stem
            try:
                snapshots[case_id] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
    return snapshots


def save_snapshot(case_id: str, result: dict):
    """保存单个案例的快照"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {k: result.get(k) for k in SNAPSHOT_FIELDS if k in result}
    # 只保存关键字段，减少噪音
    (SNAPSHOT_DIR / f"{case_id}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def compare_with_snapshot(case_id: str, result: dict, snapshots: dict) -> dict:
    """对比当前结果与快照，返回差异报告"""
    snapshot = snapshots.get(case_id)
    if not snapshot:
        return {"status": "no_snapshot", "message": "无基准快照"}

    diffs = []
    for field in SNAPSHOT_FIELDS:
        curr = result.get(field)
        snap = snapshot.get(field)
        if curr != snap:
            diffs.append({
                "field": field,
                "current": curr,
                "snapshot": snap
            })

    return {
        "status": "changed" if diffs else "unchanged",
        "diffs": diffs
    }


# ========== LLM 判官（独立模型） ==========

def judge_root_cause(root_cause: str, expected_list: list, model: str, skip: bool = False) -> dict:
    """判官打分：核心一致 / 覆盖全部 / 理由。失败或跳过时返回 None。"""
    if skip or not root_cause or not expected_list:
        return None
    llm = _ensure_judge(model)
    sys_msg = SystemMessage(content="你是独立的第三方技术评估员，只依据给定的标准答案与系统输出做客观判断，不得夹带个人假设。")
    prompt = f"""
请评估一个故障诊断系统对某工业设备报告的"最终根因判断"是否正确。

【标准答案｜人工标注的黄金根因】（JSON 数组，可能有多条，全部都应被系统覆盖）
{json.dumps(expected_list, ensure_ascii=False)}

【系统最终根因判断】
{root_cause}

请判断：
1. 核心一致：系统的根因与"至少一条"黄金根因语义一致（表述不同但指向同一根因也算一致）。输出 true 或 false。
2. 覆盖全部：系统的根因覆盖了黄金答案中的"所有"根因，一条都不漏。输出 true 或 false。
3. 理由：一两句话说明判断依据（不超过60字）。

注意：不要因为系统多写了额外候选原因就判定不一致，只要覆盖了黄金根因即可。
严格只输出 JSON，不要输出其他内容：
{{"核心一致": true或false, "覆盖全部": true或false, "理由": "一句话"}}
"""
    content = _invoke_llm(llm, [sys_msg, HumanMessage(content=prompt)])
    if content is None:
        return None
    data = _parse_json_quiet(content)
    if not data:
        print(f"  [判官] 输出无法解析：{content[:200]}")
        return None
    return {
        "core": data.get("核心一致") is True,
        "coverage": data.get("覆盖全部") is True,
        "reason": str(data.get("理由", ""))[:100],
    }


# ========== 案例评估 ==========

def evaluate_case(case: dict, args, snapshots: dict = None) -> dict:
    case_id = case["id"]
    fault = case["fault_description"]
    excluded = case.get("excluded_keywords", [])
    expect_unknown = bool(case.get("expect_unknown", False))
    expected_list = extract_expected_root_causes(case)

    t0 = time.perf_counter()
    result = run_diagnosis(fault)
    elapsed = time.perf_counter() - t0

    diagnosis = result.get("diagnosis") or {}
    rebuttal = result.get("rebuttal") or {}
    evidence = result.get("evidence") or ""
    rounds = int(result.get("debate_round", 0) or 0)

    initial_root = diagnosis.get("根因判断", "") or ""
    final_root = (rebuttal.get("最终根因") or initial_root) if rebuttal.get("最终根因") else initial_root

    record = {
        "id": case_id,
        "fault_description": fault,
        "expect_unknown": expect_unknown,
        "expected_root_causes": expected_list,
        "excluded_keywords": excluded,
        "debate_rounds": rounds,
        "elapsed_sec": round(elapsed, 1),
        "initial_root": initial_root,
        "final_root": final_root,
        "missing_nodes": _missing_nodes(result),
        "token_usage": result.get("token_usage", {}),
        "correlation_id": result.get("correlation_id", ""),
        "evidence": evidence,
        "evidence_chars": len(evidence),
    }

    # ---- 确定性指标 ----
    record["unknown_honest"] = is_unknown_response(final_root) if expect_unknown else None
    record["false_degraded"] = (not expect_unknown and is_unknown_response(final_root)) if expected_list else None
    hallucinated, fake_causes = (False, [])
    if not expect_unknown and not is_unknown_response(final_root):
        hallucinated, fake_causes = is_hallucinated(final_root, evidence)
    record["hallucinated"] = hallucinated if (KB_TEXT and not expect_unknown) else None
    record["hallucinated_causes"] = fake_causes
    violation, violated_kw = check_exclusion(final_root, excluded)
    record["exclusion_violated"] = violation if excluded else None
    record["exclusion_violated_keywords"] = violated_kw

    # ---- LLM 判官（只对"该诊断且系统给了结论"的非未知案例） ----
    record["judgment"] = None
    if expected_list and not is_unknown_response(final_root):
        record["judgment"] = judge_root_cause(final_root, expected_list, args.judge_model, args.skip_judge)

    # ---- 辩论增益：初诊 vs 终诊（仅辩论被触发的案例） ----
    record["debate_initial_judgment"] = None
    if rounds > 0 and not args.no_debate_analysis:
        if expected_list and not is_unknown_response(initial_root):
            record["debate_initial_judgment"] = judge_root_cause(
                initial_root, expected_list, args.judge_model, args.skip_judge
            )

    # ---- 快照对比 ----
    if snapshots is not None:
        record["snapshot_diff"] = compare_with_snapshot(case_id, result, snapshots)
    else:
        record["snapshot_diff"] = {"status": "skipped"}

    return record


# ========== 汇总统计 ==========

def aggregate(records: list) -> dict:
    n = len(records)

    judged = [r for r in records if r["judgment"] is not None]
    core_ok = sum(1 for r in judged if r["judgment"]["core"])
    coverage_ok = sum(1 for r in judged if r["judgment"]["coverage"])

    halluc_eval = [r for r in records if r["hallucinated"] is not None and not r["expect_unknown"]
                   and not is_unknown_response(r["final_root"])]
    halluc_bad = sum(1 for r in halluc_eval if r["hallucinated"])

    excl_cases = [r for r in records if r["exclusion_violated"] is not None]
    excl_bad = sum(1 for r in excl_cases if r["exclusion_violated"])

    unknown_cases = [r for r in records if r["expect_unknown"]]
    unknown_honest = sum(1 for r in unknown_cases if r["unknown_honest"])

    known_with_expected = [r for r in records if r["expected_root_causes"]]
    false_deg = sum(1 for r in known_with_expected if r["false_degraded"])

    # 快照对比统计
    snapshot_changed = sum(1 for r in records if r.get("snapshot_diff", {}).get("status") == "changed")
    snapshot_total = sum(1 for r in records if r.get("snapshot_diff", {}).get("status") != "skipped")

    return {
        "total": n,
        "core_accuracy": {"pass": core_ok, "total": len(judged),
                          "rate": (core_ok / len(judged)) if judged else None},
        "coverage_rate": {"pass": coverage_ok, "total": len(judged),
                          "rate": (coverage_ok / len(judged)) if judged else None},
        # 严格口径：把「弃权」留在分母里。
        #
        # 现行 coverage_rate 的分母是 len(judged)，而降级样本的 judgment 是 None
        # （没有根因可判），于是**每弃权一例，分母就少一** —— 多降级反而让覆盖率
        # 更好看。实测 2026-09-25：多 Agent 25/27 = 92.6%，单 Agent 27/30 = 90.0%，
        # 看着是多 Agent 更优；但多 Agent 通过的**绝对例数更少**（25 < 27），
        # 按同一个分母算 25/30 = 83.3%，反而低 6.7pp。
        #
        # 「没测到」不能冒充「测得好」：降级是弃权，不是通过。两个口径必须一起报。
        "coverage_rate_strict": {"pass": coverage_ok, "total": len(known_with_expected),
                                 "abstained": len(known_with_expected) - len(judged),
                                 "rate": (coverage_ok / len(known_with_expected))
                                 if known_with_expected else None},
        "hallucination_rate": {"bad": halluc_bad, "total": len(halluc_eval),
                               "rate": (halluc_bad / len(halluc_eval)) if halluc_eval else None},
        "exclusion_compliance": {"violated": excl_bad, "total": len(excl_cases),
                                 "rate": None if not excl_cases else (1 - excl_bad / len(excl_cases))},
        "completeness": {"ok": sum(1 for r in records if not r["missing_nodes"]), "total": n,
                         "rate": (sum(1 for r in records if not r["missing_nodes"]) / n) if records else None},
        "honest_degradation": {"pass": unknown_honest, "total": len(unknown_cases),
                               "rate": (unknown_honest / len(unknown_cases)) if unknown_cases else None},
        "false_degradation": {"bad": false_deg, "total": len(known_with_expected),
                              "rate": (false_deg / len(known_with_expected)) if known_with_expected else None},
        "snapshot_regression": {"changed": snapshot_changed, "total": snapshot_total,
                                "rate": (snapshot_changed / snapshot_total) if snapshot_total else None},
    }


def debate_summary(records: list, metrics: dict) -> dict:
    debated = [r for r in records if r["debate_rounds"] > 0]
    compared = [r for r in debated if r["judgment"] and r["debate_initial_judgment"]]
    corrected = [r for r in compared if not r["debate_initial_judgment"]["core"] and r["judgment"]["core"]]
    worsened = [r for r in compared if r["debate_initial_judgment"]["core"] and not r["judgment"]["core"]]
    kept_ok = [r for r in compared if r["debate_initial_judgment"]["core"] and r["judgment"]["core"]]
    kept_bad = [r for r in compared if not r["debate_initial_judgment"]["core"] and not r["judgment"]["core"]]

    initial_ok = sum(1 for r in debated if r["debate_initial_judgment"] and r["debate_initial_judgment"]["core"])
    final_ok = sum(1 for r in debated if r["judgment"] and r["judgment"]["core"])

    return {
        "triggered": len(debated),
        "avg_rounds": round(sum(r["debate_rounds"] for r in debated) / len(debated), 2) if debated else 0,
        "initial_core": {"pass": initial_ok, "total": len(debated)},
        "final_core": {"pass": final_ok, "total": len(debated)},
        "compared": len(compared),
        "corrected": [r["id"] for r in corrected],
        "worsened": [r["id"] for r in worsened],
        "kept_ok": len(kept_ok),
        "kept_bad": len(kept_bad),
    }


# ========== 报告输出 ==========

def format_rate(rate) -> str:
    return "N/A" if rate is None else f"{rate * 100:.1f}%"


def build_markdown(meta: dict, metrics: dict, debate: dict, records: list) -> str:
    m = metrics
    lines = []
    lines.append("# FlawScope 评估报告")
    lines.append("")
    lines.append(f"- 生成时间：{meta['time']}")
    lines.append(f"- 测试集：{meta['cases']}（{m['total']} 例）")
    lines.append(f"- 判官模型：{meta['judge_model']}（skip={meta['skip_judge']}）")
    lines.append(f"- 知识库文本：{'已加载' if KB_TEXT else '未找到'}（幻觉检测{'可用' if KB_TEXT else '不可用'}）")
    lines.append(
        f"- 知识库规模：{len(KB_TEXT)} 字符 / {len(KB_SENTENCES)} 行"
        f"（口径：幻觉率比对**检索命中的资料片段**，非全库子串匹配）"
    )
    if m.get("snapshot_regression"):
        sr = m["snapshot_regression"]
        lines.append(f"- 快照回归：{sr['changed']}/{sr['total']} 变更 ({format_rate(sr['rate'])})")
    lines.append("")

    lines.append("## 一、核心指标")
    lines.append("")
    lines.append("| 指标 | 通过/样本 | 比率 | 说明 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| ① 终诊根因·核心一致率 | {m['core_accuracy']['pass']}/{m['core_accuracy']['total']} | {format_rate(m['core_accuracy']['rate'])} | 判官评估，命中至少一条黄金根因 |")
    lines.append(f"| ② 终诊根因·全覆盖率 | {m['coverage_rate']['pass']}/{m['coverage_rate']['total']} | {format_rate(m['coverage_rate']['rate'])} | 判官评估，覆盖全部黄金根因（严格） |")
    lines.append(f"| ②b 全覆盖率·严格口径 | {m['coverage_rate_strict']['pass']}/{m['coverage_rate_strict']['total']} | {format_rate(m['coverage_rate_strict']['rate'])} | **弃权（降级）留在分母**：没测到 ≠ 测得好 |")
    lines.append(f"| ③ 幻觉率 | {m['hallucination_rate']['bad']}/{m['hallucination_rate']['total']} | {format_rate(m['hallucination_rate']['rate'])} | 程序化检测，终诊引用了**本次检索片段之外**原因的比例（严格溯源） |")
    lines.append(f"| ④ 排除条件遵守率 | {m['exclusion_compliance']['violated']}/{m['exclusion_compliance']['total']} 违规 | {format_rate(m['exclusion_compliance']['rate'])} | 程序化检测，被排除原因不得泄漏进终诊 |")
    lines.append(f"| ⑤ 完整输出率 | {m['completeness']['ok']}/{m['completeness']['total']} | {format_rate(m['completeness']['rate'])} | 诊断/审核/成本/工单各节点均产出有效结构 |")
    lines.append(f"| ⑥ 诚实降级 | {m['honest_degradation']['pass']}/{m['honest_degradation']['total']} | {format_rate(m['honest_degradation']['rate'])} | 知识库外对抗案例拒绝编造 |")
    lines.append(f"| ⑦ 误降级 | {m['false_degradation']['bad']}/{m['false_degradation']['total']} | {format_rate(m['false_degradation']['rate'])} | 有依据的案例却被错误拒绝诊断 |")
    if m.get("snapshot_regression") and m["snapshot_regression"]["total"] > 0:
        sr = m["snapshot_regression"]
        lines.append(f"| ⑧ 快照回归通过率 | {sr['total'] - sr['changed']}/{sr['total']} | {format_rate(1 - sr['rate']) if sr['rate'] is not None else 'N/A'} | 输出与基准快照一致的比例 |")
    lines.append("")

    lines.append("## 二、辩论增益分析")
    lines.append("")
    lines.append(f"- 辩论触发案例：{debate['triggered']} 例，平均 {debate['avg_rounds']} 轮")
    lines.append(f"- 辩论案例初诊·核心一致：{debate['initial_core']['pass']}/{debate['initial_core']['total']}")
    lines.append(f"- 辩论案例终诊·核心一致：{debate['final_core']['pass']}/{debate['final_core']['total']}")
    lines.append(f"- 修正（初诊错→终诊对）：{len(debate['corrected'])} 例 {debate['corrected']}")
    lines.append(f"- 恶化（初诊对→终诊错）：{len(debate['worsened'])} 例 {debate['worsened']}")
    lines.append(f"- 持续正确：{debate['kept_ok']} 例 / 持续错误：{debate['kept_bad']} 例")
    lines.append("")

    lines.append("## 三、逐案例明细")
    lines.append("")
    lines.append("| ID | 类 | 辩论轮 | 初诊根因 | 终诊根因 | 幻觉 | 排除违规 | 判官(核心/覆盖) | 快照 |")
    lines.append("|---|---|:--:|---|---|:--:|:--:|:--:|:--:|")
    for r in records:
        kind = "知识库外" if r["expect_unknown"] else ("排除条件" if r["excluded_keywords"] else "常规")
        h = "✗" if r["hallucinated"] else ("-" if r["hallucinated"] is None else "✓")
        e = "✗" if r["exclusion_violated"] else ("-" if r["exclusion_violated"] is None else "✓")
        j = "--"
        if r["judgment"]:
            j = "✓/✗" if r["judgment"]["core"] and not r["judgment"]["coverage"] else (
                "✓/✓" if r["judgment"]["coverage"] else "✗/✗")
        elif not r["expect_unknown"] and not r["expected_root_causes"]:
            j = "跳过(无黄金标准)"
        snap_status = r.get("snapshot_diff", {}).get("status", "skipped")
        snap_icon = {"unchanged": "✓", "changed": "✗", "no_snapshot": "○", "skipped": "-"}.get(snap_status, "?")
        lines.append(
            f"| {r['id']} | {kind} | {r['debate_rounds']} | {r['initial_root'][:36]} | {r['final_root'][:36]} | {h} | {e} | {j} | {snap_icon} |")
    lines.append("")
    lines.append("## 四、判官理由摘要")
    lines.append("")
    for r in records:
        if r["judgment"] and r["judgment"].get("reason"):
            lines.append(f"- **{r['id']}**（{'✓' if r['judgment']['core'] else '✗'}）：{r['judgment']['reason']}")
    if not any(r["judgment"] and r["judgment"].get("reason") for r in records):
        lines.append("（无）")
    lines.append("")
    lines.append("## 五、快照差异详情")
    lines.append("")
    for r in records:
        diff = r.get("snapshot_diff", {})
        if diff.get("status") == "changed":
            lines.append(f"### {r['id']} (变更)")
            for d in diff.get("diffs", []):
                lines.append(f"- **{d['field']}**: 不同")
                lines.append(f"  - 当前: `{str(d['current'])[:100]}`")
                lines.append(f"  - 基准: `{str(d['snapshot'])[:100]}`")
            lines.append("")
    if not any(r.get("snapshot_diff", {}).get("status") == "changed" for r in records):
        lines.append("（无变更）")
    lines.append("")
    lines.append("*注：判官核心一致率与全覆盖率的分母是**判官实际判过的**案例数。降级样本没有根因可判"
                 "（`judgment` 为 `None`），会被默认口径整个剔出分母——于是**弃权越多、百分比越好看**。"
                 "所以另给「②b 严格口径」把弃权留在分母里，两个口径必须一起报。"
                 "另外辩论触发后终诊以最终根因为准，可能由多条收敛为一条，全覆盖率本身已偏严格。*")
    return "\n".join(lines)


def print_console_summary(metrics: dict, debate: dict):
    m = metrics
    print()
    print("=" * 72)
    print("核心指标")
    print("-" * 72)
    print(f"① 终诊根因·核心一致率     {m['core_accuracy']['pass']}/{m['core_accuracy']['total']}  {format_rate(m['core_accuracy']['rate'])}")
    print(f"② 终诊根因·全覆盖率       {m['coverage_rate']['pass']}/{m['coverage_rate']['total']}  {format_rate(m['coverage_rate']['rate'])}   ← 严格标准")
    print(f"②b 全覆盖率·严格口径      {m['coverage_rate_strict']['pass']}/{m['coverage_rate_strict']['total']}  {format_rate(m['coverage_rate_strict']['rate'])}   ← 弃权留在分母")
    print(f"③ 幻觉率                 {m['hallucination_rate']['bad']}/{m['hallucination_rate']['total']}  {format_rate(m['hallucination_rate']['rate'])}")
    print(f"④ 排除条件遵守率          {m['exclusion_compliance']['violated']}/{m['exclusion_compliance']['total']} 违规  {format_rate(m['exclusion_compliance']['rate'])}")
    print(f"⑤ 完整输出率              {m['completeness']['ok']}/{m['completeness']['total']}  {format_rate(m['completeness']['rate'])}")
    print(f"⑥ 诚实降级（知识库外）     {m['honest_degradation']['pass']}/{m['honest_degradation']['total']}  {format_rate(m['honest_degradation']['rate'])}")
    print(f"⑦ 误降级                 {m['false_degradation']['bad']}/{m['false_degradation']['total']}  {format_rate(m['false_degradation']['rate'])}")
    if m.get("snapshot_regression") and m["snapshot_regression"]["total"] > 0:
        sr = m["snapshot_regression"]
        print(f"⑧ 快照回归通过率          {sr['total'] - sr['changed']}/{sr['total']}  {format_rate(1 - sr['rate']) if sr['rate'] is not None else 'N/A'}")
    print("-" * 72)
    print("辩论增益分析")
    print("-" * 72)
    print(f"辩论触发 {debate['triggered']} 例，平均 {debate['avg_rounds']} 轮")
    print(f"初诊核心一致 {debate['initial_core']['pass']}/{debate['initial_core']['total']} → 终诊 {debate['final_core']['pass']}/{debate['final_core']['total']}")
    print(f"修正 {len(debate['corrected'])} 例 {debate['corrected']}，恶化 {len(debate['worsened'])} 例 {debate['worsened']}")
    print("=" * 72)


def main():
    parser = argparse.ArgumentParser(description="FlawScope 三类硬指标评估框架")
    parser.add_argument("--cases", default=str(BASE_DIR / "test_cases.json"), help="测试集路径")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    parser.add_argument("--judge-model", default=JUDGE_MODEL, help="判官模型，默认取 JUDGE_MODEL 环境变量")
    parser.add_argument("--skip-judge", action="store_true", help="跳过 LLM 判官，只跑确定性指标")
    parser.add_argument("--no-debate-analysis", action="store_true", help="跳过辩论增益判官分析")
    parser.add_argument("--output", default="eval_reports", help="报告输出目录")
    parser.add_argument("--snapshot", action="store_true", help="生成/更新快照（黄金标准输出）")
    parser.add_argument("--regression", action="store_true", help="回归测试：对比当前输出与快照")
    args = parser.parse_args()

    if not os.getenv("SILICONFLOW_API_KEY"):
        print("未配置 SILICONFLOW_API_KEY，无法执行诊断。请先在 .env 中配置。")
        sys.exit(1)

    cases_path = Path(args.cases)
    with open(cases_path, "r", encoding="utf-8") as f:
        cases = json.load(f)
    if args.limit > 0:
        cases = cases[: args.limit]

    # 加载快照（如果需要）
    snapshots = load_snapshots() if (args.snapshot or args.regression) else None

    print(f"评估开始：{len(cases)} 案例 | 判官模型：{args.judge_model}{'（已跳过）' if args.skip_judge else ''}")
    if args.snapshot:
        print("模式：生成/更新快照")
    elif args.regression:
        print(f"模式：回归测试（已加载 {len(snapshots)} 个快照）")
    print(f"知识库文本：{'已加载' if KB_TEXT else '未找到'} | 幻觉检测：{'可用' if KB_TEXT else '不可用'}")
    print(f"知识库规模：{len(KB_TEXT)} 字符 / {len(KB_SENTENCES)} 行 | 幻觉口径：检索片段溯源（严格）")
    print("=" * 72)

    records = []
    for i, case in enumerate(cases, 1):
        tag = ""
        if case.get("expect_unknown"):
            tag = "（对抗：知识库外）"
        elif case.get("excluded_keywords"):
            tag = "（对抗：排除条件）"
        print(f"\n[{i}/{len(cases)}] {case['id']}{tag}")
        rec = evaluate_case(case, args, snapshots)
        records.append(rec)
        rounds = f"（辩论{rec['debate_rounds']}轮）" if rec["debate_rounds"] else ""
        print(f"  ✓ 终诊：{rec['final_root'][:56]}{rounds}")
        if rec["hallucinated"]:
            print(f"  ✗ 幻觉：{rec['hallucinated_causes']}")
        if rec["exclusion_violated"]:
            print(f"  ✗ 排除条件泄漏：{rec['exclusion_violated_keywords']}")
        if rec["missing_nodes"]:
            print(f"  ✗ 节点输出缺失（schema解析失败）：{rec['missing_nodes']}")
        snap_diff = rec.get("snapshot_diff", {})
        if snap_diff.get("status") == "changed":
            print(f"  ⚠ 快照变更：{len(snap_diff['diffs'])} 字段不同")
        elif snap_diff.get("status") == "no_snapshot":
            print("  ○ 无基准快照")

        # 生成快照模式
        if args.snapshot:
            save_snapshot(case["id"], rec)
            print("  💾 快照已保存")

    metrics = aggregate(records)
    debate = debate_summary(records, metrics)
    print_console_summary(metrics, debate)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {
        "meta": {
            "time": datetime.now().isoformat(timespec="seconds"),
            "cases": str(cases_path),
            "judge_model": args.judge_model,
            "skip_judge": args.skip_judge,
            "mode": "snapshot" if args.snapshot else ("regression" if args.regression else "evaluation"),
        },
        "metrics": metrics,
        "debate_analysis": debate,
        "records": records,
    }

    json_path = out_dir / f"eval_report_{ts}.json"
    md_path = out_dir / f"eval_report_{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(build_markdown(report["meta"], metrics, debate, records), encoding="utf-8")
    print(f"\n报告已保存：\n  {json_path}\n  {md_path}")

    # 回归测试模式：有变更时非零退出码
    if args.regression and metrics.get("snapshot_regression", {}).get("changed", 0) > 0:
        print("\n❌ 回归测试失败：存在输出变更")
        sys.exit(1)
    elif args.regression:
        print("\n✅ 回归测试通过：所有输出与快照一致")


if __name__ == "__main__":
    main()