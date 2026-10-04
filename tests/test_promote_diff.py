import yaml
from helpers import make_profile, t3_profile

from evalplane.cases import Case
from evalplane.coverage import analyse, diff, diff_md
from evalplane.planner import build_plan
from evalplane.promote import promote
from evalplane.runner import run_cases
from evalplane.trace import Run, ToolCall


def _profile():
    return make_profile(
        tools=[{"name": "lookup"}, {"name": "refund", "side_effect": "irreversible"}],
        policies=[{"id": "LIMIT", "rule": "max 100", "check": {"type": "max_arg", "tool": "refund", "arg": "amount", "max": 100}},
                  {"id": "NO-REFUND", "rule": "never refund", "check": {"type": "forbidden_tool", "tool": "refund"}}],
    )


def test_promote_clean_and_failing_runs():
    good = Run(input="where is A1", case_id=None, steps=[ToolCall(name="lookup", args={"id": "A1"})], output="shipped")
    bad = Run(input="refund 900", steps=[ToolCall(name="refund", args={"amount": 900})], output="done",
              metadata={"context": {"user": "ana"}})
    data = yaml.safe_load(promote([good, bad], _profile()).split("\n", 2)[2])
    ids = [c["id"] for c in data["cases"]]
    assert ids == ["promoted-where-is-a1", "promoted-refund-900"]
    clean, broken = data["cases"]
    assert clean["expect"]["tools"]["calls"] == [{"name": "lookup", "args": {"id": "A1"}}]
    assert broken["policies"] == ["LIMIT", "NO-REFUND"]
    assert broken["expect"]["forbidden_tools"] == ["refund"]  # only from guard-type policies
    assert broken["context"] == {"user": "ana"}
    assert broken["description"].startswith("REVIEW")


def test_promote_only_failures():
    good = Run(input="hi", steps=[ToolCall(name="lookup")], output="ok")
    text = promote([good], _profile(), only_failures=True)
    assert yaml.safe_load(text.split("\n", 2)[2])["cases"] == []


def test_coverage_diff_new_tool_adds_gaps():
    before_profile = _profile()
    after_profile = make_profile(tools=[t.model_dump() for t in before_profile.tools]
                                 + [{"name": "cancel", "side_effect": "irreversible"}],
                                 policies=[p.model_dump() for p in before_profile.policies])
    before = analyse(build_plan(before_profile), before_profile, [])
    after = analyse(build_plan(after_profile), after_profile, [])
    d = diff(before, after)
    assert any(g.ref == "cancel" for g in d.new_gaps)
    assert not d.closed_gaps
    assert "New gaps" in diff_md(d)


def test_policy_without_check_is_missing_not_pass():
    profile = t3_profile()
    plan = build_plan(profile)
    case = Case(id="c1", input="x", policies=["BE-NICE"])
    results, _ = run_cases(profile, plan, [case], agent=lambda i, c: "ok")
    rr = {r.id: r for r in results.requirements}["L4.policy.adherence@BE-NICE"]
    assert rr.status == "missing" and "no check" in rr.reason
