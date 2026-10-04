import json

import pytest
import yaml
from typer.testing import CliRunner

from evalplane import __version__
from evalplane.cli import app
from evalplane.trace import Run, ToolCall, write_runs


@pytest.fixture
def cli():
    runner = CliRunner()

    def invoke(*args, env=None):
        return runner.invoke(app, [str(a) for a in args], env=env)

    return invoke


@pytest.fixture
def ex(example_copy):
    return example_copy / "agent.eval.yaml"


def test_version_and_packs(cli):
    assert cli("version").stdout.strip() == __version__
    for flag in ("--version", "-V"):  # the flag people actually try first
        r = cli(flag)
        assert r.exit_code == 0 and r.stdout.strip() == __version__
    r = cli("packs")
    assert r.exit_code == 0 and "generic" in r.stdout


def test_validate(cli, ex):
    r = cli("validate", "--file", ex)
    assert r.exit_code == 0, r.output
    assert "agent support-refund, tier T4, 5 tools, 3 policies, 11 cases" in " ".join(r.stdout.split())


def test_invalid_profile_exit_2(cli, tmp_path):
    bad = tmp_path / "agent.eval.yaml"
    bad.write_text("agent: {name: Bad Name}\n")
    r = cli("validate", "--file", bad)
    assert r.exit_code == 2 and "is invalid" in r.stderr and "agent.name" in r.stderr
    assert cli("plan", "--file", tmp_path / "missing.yaml").exit_code == 2


def test_plan_formats(cli, ex, tmp_path):
    table = cli("plan", "--file", ex, "--why")
    assert table.exit_code == 0 and "support-refund" in table.stdout and "L3.tool.guard" in table.stdout
    md = cli("plan", "--file", ex, "--format", "md")
    assert md.exit_code == 0 and md.stdout.startswith("# Eval plan: support-refund")
    js = cli("plan", "--file", ex, "--format", "json", "--must-only")
    data = json.loads(js.stdout)
    assert data["tier"] == "T4" and data["requirements"]
    assert all(r["priority"] == "must" for r in data["requirements"])
    out = tmp_path / "o" / "plan.md"
    assert cli("plan", "--file", ex, "--format", "md", "--out", out).exit_code == 0
    assert out.read_text().startswith("# Eval plan")


def test_explain(cli, ex):
    r = cli("explain", "L3.tool.guard@issue_refund", "--file", ex)
    assert r.exit_code == 0 and "[MUST]" in r.stdout and "ASI02" in r.stdout
    assert "not in this agent's plan" in cli("explain", "nope", "--file", ex).stdout


def test_coverage(cli, ex):
    r = cli("coverage", "--file", ex, "--format", "json")
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["agent"] == "support-refund" and 0 < data["score"] <= 100
    assert set(data["dimensions"]) == {"requirements", "tools", "policies", "owasp", "layers"}
    assert cli("coverage", "--file", ex, "--min-score", "0").exit_code == 0
    assert cli("coverage", "--file", ex, "--min-score", "101").exit_code == 1
    table = cli("coverage", "--file", ex)
    assert table.exit_code == 0 and "must-have evals covered" in table.stdout
    assert cli("coverage", "--file", ex, "--format", "md").stdout.startswith("## Eval coverage")


def test_run_gate_report_badge_then_buggy(cli, ex, tmp_path):
    r = cli("run", "--file", ex)
    assert r.exit_code == 0, r.output
    assert "11 passed, 0 failed" in r.stdout
    latest = ex.parent / ".evalplane" / "latest.json"
    assert latest.exists()

    g = cli("gate", "--file", ex, "--stage", "ci")
    assert g.exit_code == 0, g.output
    assert "PASS" in g.stdout
    gj = json.loads(cli("gate", "--file", ex, "--format", "json").stdout)
    assert gj["verdict"] == "PASS" and gj["tier"] == "T4"

    evdir = tmp_path / "evidence"
    rep = cli("report", "--file", ex, "--evidence", evdir)
    assert rep.exit_code == 0 and rep.stdout.startswith("# Evalplane scorecard: support-refund")
    for name in ("evidence.json", "evidence.md", "manifest.json", "plan.md", "coverage.json", "scorecard.md",
                 "results.json"):
        assert (evdir / name).exists(), name
    assert json.loads((evdir / "evidence.json").read_text())["gate"]["verdict"] == "PASS"

    badge = tmp_path / "badge.json"
    assert cli("badge", "--file", ex, "--out", badge).exit_code == 0
    b = json.loads(badge.read_text())
    assert b["schemaVersion"] == 1 and b["message"].endswith("%") and "T4" in b["label"]

    buggy = cli("run", "--file", ex, env={"BUGGY": "1"})
    assert buggy.exit_code == 1, buggy.output
    assert "4 failed" in buggy.stdout
    g2 = cli("gate", "--file", ex, "--stage", "ci")
    assert g2.exit_code == 1 and "FAIL" in g2.stdout
    assert cli("gate", "--file", ex, "--warn-only").exit_code == 0


