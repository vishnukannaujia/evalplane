"""Regressions for issues found in the first-time-user usability test."""

from helpers import make_profile

import evalplane as ep
from evalplane.cases import Case
from evalplane.planner import build_plan
from evalplane.runner import run_cases


@ep.tool
def restart(service: str, context: dict | None = None) -> str:
    return f"restarted {service}"


def agent(user_input, context):
    state = context.setdefault("state", {"services": {"api": "up"}})  # agent's own default must survive
    try:
        restart("api", context=context)
    except RuntimeError:
        return "restart failed, paging a human"
    return f"ok {sorted(state['services'])}"


def _run(case):
    profile = make_profile(tools=[{"name": "restart", "side_effect": "write"}])
    results, runs = run_cases(profile, build_plan(profile), [case], agent=agent)
    return results.cases[0], runs[0]


def test_agent_default_state_is_not_overwritten():
    res, run = _run(Case(id="c", input="x", expect={"output": {"contains": ["api"]}}))
    assert res.status == "pass", res.reason


def test_fail_tools_is_injected_by_evalplane():
    res, run = _run(Case(id="c", input="x", context={"fail_tools": ["restart"]},
                         expect={"output": {"contains": ["failed"]}}))
    assert res.status == "pass", res.reason
    assert run.tool_calls()[0].status == "error"


def test_context_kwarg_is_not_recorded_as_tool_arg():
    _, run = _run(Case(id="c", input="x"))
    assert run.tool_calls()[0].args == {"service": "api"}


def test_crash_reports_location():
    def boom(i, c):
        raise KeyError("services")

    profile = make_profile()
    results, runs = run_cases(profile, build_plan(profile), [Case(id="c", input="x")], agent=boom)
    assert "KeyError" in runs[0].error and "(at test_ux_fixes.py:" in runs[0].error
    assert "Traceback" in runs[0].metadata["traceback"]


def test_output_failure_shows_actual_output():
    res, _ = _run(Case(id="c", input="x", expect={"output": {"contains": ["nope"]}}))
    assert "output was" in res.reason and "ok ['api']" in res.reason


def test_cli_ux_batch3(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    from typer.testing import CliRunner

    from evalplane.cli import app

    src = Path(__file__).parent.parent / "examples" / "support-refund"
    d = tmp_path / "ex"
    shutil.copytree(src, d, ignore=shutil.ignore_patterns(".evalplane", "__pycache__"))
    (d / ".git").mkdir()
    cli = CliRunner()
    monkeypatch.chdir(d / "evals")  # commands find agent.eval.yaml in a parent directory
    assert cli.invoke(app, ["validate"]).exit_code == 0
    r = cli.invoke(app, ["gate", "--stage", "pre-release"])  # dash alias accepted
    assert r.exit_code in (0, 1) and "unknown stage" not in (r.output or "")
    r = cli.invoke(app, ["attest", "L7.governance.review", "--by", "me", "--note", "looked"])
    assert r.exit_code == 0, r.output
    text = (d / "agent.eval.yaml").read_text()
    assert "{requirement: L7.governance.review, by: me" in text
    # typo in a tool field gets a suggestion
    (d / "agent.eval.yaml").write_text(text.replace("max_calls_per_run: 1", "max_call_per_run: 1", 1))
    r = cli.invoke(app, ["validate"])
    assert r.exit_code == 2 and "did you mean 'max_calls_per_run'" in r.output
    # broken YAML gives a line number, not a traceback
    (d / "agent.eval.yaml").write_text("version: 1\nagent: [unclosed\n")
    r = cli.invoke(app, ["validate"])
    assert r.exit_code == 2 and "not valid YAML at line" in r.output


def test_placeholder_cases_are_skipped_and_not_covering():
    from evalplane.coverage import analyse

    profile = make_profile(tools=[{"name": "restart", "side_effect": "write"}])
    plan = build_plan(profile)
    todo = Case(id="t", input="TODO: a request where restart is right", expect={"tools": ["restart"]})
    assert todo.is_placeholder
    assert "L3.tool.selection@restart" not in analyse(plan, profile, [todo]).covered
    results, _ = run_cases(profile, plan, [todo], agent=agent)
    assert results.cases[0].status == "skip" and "placeholder" in results.cases[0].reason
