"""The full HTML report: one self-contained file, no external assets, no network.

Shaped like the test reports people already read (Cucumber / Allure / Playwright): summary cards, a result
chart, then every case as a scenario you can open to see its steps (user turns, tool calls with arguments
and results) and the checks that passed or failed. Searchable and filterable, works offline, light and dark.
"""

from __future__ import annotations

import html
import json
from typing import Any

from . import __version__
from .coverage import CoverageReport
from .gate import GateResult
from .models import LAYER_NAMES, Layer
from .planner import Plan
from .runner import CaseResult, Results
from .trace import Run

_ICON = {"pass": "✔", "fail": "✘", "error": "!", "skip": "–", "missing": "○", "unverified": "?", "waived": "~",
         "PASS": "✔", "WARN": "!", "FAIL": "✘"}
_OK = ("pass", "waived", "PASS")

CSS = """
:root{--bg:#fbfbfd;--fg:#1d1d20;--muted:#6b7280;--card:#fff;--line:#e5e7eb;--pass:#15803d;--passbg:#dcfce7;
--fail:#b91c1c;--failbg:#fee2e2;--warn:#a16207;--warnbg:#fef9c3;--skip:#6b7280;--skipbg:#f1f5f9;--accent:#1d4ed8;}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e7e9ee;--muted:#9aa3b2;--card:#171a21;--line:#272b35;
--pass:#4ade80;--passbg:#0f2e1d;--fail:#f87171;--failbg:#35161a;--warn:#fbbf24;--warnbg:#3a2e0b;--skip:#9aa3b2;
--skipbg:#1c2029;--accent:#8ab4ff;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,Helvetica,Arial,sans-serif}
main{max-width:1060px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:32px 0 10px}
.meta{color:var(--muted);font-size:13px;margin-bottom:16px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.92em}
.banner{display:flex;align-items:center;gap:12px;padding:14px 16px;border-radius:12px;font-weight:600;
border:1px solid var(--line)}
.banner.PASS{background:var(--passbg);color:var(--pass)} .banner.FAIL{background:var(--failbg);color:var(--fail)}
.banner.WARN{background:var(--warnbg);color:var(--warn)} .banner .sub{font-weight:400;color:var(--fg);opacity:.75}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card .n{font-size:22px;font-weight:650} .card .l{color:var(--muted);font-size:12px;margin-top:2px}
.row{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}
.chip.pass{background:var(--passbg);color:var(--pass)} .chip.fail,.chip.error{background:var(--failbg);color:var(--fail)}
.chip.skip,.chip.missing,.chip.unverified,.chip.waived{background:var(--skipbg);color:var(--skip)}
.bar{display:flex;height:10px;border-radius:999px;overflow:hidden;background:var(--skipbg);margin:6px 0 2px}
.bar i{display:block} .bar .p{background:var(--pass)} .bar .f{background:var(--fail)} .bar .s{background:var(--skip)}
table{border-collapse:collapse;width:100%;font-size:14px;background:var(--card);border:1px solid var(--line);
border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:transparent}
tr:last-child td{border-bottom:none}
details.case{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:8px 0;padding:0}
details.case>summary{cursor:pointer;padding:10px 14px;display:flex;gap:10px;align-items:center;list-style:none}
details.case>summary::-webkit-details-marker{display:none}
details.case .body{padding:0 14px 14px;border-top:1px solid var(--line)}
.name{font-weight:600} .why{color:var(--muted);font-size:13px;margin-left:auto;text-align:right;max-width:52%}
.steps{margin:10px 0 0;padding:0;list-style:none;border-left:2px solid var(--line)}
.steps li{padding:5px 0 5px 12px;font-size:13.5px}
.steps .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin-right:6px}
.steps .res{color:var(--muted);display:block;margin-top:2px;white-space:pre-wrap;word-break:break-word}
.checks{margin:10px 0 0;padding:0;list-style:none;font-size:13.5px}
.checks li{padding:2px 0} .checks .r{color:var(--muted)}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.controls input{flex:1;min-width:200px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--fg)}
.controls button{padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);
color:var(--fg);cursor:pointer;font-size:13px}
.controls button[aria-pressed="true"]{border-color:var(--accent);color:var(--accent);font-weight:600}
pre{background:var(--skipbg);border-radius:8px;padding:10px 12px;overflow:auto;font-size:12.5px;margin:6px 0 0}
.gap{padding:8px 0;border-bottom:1px solid var(--line)} .gap:last-child{border-bottom:none}
footer{color:var(--muted);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:12px}
"""

JS = """
const q=document.getElementById('q'),cases=[...document.querySelectorAll('details.case')];
let filter='all';
function apply(){const t=(q.value||'').toLowerCase();
 for(const c of cases){const s=c.dataset.status,txt=c.dataset.text;
  c.style.display=((filter==='all'||filter===s||(filter==='fail'&&s==='error'))&&(!t||txt.includes(t)))?'':'none';}}
q.addEventListener('input',apply);
for(const b of document.querySelectorAll('[data-filter]')){b.addEventListener('click',()=>{
 filter=b.dataset.filter;for(const o of document.querySelectorAll('[data-filter]'))
  o.setAttribute('aria-pressed',String(o===b));apply();});}
document.getElementById('expand').addEventListener('click',()=>{const any=cases.some(c=>!c.open);
 for(const c of cases)c.open=any;});
"""


