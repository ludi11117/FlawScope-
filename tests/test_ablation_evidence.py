"""对照实验不能把复议后的证据提前交给单 Agent。"""

import ablate_single_vs_multi as ablate


def test_single_agent_receives_initial_evidence(monkeypatch):
    seen = []
    monkeypatch.setattr(ablate, "multi_agent_run", lambda _: {
        "ok": True, "reason": "", "root": "终诊", "initial_root": "初诊",
        "initial_evidence": "初始证据", "evidence": "复议后证据", "rounds": 1,
    })

    def single_run(_, evidence):
        seen.append(evidence)
        return {"ok": True, "reason": "", "root": "单诊", "evidence": evidence}

    monkeypatch.setattr(ablate, "single_agent_run", single_run)
    monkeypatch.setattr(ablate, "score_one", lambda *args: {
        "judgment": None, "degraded": False, "hallucinated": False,
        "exclusion_violated": None,
    })

    records = ablate.run_once([{"id": "T1", "fault_description": "故障"}], None)

    assert seen == ["初始证据"]
    assert records[0]["multi"]["rounds"] == 1
