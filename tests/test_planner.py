import pytest
from helpers import make_profile, t1_profile, t3_profile, t4_profile

from evalplane.errors import ConfigError
from evalplane.models import Tier
from evalplane.planner import build_plan, explain, load_catalog


def ids(plan, priority=None):
    return {r.id for r in plan.requirements if priority is None or r.priority == priority}


def test_catalog_loads_with_generic_pack():
    version, rules, pack = load_catalog("generic")
    assert version.endswith("+generic") and pack["id"] == "generic"
    assert len({r.id for r in rules}) == len(rules)


def test_unknown_pack_is_config_error():
    with pytest.raises(ConfigError, match="unknown pack"):
        build_plan(make_profile(risk={"pack": "does-not-exist"}))


def test_t1_plan():
    plan = build_plan(t1_profile())
    assert plan.tier == Tier.T1
    assert ids(plan, "must") == {"L4.task.success", "L4.policy.adherence@NO-PII"}
    should = ids(plan, "should")
    # read-only tools: selection/args are recommended, not required
    assert {"L3.tool.selection@search", "L3.tool.selection@fetch_web", "L1.golden.accuracy", "L3.tool.args@search", "L3.tool.injection@fetch_web", "L4.trajectory.match",
            "L4.efficiency.budget", "L6.outcome.defined"} <= should
    # no action tools: no guard / goal-hijack / error-recovery; T1: no pass^k, no governance
    everything = ids(plan)
    assert not any(i.startswith(("L3.tool.guard", "L3.tool.error_recovery", "L4.safety.goal_hijack",
                                 "L4.reliability.pass_k", "L7.")) for i in everything)
    assert "L3.tool.injection@search" not in everything
    assert not any(i.startswith("L2.") for i in everything)


def test_t3_plan():
    plan = build_plan(t3_profile())
    assert plan.tier == Tier.T3
    must = ids(plan, "must")
    assert {"L3.tool.guard@update_ticket", "L3.tool.error_recovery", "L3.tool.injection@lookup",
            "L4.policy.adherence@VERIFY", "L4.policy.adherence@BE-NICE", "L4.safety.scope",
            "L4.safety.goal_hijack", "L4.reliability.pass_k", "L7.governance.recertify",
            "L0.model.acceptance"} <= must
    # guard only for action tools, injection only for untrusted-output tools
    assert "L3.tool.guard@lookup" not in ids(plan)
    assert "L3.tool.injection@update_ticket" not in ids(plan)
    assert "L7.governance.kill_switch" in ids(plan, "should")
    assert "L7.governance.review" not in ids(plan)
    pk = plan.get("L4.reliability.pass_k")
    assert pk.metric == "pass_hat_k" and pk.k == 5 and pk.threshold == pytest.approx(0.8)
    assert plan.get("L3.tool.selection@lookup").threshold == pytest.approx(0.95)
    assert plan.get("L4.policy.adherence@VERIFY").threshold == 0


def test_t4_plan():
    plan = build_plan(t4_profile())
    assert plan.tier == Tier.T4
    must = ids(plan, "must")
    assert {"L7.governance.review", "L7.governance.kill_switch", "L3.tool.guard@pay",
            "L4.policy.adherence@LIMIT"} <= must
    pk = plan.get("L4.reliability.pass_k")
    assert pk.k == 8 and pk.threshold == pytest.approx(0.9)
    assert plan.get("L3.tool.selection@pay").threshold == pytest.approx(0.98)


def test_one_policy_requirement_per_policy():
    plan = build_plan(t3_profile())
    pol = [r for r in plan.requirements if r.rule_id == "L4.policy.adherence"]
    assert sorted(r.target for r in pol) == ["BE-NICE", "VERIFY"]
    assert "VERIFY" in plan.get("L4.policy.adherence@VERIFY").why


def test_approval_rule_only_for_approval_tools():
    plan = build_plan(make_profile(tools=[{"name": "w", "side_effect": "write", "requires_approval": True},
                                          {"name": "x", "side_effect": "write"}]))
    assert "L3.tool.approval@w" in ids(plan, "must")
    assert "L3.tool.approval@x" not in ids(plan)


