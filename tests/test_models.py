import datetime as dt

import pytest
from helpers import make_profile
from pydantic import ValidationError

from evalplane.errors import ConfigError
from evalplane.models import (
    AgentProfile,
    Layer,
    Sensitivity,
    SideEffect,
    Stage,
    Tier,
    Tool,
    Waiver,
    dump_profile,
    load_profile,
)


@pytest.mark.parametrize("side_effect, reversible, untrusted, is_action", [
    ("read", True, False, False),
    ("write", True, False, True),
    ("notify", True, False, True),
    ("external", False, False, True),  # untrusted_output is opt-in: only tools returning others' text
    ("irreversible", False, False, True),
])
def test_tool_defaults(side_effect, reversible, untrusted, is_action):
    t = Tool(name="t", side_effect=side_effect)
    assert t.reversible is reversible
    assert t.untrusted_output is untrusted
    assert t.is_action is is_action


def test_tool_explicit_values_are_kept():
    t = Tool(name="t", side_effect="external", reversible=True, untrusted_output=False)
    assert t.reversible is True and t.untrusted_output is False
    assert Tool(name="r", untrusted_output=True).untrusted_output is True


def test_tool_field_defaults():
    t = Tool(name="t")
    assert t.side_effect == SideEffect.read
    assert t.data_sensitivity == Sensitivity.internal
    assert t.requires_approval is False


def test_irreversible_cannot_be_reversible():
    with pytest.raises(ValidationError, match="irreversible tool cannot be reversible"):
        Tool(name="pay", side_effect="irreversible", reversible=True)


def test_duplicate_tools_rejected():
    with pytest.raises(ValidationError, match="duplicate tool names"):
        make_profile(tools=[{"name": "a"}, {"name": "a", "side_effect": "write"}])


def test_duplicate_policy_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate policy ids"):
        make_profile(policies=[{"id": "P1", "rule": "x"}, {"id": "P1", "rule": "y"}])


@pytest.mark.parametrize("check", [
    {"type": "forbidden_tool", "tool": "ghost"},
    {"type": "requires_prior_tool", "tool": "a", "prior": "ghost"},
])
def test_policy_check_unknown_tool(check):
    with pytest.raises(ValidationError, match="unknown tool 'ghost'"):
        make_profile(tools=[{"name": "a"}], policies=[{"id": "P1", "rule": "x", "check": check}])


@pytest.mark.parametrize("name", ["Bad Name", "a", "UPPER", "-lead", "x" * 70, "has.dot"])
def test_bad_agent_name(name):
    with pytest.raises(ValidationError):
        make_profile(name=name)


@pytest.mark.parametrize("name", ["ab", "my-agent", "agent_2"])
def test_good_agent_name(name):
    assert make_profile(name=name).agent.name == name


def test_bad_tool_name_and_policy_id():
    with pytest.raises(ValidationError):
        make_profile(tools=[{"name": "1bad"}])
    with pytest.raises(ValidationError):
        make_profile(policies=[{"id": "lower", "rule": "x"}])


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        AgentProfile.model_validate({"agent": {"name": "ab"}, "surprise": 1})
    with pytest.raises(ValidationError):
        make_profile(tools=[{"name": "a", "sideeffect": "write"}])


def test_profile_defaults_and_lookup():
    p = make_profile(tools=[{"name": "a"}])
    assert p.version == 1
    assert p.risk.tier == "auto" and p.risk.pack == "generic" and p.risk.regulated is False
    assert p.evals.cases == ["evals"] and p.evals.results_dir == ".evalplane"
    assert p.tool("a").name == "a" and p.tool("zzz") is None
    assert p.agent.user_facing is True


def test_load_profile_missing(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_profile(tmp_path / "nope.yaml")


def test_load_profile_sets_base_dir_and_dump_roundtrip(tmp_path):
    f = tmp_path / "agent.eval.yaml"
    f.write_text("agent: {name: demo}\ntools:\n  - {name: w, side_effect: write}\n")
    p = load_profile(f)
    assert p.base_dir == tmp_path.resolve()
    again = AgentProfile.model_validate(__import__("yaml").safe_load(dump_profile(p)))
    assert again.model_dump() == p.model_dump()


def test_waiver_active():
    today = dt.date(2026, 1, 10)
    assert Waiver(requirement="x", reason="r", approved_by="me").active(today)
    assert Waiver(requirement="x", reason="r", approved_by="me", expires="2026-01-10").active(today)
    assert not Waiver(requirement="x", reason="r", approved_by="me", expires="2026-01-09").active(today)


def test_ordered_enums():
    assert Tier.T1 < Tier.T4 and Tier.T3 >= Tier.T3 and max(Tier.T2, Tier.T1) == Tier.T2
    assert Layer.L0 < Layer.L7
    assert Stage.design < Stage.ci < Stage.pre_release < Stage.production
    assert Sensitivity.regulated > Sensitivity.personal
    assert str(Tier.T2) == "T2"
