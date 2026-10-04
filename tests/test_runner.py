import json
from math import comb

import pytest
from helpers import make_profile, mk_case, tc

import evalplane as ep
from evalplane.errors import ConfigError
from evalplane.models import Stage
from evalplane.planner import build_plan
from evalplane.runner import (
    CaseResult,
    audit_traces,
    load_agent,
    load_results,
    load_trace_paths,
    run_cases,
    save_results,
)
from evalplane.trace import Run, write_runs


@ep.tool
def lookup(order_id):
    return {"order_id": order_id, "total": 40}


@ep.tool
def refund(order_id, amount):
    return {"ok": True}


def tiny_agent(user_input, context):
    order = lookup("A1")
    if "refund" in user_input:
        refund("A1", order["total"])
        context["state"]["refunded"] = order["total"]
    return f"done: {user_input}"


def flaky_agent(user_input, context):
    lookup("A1")
    return "ok" if context["attempt"] % 2 == 0 else "bad"


def crashing_agent(user_input, context):
    lookup("A1")
    raise RuntimeError("kaboom")


def profile(**extra):
    return make_profile(
        tools=[{"name": "lookup"}, {"name": "refund", "side_effect": "write"}],
        policies=[{"id": "NO-REFUND", "rule": "never refund", "check": {"type": "forbidden_tool", "tool": "refund"}}],
        **extra,
    )


def run(p, cases, **kw):
    return run_cases(p, build_plan(p), cases, **kw)


def case_by_id(results, cid):
    return next(c for c in results.cases if c.case_id == cid)


def req(results, rid):
    return next(r for r in results.requirements if r.id == rid)


def test_live_run_with_tiny_agent():
    p = profile()
    c = mk_case("status", input="where is it", expect={"tools": ["lookup"], "forbidden_tools": ["refund"],
                                                       "output": {"contains": "done"}})
    results, runs = run(p, [c], agent=tiny_agent)
    cr = case_by_id(results, "status")
    assert (cr.status, cr.n, cr.c) == ("pass", 1, 1)
    assert runs[0].tool_names() == ["lookup"] and runs[0].output == "done: where is it"
    assert runs[0].metadata["source"] == "live" and runs[0].case_id == "status" and runs[0].latency_ms >= 0
    assert results.agent == "test-agent" and results.plan_hash == build_plan(p).plan_hash
    assert results.finished_at and results.summary["passed"] == 1


def test_live_final_state_and_context_isolation():
    p = profile()
    ctx = {"state": {"orders": 1}}
    c = mk_case("refund", input="refund please", context=ctx,
                expect={"final_state": [{"path": "refunded", "equals": 40}]}, repeat=2)
    results, runs = run(p, [c], agent=tiny_agent)
    assert runs[0].final_state == {"orders": 1, "refunded": 40}
    assert ctx == {"state": {"orders": 1}}  # the case's context is deep-copied per attempt
    cr = case_by_id(results, "refund")
    assert cr.status == "fail"  # NO-REFUND is violated on every attempt
    assert "policy[NO-REFUND]" in cr.reason
    assert len(results.violations) == 2


def test_agent_returning_dict_or_run():
    p = make_profile(tools=[{"name": "lookup"}])

    def dict_agent(i, ctx):
        lookup("A1")
        return {"output": "hi", "final_state": {"x": 1}}

    def run_agent(i, ctx):
        lookup("A1")
        return Run(output="from run", metadata={"k": "v"})

    c = mk_case("d", expect={"output": {"equals": "hi"}, "final_state": [{"path": "x", "equals": 1}]})
    results, runs = run(p, [c], agent=dict_agent)
    assert case_by_id(results, "d").status == "pass" and runs[0].final_state == {"x": 1}
    results, runs = run(p, [mk_case("r", expect={"tools": ["lookup"]})], agent=run_agent)
    assert runs[0].output == "from run" and runs[0].case_id == "r" and runs[0].tool_names() == ["lookup"]
    assert case_by_id(results, "r").status == "pass"


def test_replay_from_traces_by_case_id():
    p = profile()
    traces = [Run(case_id="a", output="done", steps=[tc("lookup")]),
              Run(case_id="a", output="second"),
              Run(case_id="other", output="x"),
              Run(output="no case id")]
    c = mk_case("a", expect={"output": {"contains": "done"}})
    called = []
    results, runs = run(p, [c], traces=traces, agent=lambda *a: called.append(a))
    assert not called  # a recorded trace wins over calling the agent
    cr = case_by_id(results, "a")
    assert (cr.status, cr.n) == ("pass", 1)
    results, _ = run(p, [c.model_copy(update={"repeat": 5})], traces=traces)
    assert case_by_id(results, "a").n == 2  # only as many runs as were recorded


