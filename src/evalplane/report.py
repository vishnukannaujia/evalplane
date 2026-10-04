"""Markdown/JSON reports: plan, coverage, scorecard and an evidence pack."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import __version__
from .coverage import CoverageReport
from .gate import GateResult
from .models import LAYER_NAMES, TIER_NAMES, Layer
from .planner import Plan, load_owasp
from .runner import Results

ICON = {"pass": "✅", "fail": "❌", "missing": "⬜", "skip": "⏭️", "waived": "〰️", "error": "💥",
        "PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}

FRAMEWORKS = {"NIST": "NIST AI RMF", "ISO42001": "ISO/IEC 42001 Annex A", "EUAIA": "EU AI Act",
              "OWASP": "OWASP Agentic Top 10 (2026)"}


def _control_titles() -> dict:
    from importlib import resources

    import yaml

    return yaml.safe_load((resources.files("evalplane") / "rules" / "controls.yaml").read_text()) or {}


def _fmt_value(r: dict) -> str:
    """Human-readable result for an evidence line."""
    v, metric = r.get("value"), r.get("metric")
    cases = r.get("cases") or []
    tail = f"; cases: {', '.join(cases[:4])}{'…' if len(cases) > 4 else ''}" if cases else ""
    if v is None:
        return f" ({r.get('title', '')})" if r.get("title") else ""
    if metric == "violations":
        return f" ({v:g} violations{tail})"
    if metric == "attested":
        return " (attested)" if v else " (not attested)"
    return f" ({v * 100:.0f}% of runs{tail})"


def plan_md(plan: Plan) -> str:
    lines = [f"# Eval plan: {plan.agent}", "",
             f"**Risk tier:** {plan.tier.value} ({TIER_NAMES[plan.tier]}) · pack `{plan.pack}` · "
             f"{len(plan.must)} must, {sum(r.priority == 'should' for r in plan.requirements)} should", "",
             "Why this tier:", *[f"- {r}" for r in plan.tier_reasons], ""]
    for layer in Layer:
        reqs = [r for r in plan.requirements if r.layer == layer]
        if not reqs:
            continue
        lines += [f"## {layer.value} · {LAYER_NAMES[layer]}", "",
                  "| Requirement | Priority | Stage | Method | Threshold | Why |", "|---|---|---|---|---|---|"]
        for r in reqs:
            th = "" if r.threshold is None else (f"{r.metric} ≤ {r.threshold:g}" if r.metric == "violations"
                                                 else f"{r.metric}{f'(k={r.k})' if r.k else ''} ≥ {r.threshold:g}")
            pr = "waived" if r.waived else r.priority
            lines.append(f"| `{r.id}` | {pr} | {r.stage.value} | {r.method.value} | {th} | {r.why} |")
        lines.append("")
    return "\n".join(lines)


def coverage_md(cov: CoverageReport, max_gaps: int = 15) -> str:
    lines = [f"## Eval coverage: {cov.score:g}%", "",
             "| Dimension | Score | Covered |", "|---|---|---|"]
    for k, d in cov.dimensions.items():
        s = "n/a" if d.score is None else f"{d.score * 100:.0f}%"
        lines.append(f"| {k} | {s} | {d.covered:g} / {d.total:g} |")
    lines += ["", "| Layer | Must covered |", "|---|---|"]
    for layer, (c, t) in cov.layer_matrix.items():
        lines.append(f"| {layer} {LAYER_NAMES[Layer(layer)]} | {c} / {t} |")
    if cov.gaps:
        lines += ["", f"### Top gaps ({len(cov.gaps)} total)", ""]
        for g in cov.gaps[:max_gaps]:
            lines.append(f"- **[{g.priority}] {g.kind}** `{g.ref}`: {g.message}")
    return "\n".join(lines)


def scorecard_md(plan: Plan, cov: CoverageReport, results: Results | None, gate: GateResult | None) -> str:
    head = f"# Evalplane scorecard: {plan.agent}"
    verdict = f"{ICON[gate.verdict]} **{gate.verdict}** at stage `{gate.stage}`" if gate else ""
    lines = [head, "", f"{verdict} · tier **{plan.tier.value}** · coverage **{cov.score:g}%** · "
             f"plan `{plan.plan_hash}` · evalplane {__version__}", ""]
    if results:
        s = results.summary
        lines += [f"**Cases:** {s['passed']} passed, {s['failed']} failed, {s['skipped']} skipped "
                  f"({s['attempts']} runs) · **policy/scope violations:** {s['violations']}", ""]
        fails = [c for c in results.cases if c.status in ("fail", "error")]
        if fails:
            lines += ["### Failing cases", ""]
            for c in fails:
                lines.append(f"- ❌ `{c.case_id}` ({c.c}/{c.n} runs passed): {c.reason}")
            lines.append("")
    if gate:
        blocking = [c for c in gate.checks if c.verdict == "FAIL"]
        warns = [c for c in gate.checks if c.verdict == "WARN"]
        if blocking:
            lines += ["### Blocking", "", *[f"- ❌ `{c.id}`: {c.detail}" for c in blocking], ""]
        if warns:
            lines += ["<details><summary>Warnings (%d)</summary>" % len(warns), "",
                      *[f"- ⚠️ `{c.id}`: {c.detail}" for c in warns], "", "</details>", ""]
    lines.append(coverage_md(cov, max_gaps=10))
    return "\n".join(lines) + "\n"


def evidence(plan: Plan, cov: CoverageReport, results: Results | None, gate: GateResult | None) -> dict:
    by_req = {r.id: r for r in results.requirements} if results else {}
    controls: dict[str, dict] = {}
    for r in plan.requirements:
        rr = by_req.get(r.id)
        status = rr.status if rr else ("covered" if r.id in cov.covered else "missing")
        entry = {"id": r.id, "title": r.title, "priority": r.priority, "status": status, "metric": r.metric,
                 "value": rr.value if rr else None, "threshold": r.threshold, "cases": rr.cases if rr else []}
        for ctl in r.controls + [f"OWASP:{o}" for o in r.owasp]:
            fw, _, cid = ctl.partition(":")
            c = controls.setdefault(ctl, {"framework": FRAMEWORKS.get(fw, fw), "control": cid, "requirements": [],
                                          "title": _control_titles().get(fw, {}).get(cid, "")})
            c["requirements"].append(entry)
    owasp_names = load_owasp()
    for key, c in controls.items():
        if key.startswith("OWASP:"):
            c["title"] = owasp_names.get(c["control"], "")
    return {
        "evalplane_version": __version__,
        "disclaimer": ("Which of this agent's automated tests relate to each control, and how they did. This is "
                       "input for a reviewer, not a compliance assessment: a passing test does not mean a control "
                       "is satisfied."),
        "agent": plan.agent, "tier": plan.tier.value, "tier_reasons": plan.tier_reasons,
        "plan_hash": plan.plan_hash, "catalog": plan.catalog_version,
        "gate": gate.model_dump() if gate else None,
        "coverage": {"score": cov.score},
        "controls": [controls[k] for k in sorted(controls)],
        "waivers": [r.waived.model_dump(mode="json") | {"requirement_id": r.id} for r in plan.requirements if r.waived],
        "results_git_commit": results.git_commit if results else None,
    }


def evidence_md(ev: dict) -> str:
    lines = [f"# Evidence pack: {ev['agent']}", "", f"_{ev['disclaimer']}_", "",
             f"- Tier: **{ev['tier']}**; plan `{ev['plan_hash']}`; catalog `{ev['catalog']}`",
             f"- Coverage: {ev['coverage']['score']}%",
             f"- Gate: {ev['gate']['verdict'] + ' @ ' + ev['gate']['stage'] if ev['gate'] else 'not evaluated'}",
             f"- Git commit: {ev['results_git_commit'] or 'n/a'}", ""]
    current = None
    for c in ev["controls"]:
        if c["framework"] != current:
            current = c["framework"]
            lines += [f"## {current}", ""]
        title = f" {c['title']}" if c.get("title") else ""
        lines.append(f"### {c['control']}{title}")
        for r in c["requirements"]:
            v = _fmt_value(r)
            lines.append(f"- `{r['id']}`: test result **{r['status']}**{v}")
        lines.append("")
    if ev["waivers"]:
        lines += ["## Waivers", "", *[f"- `{w['requirement_id']}`: {w['reason']} (approved by {w['approved_by']}, "
                                      f"expires {w.get('expires') or 'never'})" for w in ev["waivers"]]]
    return "\n".join(lines) + "\n"


def write_evidence(out_dir: Path, ev: dict, extra_files: dict[str, str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {"evidence.json": json.dumps(ev, indent=2, sort_keys=True, default=str), "evidence.md": evidence_md(ev),
             **extra_files}
    manifest = {}
    for name, content in files.items():
        (out_dir / name).write_text(content)
        manifest[name] = hashlib.sha256(content.encode()).hexdigest()
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return out_dir
