import pytest
from helpers import make_profile

from evalplane.errors import ConfigError
from evalplane.models import Tier, Tool
from evalplane.tiering import derive_tier, tool_tier


@pytest.mark.parametrize("spec, tier, reason", [
    ({}, Tier.T1, "read-only"),
    ({"data_sensitivity": "personal"}, Tier.T1, "read-only"),
    ({"data_sensitivity": "regulated"}, Tier.T2, "reads regulated data"),
    ({"side_effect": "write"}, Tier.T3, "can be undone"),
    ({"side_effect": "write", "reversible": False}, Tier.T4, "cannot be undone"),
    ({"side_effect": "external"}, Tier.T4, "external action that cannot be undone"),
    ({"side_effect": "external", "reversible": True}, Tier.T3, "can be undone"),
    ({"side_effect": "irreversible"}, Tier.T4, "irreversible action that cannot be undone"),
    ({"side_effect": "irreversible", "requires_approval": True}, Tier.T3, "irreversible, but a human approves"),
    ({"side_effect": "write", "requires_approval": True}, Tier.T2, "a human approves each call"),
    ({"side_effect": "write", "data_sensitivity": "regulated"}, Tier.T4, "regulated data"),
])
def test_tool_tier(spec, tier, reason):
    got, why = tool_tier(Tool(name="t", **spec))
    assert got == tier
    assert reason in why


def test_no_tools_is_t1():
    d = derive_tier(make_profile())
    assert d.tier == Tier.T1 and d.derived == Tier.T1
    assert any("no tools declared" in r for r in d.reasons)


def test_derived_is_max_of_tools():
    d = derive_tier(make_profile(tools=[{"name": "r"}, {"name": "w", "side_effect": "write"}]))
    assert d.tier == Tier.T3
    assert d.per_tool == {"r": Tier.T1, "w": Tier.T3}
    assert d.reasons[0].startswith("r: T1")


def test_regulated_flag_raises_actions_to_t4():
    d = derive_tier(make_profile(tools=[{"name": "w", "side_effect": "write", "requires_approval": True}],
                                 risk={"regulated": True}))
    assert d.tier == Tier.T4
    assert any("regulated domain" in r for r in d.reasons)


def test_regulated_flag_without_actions_does_nothing():
    assert derive_tier(make_profile(tools=[{"name": "r"}], risk={"regulated": True})).tier == Tier.T1


def test_persistent_memory_raises_to_t2():
    d = derive_tier(make_profile(tools=[{"name": "r"}], features={"persistent_memory": True}))
    assert d.tier == Tier.T2
    assert any("persistent memory" in r for r in d.reasons)
    # already above T2: unchanged
    d3 = derive_tier(make_profile(tools=[{"name": "w", "side_effect": "write"}], features={"persistent_memory": True}))
    assert d3.tier == Tier.T3


def test_declared_stricter_tier_is_used():
    d = derive_tier(make_profile(tools=[{"name": "r"}], risk={"tier": "T3"}))
    assert d.tier == Tier.T3 and d.derived == Tier.T1 and d.declared == "T3" and not d.overridden
    assert any("stricter" in r for r in d.reasons)


def test_declared_equal_tier():
    d = derive_tier(make_profile(tools=[{"name": "r"}], risk={"tier": "T1"}))
    assert d.tier == Tier.T1 and not d.overridden
    assert not any("stricter" in r for r in d.reasons)


def test_declared_lower_without_reason_raises():
    with pytest.raises(ConfigError, match="override_reason"):
        derive_tier(make_profile(tools=[{"name": "p", "side_effect": "irreversible"}], risk={"tier": "T2"}))


def test_declared_lower_with_reason_overrides():
    d = derive_tier(make_profile(tools=[{"name": "p", "side_effect": "irreversible"}],
                                 risk={"tier": "T2", "override_reason": "sandbox only"}))
    assert d.tier == Tier.T2 and d.derived == Tier.T4 and d.overridden
    assert any(r.startswith("OVERRIDE") and "sandbox only" in r for r in d.reasons)