def test_l2_rules_only_with_retrieval():
    without = build_plan(t3_profile())
    assert not any(i.startswith("L2.") for i in ids(without))
    with_r = build_plan(t3_profile(features={"retrieval": True}))
    assert {"L2.retrieval.recall", "L2.grounding.faithfulness"} <= ids(with_r, "must")
    assert "L2.citations.accuracy" not in ids(with_r)
    cited = build_plan(t3_profile(features={"retrieval": True, "citations": True}))
    assert "L2.citations.accuracy" in ids(cited, "must")
    mem = build_plan(t3_profile(features={"persistent_memory": True}))
    assert "L2.memory.poisoning" in ids(mem, "must")


def test_feature_rules():
    plan = build_plan(t1_profile(features={"structured_output": True, "code_execution": True}))
    assert {"L1.output.schema", "L3.code.sandbox"} <= ids(plan, "must")


def test_data_leakage_needs_personal_data():
    assert "L4.safety.data_leakage" not in ids(build_plan(t1_profile()))
    p = make_profile(tools=[{"name": "r", "data_sensitivity": "personal"}])
    assert "L4.safety.data_leakage" in ids(build_plan(p), "must")


def test_goal_hijack_needs_user_facing():
    p = t3_profile(agent={"user_facing": False})
    assert "L4.safety.goal_hijack" not in ids(build_plan(p))


def test_threshold_override_precedence():
    p = t3_profile(thresholds={"tool_correctness": 0.5, "L3.tool.selection": 0.6, "L3.tool.selection@lookup": 0.7})
    plan = build_plan(p)
    assert plan.get("L3.tool.selection@lookup").threshold == pytest.approx(0.7)  # requirement id wins
    assert plan.get("L3.tool.selection@update_ticket").threshold == pytest.approx(0.6)  # then rule id
    assert plan.get("L3.tool.args@lookup").threshold == pytest.approx(0.5)  # then named threshold
    assert plan.get("L4.task.success").threshold == pytest.approx(0.9)  # untouched


def test_waivers_fnmatch_and_expiry():
    p = t3_profile(waivers=[
        {"requirement": "L3.tool.guard@*", "reason": "later", "approved_by": "lead"},
        {"requirement": "L4.task.success", "reason": "old", "approved_by": "lead", "expires": "2000-01-01"},
    ])
    plan = build_plan(p)
    guard = plan.get("L3.tool.guard@update_ticket")
    assert guard.waived is not None and guard.waived.reason == "later"
    assert guard not in plan.must
    assert plan.get("L4.task.success").waived is None
    assert plan.get("L4.task.success") in plan.must


def test_plan_hash_deterministic_and_sensitive():
    a, b = build_plan(t3_profile()), build_plan(t3_profile())
    assert a.plan_hash == b.plan_hash and len(a.plan_hash) == 16
    c = build_plan(t3_profile(agent={"description": "changed"}))
    assert c.plan_hash != a.plan_hash
    d = build_plan(t3_profile(thresholds={"guard": 0.9}))
    assert d.plan_hash != a.plan_hash


def test_requirements_sorted_by_layer_and_owasp_collected():
    plan = build_plan(t3_profile())
    keys = [(r.layer, r.id) for r in plan.requirements]
    assert keys == sorted(keys)
    assert plan.owasp_applicable == sorted(plan.owasp_applicable)
    assert {"ASI01", "ASI02", "ASI08", "ASI10"} <= set(plan.owasp_applicable)
    assert plan.catalog_version.endswith("+generic") and plan.pack == "generic"


def test_explain():
    plan = build_plan(t3_profile())
    text = explain("L3.tool.guard@update_ticket", plan)
    assert text.startswith("L3.tool.guard@update_ticket [MUST]")
    assert "why:" in text and "how:" in text and "update_ticket" in text
    assert "ASI02 Tool Misuse and Exploitation" in text
    assert "controls: NIST:MEASURE-2.7" in text
    assert "(threshold 1)" in text
    assert "not in this agent's plan" in explain("L9.nope", plan)


def test_governance_is_opt_in():
    from helpers import make_profile

    tools = [{"name": "pay", "side_effect": "irreversible"}]
    off = build_plan(make_profile(tools=tools, risk={"governance": False}))
    on = build_plan(make_profile(tools=tools))
    assert not any(r.layer.value in ("L6", "L7") for r in off.requirements)
    assert {"L7.governance.kill_switch", "L6.outcome.defined"} <= {r.id for r in on.requirements}
