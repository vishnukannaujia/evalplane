import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from conftest import REPO

from evalplane.cases import load_cases
from evalplane.coverage import analyse, diff
from evalplane.gate import evaluate_gate
from evalplane.models import Stage, load_profile
from evalplane.planner import build_plan
from evalplane.reporters import PR_MARKER, html_report, junit_xml, pr_comment_md
from evalplane.runner import load_agent, run_cases
from evalplane.tiering import derive_tier


def _world(example: Path, monkeypatch, buggy: bool):
    if buggy:
        monkeypatch.setenv("BUGGY", "1")
    else:
        monkeypatch.delenv("BUGGY", raising=False)
    profile = load_profile(example / "agent.eval.yaml")
    plan = build_plan(profile, derive_tier(profile))
    cases = load_cases(profile.evals.cases, base_dir=profile.base_dir)
    agent = load_agent(profile.agent.entrypoint, profile.base_dir)
    results, _ = run_cases(profile, plan, cases, agent=agent)
    cov = analyse(plan, profile, cases)
    gate = evaluate_gate(plan, cov, Stage.ci, results)
    return profile, plan, cases, cov, results, gate


@pytest.fixture
def good(example_copy, monkeypatch):
    return _world(example_copy, monkeypatch, buggy=False)


@pytest.fixture
def buggy(example_copy, monkeypatch):
    return _world(example_copy, monkeypatch, buggy=True)


def _junit_counts(xml: str):
    root = ET.fromstring(xml.encode())
    assert root.tag == "testsuites"
    suites = {s.get("name"): s for s in root.findall("testsuite")}
    assert "requirements" in suites
    for s in root.findall("testsuite"):  # declared counts match the elements
        tcs = s.findall("testcase")
        assert int(s.get("tests")) == len(tcs)
        assert int(s.get("failures")) == sum(t.find("failure") is not None for t in tcs)
        assert int(s.get("skipped")) == sum(t.find("skipped") is not None for t in tcs)
    return root, suites


def test_junit_normal(good):
    _, plan, _, _, results, _ = good
    root, suites = _junit_counts(junit_xml(results, plan))
    case_tcs = [t for name, s in suites.items() if name != "requirements" for t in s.findall("testcase")]
    assert len(case_tcs) == len(results.cases) == 11
    assert all(t.find("failure") is None for t in case_tcs)
    assert {t.get("classname") for t in case_tcs} == {c.suite for c in results.cases}
    reqs = suites["requirements"].findall("testcase")
    assert len(reqs) == sum(r.priority == "must" for r in plan.requirements)
    # at the ci stage, requirements that only apply before release are skipped, not failed (same as the gate)
    failing = {t.get("name") for t in reqs if t.find("failure") is not None}
    assert failing == set() and int(root.get("failures")) == 0
    skipped = {t.get("name") for t in reqs if t.find("skipped") is not None}
    assert {"L4.reliability.pass_k", "L7.governance.review"} <= skipped
    pre = ET.fromstring(junit_xml(results, plan, Stage.pre_release).encode())
    assert int(pre.get("failures")) >= 2  # ...and fail when checking for release


def test_junit_buggy(buggy):
    _, plan, _, _, results, _ = buggy
    root, suites = _junit_counts(junit_xml(results, plan))
    case_fail = [t for name, s in suites.items() if name != "requirements" for t in s.findall("testcase")
                 if t.find("failure") is not None]
    assert len(case_fail) == results.summary["failed"] == 4
    names = {t.get("name") for t in case_fail}
    assert "no-pii-leak" in names
    pii = next(t for t in case_fail if t.get("name") == "no-pii-leak")
    assert "bob@example.com" in pii.find("failure").get("message")
    req_fail = {t.get("name") for t in suites["requirements"].findall("testcase") if t.find("failure") is not None}
    stages = {r.id: r.stage for r in plan.requirements}
    expected = {r.id for r in results.requirements if r.priority == "must"
                and r.status in ("fail", "missing", "unverified") and stages[r.id] <= Stage.ci}
    assert req_fail == expected and "L4.policy.adherence@NO-PII" in req_fail
    assert int(root.get("failures")) == 4 + len(expected)


def test_junit_escapes_hostile_text(good):
    _, plan, _, _, results, _ = good
    c = results.cases[0]
    c.status, c.reason = "fail", 'bad <tag> & "quotes" ]]> \x00\x07 control'
    root = ET.fromstring(junit_xml(results, plan).encode())
    f = root.find(".//testcase[@name='%s']/failure" % c.case_id)
    assert "<tag> & \"quotes\" ]]>" in f.get("message")


_URL = re.compile(r"""(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""", re.I)


def test_html_report(good, buggy):
    for world, verdict in ((good, "PASS"), (buggy, "FAIL")):
        _, plan, _, cov, results, gate = world
        assert gate.verdict == verdict
        page = html_report(plan, cov, results, gate)
        assert page.startswith("<!DOCTYPE html>") and page.rstrip().endswith("</html>")
        assert f"Gate {verdict}" in page and "stage ci" in page
        # self-contained: inline CSS/JS only, nothing fetched from the network
        assert not _URL.search(page) and "@import" not in page and "src=" not in page
        assert page.count("<script>") == 1 and "prefers-color-scheme:dark" in page
        assert "L3 · Tools / actions" in page and "L3.tool.guard@issue_refund" in page
        assert f"{cov.counts['must_covered']}/{cov.counts['must']}" in page
    page = html_report(buggy[1], buggy[3], buggy[4], buggy[5])
    assert "no-pii-leak" in page and page.count('data-status="fail"') == 4
    assert "bob@example.com" in page  # the reason, HTML-escaped where needed
    assert "No results yet" in html_report(good[1], good[3], None, None)


