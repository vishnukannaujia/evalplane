from pathlib import Path

from evalplane.scan import guess_side_effect, scan

FIX = Path(__file__).parent / "fixtures" / "scan_project"


def test_scan_finds_tools_across_frameworks():
    found = {t.name: t for t in scan(FIX)}
    assert set(found) == {"search_orders", "issue_refund", "send_slack_message", "update_ticket", "delete_file",
                          "spawn_task", "get_weather"}
    assert found["spawn_task"].side_effect == "write" and found["spawn_task"].args_schema["required"] == ["goal"]
    assert found["get_weather"].side_effect == "read"
    assert found["search_orders"].side_effect == "read" and found["search_orders"].sensitive
    assert found["issue_refund"].side_effect == "irreversible"
    assert found["send_slack_message"].side_effect == "external"
    assert found["update_ticket"].side_effect == "write"
    assert found["delete_file"].side_effect == "irreversible"
    assert found["search_orders"].description == "Find a customer's orders."


def test_guess_side_effect():
    assert guess_side_effect("lookup_order") == "read"
    assert guess_side_effect("place_order") == "irreversible"
    assert guess_side_effect("createEvent") == "write"
    assert guess_side_effect("do_thing", "This permanently removes the record") == "irreversible"
    assert guess_side_effect("weather") == "read"


def test_init_with_scan(tmp_path):
    from typer.testing import CliRunner

    from evalplane.cli import app
    from evalplane.models import load_profile

    r = CliRunner().invoke(app, ["init", "-y", "--pack", "support-agent", "--scan", str(FIX), "--dir", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    p = load_profile(tmp_path / "agent.eval.yaml")
    assert {t.name for t in p.tools} >= {"search_orders", "issue_refund", "send_slack_message", "update_ticket",
                                         "delete_file", "spawn_task"}
    assert p.tool("spawn_task").args_schema["required"] == ["goal"]
    names = {t.name for t in p.tools}
    assert all(not pol.check or pol.check.tool in (None, *names) for pol in p.policies)
    assert "issue-refund-happy-path" in (tmp_path / "evals" / "starter.yaml").read_text()


def test_notify_and_scale_guesses():
    assert guess_side_effect("page_oncall") == "notify"
    assert guess_side_effect("scale_service") == "write"
    assert guess_side_effect("rollback_deploy") == "irreversible"


def test_find_entrypoint_and_model(tmp_path):
    from evalplane.scan import find_entrypoint, find_model

    (tmp_path / "bot.py").write_text('MODEL = "claude-sonnet-5"\n\ndef helper(x): ...\n\ndef run(user_input, context):\n    return "ok"\n')
    assert find_entrypoint(tmp_path) == "bot:run"
    assert find_model(tmp_path) == "claude-sonnet-5"


def test_notify_tool_is_t2():
    from helpers import make_profile

    from evalplane.tiering import derive_tier

    assert derive_tier(make_profile(tools=[{"name": "page_oncall", "side_effect": "notify"}])).tier.value == "T2"


def test_init_y_scans_the_code_it_was_pointed_at(tmp_path):
    """`init -y` used to skip the scan and fall back to the pack's read-only example tools.

    Next to an agent that deletes records, that produced "Risk tier: T1 (read-only)" — a confident wrong
    answer about the one thing this tool exists to get right. Always scan; guesses get reviewed.
    """
    from typer.testing import CliRunner

    from evalplane.cli import app
    from evalplane.models import load_profile

    (tmp_path / "agent.py").write_text(
        "from langchain_core.tools import tool\n\n"
        "@tool\ndef search_docs(q: str) -> str:\n    '''Search the docs.'''\n    return ''\n\n"
        "@tool\ndef delete_record(record_id: str) -> str:\n    '''Permanently delete a record.'''\n    return ''\n")

    r = CliRunner().invoke(app, ["init", "-y", "--dir", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    assert "T4" in r.stdout and "T1 (read-only)" not in r.stdout.split("Risk tier")[1][:60]

    p = load_profile(tmp_path / "agent.eval.yaml")
    assert {t.name for t in p.tools} == {"search_docs", "delete_record"}
    assert p.tool("delete_record").side_effect.value == "irreversible"

    # an action tool with no policy leaves the user a stub naming that tool, not an empty list
    text = (tmp_path / "agent.eval.yaml").read_text()
    assert "requires_user_confirmation, tool: delete_record" in text
