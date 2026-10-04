"""Release gates: may this agent version ship at this stage, given its risk tier?

Pure function of (plan, results, coverage, stage): no I/O, no clock.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .coverage import CoverageReport
from .models import Stage, Tier
from .planner import Plan, load_thresholds
from .runner import Results


class GateCheck(BaseModel):
    id: str
    kind: str  # requirement | coverage | staleness | design
    verdict: str  # PASS | WARN | FAIL
    detail: str = ""


class GateResult(BaseModel):
    agent: str
    stage: str
    tier: str
    verdict: str
    plan_hash: str
    checks: list[GateCheck] = Field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.verdict == "FAIL" else 0

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "WARN": 0, "FAIL": 0}
        for c in self.checks:
            out[c.verdict] += 1
        return out


def evaluate_gate(plan: Plan, coverage: CoverageReport, stage: Stage, results: Results | None = None,
                  allow_stale: bool = False, calibration: dict | None = None) -> GateResult:
    tier = plan.tier
    strict = tier >= Tier.T3 or stage >= Stage.pre_release
    checks: list[GateCheck] = []

    # staleness: results must come from the current plan
    if results is not None and results.plan_hash != plan.plan_hash:
        checks.append(GateCheck(id="results.fresh", kind="staleness", verdict="WARN" if allow_stale else "FAIL",
                                detail="results were produced for a different agent.eval.yaml; re-run `evalplane run`"))

    # coverage floor
    floors = load_thresholds()["coverage_floor"]
    floor_stage = "pre_release" if stage >= Stage.pre_release else "ci"
    floor = floors[floor_stage][tier.value]
    ok = coverage.score >= floor
    if stage != Stage.design:
        checks.append(GateCheck(id="coverage.floor", kind="coverage", verdict="PASS" if ok else "FAIL",
                                detail=f"coverage {coverage.score:g} {'>=' if ok else '<'} floor {floor} "
                                       f"({tier.value}, {floor_stage})"))

    # design gate: static checks only
    if stage == Stage.design:
        from_profile = [
            ("design.policies_checkable", all(g.kind != "UNTESTABLE_POLICY" for g in coverage.gaps),
             "every policy has a check"),
        ]
        for cid, passed, detail in from_profile:
            checks.append(GateCheck(id=cid, kind="design", verdict="PASS" if passed else ("FAIL" if strict else "WARN"),
                                    detail=detail))

    # requirements in scope: stage <= requested stage (production-only ones are never gated here)
    stale = results is not None and results.plan_hash != plan.plan_hash
    if stale and not allow_stale:
        pass  # old results say nothing about the current plan: only report staleness
    elif results is not None:
        by_id = {r.id: r for r in results.requirements}
        for req in plan.requirements:
            if req.stage > stage or req.stage == Stage.production:
                continue
            rr = by_id.get(req.id)
            status = rr.status if rr else "missing"
            if req.priority == "should":
                verdict = "PASS" if status in ("pass", "waived") else "WARN"
            elif status in ("pass", "waived"):
                verdict = "PASS"
            elif status == "fail":
                verdict = "FAIL"
            elif status in ("missing", "unverified"):
                verdict = "FAIL" if strict else "WARN"
            else:  # skip, or warn (a violation whose every attempt the tool refused)
                verdict = "WARN"
            detail = status + (f": {rr.reason}" if rr and rr.reason else "")
            if rr and (note := rr.threshold_note()):
                detail += f" [{note}]"   # don't let a decimal threshold imply precision n can't support
            checks.append(GateCheck(id=req.id, kind="requirement", verdict=verdict, detail=detail))
        seen: set[tuple[str, str]] = set()
        for v in results.violations:
            key = (v.policy, f"{v.detail} (case {v.case_id})")
            if v.policy in ("SCOPE", "ARGS", "LOOP") and key not in seen:
                seen.add(key)
                checks.append(GateCheck(id=f"violation.{v.policy}", kind="requirement",
                                        verdict="FAIL" if strict else "WARN", detail=key[1]))
    elif stage != Stage.design:
        checks.append(GateCheck(id="results.present", kind="staleness", verdict="FAIL",
                                detail="no results: run `evalplane run` first"))

    # judge-graded requirements at T3/T4 need a calibrated judge (`evalplane calibrate`)
    if results is not None and tier >= Tier.T3:
        judged = [r for r in results.requirements if r.status in ("pass", "fail")
                  and (plan.get(r.id) is not None and plan.get(r.id).method.value == "judge")]
        llm_judge_ran = any(ch.name == "judge" and ch.status in ("pass", "fail")
                            for c in results.cases for a in c.attempts for ch in a.checks)
        if judged and llm_judge_ran:
            kappa = (calibration or {}).get("kappa")
            ok = kappa is not None and kappa >= 0.6
            checks.append(GateCheck(id="judge.calibrated", kind="requirement", verdict="PASS" if ok else "WARN",
                                    detail=f"judge agreement with your labels: kappa {kappa}" if kappa is not None
                                    else "judge-graded results, but the judge is not calibrated (evalplane calibrate)"))

    verdict = "FAIL" if any(c.verdict == "FAIL" for c in checks) else \
        "WARN" if any(c.verdict == "WARN" for c in checks) else "PASS"
    return GateResult(agent=plan.agent, stage=stage.value, tier=tier.value, verdict=verdict,
                      plan_hash=plan.plan_hash, checks=checks)
