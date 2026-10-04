import pytest
from helpers import make_profile

import evalplane as ep
from evalplane.guard import Guard

PROFILE = make_profile(
    tools=[{"name": "lookup_order"}, {"name": "issue_refund", "side_effect": "irreversible", "max_calls_per_run": 2}],
    policies=[
        {"id": "LIMIT", "rule": "max $100", "check": {"type": "max_arg", "tool": "issue_refund", "arg": "amount", "max": 100}},
        {"id": "LOOKUP-FIRST", "rule": "look up first",
         "check": {"type": "requires_prior_tool", "tool": "issue_refund", "prior": "lookup_order"}},
        {"id": "CONFIRM", "rule": "user confirms",
         "check": {"type": "requires_user_confirmation", "tool": "issue_refund"}},
    ])


def make(mode="enforce", seen=None):
    guard = Guard(PROFILE, mode=mode, on_violation=(lambda v, c: seen.append(v.policy)) if seen is not None else None)
    refunds = []

    @guard.tool
    def lookup_order(order_id):
        return {"order_id": order_id}

    @guard.tool
    def issue_refund(order_id, amount):
        refunds.append(amount)
        return "ok"

    return guard, lookup_order, issue_refund, refunds


def test_blocks_before_the_action_happens():
    guard, lookup, refund, refunds = make()
    with guard.session("refund A1") as run:
        lookup("A1")
        guard.user_said("yes please")
        with pytest.raises(ep.ToolDenied, match="LIMIT"):
            refund("A1", 900)
        assert refunds == []  # money never moved
        guard.user_said("ok, 40 then, yes")
        assert refund("A1", 40) == "ok"
    assert refunds == [40]
    assert [s.status for s in run.tool_calls()] == ["ok", "denied", "ok"]


def test_ordering_and_confirmation_rules():
    guard, lookup, refund, _ = make()
    with guard.session("refund"):
        with pytest.raises(ep.ToolDenied) as e:
            refund("A1", 10)  # no lookup, no confirmation
        assert "LOOKUP-FIRST" in str(e.value) and "CONFIRM" in str(e.value)


def test_repeated_violation_is_still_blocked():
    seen = []
    guard, lookup, refund, _ = make(mode="shadow", seen=seen)
    with guard.session("x"):
        refund("A1", 10)
        refund("A1", 20)  # the same LOOKUP-FIRST violation again must still be reported
    assert seen.count("LOOKUP-FIRST") == 2 and guard.blocked == []


def test_shadow_mode_only_reports():
    seen = []
    guard, lookup, refund, refunds = make(mode="shadow", seen=seen)
    with guard.session("refund"):
        lookup("A1")
        guard.user_said("yes")
        refund("A1", 500)
    assert refunds == [500] and "LIMIT" in seen


def test_call_limit_from_profile():
    guard, lookup, refund, _ = make()
    with guard.session("x"):
        lookup("A1")
        for amt in (10, 20):
            guard.user_said("yes")
            refund("A1", amt)
        guard.user_said("yes")
        with pytest.raises(ep.ToolDenied, match="max 2"):
            refund("A1", 30)