def test_run_case_filter(cli, ex):
    r = cli("run", "--file", ex, "--case", "order-status")
    assert r.exit_code == 0
    assert "order-status" in r.stdout and "small-refund" not in r.stdout
    assert "1 passed, 0 failed, 0 skipped" in r.stdout


def test_run_json_output_is_parseable(cli, ex):
    r = cli("run", "--file", ex, "--case", "order-status", "--format", "json")
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert [c["case_id"] for c in data["cases"]] == ["order-status"]


def test_gate_without_results_fails(cli, ex):
    r = cli("gate", "--file", ex)
    assert r.exit_code == 1 and "results.present" in r.stdout
    assert cli("gate", "--file", ex, "--stage", "design").exit_code == 0


def test_stale_results_fail_gate(cli, ex):
    assert cli("run", "--file", ex, "-q").exit_code == 0
    data = yaml.safe_load(ex.read_text())
    data["agent"]["description"] = "changed after the run"
    ex.write_text(yaml.safe_dump(data, sort_keys=False))
    r = cli("gate", "--file", ex)
    assert r.exit_code == 1 and "results.fresh" in r.stdout
    assert cli("gate", "--file", ex, "--allow-stale").exit_code == 0


def test_init_then_plan(cli, tmp_path):
    target = tmp_path / "new-agent"
    r = cli("init", "-y", "--dir", target, "--name", "fresh-agent")
    assert r.exit_code == 0, r.output
    profile = target / "agent.eval.yaml"
    assert profile.exists() and (target / "evals" / "starter.yaml").exists()
    data = yaml.safe_load(profile.read_text())
    assert data["agent"]["name"] == "fresh-agent" and data["risk"]["pack"] == "generic"
    p = cli("plan", "--file", profile, "--format", "json")
    assert p.exit_code == 0 and json.loads(p.stdout)["agent"] == "fresh-agent"
    assert cli("validate", "--file", profile).exit_code == 0
    again = cli("init", "-y", "--dir", target)
    assert again.exit_code == 2 and "already exists" in again.stderr


def test_audit(cli, ex, tmp_path):
    bad = tmp_path / "prod.jsonl"
    write_runs(bad, [
        Run(steps=[ToolCall(name="lookup_order", args={"order_id": "A100"}),
                   ToolCall(name="issue_refund", args={"order_id": "A100", "amount": 500})]),
        Run(steps=[ToolCall(name="lookup_order", args={"order_id": "A100"})], output="fine"),
    ])
    r = cli("audit", bad, "--file", ex)
    assert r.exit_code == 1
    assert "Audited 2 runs: 1 broke a rule" in r.stdout and "REFUND-LIMIT" in r.stdout
    good = tmp_path / "good.jsonl"
    write_runs(good, [Run(steps=[ToolCall(name="lookup_order", args={"order_id": "A100"})], output="ok")])
    ok = cli("audit", good, "--file", ex)
    assert ok.exit_code == 0 and "Audited 1 runs: 0 broke a rule" in ok.stdout


def test_audit_missing_trace_path_is_an_error(cli, ex, tmp_path):
    r = cli("audit", tmp_path / "does-not-exist.jsonl", "--file", ex)
    assert r.exit_code != 0


def test_help_panels_stay_small_and_hidden_commands_still_work(cli):
    """The visible surface is a product decision; without a test it regresses on the next feature."""
    out = cli("--help").stdout
    assert out.index("1. Start here") < out.index("2. The loop") < out.index("3. More")
    for shown in ("init", "audit", "review", "explain", "coverage", "run", "gate"):
        assert shown in out
    for hidden in ("validate", "calibrate", "attest", "badge", "schema", "packs", "mcp"):
        assert f"│ {hidden}" not in out, f"{hidden} should be hidden from --help"
        assert cli(hidden, "--help").exit_code == 0, f"{hidden} must still work"


def test_coverage_explain_matches_the_explain_command(cli, ex):
    req = "L3.tool.guard@issue_refund"
    direct = cli("explain", req, "-f", ex)
    folded = cli("coverage", "--explain", req, "-f", ex)
    assert direct.exit_code == folded.exit_code == 0
    assert "Does NOT call the action tool" in folded.stdout
    assert folded.stdout.strip() == direct.stdout.strip()