def test_case_trace_file_replay(tmp_path):
    p = profile()
    p._base_dir = tmp_path
    write_runs(tmp_path / "t.jsonl", [Run(output="alpha"), Run(case_id="mine", output="beta"),
                                      Run(case_id="someone-else", output="gamma")])
    c = mk_case("mine", trace="t.jsonl", repeat=5, expect={"output": {"not_contains": "gamma"}})
    results, runs = run(p, [c])
    cr = case_by_id(results, "mine")
    assert (cr.status, cr.n) == ("pass", 2)
    assert [r.output for r in runs] == ["alpha", "beta"] and {r.case_id for r in runs} == {"mine"}
    abs_case = mk_case("mine", trace=str(tmp_path / "t.jsonl"))
    assert case_by_id(run(profile(), [abs_case])[0], "mine").n == 1


def test_skip_without_agent_or_trace():
    results, runs = run(profile(), [mk_case("lonely", expect={"tools": ["lookup"]})])
    cr = case_by_id(results, "lonely")
    assert cr.status == "skip" and cr.n == 0 and "no agent" in cr.reason
    assert "L3.tool.selection@lookup" in cr.covers
    assert runs == [] and results.summary["skipped"] == 1


def test_agent_exception_is_a_failed_attempt():
    results, runs = run(profile(), [mk_case("boom")], agent=crashing_agent)
    cr = case_by_id(results, "boom")
    assert cr.status == "error" and cr.c == 0 and not cr.attempts[0].passed
    assert "RuntimeError: kaboom" in cr.reason
    assert runs[0].status == "error" and runs[0].tool_names() == ["lookup"]
    ok = run(profile(), [mk_case("boom", expect={"status": "error"})], agent=crashing_agent)[0]
    assert case_by_id(ok, "boom").status == "pass"


def test_error_status_when_every_attempt_errors():
    c = mk_case("py")

    def broken(run):
        raise KeyError("x")

    c.fn = broken
    results, _ = run(profile(), [c], agent=tiny_agent)
    assert case_by_id(results, "py").status == "error"


def test_repeat_and_pass_hat_k():
    c = mk_case("flaky", repeat=5, expect={"output": {"equals": "ok"}})
    results, runs = run(profile(), [c], agent=flaky_agent)
    cr = case_by_id(results, "flaky")
    assert (cr.n, cr.c, cr.status) == (5, 3, "fail")
    assert [a.passed for a in cr.attempts] == [True, False, True, False, True]
    assert [r.attempt for r in runs] == [0, 1, 2, 3, 4]
    assert cr.pass_rate == pytest.approx(0.6)
    assert cr.pass_hat_k(1) == pytest.approx(0.6)
    assert cr.pass_hat_k(2) == pytest.approx(comb(3, 2) / comb(5, 2)) == pytest.approx(0.3)
    assert cr.pass_hat_k(3) == pytest.approx(0.1)
    assert cr.pass_hat_k(4) == 0 and cr.pass_hat_k(6) is None
    # T3 plan: k=5 -> pass^5 = 0 -> fail
    pk = req(results, "L4.reliability.pass_k")
    assert pk.status == "fail" and pk.value == 0.0 and pk.threshold == pytest.approx(0.8)


def test_pass_hat_k_aggregate_t2_and_missing_when_too_few_repeats():
    t2 = make_profile(tools=[{"name": "lookup"}, {"name": "refund", "side_effect": "write",
                                                  "requires_approval": True}])
    c = mk_case("flaky", repeat=5, expect={"output": {"equals": "ok"}})
    results, _ = run(t2, [c], agent=flaky_agent)
    pk = req(results, "L4.reliability.pass_k")
    assert pk.priority == "should" and pk.value == pytest.approx(0.1) and pk.status == "fail"
    few, _ = run(profile(), [c.model_copy(update={"repeat": 2})], agent=flaky_agent)
    pk = req(few, "L4.reliability.pass_k")
    assert pk.status == "missing" and "repeat >= 5" in pk.reason


def test_repeat_override():
    c = mk_case("flaky", expect={"output": {"equals": "ok"}})
    results, _ = run(profile(), [c], agent=flaky_agent, repeat=4)
    assert case_by_id(results, "flaky").n == 4 and case_by_id(results, "flaky").c == 2


def test_stage_filter():
    later = mk_case("later", stage="pre_release")
    results, _ = run(profile(), [mk_case("now"), later], agent=tiny_agent, stage=Stage.ci)
    assert [c.case_id for c in results.cases] == ["now"] and results.stage == "ci"


