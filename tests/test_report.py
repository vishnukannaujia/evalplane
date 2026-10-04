import hashlib
import json

import pytest
from helpers import mk_case, t3_profile, tc

from evalplane.coverage import analyse
from evalplane.gate import evaluate_gate
from evalplane.models import Stage
from evalplane.planner import build_plan
from evalplane.report import coverage_md, evidence, evidence_md, plan_md, scorecard_md, write_evidence
from evalplane.runner import run_cases
from evalplane.trace import Run


@pytest.fixture
def world():
    profile = t3_profile(waivers=[{"requirement": "L0.model.acceptance", "reason": "same model as last release",
                                   "approved_by": "lead", "expires": "2999-01-01"}])
    plan = build_plan(profile)
    cases = [mk_case("good", expect={"tools": ["lookup"], "forbidden_tools": ["update_ticket"],
                                     "output": {"contains": "ok"}}),
             mk_case("bad", policies=["VERIFY"], expect={"tools": ["update_ticket"]})]
    traces = [Run(case_id="good", output="ok", steps=[tc("lookup")]),
              Run(case_id="bad", output="done", steps=[tc("update_ticket")])]
    results, _ = run_cases(profile, plan, cases, traces=traces)
    cov = analyse(plan, profile, cases)
    gate = evaluate_gate(plan, cov, Stage.ci, results)
    return plan, cov, results, gate


def test_plan_md(world):
    plan, *_ = world
    md = plan_md(plan)
    assert md.startswith("# Eval plan: test-agent")
    assert "**Risk tier:** T3 (Autonomous, reversible actions) · pack `generic`" in md
    assert "Why this tier:" in md and "- update_ticket: T3" in md
    assert "## L3 · Tools / actions" in md and "## L2" not in md
    assert "| `L3.tool.guard@update_ticket` | must | ci | code | pass_rate ≥ 1 |" in md
    assert "pass_hat_k(k=5) ≥ 0.8" in md
    assert "violations ≤ 0" in md
    assert "| `L0.model.acceptance` | waived |" in md


def test_coverage_md(world):
    _, cov, *_ = world
    md = coverage_md(cov, max_gaps=3)
    assert md.startswith(f"## Eval coverage: {cov.score:g}%")
    assert "| requirements |" in md and "| L3 Tools / actions |" in md
    assert f"### Top gaps ({len(cov.gaps)} total)" in md
    assert md.count("\n- **[") == 3


def test_scorecard_md(world):
    plan, cov, results, gate = world
    assert gate.verdict == "FAIL"
    md = scorecard_md(plan, cov, results, gate)
    assert md.startswith("# Evalplane scorecard: test-agent")
    assert "**FAIL** at stage `ci`" in md and "tier **T3**" in md and f"plan `{plan.plan_hash}`" in md
    assert "**Cases:** 1 passed, 1 failed, 0 skipped (2 runs)" in md
    assert "### Failing cases" in md and "`bad` (0/1 runs passed): policy[VERIFY]" in md
    assert "### Blocking" in md and "`L4.policy.adherence@VERIFY`" in md
    assert "## Eval coverage:" in md
    bare = scorecard_md(plan, cov, None, None)
    assert "Failing cases" not in bare and "Blocking" not in bare


def test_evidence_maps_controls_and_owasp(world):
    plan, cov, results, gate = world
    ev = evidence(plan, cov, results, gate)
    assert ev["agent"] == "test-agent" and ev["tier"] == "T3" and ev["plan_hash"] == plan.plan_hash
    assert ev["gate"]["verdict"] == "FAIL" and "not a compliance assessment" in ev["disclaimer"]
    by_key = {(c["framework"], c["control"]): c for c in ev["controls"]}
    nist = by_key[("NIST AI RMF", "MEASURE-2.7")]
    req_ids = {r["id"] for r in nist["requirements"]}
    assert {"L3.tool.guard@update_ticket", "L4.policy.adherence@VERIFY"} <= req_ids
    pol = next(r for r in nist["requirements"] if r["id"] == "L4.policy.adherence@VERIFY")
    assert pol["status"] == "fail" and pol["value"] == 1.0 and pol["cases"] == ["bad"]
    asi02 = by_key[("OWASP Agentic Top 10 (2026)", "ASI02")]
    assert asi02["title"] == "Tool Misuse and Exploitation"
    assert [r["id"] for r in asi02["requirements"]] == ["L3.tool.guard@update_ticket"]
    assert ("EU AI Act", "Art15") in by_key and ("ISO/IEC 42001 Annex A", "A.6.2.6") in by_key
    assert ev["waivers"] == [{"requirement": "L0.model.acceptance", "reason": "same model as last release",
                              "approved_by": "lead", "expires": "2999-01-01",
                              "requirement_id": "L0.model.acceptance"}]
    no_results = evidence(plan, cov, None, None)
    statuses = {r["id"]: r["status"] for c in no_results["controls"] for r in c["requirements"]}
    assert statuses["L3.tool.guard@update_ticket"] == "covered"
    assert statuses["L3.tool.error_recovery"] == "missing"
    assert no_results["gate"] is None


def test_evidence_md_and_write_evidence(world, tmp_path):
    plan, cov, results, gate = world
    ev = evidence(plan, cov, results, gate)
    md = evidence_md(ev)
    assert md.startswith("# Evidence pack: test-agent")
    assert "## NIST AI RMF" in md and "### ASI02 Tool Misuse and Exploitation" in md
    assert "- Gate: FAIL @ ci" in md and "## Waivers" in md and "expires 2999-01-01" in md
    out = write_evidence(tmp_path / "pack", ev, {"plan.md": plan_md(plan), "extra.txt": "hello"})
    manifest = json.loads((out / "manifest.json").read_text())
    assert set(manifest) == {"evidence.json", "evidence.md", "plan.md", "extra.txt"}
    for name, digest in manifest.items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert json.loads((out / "evidence.json").read_text())["plan_hash"] == plan.plan_hash
