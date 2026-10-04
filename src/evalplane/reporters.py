"""CI reporters: JUnit XML (GitLab, Jenkins, Azure), a self-contained HTML report and a sticky PR comment.

Pure functions of (plan, coverage, results, gate): no I/O, so the CLI and the GitHub Action can share them.
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET

from . import __version__
from .coverage import CoverageDiff, CoverageReport
from .gate import GateResult
from .models import Stage
from .planner import Plan
from .runner import CaseResult, Results
from .trace import Run

PR_MARKER = "<!-- evalplane -->"
REQ_FAILING = ("fail", "missing", "unverified")
_ICON = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "pass": "✅", "fail": "❌", "error": "💥", "skip": "⏭️"}

# XML 1.0 forbids most control characters, even escaped; agent output can contain anything
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _x(text: object) -> str:
    return _XML_ILLEGAL.sub("�", str(text or ""))


def _failed_checks(c: CaseResult) -> list[str]:
    """One line per failing check of each failing attempt (the detail behind a case's one-line reason)."""
    out: list[str] = []
    for a in c.attempts:
        if a.passed:
            continue
        for ch in a.checks:
            if ch.status not in ("pass", "skip"):
                out.append(f"attempt {a.attempt}: {ch.name}: {ch.reason}".rstrip(": "))
    return out


def _counted(suite: ET.Element) -> None:
    cases = suite.findall("testcase")
    suite.set("tests", str(len(cases)))
    suite.set("failures", str(sum(1 for t in cases if t.find("failure") is not None)))
    suite.set("errors", str(sum(1 for t in cases if t.find("error") is not None)))
    suite.set("skipped", str(sum(1 for t in cases if t.find("skipped") is not None)))


# --------------------------------------------------------------------------- JUnit


def junit_xml(results: Results, plan: Plan, stage: Stage = Stage.ci) -> str:
    """JUnit XML: a testsuite per eval suite (one testcase per case) plus a `requirements` testsuite with one
    testcase per must requirement, failing when the requirement failed, has no evidence, or is unverified."""
    root = ET.Element("testsuites", name=_x(f"evalplane {plan.agent}"))
    suites: dict[str, ET.Element] = {}
    for c in results.cases:
        suite_name = c.suite or "evals"
        suite = suites.get(suite_name)
        if suite is None:
            suite = suites[suite_name] = ET.SubElement(root, "testsuite", name=_x(suite_name))
        tc = ET.SubElement(suite, "testcase", classname=_x(suite_name), name=_x(c.case_id))
        if c.status in ("fail", "error"):
            msg = c.reason or c.status
            f = ET.SubElement(tc, "failure", message=_x(msg), type="error" if c.status == "error" else "fail")
            detail = [f"{c.c}/{c.n} runs passed", *_failed_checks(c)]
            f.text = _x("\n".join(detail))
        elif c.status == "skip":
            ET.SubElement(tc, "skipped", message=_x(c.reason or "skipped"))
        if c.covers:
            props = ET.SubElement(tc, "properties")
            ET.SubElement(props, "property", name="covers", value=_x(", ".join(c.covers)))

    req_suite = ET.SubElement(root, "testsuite", name="requirements")
    by_id = {r.id: r for r in results.requirements}
    for req in plan.requirements:
        if req.priority != "must":
            continue
        rr = by_id.get(req.id)
        status = "waived" if req.waived else (rr.status if rr else "missing")
        tc = ET.SubElement(req_suite, "testcase", classname=f"requirements.{req.layer.value}", name=_x(req.id))
        if status in REQ_FAILING and (req.stage == Stage.production or req.stage > stage):
            ET.SubElement(tc, "skipped", message=f"required at stage {req.stage.value}, not {stage.value}")
        elif status in REQ_FAILING:
            reason = rr.reason if rr and rr.reason else {"missing": "no eval covers this requirement",
                                                         "unverified": "not verified"}.get(status, status)
            f = ET.SubElement(tc, "failure", message=_x(f"{status}: {reason}"), type=status)
            f.text = _x(f"{req.title}\n{status}: {reason}" + (f"\ncases: {', '.join(rr.cases)}" if rr and rr.cases
                                                              else ""))
        elif status in ("skip", "waived"):
            ET.SubElement(tc, "skipped", message=_x(status + (f": {rr.reason}" if rr and rr.reason else "")))

    for suite in root.findall("testsuite"):
        _counted(suite)
    for k in ("tests", "failures", "errors", "skipped"):
        root.set(k, str(sum(int(s.get(k, "0")) for s in root.findall("testsuite"))))
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# --------------------------------------------------------------------------- HTML

_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--card:#f6f8fa;
--pass:#1a7f37;--warn:#9a6700;--fail:#cf222e;--passbg:#dafbe1;--warnbg:#fff8c5;--failbg:#ffebe9}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--card:#161b22;
--pass:#3fb950;--warn:#d29922;--fail:#f85149;--passbg:#12261e;--warnbg:#272115;--failbg:#2d1215}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
Helvetica,Arial,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px;padding-bottom:4px;
border-bottom:1px solid var(--line)}h3{font-size:14px;margin:20px 0 6px}
.meta{color:var(--muted)}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--card);
padding:1px 4px;border-radius:4px;overflow-wrap:anywhere}
.verdict{display:inline-block;font-weight:700;padding:4px 12px;border-radius:6px;margin:12px 0}
.PASS,.pass{color:var(--pass)}.WARN,.warn,.skip,.waived{color:var(--warn)}.FAIL,.fail,.error,.missing,.unverified{
color:var(--fail)}
.verdict.PASS{background:var(--passbg)}.verdict.WARN{background:var(--warnbg)}.verdict.FAIL{background:var(--failbg)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px}
.stat b{display:block;font-size:20px}.stat span{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;margin:4px 0}
th,td{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--line)}
th{font-size:12px;color:var(--muted);font-weight:600}
td.num{white-space:nowrap;font-variant-numeric:tabular-nums}
ul{padding-left:20px}li{margin:4px 0}
.scroll{overflow-x:auto}
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""


