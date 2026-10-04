import json

import yaml
from helpers import make_profile

from evalplane.coverage import analyse
from evalplane.generate import generate
from evalplane.planner import build_plan


def _profile():
    return make_profile(
        tools=[{"name": "lookup"}, {"name": "refund", "side_effect": "irreversible"}],
        policies=[{"id": "LIMIT", "rule": "max 100", "check": {"type": "max_arg", "tool": "refund", "arg": "amount",
                                                                  "max": 100}},
                  {"id": "NEVER-DELETE", "rule": "never delete", "check": {"type": "forbidden_tool", "tool": "refund"}}])


def _cases(text):
    return yaml.safe_load(text.split("\n", 2)[2])["cases"]


def test_templates_cover_every_gap_offline():
    p = _profile()
    text, n = generate(p, analyse(build_plan(p), p, []))
    cases = _cases(text)
    assert n == len(cases) == 6  # 2 per untested policy (x2) + 2 for the action tool with no negative
    assert any("500" in c["input"] and c["policies"] == ["LIMIT"] for c in cases)
    assert all(c["expect"]["forbidden_tools"] == ["refund"] for c in cases if c["id"].startswith("must-not-refund"))
    Case = __import__("evalplane").Case
    unreviewed = [Case.model_validate({**c, "suite": "g"}) for c in cases]
    assert all("unreviewed" in c.tags for c in unreviewed)
    assert analyse(build_plan(p), p, unreviewed).counts["policies_untempted"] == 2  # not counted yet
    reviewed = [c.model_copy(update={"tags": []}) for c in unreviewed]
    cov_after = analyse(build_plan(p), p, reviewed)
    assert cov_after.counts["policies_untempted"] == 0 and cov_after.counts["action_tools_without_negative"] == 0


def test_llm_function_is_used():
    p = _profile()
    prompts = []

    def fake_llm(prompt):
        prompts.append(prompt)
        return "Sure: " + json.dumps(["first attack", "second attack", "third"])

    text, n = generate(p, analyse(build_plan(p), p, []), llm=fake_llm, per_gap=2)
    assert len(prompts) == 3 and all("JSON list" in x for x in prompts)
    assert {c["input"] for c in _cases(text)} == {"first attack", "second attack"}
