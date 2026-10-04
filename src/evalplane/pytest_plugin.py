"""pytest plugin: let tests you already have count toward Evalplane coverage and gates.

    import pytest

    @pytest.mark.evalplane(covers=["L4.safety.goal_hijack"], tools=["lookup_order"],
                           forbidden_tools=["issue_refund"], policies=["REFUND-LIMIT"])
    def test_refuses_big_refund(agent): ...

After `pytest`, outcomes of marked tests are written to `.evalplane/pytest.json` (in the rootdir).
`evalplane coverage` counts them, and `evalplane run` / `gate` use their pass/fail results.
The plugin does nothing unless at least one test carries the marker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_KEY = pytest.StashKey[dict]()


def pytest_configure(config):
    config.stash[_KEY] = {}
    config.addinivalue_line(
        "markers",
        "evalplane(covers=[], tools=[], forbidden_tools=[], policies=[], owasp=[]): count this test toward "
        "Evalplane coverage for the listed requirements.",
    )


def _reason(rep) -> str:
    crash = getattr(getattr(rep, "longrepr", None), "reprcrash", None)
    if crash is not None and getattr(crash, "message", None):
        return crash.message.splitlines()[0][:300]
    text = (rep.longreprtext or "").strip()
    return text.splitlines()[-1][:300] if text else "failed"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    marker = item.get_closest_marker("evalplane")
    if marker is None:
        return
    rep = outcome.get_result()
    results = item.config.stash.setdefault(_KEY, {})
    entry = results.setdefault(item.nodeid, {
        "id": item.nodeid,
        "covers": list(marker.kwargs.get("covers", [])),
        "tools": list(marker.kwargs.get("tools", [])),
        "forbidden_tools": list(marker.kwargs.get("forbidden_tools", [])),
        "policies": list(marker.kwargs.get("policies", [])),
        "owasp": list(marker.kwargs.get("owasp", [])),
        "status": "pass",
        "reason": "",
    })
    if rep.skipped and entry["status"] == "pass":
        entry["status"] = "skip"
    elif rep.failed:
        entry["status"] = "fail"
        entry["reason"] = _reason(rep)


def pytest_sessionfinish(session, exitstatus):
    results = session.config.stash.get(_KEY, {})
    if not results:
        return
    out = Path(str(session.config.rootpath)) / ".evalplane" / "pytest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    import datetime as dt

    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out.write_text(json.dumps({"finished_at": stamp, "tests": list(results.values())}, indent=2))
