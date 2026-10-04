import pytest
import yaml
from helpers import make_profile, mk_case, t3_profile, tc

from evalplane.coverage import analyse, covered_ids, suggest
from evalplane.planner import build_plan
from evalplane.trace import Run


def cov_for(profile, cases, traces=None):
    return analyse(build_plan(profile), profile, cases, traces)


def uncheckable_t3():
    return make_profile(tools=[{"name": "lookup"}, {"name": "update", "side_effect": "write"}],
                        policies=[{"id": "BE-NICE", "rule": "Be nice"}])


def test_empty_suite_is_zero_with_gaps_must_first():
    cov = cov_for(uncheckable_t3(), [])
    assert cov.score == 0.0
    assert all(d.score in (0.0, None) for d in cov.dimensions.values())
    assert cov.covered == []
    prios = [g.priority for g in cov.gaps]
    assert prios == sorted(prios, key=lambda p: {"must": 0, "should": 1}[p])
    assert prios[0] == "must" and "should" in prios
    kinds = {g.kind for g in cov.gaps}
    assert {"TOOL_UNTESTED", "TOOL_NO_NEGATIVE", "POLICY_UNTESTED", "UNTESTABLE_POLICY", "REQUIREMENT_UNCOVERED",
            "LAYER_EMPTY", "OWASP_UNADDRESSED"} <= kinds
    # riskiest first: safety requirements (e.g. injection) and guards on the most powerful tools
    assert cov.gaps[0].kind in ("TOOL_NO_NEGATIVE", "REQUIREMENT_UNCOVERED") and cov.gaps[0].weight >= 3
    # tool-level requirements are reported as tool gaps, not requirement gaps
    assert not any(g.kind == "REQUIREMENT_UNCOVERED" and g.ref.startswith("L3.tool.selection") for g in cov.gaps)


def test_empty_profile_without_tools():
    cov = cov_for(make_profile(), [])
    assert cov.dimensions["tools"].score is None and cov.dimensions["policies"].score is None
    assert cov.score == 0.0


@pytest.mark.parametrize("cases, score", [
    ([], 0.0),
    ([mk_case(expect={"tools": ["w"]})], 0.5),
    ([mk_case(expect={"forbidden_tools": ["w"]})], 0.5),
    ([mk_case("a", expect={"tools": ["w"]}), mk_case("b", expect={"forbidden_tools": ["w"]})], 1.0),
])
def test_action_tool_needs_positive_and_negative(cases, score):
    cov = cov_for(make_profile(tools=[{"name": "w", "side_effect": "write"}]), cases)
    assert cov.dimensions["tools"].score == pytest.approx(score)


def test_read_tool_positive_is_full_credit_and_tier_weighting():
    p = make_profile(tools=[{"name": "r"}, {"name": "w", "side_effect": "write"}])
    cov = cov_for(p, [mk_case(expect={"tools": ["r"]})])
    d = cov.dimensions["tools"]
    assert (d.covered, d.total) == (1.0, 4.0)  # r: T1 weight 1 (full), w: T3 weight 3 (none)
    gaps = {(g.kind, g.ref): g.priority for g in cov.gaps if g.kind.startswith("TOOL")}
    assert gaps == {("TOOL_UNTESTED", "w"): "must", ("TOOL_NO_NEGATIVE", "w"): "must"}


def test_policy_half_credit_for_check_only():
    p = make_profile(tools=[{"name": "w", "side_effect": "write"}], policies=[
        {"id": "P-CRIT", "rule": "x", "severity": "critical", "check": {"type": "forbidden_tool", "tool": "w"}},
        {"id": "P-LOW", "rule": "y", "severity": "low"},
    ])
    none = cov_for(p, []).dimensions["policies"]
    assert (none.covered, none.total) == (2.5, 6.0)
    both = cov_for(p, [mk_case(policies=["P-CRIT", "P-LOW"])])
    assert both.dimensions["policies"].covered == 5.5
    assert [g.ref for g in both.gaps if g.kind == "UNTESTABLE_POLICY"] == ["P-LOW"]
    assert not any(g.kind == "POLICY_UNTESTED" for g in both.gaps)
    # the adherence requirement only counts as covered when the policy has a check
    assert "L4.policy.adherence@P-CRIT" in both.covered
    assert "L4.policy.adherence@P-LOW" not in both.covered


def test_owasp_applicable_and_addressed():
    p = uncheckable_t3()
    plan = build_plan(p)
    cov = analyse(plan, p, [])
    assert cov.dimensions["owasp"].total == len(plan.owasp_applicable) > 0
    guard = analyse(plan, p, [mk_case(expect={"forbidden_tools": ["update"]})])
    assert "ASI02" not in {g.ref for g in guard.gaps if g.kind == "OWASP_UNADDRESSED"}
    base = analyse(plan, p, [mk_case()]).dimensions["owasp"].covered
    tagged = analyse(plan, p, [mk_case(owasp=["ASI08"])]).dimensions["owasp"].covered
    assert tagged == base + 1  # a case can address an OWASP risk directly via `owasp:`