def _e(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _fmt_rr(rr) -> str:
    if rr is None or rr.value is None:
        return ""
    if rr.metric == "violations":
        return f"{rr.value:g} violations"
    if rr.metric == "attested":
        return "attested" if rr.value else "not attested"
    th = f" / {rr.threshold:g}" if rr.threshold is not None else ""
    return f"{rr.value:.2f}{th}"


def html_report(plan: Plan, coverage: CoverageReport, results: Results | None, gate: GateResult | None,
                runs: list[Run] | None = None) -> str:
    """A single self-contained HTML page: summary,every case as an openable scenario with its steps, rule
    violations, requirements and gaps. See evalplane.html_report."""
    from .html_report import html_report as _full

    return _full(plan, coverage, results, gate, runs)


def cucumber_json(results: Results, plan: Plan) -> str:
    """Cucumber JSON: eval suites become features, cases become scenarios and checks become steps.
    Consumed by the Jenkins cucumber-reports plugin, cucumber-html-reporter, ReportPortal and others."""
    features: dict[str, dict] = {}
    for c in results.cases:
        feat = features.setdefault(c.suite, {
            "uri": c.source or f"evals/{c.suite}", "id": _slug(c.suite), "keyword": "Feature",
            "name": f"{c.suite} ({plan.agent}, {plan.tier.value})", "description": "", "line": 1, "elements": [],
            "tags": [{"name": f"@{_slug(plan.agent)}"}],
        })
        steps = []
        for a in c.attempts:
            for ch in a.checks:
                status = {"pass": "passed", "fail": "failed", "skip": "skipped", "error": "failed"}.get(ch.status,
                                                                                                         "undefined")
                steps.append({
                    "keyword": "Then ", "name": ch.name + (f" (attempt {a.attempt + 1})" if len(c.attempts) > 1 else ""),
                    "line": 1,
                    "result": {"status": status, "duration": 0,
                               **({"error_message": ch.reason} if status == "failed" and ch.reason else {})},
                })
        if not steps:
            steps = [{"keyword": "Then ", "name": c.reason or "not run", "line": 1,
                      "result": {"status": "skipped", "duration": 0}}]
        feat["elements"].append({
            "id": f"{_slug(c.suite)};{_slug(c.case_id)}", "keyword": "Scenario", "name": c.case_id,
            "description": c.reason, "line": 1, "type": "scenario",
            "tags": [{"name": f"@{_slug(t)}"} for t in ([*c.covers[:5], *( ["repeat"] if c.n > 1 else [])])],
            "steps": steps,
        })
    return json.dumps(list(features.values()), indent=2)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "x"


def _one_line(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pr_comment_md(plan: Plan, coverage: CoverageReport, results: Results | None, gate: GateResult | None,
                  diff: CoverageDiff | None = None) -> str:
    """A compact sticky PR comment. Starts with PR_MARKER so CI can find and update its previous comment."""
    verdict = gate.verdict if gate else "N/A"
    lines = [PR_MARKER,
             f"### {_ICON.get(verdict, '')} Evalplane: **{verdict}**"
             + (f" at stage `{gate.stage}`" if gate else "") + f" · `{plan.agent}` ({plan.tier.value})", ""]
    facts = [f"coverage **{coverage.score:g}%**"]
    if diff is not None:
        arrow = "▲" if diff.delta > 0 else "▼" if diff.delta < 0 else "="
        facts[0] += f" {arrow} {diff.delta:+g} vs base"
    k = coverage.counts
    if k:
        facts.append(f"must covered **{k.get('must_covered', 0)}/{k.get('must', 0)}**")
    if results:
        s = results.summary
        facts.append(f"cases **{s['passed']} passed, {s['failed']} failed, {s['skipped']} skipped**")
        facts.append(f"violations **{s['violations']}**")
    else:
        facts.append("no results (run `evalplane run`)")
    if gate:
        n = gate.counts()
        facts.append(f"gate checks {n['FAIL']} fail / {n['WARN']} warn")
    lines += [" · ".join(facts), ""]

    fails = [c for c in results.cases if c.status in ("fail", "error")] if results else []
    if fails:
        lines += [f"**Failing cases ({len(fails)})**", ""]
        lines += [f"- `{c.case_id}` ({c.c}/{c.n}): {_one_line(c.reason)}" for c in fails[:10]]
        if len(fails) > 10:
            lines.append(f"- … and {len(fails) - 10} more")
        lines.append("")

    if diff is not None:
        if diff.new_gaps:
            lines += [f"**New gaps introduced by this PR ({len(diff.new_gaps)})**", ""]
            lines += [f"- [{g.priority}] `{g.ref}`: {_one_line(g.message)}" for g in diff.new_gaps[:10]]
            if len(diff.new_gaps) > 10:
                lines.append(f"- … and {len(diff.new_gaps) - 10} more")
            lines.append("")
        else:
            lines += ["No new coverage gaps introduced by this PR."
                      + (f" {len(diff.closed_gaps)} closed." if diff.closed_gaps else ""), ""]

    if gate:
        blocking = [c for c in gate.checks if c.verdict == "FAIL"]
        if blocking:
            lines += [f"<details><summary>Blocking checks ({len(blocking)})</summary>", ""]
            lines += [f"- `{c.id}`: {_one_line(c.detail, 120)}" for c in blocking[:15]]
            if len(blocking) > 15:
                lines.append(f"- … and {len(blocking) - 15} more")
            lines += ["", "</details>", ""]

    lines.append(f"<sub>evalplane {__version__} · plan `{plan.plan_hash}` · full report in the job artifacts</sub>")
    return "\n".join(lines) + "\n"
