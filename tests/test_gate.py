import pytest
from helpers import make_profile, t1_profile, t3_profile

from evalplane.coverage import analyse
from evalplane.gate import evaluate_gate
from evalplane.models import Stage
from evalplane.planner import build_plan
from evalplane.runner import CaseResult, RequirementResult, Results, ViolationResult


def setup(profile, score=100.0):
    plan = build_plan(profile)
    cov = analyse(plan, profile, []).model_copy(update={"score": score})
    return plan, cov


def results_for(plan, statuses=None, default="pass", plan_hash=None, violations=()):
    statuses = statuses or {}
    reqs = [RequirementResult(id=r.id, priority=r.priority, metric=r.metric, status=statuses.get(r.id, default),
                              reason="because" if statuses.get(r.id) else "")
            for r in plan.requirements]
    return Results(agent=plan.agent, tier=plan.tier.value, plan_hash=plan_hash or plan.plan_hash, started_at="t",
                   requirements=reqs, violations=list(violations))


def check(g, cid):
    return next(c for c in g.checks if c.id == cid)


def test_all_pass():
    plan, cov = setup(t3_profile())
    g = evaluate_gate(plan, cov, Stage.ci, results_for(plan))
    assert g.verdict == "PASS" and g.exit_code == 0
    assert g.counts()["FAIL"] == 0 and g.counts()["PASS"] == len(g.checks)
    assert (g.agent, g.stage, g.tier, g.plan_hash) == ("test-agent", "ci", "T3", plan.plan_hash)


def test_t1_missing_must_warns_t3_fails():
    plan1, cov1 = setup(t1_profile())
    g1 = evaluate_gate(plan1, cov1, Stage.ci, results_for(plan1, {"L4.task.success": "missing"}))
    assert check(g1, "L4.task.success").verdict == "WARN" and g1.verdict == "WARN" and g1.exit_code == 0
    plan3, cov3 = setup(t3_profile())
    g3 = evaluate_gate(plan3, cov3, Stage.ci, results_for(plan3, {"L4.task.success": "missing"}))
    assert check(g3, "L4.task.success").verdict == "FAIL" and g3.verdict == "FAIL" and g3.exit_code == 1
    assert check(g3, "L4.task.success").detail == "missing: because"


def test_t1_missing_fails_at_pre_release():
    plan, cov = setup(t1_profile())
    g = evaluate_gate(plan, cov, Stage.pre_release, results_for(plan, {"L4.task.success": "missing"}))
    assert check(g, "L4.task.success").verdict == "FAIL"


def test_requirement_absent_from_results_counts_as_missing():
    plan, cov = setup(t3_profile())
    res = results_for(plan)
    res.requirements = [r for r in res.requirements if r.id != "L4.task.success"]
    assert check(evaluate_gate(plan, cov, Stage.ci, res), "L4.task.success").verdict == "FAIL"


@pytest.mark.parametrize("status, verdict", [("fail", "FAIL"), ("waived", "PASS"), ("skip", "WARN"),
                                             ("pass", "PASS")])
def test_must_statuses(status, verdict):
    plan, cov = setup(t3_profile())
    g = evaluate_gate(plan, cov, Stage.ci, results_for(plan, {"L4.task.success": status}))
    assert check(g, "L4.task.success").verdict == verdict


@pytest.mark.parametrize("status, verdict", [("missing", "WARN"), ("fail", "WARN"), ("waived", "PASS")])
def test_should_never_fails(status, verdict):
    plan, cov = setup(t3_profile())
    should = next(r for r in plan.requirements if r.priority == "should" and r.stage <= Stage.ci)
    g = evaluate_gate(plan, cov, Stage.ci, results_for(plan, {should.id: status}))
    assert check(g, should.id).verdict == verdict


def test_stale_plan_hash():
    plan, cov = setup(t3_profile())
    res = results_for(plan, plan_hash="0" * 16)
    g = evaluate_gate(plan, cov, Stage.ci, res)
    assert check(g, "results.fresh").verdict == "FAIL" and g.verdict == "FAIL"
    g2 = evaluate_gate(plan, cov, Stage.ci, res, allow_stale=True)
    assert check(g2, "results.fresh").verdict == "WARN" and g2.verdict == "WARN"


@pytest.mark.parametrize("stage, score, verdict", [
    (Stage.ci, 69.9, "FAIL"), (Stage.ci, 70, "PASS"),
    (Stage.pre_release, 84.9, "FAIL"), (Stage.pre_release, 85, "PASS"),
])
def test_coverage_floor_t3(stage, score, verdict):
    plan, cov = setup(t3_profile(), score=score)
    c = check(evaluate_gate(plan, cov, stage, results_for(plan)), "coverage.floor")
    assert c.verdict == verdict
    assert "T3" in c.detail


def test_coverage_floor_t1_ci():
    plan, cov = setup(t1_profile(), score=30)
    assert check(evaluate_gate(plan, cov, Stage.ci, results_for(plan)), "coverage.floor").verdict == "PASS"


