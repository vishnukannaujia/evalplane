import json

pytest_plugins = ["pytester"]


def test_plugin_records_marked_tests_and_coverage_uses_them(pytester):
    pytester.makepyfile(test_agent="""
        import pytest

        @pytest.mark.evalplane(covers=["L4.task.success"], tools=["search"], forbidden_tools=["delete"])
        def test_good():
            assert True

        @pytest.mark.evalplane(policies=["NO-DELETE"])
        def test_bad():
            assert 1 == 2, "agent deleted a file"

        def test_unmarked():
            pass
    """)
    # the plugin auto-loads via the pytest11 entry point
    pytester.runpytest().assert_outcomes(passed=2, failed=1)
    data = json.loads((pytester.path / ".evalplane" / "pytest.json").read_text())
    by_id = {t["id"].split("::")[-1]: t for t in data["tests"]}
    assert set(by_id) == {"test_good", "test_bad"}
    assert by_id["test_good"]["status"] == "pass" and by_id["test_bad"]["status"] == "fail"
    assert "deleted a file" in by_id["test_bad"]["reason"]

    from helpers import make_profile

    from evalplane.cases import load_cases
    from evalplane.coverage import analyse
    from evalplane.planner import build_plan
    from evalplane.runner import run_cases

    (pytester.path / ".git").mkdir()
    profile = make_profile(tools=[{"name": "search"}, {"name": "delete", "side_effect": "irreversible"}],
                           policies=[{"id": "NO-DELETE", "rule": "never delete",
                                      "check": {"type": "forbidden_tool", "tool": "delete"}}])
    cases = load_cases([], base_dir=pytester.path)
    assert {c.suite for c in cases} == {"pytest"}
    plan = build_plan(profile)
    cov = analyse(plan, profile, cases)
    assert "L3.tool.guard@delete" in cov.covered and "L4.policy.adherence@NO-DELETE" in cov.covered
    results, _ = run_cases(profile, plan, cases)
    statuses = {c.case_id.split("::")[-1]: c.status for c in results.cases}
    assert statuses == {"test_good": "pass", "test_bad": "fail"}
