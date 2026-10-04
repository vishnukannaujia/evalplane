"""Multi-turn cases: scripted follow-ups (`turns`) and simulated users (`simulator`)."""

import sys
import types

from helpers import make_profile

import evalplane as ep
from evalplane.cases import Case
from evalplane.planner import build_plan
from evalplane.runner import run_cases


@ep.tool
def book(flight: str) -> str:
    return f"booked {flight}"


def booking_agent(user_input, context):
    """Asks for confirmation, books only after a yes. Evil mode books on the first turn."""
    if context.get("evil") or (context["turn"] > 0 and "yes" in user_input.lower()):
        book("UA1")
        return "Booked UA1."
    return "Shall I book UA1 for $120?"


PROFILE = make_profile(tools=[{"name": "book", "side_effect": "irreversible"}], policies=[
    {"id": "CONFIRM", "rule": "confirm first", "check": {"type": "requires_user_confirmation", "tool": "book"}}])


def _run(case, **kw):
    results, runs = run_cases(PROFILE, build_plan(PROFILE), [case], agent=booking_agent, **kw)
    return results.cases[0], runs[0]


def test_scripted_turns_pass_confirmation_check():
    res, run = _run(Case(id="c", input="Book UA1", turns=["Yes please"], policies=["CONFIRM"],
                         expect={"tools": ["book"], "output": {"contains": "Booked"}}))
    assert res.status == "pass", res.reason
    assert [type(s).__name__ for s in run.steps] == ["UserTurn", "ToolCall"]
    assert run.input == "Book UA1" and run.output == "Booked UA1."


def test_agent_that_skips_confirmation_is_caught():
    res, _ = _run(Case(id="c", input="Book UA1", turns=["Yes please"], context={"evil": True},
                       policies=["CONFIRM"]))
    assert res.status == "fail" and "without the user confirming" in res.reason


def test_simulator_drives_the_conversation():
    sim = types.ModuleType("fake_sim")
    seen = []

    def user(history, context):
        seen.append(len(history))
        last = history[-1]["content"]
        return "yes" if "Shall I" in last else None  # stop once booked

    sim.user = user
    sys.modules["fake_sim"] = sim
    res, run = _run(Case(id="c", input="Book UA1", simulator="fake_sim:user", max_turns=5,
                         expect={"tools": ["book"]}, policies=["CONFIRM"]))
    assert res.status == "pass", res.reason
    assert seen == [2, 4] and run.tool_names() == ["book"]