def test_layer_matrix():
    p = uncheckable_t3()
    plan = build_plan(p)
    cov = analyse(plan, p, [mk_case(expect={"output": {"contains": "x"}})])
    must = [r for r in plan.requirements if r.priority == "must" and not r.waived]
    for layer, (c, t) in cov.layer_matrix.items():
        assert t == sum(1 for r in must if r.layer.value == layer)
        assert c == sum(1 for r in must if r.layer.value == layer and r.id in cov.covered)
    assert cov.layer_matrix["L4"][0] >= 2  # task success + scope
    assert list(cov.layer_matrix) == sorted(cov.layer_matrix)
    empty = {g.ref for g in cov.gaps if g.kind == "LAYER_EMPTY"}
    assert "L4" not in empty and "L3" in empty


def test_covered_ids_rules():
    p = t3_profile(attestations=[{"requirement": "L7.governance.recertify", "by": "me", "date": "2026-01-01"}])
    plan = build_plan(p)
    assert covered_ids(plan, p, []) == {"L7.governance.recertify"}
    ids = covered_ids(plan, p, [mk_case(covers=["L7.governance.kill_switch", "L4.policy.adherence@BE-NICE"])])
    assert "L4.safety.scope" in ids  # automatic once any case exists
    assert "L7.governance.kill_switch" not in ids  # attested requirements need an attestation, not a case
    assert "L4.policy.adherence@BE-NICE" not in ids  # no check


def test_human_requirements_count_half():
    p = uncheckable_t3()
    plan = build_plan(p)
    must = [r for r in plan.requirements if r.priority == "must" and not r.waived]
    expected = sum(0.5 if r.method.value == "human" else 1.0 for r in must)
    assert analyse(plan, p, []).dimensions["requirements"].total == expected


def test_waived_and_uncovered():
    p = make_profile(tools=[{"name": "update", "side_effect": "write"}],
                     waivers=[{"requirement": "L0.*", "reason": "r", "approved_by": "x"}])
    cov = cov_for(p, [])
    assert cov.waived == ["L0.model.acceptance"]
    assert "L0.model.acceptance" not in cov.uncovered and "L4.task.success" in cov.uncovered
    assert not any(g.ref == "L0.model.acceptance" for g in cov.gaps)


def test_tools_seen_untested():
    p = make_profile(tools=[{"name": "a"}, {"name": "b"}])
    cov = cov_for(p, [mk_case(expect={"tools": ["a"]})], traces=[Run(steps=[tc("a"), tc("b"), tc("zz")])])
    assert cov.tools_seen_untested == ["b", "zz"]


def test_score_improves_with_cases():
    p = make_profile(tools=[{"name": "r"}])
    partial = cov_for(p, [mk_case(expect={"tools": ["r"], "output": {"contains": "x"}})])
    assert partial.score == 100.0  # OWASP now counts only risks from must requirements
    cov = cov_for(p, [mk_case(expect={"tools": ["r"], "output": {"contains": "x"}, "budgets": {"max_steps": 5}})])
    assert cov.score == 100.0


def _claims_yaml(s: str) -> bool:
    return s.startswith(("- ", "attestations:", "success_criteria:"))


def test_stub_suggestions_are_valid_yaml():
    p = make_profile(
        tools=[{"name": "lookup", "untrusted_output": True, "data_sensitivity": "personal"},
               {"name": "pay", "side_effect": "irreversible", "requires_approval": True},
               {"name": "update", "side_effect": "write"}],
        policies=[{"id": "LIMIT", "rule": "Keep it: small", "check": {"type": "max_calls", "tool": "pay", "n": 1}}],
        features={"retrieval": True, "persistent_memory": True, "structured_output": True, "code_execution": True,
                  "multi_agent": True, "citations": True},
        risk={"regulated": True},
    )
    plan = build_plan(p)
    stubs = set()
    yaml_count = 0
    for r in plan.requirements:
        text = suggest(r, p)
        assert text
        stubs.add(r.stub)
        if _claims_yaml(text):
            yaml_count += 1
            data = yaml.safe_load(text)
            if text.startswith("- "):
                assert isinstance(data, list) and isinstance(data[0], dict) and "id" in data[0]
            else:
                assert isinstance(data, dict)
    assert yaml_count >= 10
    assert {"tool_positive", "tool_negative", "policy", "injection", "approval", "tool_error", "leakage", "repeat",
            "budget", "retrieval", "output_schema", "judge", "attestation", "success_criterion", "none",
            "golden"} <= stubs
    for g in analyse(plan, p, []).gaps:
        if g.suggestion and _claims_yaml(g.suggestion):
            yaml.safe_load(g.suggestion)