def test_aggregation_statuses():
    p = profile(
        attestations=[{"requirement": "L7.governance.recertify", "by": "me", "date": "2026-01-01"}],
        waivers=[{"requirement": "L3.tool.guard@refund", "reason": "later", "approved_by": "lead"}],
    )
    cases = [
        mk_case("golden", expect={"tools": ["lookup"], "output": {"contains": "done"}}),
        mk_case("violator", policies=["NO-REFUND"], expect={"tools": ["refund"]}),
        mk_case("skipped", covers=["L0.model.acceptance"], expect={"output": {"contains": "x"}}),
    ]
    traces = [Run(case_id="golden", output="done", steps=[tc("lookup")]),
              Run(case_id="violator", output="x", steps=[tc("refund")])]
    results, _ = run(p, cases, traces=traces)
    assert req(results, "L7.governance.recertify").status == "pass"
    missing_att = req(results, "L6.outcome.defined")
    assert missing_att.status == "missing" and missing_att.reason == "no attestation"
    assert req(results, "L3.tool.guard@refund").status == "waived"
    assert req(results, "L0.model.acceptance").status == "skip"
    ts = req(results, "L4.task.success")
    assert (ts.status, ts.value, ts.cases) == ("pass", 1.0, ["golden"])
    sel = req(results, "L3.tool.selection@refund")
    # the agent picked the right tool; the failure is the policy's, not tool selection's
    assert sel.status == "pass" and sel.value == 1.0
    pol = req(results, "L4.policy.adherence@NO-REFUND")
    assert (pol.status, pol.value, pol.metric) == ("fail", 1.0, "violations")
    assert pol.cases == ["violator"] and pol.reason == "1 violation(s)"
    scope = req(results, "L4.safety.scope")
    assert scope.status == "pass" and scope.value == 0.0
    assert req(results, "L3.tool.error_recovery").status == "missing"
    assert results.summary == {"cases": 3, "passed": 1, "failed": 1, "skipped": 1, "attempts": 2, "violations": 1}


def test_violation_requirements_missing_without_runs():
    results, _ = run(profile(), [mk_case("skipped")])
    assert req(results, "L4.safety.scope").status == "missing"
    assert req(results, "L4.policy.adherence@NO-REFUND").reason == "no runs"


def test_save_and_load_results(tmp_path):
    results, runs = run(profile(), [mk_case("status", expect={"tools": ["lookup"]})], agent=tiny_agent)
    out = save_results(results, runs, tmp_path / ".evalplane")
    assert out.exists() and out.parent.name == "results" and results.plan_hash[:8] in out.name
    assert (tmp_path / ".evalplane" / ".gitignore").read_text() == "*\n"
    latest = tmp_path / ".evalplane" / "latest.json"
    assert json.loads(latest.read_text()) == json.loads(out.read_text())
    loaded = load_results(latest)
    assert loaded.model_dump() == results.model_dump()
    assert loaded.runs_file and len(load_trace_paths(profile(), [loaded.runs_file])) == 1


def test_case_result_pass_rate_none_without_runs():
    assert CaseResult(case_id="x", suite="s", status="skip").pass_rate is None


def test_load_agent(tmp_path):
    (tmp_path / "evalplane_test_agent_mod.py").write_text("def go(i, ctx):\n    return 'hi'\nNOT_CALLABLE = 1\n")
    fn = load_agent("evalplane_test_agent_mod:go", tmp_path)
    assert fn("x", {}) == "hi"
    with pytest.raises(ConfigError, match="module:function"):
        load_agent("no_colon")
    with pytest.raises(ConfigError, match="cannot import"):
        load_agent("definitely_not_a_module_xyz:go", tmp_path)
    with pytest.raises(ConfigError, match="no callable"):
        load_agent("evalplane_test_agent_mod:NOT_CALLABLE", tmp_path)


def test_audit_traces():
    runs = [Run(steps=[tc("refund")]), Run(steps=[tc("lookup")]), Run(steps=[tc("ghost")])]
    v = audit_traces(profile(), runs)
    assert [x.policy for x in v] == ["NO-REFUND", "SCOPE"]


def test_a_threshold_below_its_resolving_power_is_just_all_must_pass():
    """With n cases the best rate under 1.0 is (n-1)/n, so 0.98 needs 50 cases to mean anything.

    On the support-refund example 19 of 28 rate-metric requirements are decided by too few cases for their
    threshold to differ from "no failures allowed". Publishing T1 0.85 vs T4 0.98 on top of n=1 implies a
    gradation the data cannot carry, so every such line says so.
    """
    from evalplane.runner import RequirementResult, resolving_power

    assert [resolving_power(t) for t in (0.85, 0.90, 0.95, 0.98)] == [7, 11, 20, 50]
    assert resolving_power(1.0) == 1

    def req(threshold, n, metric="pass_rate"):
        return RequirementResult(id="r", priority="must", status="fail", metric=metric,
                                 threshold=threshold, cases=[f"c{i}" for i in range(n)])

    weak = req(0.98, 1)
    assert not weak.threshold_is_meaningful
    assert "acts as all-must-pass (needs 50)" in weak.threshold_note()
    assert req(0.85, 7).threshold_is_meaningful and req(0.85, 7).threshold_note() == ""

    # 1.0 is already honest, and a violation count is not a proportion
    assert req(1.0, 1).threshold_is_meaningful
    assert req(0.0, 0, metric="violations").threshold_is_meaningful
    assert req(0.98, 1, metric="attested").threshold_is_meaningful
