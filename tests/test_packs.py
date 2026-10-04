"""Every built-in pack must produce a valid profile, a plan that includes its own rules, and loadable cases."""

import pytest
import yaml

from evalplane.cases import load_cases
from evalplane.coverage import analyse
from evalplane.models import AgentProfile
from evalplane.packs import get_pack, list_packs, pack_dir
from evalplane.planner import build_plan, load_owasp

PACKS = [p["id"] for p in list_packs()]


def test_expected_packs_present():
    assert {"generic", "support-agent", "coding-agent", "knowledge-assistant", "data-analyst",
            "personal-assistant", "claims-processing"} <= set(PACKS)


@pytest.mark.parametrize("pid", PACKS)
def test_pack_template_plans_and_cases_load(pid, tmp_path):
    d = pack_dir(pid)
    profile = AgentProfile.model_validate(yaml.safe_load((d / "agent.eval.yaml").read_text()))
    assert profile.risk.pack == pid
    plan = build_plan(profile)
    ids = {r.id for r in plan.requirements}
    pack_rule_ids = {r["id"] for r in get_pack(pid).get("rules") or []}
    # each pack rule either applies to this template or is filtered by tier/conditions, but at least one applies
    if pack_rule_ids:
        assert {r.rule_id for r in plan.requirements} & pack_rule_ids
    cases = load_cases([d / "cases.yaml"])
    assert cases, "pack needs starter cases"
    unknown = {c for case in cases for c in case.covers if c not in ids}
    assert not unknown, f"starter cases cover ids not in the plan: {unknown}"
    policies = {p.id for p in profile.policies}
    assert {p for case in cases for p in case.policies} <= policies
    tools = {t.name for t in profile.tools}
    assert {t for case in cases for t in case.exercised_tools() | set(case.expect.forbidden_tools)} <= tools
    cov = analyse(plan, profile, cases)
    assert cov.score > 0 or pid == "generic"  # generic's starter case is a placeholder to fill in


@pytest.mark.parametrize("pid", PACKS)
def test_pack_rules_are_well_formed(pid):
    owasp = set(load_owasp())
    for r in get_pack(pid).get("rules") or []:
        assert set(r.get("owasp", [])) <= owasp, r["id"]
        assert r["id"].count(".") >= 2
