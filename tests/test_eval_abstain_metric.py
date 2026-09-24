"""评估口径守卫：「弃权」必须留在分母里。

## 这条测试要防什么

判官指标（核心一致率 / 全覆盖率）的分母是 `len(judged)` —— 只有判官**实际判过**
的用例才进分母。而降级样本没有根因可判，`judgment` 是 `None`，于是被整个剔出去。

后果：**每弃权一例，分母就少一**，百分比反而更高。多 Agent 比单 Agent 更容易
降级，所以这个口径**系统性地美化多 Agent**。

实测 2026-09-25（33 例）：多 Agent 25/27 = 92.6%，单 Agent 27/30 = 90.0%，
看数字是多 Agent 更优；但多 Agent 通过的**绝对例数更少**（25 < 27），
按同一个分母算是 25/30 = 83.3%，反而低 6.7pp。

本文件构造一批带弃权的样本，断言两个口径**会给出相反的结论** —— 只要有人把
严格口径删掉、或把它改回默认分母，这里就红。
"""

import eval_test


def _rec(judgment=None, expect_unknown=False, expected=None, false_degraded=None,
         final_root=""):
    """构造 aggregate() 需要的最小 record。"""
    return {
        "judgment": judgment,
        "hallucinated": None,
        "unknown_honest": None,
        "final_root": final_root,
        "exclusion_violated": None,
        "expect_unknown": expect_unknown,
        "expected_root_causes": expected or [],
        "false_degraded": false_degraded,
        "missing_nodes": [],
        "snapshot_diff": {"status": "skipped"},
    }


OK = {"core": True, "coverage": True}
MISS = {"core": True, "coverage": False}


def _multi_agent_like():
    """仿多 Agent：27 例判过（全通过）+ 3 例弃权 + 3 例知识库外。"""
    recs = [_rec(judgment=OK, expected=["a"]) for _ in range(27)]
    recs += [_rec(judgment=None, expected=["a"], false_degraded=True) for _ in range(3)]
    recs += [_rec(judgment=None, expect_unknown=True) for _ in range(3)]
    return recs


def _single_agent_like():
    """仿单 Agent：30 例全判过（27 通过 / 3 漏），从不弃权 + 3 例知识库外。"""
    recs = [_rec(judgment=OK, expected=["a"]) for _ in range(27)]
    recs += [_rec(judgment=MISS, expected=["a"]) for _ in range(3)]
    recs += [_rec(judgment=None, expect_unknown=True) for _ in range(3)]
    return recs


class TestAbstainStaysInDenominator:
    def test_default_rate_excludes_abstentions(self):
        """默认口径：分母只算判过的用例——这是现行行为，保留它是为了跟历史报告可比。"""
        m = eval_test.aggregate(_multi_agent_like())
        assert m["coverage_rate"]["total"] == 27
        assert m["coverage_rate"]["pass"] == 25 or m["coverage_rate"]["pass"] == 27

    def test_strict_rate_keeps_abstentions(self):
        """严格口径：分母 = 所有**本应能答**的用例，弃权不算通过。"""
        m = eval_test.aggregate(_multi_agent_like())
        assert m["coverage_rate_strict"]["total"] == 30, "分母必须是全部可答用例（含弃权）"
        assert m["coverage_rate_strict"]["abstained"] == 3, "必须报出弃权了几例"

    def test_two_metrics_can_disagree(self):
        """核心断言：同一批数据，两个口径给出**相反**的结论。

        多 Agent：默认 27/27 = 100% vs 单 Agent 27/30 = 90% → 多 Agent 赢
                  严格 27/30 = 90%  vs 单 Agent 27/30 = 90% → 持平
        （真实数据里多 Agent 还更低，因为它的弃权多于它多答对的。）
        """
        multi = eval_test.aggregate(_multi_agent_like())
        single = eval_test.aggregate(_single_agent_like())

        # 单 Agent 从不弃权 → 两个口径相同
        assert single["coverage_rate"]["rate"] == single["coverage_rate_strict"]["rate"]

        # 多 Agent 弃权 3 例 → 严格口径必须低于默认口径
        assert multi["coverage_rate_strict"]["rate"] < multi["coverage_rate"]["rate"], (
            "弃权被剔出分母时百分比必然虚高；严格口径必须更低"
        )
        # 默认口径下多 Agent 占优，严格口径下这个优势消失
        assert multi["coverage_rate"]["rate"] > single["coverage_rate"]["rate"]
        assert multi["coverage_rate_strict"]["rate"] <= single["coverage_rate_strict"]["rate"]

    def test_no_abstention_means_two_metrics_agree(self):
        """没有弃权时两个口径必须一致——否则说明实现里算错了。"""
        recs = [_rec(judgment=OK, expected=["a"]) for _ in range(5)]
        m = eval_test.aggregate(recs)
        assert m["coverage_rate_strict"] == {
            "pass": m["coverage_rate"]["pass"],
            "total": 5,
            "abstained": 0,
            "rate": m["coverage_rate"]["rate"],
        }

    def test_empty_records_do_not_crash(self):
        m = eval_test.aggregate([])
        assert m["coverage_rate_strict"]["rate"] is None
        assert m["coverage_rate_strict"]["abstained"] == 0


class TestAblationUsesSameDenominatorRule:
    """消融脚本自带一套汇总，口径必须跟 eval_test 对齐——否则两个报告的数字对不上。"""

    def test_summarize_exposes_strict_rate(self):
        import ablate_single_vs_multi as ablate

        recs = [
            {"expect_unknown": False, "expected_root_causes": ["a"],
             "single": {"judgment": OK, "hallucinated": None, "exclusion_violated": None,
                        "unknown_honest": None, "false_degraded": None, "ok": True},
             "multi": {"judgment": None, "hallucinated": None, "exclusion_violated": None,
                       "unknown_honest": None, "false_degraded": True, "ok": True}},
        ]
        s = ablate.summarize(recs, "single")
        m = ablate.summarize(recs, "multi")

        assert s["coverage_rate_strict"]["total"] == 1 and s["coverage_rate_strict"]["abstained"] == 0
        assert m["coverage_rate_strict"]["total"] == 1 and m["coverage_rate_strict"]["abstained"] == 1, (
            "多 Agent 这例弃权了，严格口径必须把它算进分母并记为未通过"
        )
        assert m["coverage_rate_strict"]["pass"] == 0