def test_html_escapes(good):
    _, plan, _, cov, results, gate = good
    results.cases[0].status, results.cases[0].reason = "fail", "<script>alert(1)</script>"
    page = html_report(plan, cov, results, gate)
    # the page has exactly one inline <script> (the filter); the case's text is escaped, not executed
    assert page.count("<script>") == 1 and "alert(1)" not in page.split("<script>")[1]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_pr_comment(buggy, good, example_copy):
    _, plan, _, cov, results, gate = buggy
    md = pr_comment_md(plan, cov, results, gate)
    assert md.startswith(PR_MARKER + "\n")
    assert "**FAIL**" in md and "no-pii-leak" in md and "New gaps" not in md
    assert len(md.splitlines()) <= 60
    ok = pr_comment_md(good[1], good[3], good[4], good[5])
    assert "**PASS**" in ok and "Failing cases" not in ok


def test_pr_comment_with_diff(good, example_copy):
    profile, plan, cases, before, results, gate = good
    # this PR drops every case that forbids issue_refund: a new must gap appears
    kept = [c for c in cases if "issue_refund" not in c.expect.forbidden_tools]
    assert len(kept) < len(cases)
    after = analyse(plan, profile, kept)
    d = diff(before, after)
    assert any(g.ref == "issue_refund" for g in d.new_gaps)
    md = pr_comment_md(plan, after, results, evaluate_gate(plan, after, Stage.ci, results), diff=d)
    assert md.startswith(PR_MARKER)
    assert "New gaps introduced by this PR" in md and "`issue_refund`" in md and "vs base" in md
    assert len(md.splitlines()) <= 60
    same = pr_comment_md(plan, before, results, gate, diff=diff(before, before))
    assert "No new coverage gaps" in same


def test_pr_comment_caps_failing_cases(buggy):
    _, plan, _, cov, results, gate = buggy
    many = results.model_copy(deep=True)
    c = many.cases[0]
    many.cases = [c.model_copy(update={"case_id": f"case-{i}", "status": "fail", "reason": "x" * 500})
                  for i in range(25)]
    md = pr_comment_md(plan, cov, many, gate)
    assert "case-9" in md and "case-10" not in md and "… and 15 more" in md
    assert len(md.splitlines()) <= 60


# --------------------------------------------------------------------------- packaging for CI


def test_action_yml():
    action = yaml.safe_load((REPO / "action.yml").read_text())
    assert {"name", "description", "inputs", "runs"} <= set(action)
    assert action["runs"]["using"] == "composite"
    inputs = action["inputs"]
    for k in ("working-directory", "stage", "python-version", "install", "baseline", "comment",
              "fail-on-new-gaps"):
        assert k in inputs, k
    assert inputs["stage"]["default"] == "ci" and inputs["install"]["default"] == "pip install evalplane"
    assert str(inputs["baseline"]["default"]).lower() == "true"
    assert str(inputs["comment"]["default"]).lower() == "true"
    assert str(inputs["fail-on-new-gaps"]["default"]).lower() == "false"
    steps = action["runs"]["steps"]
    for s in steps:
        assert "uses" in s or ("run" in s and s.get("shell")), s.get("name")  # composite run steps need a shell
    body = yaml.safe_dump(steps)
    assert "evalplane run -q" in body and "evalplane gate" in body and PR_MARKER in body
    assert any("upload-artifact" in s.get("uses", "") for s in steps)


def test_precommit_and_example_workflow():
    hooks = yaml.safe_load((REPO / ".pre-commit-hooks.yaml").read_text())
    hook = next(h for h in hooks if h["id"] == "evalplane-validate")
    assert hook["entry"] == "evalplane validate" and hook["language"] == "python"
    assert hook["pass_filenames"] is False and re.search(hook["files"], "evals/refunds.yaml")
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "example-pr.yml").read_text())
    triggers = wf.get("on", wf.get(True))  # PyYAML reads the bare key `on` as True
    assert set(triggers) == {"workflow_dispatch"}
    assert "vishnukannaujia/evalplane@main" in yaml.safe_dump(wf)


def test_cucumber_json(buggy):
    """Cucumber JSON: suites -> features, cases -> scenarios, checks -> steps (Jenkins, cucumber-html-reporter)."""
    import json

    from evalplane.reporters import cucumber_json

    _, plan, _, _, results, _ = buggy
    features = json.loads(cucumber_json(results, plan))
    assert features and all({"keyword", "name", "elements", "uri", "id"} <= set(f) for f in features)
    scenarios = [e for f in features for e in f["elements"]]
    assert {s["name"] for s in scenarios} == {c.case_id for c in results.cases}
    assert all(s["keyword"] == "Scenario" and s["steps"] for s in scenarios)
    pii = next(s for s in scenarios if s["name"] == "no-pii-leak")
    failed = [st for st in pii["steps"] if st["result"]["status"] == "failed"]
    assert failed and "bob@example.com" in failed[0]["result"]["error_message"]
    statuses = {st["result"]["status"] for f in features for e in f["elements"] for st in e["steps"]}
    assert statuses <= {"passed", "failed", "skipped", "undefined"}