def _e(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _short(v: Any, n: int = 300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _card(n: object, label: str) -> str:
    return f'<div class="card"><div class="n">{_e(n)}</div><div class="l">{_e(label)}</div></div>'


def _bar(passed: int, failed: int, skipped: int) -> str:
    total = max(passed + failed + skipped, 1)
    w = [round(100 * x / total, 2) for x in (passed, failed, skipped)]
    return (f'<div class="bar"><i class="p" style="width:{w[0]}%"></i><i class="f" style="width:{w[1]}%"></i>'
            f'<i class="s" style="width:{w[2]}%"></i></div>')


def _steps_html(run: Run) -> str:
    """The scenario's steps: what the user said, what the agent called, what came back."""
    items = []
    if run.input is not None:
        items.append(f'<li><span class="k">user</span>{_e(_short(run.input))}</li>')
    for s in run.steps:
        kind = getattr(s, "type", "")
        if kind == "tool":
            status = "" if s.status == "ok" else f' <span class="chip {_e(s.status)}">{_e(s.status)}</span>'
            res = f'<span class="res">→ {_e(_short(s.result))}</span>' if s.result is not None else ""
            err = f'<span class="res">{_e(_short(s.error))}</span>' if s.error else ""
            items.append(f'<li><span class="k">tool</span><code>{_e(s.name)}({_e(_short(s.args, 200))})</code>'
                         f"{status}{res}{err}</li>")
        elif kind == "user":
            items.append(f'<li><span class="k">user</span>{_e(_short(s.text))}</li>')
        elif kind == "retrieval":
            items.append(f'<li><span class="k">retrieval</span>{_e(_short(s.query or ""))}'
                         f'<span class="res">→ {_e(", ".join(s.doc_ids[:6]))}</span></li>')
        elif kind == "handoff":
            items.append(f'<li><span class="k">handoff</span>→ {_e(s.to_agent)}</li>')
        elif kind == "approval":
            items.append(f'<li><span class="k">approval</span>{_e(s.tool)}: {_e(s.decision)}</li>')
        elif kind == "llm" and s.model:
            items.append(f'<li><span class="k">llm</span>{_e(s.model)}</li>')
    if run.output is not None:
        items.append(f'<li><span class="k">answer</span>{_e(_short(run.output, 400))}</li>')
    return f'<ul class="steps">{"".join(items)}</ul>' if items else ""


def _case_html(c: CaseResult, runs: list[Run]) -> str:
    status = c.status
    mine = [r for r in runs if r.case_id == c.case_id]
    checks = []
    for a in c.attempts:
        for ch in a.checks:
            if ch.status == "pass" and len(c.attempts) > 1 and a.attempt:
                continue  # repeats: show the passing checks once
            reason = f' <span class="r">{_e(ch.reason)}</span>' if ch.reason else ""
            attempt = f" [{a.attempt + 1}]" if len(c.attempts) > 1 else ""
            checks.append(f'<li>{_ICON.get(ch.status, "·")} <code>{_e(ch.name)}</code>{attempt}{reason}</li>')
    repeats = f" <span class='chip skip'>{c.c}/{c.n}</span>" if c.n > 1 else ""
    body = "".join(f'<div class="body">{_steps_html(r)}'
                   + (f'<ul class="checks">{"".join(checks)}</ul>' if r is mine[0] and checks else "")
                   + "</div>" for r in mine[:1]) or \
        (f'<div class="body"><ul class="checks">{"".join(checks)}</ul></div>' if checks else
         f'<div class="body"><p class="meta">{_e(c.reason or "no details")}</p></div>')
    text = " ".join([c.case_id, c.suite, c.reason, " ".join(c.covers)]).lower()
    return (f'<details class="case" data-status="{_e(status)}" data-text="{_e(text)}">'
            f'<summary><span class="chip {_e(status)}">{_ICON.get(status, "·")} {_e(status)}</span>'
            f'<span class="name">{_e(c.case_id)}</span><code class="meta">{_e(c.suite)}</code>{repeats}'
            f'<span class="why">{_e(_short(c.reason, 120))}</span></summary>{body}</details>')


def _tier_reason(plan: Plan) -> str:
    """Why this tier: the reason for the tool that set it, not just the first one listed."""
    for r in plan.tier_reasons:
        if f": {plan.tier.value} (" in r:
            return r
    return plan.tier_reasons[0] if plan.tier_reasons else ""


def html_report(plan: Plan, coverage: CoverageReport, results: Results | None, gate: GateResult | None,
                runs: list[Run] | None = None) -> str:
    runs = runs or []
    verdict = gate.verdict if gate else "N/A"
    s = results.summary if results else {"cases": 0, "passed": 0, "failed": 0, "skipped": 0, "attempts": 0,
                                         "violations": 0}
    n = coverage.counts
    cards = [
        _card(f"{s['passed']}/{s['cases']}", "cases passed"),
        _card(s["failed"], "cases failed"),
        _card(s["violations"], "rule violations"),
        _card(f"{n.get('must_covered', 0)}/{n.get('must', 0)}", "must-have evals covered"),
        _card(n.get("action_tools_without_negative", 0), 'action tools without a "should NOT act" test'),
        _card(f"{coverage.score:g}%", "coverage score"),
    ]
    out = [
        "<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>Evalplane · {_e(plan.agent)}</title>", f"<style>{CSS}</style>", "</head><body><main>",
        f"<h1>{_e(plan.agent)}</h1>",
        f'<div class="meta">tier <b>{_e(plan.tier.value)}</b> ({_e(_tier_reason(plan))})'
        f" · pack <code>{_e(plan.pack)}</code> · plan <code>{_e(plan.plan_hash)}</code>"
        + (f" · commit <code>{_e(results.git_commit[:12])}</code>" if results and results.git_commit else "")
        + (f" · {_e(results.started_at)}" if results else "") + "</div>",
        f'<div class="banner {_e(verdict)}"><span>{_ICON.get(verdict, "·")} Gate {_e(verdict)}</span>'
        f'<span class="sub">stage {_e(gate.stage) if gate else "-"}</span></div>',
        f'<div class="cards">{"".join(cards)}</div>',
        _bar(s["passed"], s["failed"], s["skipped"]),
    ]

    if not results or not results.cases:
        out += ['<div class="card"><b>No results yet.</b> Run <code>evalplane run</code> (or '
                '<code>evalplane run --traces traces/</code>) to fill this report; the plan and gaps below come '
                "from the profile and your eval cases.</div>"]
    if results and results.cases:
        out += ["<h2>Cases</h2>",
                '<div class="controls"><input id="q" placeholder="Filter cases, reasons, requirements…">'
                '<button data-filter="all" aria-pressed="true">All</button>'
                '<button data-filter="fail">Failed</button><button data-filter="pass">Passed</button>'
                '<button data-filter="skip">Skipped</button><button id="expand">Expand / collapse</button></div>']
        order = {"fail": 0, "error": 1, "skip": 2, "pass": 3}
        out += [_case_html(c, runs) for c in sorted(results.cases, key=lambda c: (order.get(c.status, 9), c.case_id))]

    if results and results.violations:
        rows = "".join(f"<tr><td><code>{_e(v.policy)}</code></td><td>{_e(v.case_id or v.run_id[:12])}</td>"
                       f"<td>{_e(v.detail)}</td></tr>" for v in results.violations[:50])
        out += ["<h2>Rule violations</h2>",
                f"<table><tr><th>policy</th><th>where</th><th>detail</th></tr>{rows}</table>"]

    by_layer: dict[str, list] = {}
    rr = {r.id: r for r in results.requirements} if results else {}
    for r in plan.requirements:
        if r.priority == "must":
            by_layer.setdefault(r.layer.value, []).append(r)
    if by_layer:
        rows = []
        for layer, reqs in sorted(by_layer.items()):
            rows.append(f'<tr><td colspan="4"><b>{_e(layer)} · {_e(LAYER_NAMES[Layer(layer)])}</b></td></tr>')
            for r in reqs:
                res = rr.get(r.id)
                st = "waived" if r.waived else (res.status if res else ("covered" if r.id in coverage.covered
                                                                        else "missing"))
                val = "" if not res or res.value is None else (f"{res.value:g}" if res.metric == "violations"
                                                               else f"{res.value * 100:.0f}%")
                rows.append(f"<tr><td><code>{_e(r.id)}</code></td>"
                            f'<td><span class="chip {_e(st if st in _ICON else "skip")}">{_e(st)}</span></td>'
                            f"<td>{_e(val)}</td><td class='meta'>{_e(res.reason if res else r.title)}</td></tr>")
        out += ["<h2>Requirements</h2>",
                f"<table><tr><th>requirement</th><th>status</th><th>value</th><th></th></tr>{''.join(rows)}</table>"]

    if coverage.gaps:
        items = []
        for g in coverage.gaps[:15]:
            sug = f"<pre>{_e(g.suggestion)}</pre>" if g.suggestion else ""
            items.append(f'<div class="gap"><span class="chip {"fail" if g.priority == "must" else "skip"}">'
                         f'{_e(g.priority)}</span> <code>{_e(g.ref)}</code> {_e(g.message)}{sug}</div>')
        out += [f"<h2>Gaps ({len(coverage.gaps)})</h2>", f'<div class="card">{"".join(items)}</div>']

    out += [f"<footer>evalplane {_e(__version__)} · generated from "
            f"{_e(results.runs_file if results and results.runs_file else 'the latest results')}</footer>",
            "</main>", f"<script>{JS}</script>", "</body></html>"]
    return "\n".join(out)