def test_no_results_fails_outside_design():
    plan, cov = setup(t3_profile())
    g = evaluate_gate(plan, cov, Stage.ci, None)
    assert check(g, "results.present").verdict == "FAIL" and g.verdict == "FAIL"


def test_design_stage():
    # T3 profile has an uncheckable policy (BE-NICE): design gate FAILs for T3, WARNs for T1
    plan, cov = setup(t3_profile(), score=0)
    g = evaluate_gate(plan, cov, Stage.design, None)
    ids = [c.id for c in g.checks]
    assert ids == ["design.policies_checkable"] and g.verdict == "FAIL"
    t1 = make_profile(tools=[{"name": "r"}], policies=[{"id": "SOFT", "rule": "x"}])
    plan1, cov1 = setup(t1, score=0)
    assert evaluate_gate(plan1, cov1, Stage.design, None).verdict == "WARN"
    ok = make_profile(tools=[{"name": "r"}])
    plan2, cov2 = setup(ok, score=0)
    assert evaluate_gate(plan2, cov2, Stage.design, None).verdict == "PASS"
    # with results, only design-stage requirements are gated
    g3 = evaluate_gate(plan, cov, Stage.design, results_for(plan, default="missing"))
    req_ids = {c.id for c in g3.checks if c.kind == "requirement"}
    assert req_ids and all(plan.get(i).stage == Stage.design for i in req_ids)


def test_requirements_filtered_by_stage():
    plan, cov = setup(t3_profile())
    ci = {c.id for c in evaluate_gate(plan, cov, Stage.ci, results_for(plan)).checks}
    pre = {c.id for c in evaluate_gate(plan, cov, Stage.pre_release, results_for(plan)).checks}
    assert "L0.model.acceptance" not in ci and "L0.model.acceptance" in pre
    assert "L4.task.success" in ci


def test_violations_block_when_strict():
    v = [ViolationResult(policy="SCOPE", rule="r", detail="undeclared tool 'x' called", run_id="r1", case_id="c"),
         ViolationResult(policy="LOOP", rule="r", detail="loop", run_id="r1"),
         ViolationResult(policy="VERIFY", rule="r", detail="policy", run_id="r1")]
    plan3, cov3 = setup(t3_profile())
    g3 = evaluate_gate(plan3, cov3, Stage.ci, results_for(plan3, violations=v))
    assert check(g3, "violation.SCOPE").verdict == "FAIL" and "(case c)" in check(g3, "violation.SCOPE").detail
    assert check(g3, "violation.LOOP").verdict == "FAIL"
    assert not any(c.id == "violation.VERIFY" for c in g3.checks)
    plan1, cov1 = setup(t1_profile())
    g1 = evaluate_gate(plan1, cov1, Stage.ci, results_for(plan1, violations=v))
    assert check(g1, "violation.SCOPE").verdict == "WARN" and g1.verdict == "WARN"


def _refund_profile(side_effect="irreversible"):
    return make_profile(
        tools=[{"name": "issue_refund", "side_effect": side_effect}],
        policies=[{"id": "LIMIT", "rule": "never refund more than 100",
                   "check": {"type": "max_arg", "tool": "issue_refund", "arg": "amount", "max": 100}}],
)


def test_a_violation_the_tool_refused_warns_instead_of_failing():
    """"Tried to refund $900 and was declined" is not "refunded $900". Still a defect, still reported —
    but failing a release over damage that provably did not occur is how a gate gets switched off.

    Measured on τ²-bench: 3 of 106 airline violations were blocked attempts, so this is a precision fix,
    not a loophole. The exception is an irreversible tool at T4, where trying is enough.
    """
    import evalplane as ep
    from evalplane.runner import Results, aggregate
    from evalplane.scorers import global_violations

    def grade(status, side_effect):
        profile = _refund_profile(side_effect)
        run = ep.Run(steps=[ep.ToolCall(name="issue_refund", args={"amount": 900}, status=status)])
        plan = build_plan(profile)
        res = Results(agent=profile.agent.name, plan_hash=plan.plan_hash, tier=plan.tier.value,
                      started_at="2026-10-02T00:00:00Z",
                      cases=[CaseResult(case_id="c", suite="s", status="fail", n=1, c=0)],
                      violations=[ViolationResult(**v.__dict__) for v in global_violations(run, profile)])
        reqs = aggregate(plan, res, profile)
        return next(r for r in reqs if r.id == "L4.policy.adherence@LIMIT")

    # a write tool (T3): the blocked attempt warns and says why
    warned = grade("error", "write")
    assert warned.status == "warn" and "blocked by the tool" in warned.reason
    # the same call that went through still fails
    assert grade("ok", "write").status == "fail"
    # irreversible at T4: trying to move money is enough
    assert grade("error", "irreversible").status == "fail"
