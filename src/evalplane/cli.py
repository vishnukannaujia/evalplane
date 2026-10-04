"""`evalplane` command line: init, plan, coverage, run, gate, report, audit."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .cases import load_cases
from .coverage import analyse
from .errors import ConfigError, EvalplaneError
from .gate import evaluate_gate
from .models import LAYER_NAMES, TIER_NAMES, AgentProfile, Layer, Stage, load_profile
from .packs import list_packs
from .planner import build_plan
from .planner import explain as explain_req
from .report import coverage_md, evidence, plan_md, scorecard_md, write_evidence
from .runner import audit_traces, load_agent, load_results, load_trace_paths, run_cases, save_results
from .tiering import derive_tier

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
                  help="Evalplane: which evals your agent needs, what's untested, and whether it can ship.")
console = Console()
err = Console(stderr=True, soft_wrap=True)

FILE = typer.Option("agent.eval.yaml", "--file", "-f", help="Agent profile.")
FORMAT = typer.Option("table", "--format", help="table | md | json")


def _show_version(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def _root(version: bool = typer.Option(False, "--version", "-V", callback=_show_version, is_eager=True,
                                       help="Show the version and exit.")) -> None:
    """Typer reads --version through the callback above; nothing else happens here."""


def _fail(msg: str, code: int = 2) -> None:
    err.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(code)


def _floor(tier: str, stage: str = "ci") -> int:
    from .planner import load_thresholds

    return load_thresholds()["coverage_floor"][stage][tier]


def _group_violations(violations) -> list[tuple[tuple[str, str, str, str], int]]:
    """Collapse repeats of the same violation (e.g. across pass^k attempts) into one line with a count."""
    from collections import Counter

    keys = Counter((v.policy, (f"case {v.case_id} (run {v.run_id[:12]})" if v.case_id else f"run {v.run_id[:12]}"),
                    v.detail, getattr(v, "effect", "effective"))
                   for v in violations)
    return sorted(keys.items(), key=lambda kv: (-kv[1], kv[0]))


def _blocked_note(effect: str) -> str:
    """A violation whose call errored changed nothing; say so rather than letting it read as damage done."""
    return " [dim][the call errored, so nothing changed][/dim]" if effect == "blocked" else ""


def _stage(value: str | Stage | None) -> Stage | None:
    if value is None or isinstance(value, Stage):
        return value
    v = value.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return Stage(v)
    except ValueError:
        _fail(f"unknown stage {value!r}: use one of design, ci, pre-release, production")
    raise AssertionError


def _find(file: str) -> str:
    """agent.eval.yaml in the current directory, or the nearest parent (so commands work from evals/)."""
    p = Path(file)
    if p.exists() or p.is_absolute() or file != "agent.eval.yaml":
        return file
    for d in Path.cwd().parents:
        if (d / file).exists():
            return str(d / file)
        if (d / ".git").exists():
            break
    return file


def _did_you_mean(loc: tuple, model_hint: str) -> str:
    import difflib

    from .cases import Case, Expectations
    from .models import AgentInfo, Features, Policy, PolicyCheck, RiskInfo, Tool

    key = str(loc[-1])
    fields: set[str] = set()
    for m in (AgentProfile, AgentInfo, RiskInfo, Features, Tool, Policy, PolicyCheck, Case, Expectations):
        fields |= set(m.model_fields)
    close = difflib.get_close_matches(key, sorted(fields), n=1, cutoff=0.6)
    return f" (did you mean '{close[0]}'?)" if close else ""


def format_validation(e: ValidationError, where: str) -> str:
    lines = []
    for er in e.errors():
        loc = ".".join(str(x) for x in er["loc"])
        hint = _did_you_mean(er["loc"], where) if er["type"] == "extra_forbidden" else ""
        lines.append(f"  {loc}: {er['msg']}{hint}")
    return f"{where} is invalid:\n" + "\n".join(lines)


def _load(file: str) -> AgentProfile:
    import yaml

    file = _find(file)
    try:
        return load_profile(file)
    except ValidationError as e:
        _fail(format_validation(e, file))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        _fail(f"{file} is not valid YAML{where}: {getattr(e, 'problem', e)}")
    except ConfigError as e:
        _fail(str(e))
    raise AssertionError  # unreachable


def _context(file: str):
    profile = _load(file)
    try:
        decision = derive_tier(profile)
        plan = build_plan(profile, decision)
        cases = load_cases(profile.evals.cases, base_dir=profile.base_dir)
    except EvalplaneError as e:
        _fail(str(e))
    return profile, plan, cases


def _emit(text: str, out: Path | None) -> None:
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        err.print(f"[dim]wrote {out}[/dim]")
    else:
        console.print(text, markup=False, highlight=False, soft_wrap=True)


def _traces(profile: AgentProfile, paths: list[str] | None):
    try:
        return load_trace_paths(profile, paths)
    except (ConfigError, ValueError) as e:
        _fail(str(e))


# --------------------------------------------------------------------------- init


def _examples_root() -> Path:
    """Examples ship inside the wheel as evalplane/_examples; in a source checkout they are in ./examples."""
    packaged = Path(__file__).parent / "_examples"
    return packaged if packaged.exists() else Path(__file__).resolve().parents[2] / "examples"

@app.command(rich_help_panel="1. Start here")
def init(
    pack: str | None = typer.Option(None, "--pack", "-p", help="Add a sector pack's checks (see `evalplane packs`)."),
    name: str | None = typer.Option(None, help="Agent name (lowercase, dashes)."),
    non_interactive: bool = typer.Option(False, "--yes", "-y", help="No questions: use guesses and defaults."),
    directory: Path = typer.Option(Path("."), "--dir", help="Where to create the files."),
    force: bool = typer.Option(False, help="Overwrite an existing agent.eval.yaml."),
    scan_dir: Path | None = typer.Option(None, "--scan",
                                         help="Find tools in this code directory (default: --dir when interactive)."),
    example: str | None = typer.Option(None, "--example",
                                       help="Copy a runnable example (support-refund, faq-messages, tau2-airline)."),
):
    """Create agent.eval.yaml from your code's tools and a few questions (plus starter eval cases)."""
    from .init_wizard import copy_example, run_init

    if example:
        return copy_example(example, directory, force, _examples_root())
    try:
        run_init(pack=pack, name=name, non_interactive=non_interactive, directory=directory, force=force,
                 scan_dir=scan_dir)
    except typer.Abort:
        _fail("input ended before init finished. Run it in a terminal, or use `evalplane init -y` "
              "(no questions: guessed side effects, generic pack)", code=1)


# --------------------------------------------------------------------------- validate / plan / explain


@app.command(hidden=True)
def validate(file: str = FILE):
    """Check agent.eval.yaml and the eval cases for errors."""
    profile, plan, cases = _context(file)
    known = {r.id for r in plan.requirements}
    unknown = sorted({cid for c in cases for cid in c.covers if cid not in known})
    console.print(f"[green]✓[/green] {file}: agent [bold]{profile.agent.name}[/bold], tier {plan.tier.value}, "
                  f"{len(profile.tools)} tools, {len(profile.policies)} policies, {len(cases)} cases")
    for u in unknown:
        console.print(f"[yellow]warning:[/yellow] a case covers {u!r}, which is not in this agent's plan")
    untestable = [p.id for p in profile.policies if not p.check]
    for pid in untestable:
        console.print(f"[yellow]warning:[/yellow] policy {pid} has no check, so nothing enforces it")
    from .coverage import claim_problem

    for c in cases:
        for rid in c.covers:
            req = plan.get(rid)
            problem = claim_problem(req, c) if req else None
            if problem:
                console.print(f"[yellow]warning:[/yellow] case {c.id} claims {rid} but {problem}; the claim is ignored")
        if c.is_placeholder:
            console.print(f"[yellow]warning:[/yellow] case {c.id} still has a placeholder input ({c.source}); "
                          "it is skipped and covers nothing until you fill it in")
    tools = {t.name for t in profile.tools}
    for c in cases:
        for t in sorted((c.exercised_tools() | set(c.expect.forbidden_tools)) - tools):
            console.print(f"[yellow]warning:[/yellow] case {c.id} names tool {t!r}, which is not declared under tools:")
    from .scan import scan as _scan

    in_code = {t.name: t for t in _scan(profile.base_dir)}
    for name in sorted(set(in_code) - tools):
        t = in_code[name]
        console.print(f"[yellow]warning:[/yellow] tool {name!r} ({t.file}:{t.line}, looks like {t.side_effect}) is in "
                      "your code but not in agent.eval.yaml: add it so the plan and tier include it")
    if profile.agent.entrypoint:
        try:
            load_agent(profile.agent.entrypoint, profile.base_dir)
        except ConfigError as e:
            console.print(f"[red]error:[/red] agent.entrypoint: {e}")
            raise typer.Exit(2)




@app.command(rich_help_panel="1. Start here")
def explain(requirement: str, file: str = FILE):
    """Explain one requirement: why it applies and how to test it."""
    _, p, _ = _context(file)
    console.print(explain_req(requirement, p), markup=False)


# --------------------------------------------------------------------------- coverage


@app.command(rich_help_panel="2. The loop")
def coverage(file: str = FILE, fmt: str = FORMAT,
             traces: list[str] | None = typer.Option(None, "--traces", help="Trace files/dirs to compare against."),
             min_score: float | None = typer.Option(None, "--min-score", help="Exit 1 if coverage is below this."),
             gaps: int = typer.Option(10, "--gaps", help="How many gaps to show."),
             show_suggestions: bool = typer.Option(True, "--suggest/--no-suggest"),
             baseline: str | None = typer.Option(None, "--baseline",
                                                    help="Coverage JSON from the base branch: report only what changed."),
             fail_on_new_gaps: bool = typer.Option(False, "--fail-on-new-gaps",
                                                   help="With --baseline: exit 1 if the change adds a must gap."),
             explain_id: str | None = typer.Option(None, "--explain", metavar="REQUIREMENT",
                                                   help="Why one requirement applies and how to test it, "
                                                        "then exit (same as `evalplane explain`)."),
             out: Path | None = typer.Option(None, "--out", "-o")):
    """What your evals cover, and a prioritised list of what they don't."""
    if explain_id:   # you want this while reading the gap list, not as a separate trip
        _, _p, _ = _context(file)
        console.print(explain_req(explain_id, _p), markup=False)
        return
    profile, p, cases = _context(file)
    runs = _traces(profile, traces)
    cov = analyse(p, profile, cases, runs)
    if baseline:
        from .coverage import CoverageReport, diff, diff_md

        try:
            before = CoverageReport.model_validate_json(Path(baseline).read_text())
        except (OSError, ValueError):  # no baseline yet (e.g. first PR adding evalplane): everything is new
            before = cov.model_copy(update={"score": 0.0, "gaps": []})
        d = diff(before, cov)
        _emit(diff_md(d), out)
        if fail_on_new_gaps and any(g.priority == "must" for g in d.new_gaps):
            raise typer.Exit(1)
        return
    if fmt == "json":
        _emit(cov.model_dump_json(indent=2), out)
    elif fmt == "md":
        _emit(coverage_md(cov, max_gaps=gaps), out)
    else:
        n = cov.counts

        def line(text: str, ok: bool) -> None:
            console.print(f"  {'[green]✓[/]' if ok else '[red]✗[/]'} {text}")

        unreviewed = sum(1 for c in cases if "unreviewed" in c.tags)
        console.print(f"[bold]{escape(p.agent)}[/bold] ({p.tier.value}) · {n['cases']} cases"
                      + (f" [dim](+{unreviewed} generated, not counted until reviewed)[/dim]" if unreviewed else ""))
        line(f"must-have evals covered: {n['must_covered']} of {n['must']}", n["must_covered"] == n["must"])
        if n["action_tools"]:
            k = n["action_tools_without_negative"]
            line(f"action tools with a \"should NOT act\" test: {n['action_tools'] - k} of {n['action_tools']}"
                 + (f"  ({k} missing)" if k else ""), k == 0)
        if n["policies"]:
            k = n["policies_untempted"]
            line(f"policies some case tries to break: {n['policies'] - k} of {n['policies']}"
                 + (f"  ({k} untested)" if k else ""), k == 0)
            k = n["policies_without_check"]
            line(f"policies checked by code on every run: {n['policies'] - k} of {n['policies']}"
                 + (f"  ({k} unchecked)" if k else ""), k == 0)
        console.print(f"  [dim]coverage score {cov.score:g} (the {p.tier.value} CI gate needs "
                      f"{_floor(p.tier.value)})[/dim]")
        if cov.gaps:
            console.print(f"\n[bold]Top gaps[/bold] ({len(cov.gaps)} total, most important first):")
            for i, g in enumerate(cov.gaps[:gaps], 1):
                console.print(f" {i:>2}. [{'red' if g.priority == 'must' else 'yellow'}]{g.priority}[/] "
                              f"[cyan]{g.ref}[/cyan]: {g.message}")
                if show_suggestions and g.suggestion and i <= 3:
                    console.print("\n".join("       " + ln for ln in g.suggestion.splitlines()), markup=False,
                                  style="dim")
            if cov.gaps:   # the mechanism is the useful part, so point at it from where people read
                console.print("[dim]  why does one of these apply? "
                              "evalplane coverage --explain <id>[/dim]")
        if cov.tools_seen_untested:
            console.print(f"\n[yellow]Seen in traces but never tested:[/yellow] {', '.join(cov.tools_seen_untested)}")
    if min_score is not None and cov.score < min_score:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- run / audit


@app.command(rich_help_panel="2. The loop")
def run(file: str = FILE,
        agent: str | None = typer.Option(None, "--agent", help="module:function (default: agent.entrypoint)."),
        traces: list[str] | None = typer.Option(None, "--traces", help="Replay recorded runs instead of calling the agent."),
        stage: str | None = typer.Option(None, "--stage", help="Only run cases up to this stage (ci, pre-release)."),
        repeat: int | None = typer.Option(None, "--repeat", help="Override repeats per case (for pass^k)."),
        case: list[str] | None = typer.Option(None, "--case", help="Only these case ids."),
        judge: str | None = typer.Option(None, "--judge", help="module:function returning (score, reason)."),
        jobs: int = typer.Option(1, "--jobs", "-j", help="Run live attempts in parallel (agent must be thread-safe)."),
        timeout: float | None = typer.Option(None, "--timeout", help="Seconds per attempt before it counts as a timeout."),
        fmt: str = FORMAT, quiet: bool = typer.Option(False, "-q", help="Only print the summary line."),
        traceback: bool = typer.Option(False, "--traceback", help="Show the agent's stack trace for crashed runs.")):
    """Run eval cases (live, or replaying traces) and score them. No LLM calls unless you add a judge."""
    stage = _stage(stage)
    profile, p, cases = _context(file)
    if case:
        cases = [c for c in cases if c.id in case]
    runs = _traces(profile, traces) if (traces or profile.evals.traces) else []
    agent_fn = None
    ref = agent or profile.agent.entrypoint
    if ref and not traces:
        try:
            agent_fn = load_agent(ref, profile.base_dir)
        except ConfigError as e:
            _fail(str(e))
    judge_fn = load_agent(judge, profile.base_dir) if judge else None
    if not cases:
        _fail("no eval cases found (add YAML cases under evals/, or run `evalplane init`)")
    results, all_runs = run_cases(profile, p, cases, agent=agent_fn, traces=runs, judge=judge_fn, stage=stage,
                                  repeat=repeat, jobs=jobs, timeout=timeout)
    path = save_results(results, all_runs, profile.base_dir / profile.evals.results_dir)
    if fmt == "json":
        _emit(results.model_dump_json(indent=2), None)
    else:
        s = results.summary
        if not quiet:
            for c in results.cases:
                icon = {"pass": "[green]✓[/]", "fail": "[red]✗[/]", "error": "[red]![/]", "skip": "[dim]-[/]"}[c.status]
                reps = f" [dim]{c.c}/{c.n}[/dim]" if c.n > 1 else ""
                console.print(f" {icon} {escape(c.case_id)}{reps}" + (f"  [dim]{escape(c.reason)}[/dim]" if c.reason else ""))
            grouped = _group_violations(results.violations)
            for (pol, case_id, detail, effect), n in grouped[:8]:
                times = f" [dim](x{n})[/dim]" if n > 1 else ""
                console.print(f"   [red]violation[/red] {pol} in {case_id}: {escape(detail)}{times}"
                              + _blocked_note(effect))
            if len(grouped) > 8:
                console.print(f"   [dim]... {len(grouped) - 8} more distinct violations[/dim]")
            if traceback:
                for r in all_runs:
                    if r.metadata.get("traceback"):
                        console.print(f"\n[bold]{r.case_id}[/bold] attempt {r.attempt}:", highlight=False)
                        console.print(r.metadata["traceback"], markup=False, highlight=False)
            elif any(r.status == "error" for r in all_runs):
                console.print("[dim]agent raised an exception: re-run with --traceback to see where[/dim]")
            live = [r for r in all_runs if r.metadata.get("source") == "live"]
            if profile.tools and live and not any(r.tool_calls() for r in live):
                console.print("[yellow]warning:[/yellow] no tool calls were recorded in any run. Decorate your tools "
                              "with @evalplane.tool, or return the message history from your entrypoint "
                              "(see README). Until then, forbidden_tools checks pass vacuously.")
        console.print(f"{'' if quiet else chr(10)}{s['passed']} passed, {s['failed']} failed, {s['skipped']} skipped · "
                      f"{s['violations']} policy/scope violations · 0 LLM calls by evalplane"
                      f"{'' if not judge_fn else ' (plus judge calls)'}")
        if not quiet:
            console.print(f"[dim]results: {path}  ·  next: evalplane gate --stage ci[/dim]")
    if results.unmatched_runs and fmt != "json":
        console.print(f"[yellow]note:[/yellow] {results.unmatched_runs} recorded runs matched no case and were not "
                      "scored. Check them with `evalplane audit`, or turn them into cases with `evalplane promote`.")
    s = results.summary
    if s["cases"] and s["skipped"] == s["cases"]:
        err.print("[red]error:[/red] every case was skipped, so nothing was tested (see the reasons above)")
        raise typer.Exit(1)
    if s["failed"]:
        raise typer.Exit(1)


@app.command(rich_help_panel="1. Start here")
def audit(traces: list[str] = typer.Argument(..., help="Trace files or directories (JSONL, OTel JSON)."),
          file: str = FILE,
          latest: bool = typer.Option(False, "--latest", help="For a directory, only the newest file in it.")):
    """Check recorded or production traces against every policy and tool rule, with no test cases."""
    profile = _load(file)
    if latest:
        picked = []
        for t in traces:
            p = Path(t)
            if p.is_dir():
                files = sorted((f for f in p.rglob("*") if f.suffix in (".jsonl", ".json")), key=lambda f: f.stat().st_mtime)
                picked += [str(files[-1])] if files else []
            else:
                picked.append(t)
        traces = picked
    runs = _traces(profile, traces)
    violations = audit_traces(profile, runs)
    grouped = _group_violations(violations)
    bad_runs = len({v.run_id for v in violations})
    blocked = sum(1 for v in violations if not getattr(v, "took_effect", True))
    split = f", {len(violations) - blocked} took effect, {blocked} blocked by the tool" if blocked else ""
    console.print(f"Audited {len(runs)} runs: {bad_runs} broke a rule "
                  f"({len(grouped)} distinct violations{split})")
    if not profile.policies:
        console.print("[yellow]note:[/yellow] agent.eval.yaml has no policies, so audit only checks scope, call "
                      "limits, argument schemas and loops. Add your rules under `policies:` with a `check:` "
                      "(see docs/reference.md) to catch more.")
    for (pol, where, detail, effect), n in grouped:
        times = f" [dim](x{n})[/dim]" if n > 1 else ""
        console.print(f"  [red]{pol}[/red] {escape(where)}: {escape(detail)}{times}" + _blocked_note(effect))
    if violations:
        raise typer.Exit(1)


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float:
    n = len(pairs)
    if not n:
        return 0.0
    po = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


@app.command(hidden=True)
def calibrate(labels: Path = typer.Argument(..., help="YAML: {labels: {case_id: pass|fail}} (your own grading)."),
              file: str = FILE, results: str | None = typer.Option("latest", "--results")):
    """Check an LLM judge against your own labels (agreement and Cohen's kappa); the gate uses the result."""
    import datetime as dt

    import yaml

    profile = _load(file)
    res = _latest(profile, results)
    if res is None:
        _fail("no results: run `evalplane run --judge module:fn` first")
    mine = (yaml.safe_load(labels.read_text()) or {}).get("labels", {})
    pairs, rows = [], []
    for c in res.cases:
        if c.case_id not in mine:
            continue
        verdicts = [ch.status for a in c.attempts for ch in a.checks if ch.name == "judge" and ch.status in ("pass", "fail")]
        if not verdicts:
            continue
        human = str(mine[c.case_id]).lower() in ("pass", "true", "good", "yes", "1")
        judge = verdicts[0] == "pass"
        pairs.append((human, judge))
        if human != judge:
            rows.append(f"  disagree on {c.case_id}: you said {'pass' if human else 'fail'}, "
                        f"judge said {'pass' if judge else 'fail'}")
    if len(pairs) < 5:
        _fail(f"only {len(pairs)} labelled cases have judge verdicts; label at least 5 (30-50 is better)")
    kappa = cohen_kappa(pairs)
    agree = sum(a == b for a, b in pairs) / len(pairs)
    out = profile.base_dir / profile.evals.results_dir / "calibration.json"
    out.write_text(json.dumps({"kappa": round(kappa, 3), "agreement": round(agree, 3), "n": len(pairs),
                               "date": dt.date.today().isoformat(), "results": res.started_at}, indent=2))
    good = kappa >= 0.6
    console.print(f"Judge vs your labels on {len(pairs)} cases: agreement {agree:.0%}, Cohen's kappa "
                  f"[{'green' if good else 'red'}]{kappa:.2f}[/] ({'trusted' if good else 'not trusted yet: below 0.6'})")
    for r in rows[:10]:
        console.print(r, markup=False)
    console.print(f"[dim]saved {out}; the gate uses it for judge-graded requirements at T3/T4[/dim]")


@app.command(hidden=True, name="mcp")
def mcp_cmd(directory: Path | None = typer.Option(None, "--dir", help="Project folder (default: current).")):
    """Run an MCP server (stdio) so coding agents can ask Evalplane about your agent. Needs `pip install mcp`."""
    from .mcp_server import main as mcp_main

    try:
        mcp_main(str(directory) if directory else None)
    except ImportError as e:
        _fail(str(e))


@app.command(rich_help_panel="3. More", name="compare")
def compare_cmd(before: Path = typer.Argument(..., help="Results JSON from before the change."),
                after: Path = typer.Argument(Path(".evalplane/latest.json"), help="Results JSON after the change."),
                fmt: str = typer.Option("text", "--format", help="text | md | json"),
                fail_on_regression: bool = typer.Option(False, "--fail-on-regression")):
    """What changed between two runs (e.g. a model upgrade): new failures, fixes, new rule violations."""
    from .compare import compare, comparison_md

    try:
        c = compare(load_results(before), load_results(after))
    except (OSError, ValueError) as e:
        _fail(f"cannot read results: {e}")
    if fmt == "json":
        _emit(c.model_dump_json(indent=2), None)
    else:
        _emit(comparison_md(c), None)
    if fail_on_regression and c.regressed:
        raise typer.Exit(1)


@app.command(rich_help_panel="3. More")
def generate(file: str = FILE, out: Path = typer.Option(Path("evals/generated.yaml"), "--out", "-o"),
             llm: str | None = typer.Option(None, "--llm", help="module:function(prompt) -> text, for natural inputs."),
             per_gap: int = typer.Option(2, "--per-gap", help="Cases per untested policy / tool."),
             force: bool = typer.Option(False, "--force")):
    """Write temptation cases for untested policies and action tools (templates, or your LLM with --llm)."""
    from .generate import generate as _generate

    profile, p, cases = _context(file)
    if out.exists() and not force:
        _fail(f"{out} exists; use --force or another --out")
    cov = analyse(p, profile, cases)
    fn = load_agent(llm, profile.base_dir) if llm else None
    text, n = _generate(profile, cov, llm=fn, per_gap=per_gap, existing_ids={c.id for c in cases})
    if not n:
        console.print("Nothing to generate: every policy is tempted and every action tool has a negative case.")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    console.print(f"Wrote {n} cases to {out}{' (with your LLM)' if fn else ' (from templates)'}. "
                  "Review and adjust them, then `evalplane run`.")


@app.command(rich_help_panel="1. Start here")
def review(traces: list[str] = typer.Argument(..., help="Trace files or directories (JSONL, OTel JSON)."),
           file: str = FILE,
           sample: int = typer.Option(20, "--sample", help="How many runs to look at."),
           show: bool = typer.Option(False, "--summary", help="Only show the failure modes labelled so far.")):
    """Error analysis: look at real runs, label the bad ones, see your most common failure modes."""
    from .review import failure_modes, load_reviews, order_for_review, save_review, summarize_run

    profile = _load(file) if Path(_find(file)).exists() else None
    results_dir = (profile.base_dir if profile else Path(".")) / (profile.evals.results_dir if profile else ".evalplane")
    reviews = load_reviews(results_dir)
    if not show:
        runs = _traces(profile, traces) if profile else __import__("evalplane").load_traces(traces)
        todo = [r for r in order_for_review(runs, profile, sample + len(reviews)) if r.run_id not in reviews][:sample]
        if not todo:
            console.print("Nothing new to review.")
        console.print("[dim]for each run: g = good, b <what went wrong> = bad (e.g. 'b wrong recipient'), "
                      "s = skip, q = quit[/dim]")
        for i, run in enumerate(todo, 1):
            console.print(f"\n[bold]run {i}/{len(todo)}[/bold] [dim]{run.run_id[:12]}"
                          f"{' · case ' + run.case_id if run.case_id else ''}[/dim]")
            for ln in summarize_run(run, profile):
                style = "red" if ln.startswith(("ERROR", "RULE")) else None
                console.print("  " + ln, style=style, markup=False, highlight=False)
            try:
                ans = typer.prompt("verdict", default="s", show_default=False).strip()
            except typer.Abort:
                break
            if ans.lower().startswith("q"):
                break
            if ans.lower().startswith("g"):
                entry = {"run_id": run.run_id, "verdict": "good", "label": ""}
            elif ans.lower().startswith("b"):
                entry = {"run_id": run.run_id, "verdict": "bad", "label": ans[1:].strip() or "unlabelled"}
            else:
                continue
            save_review(results_dir, entry)
            reviews[run.run_id] = entry
    modes = failure_modes(reviews)
    good = sum(1 for r in reviews.values() if r.get("verdict") == "good")
    console.print(f"\n[bold]Reviewed {len(reviews)} runs:[/bold] {good} good, {sum(n for _, n in modes)} bad")
    for label, n in modes:
        console.print(f"  {n:>3} × {escape(label)}")
    if modes:
        console.print("\nNext: [cyan]evalplane promote <traces> --reviewed[/cyan] turns the labelled runs into cases "
                      "(bad runs become cases named after their failure mode).")


@app.command(rich_help_panel="3. More")
def promote(traces: list[str] = typer.Argument(..., help="Trace files or directories (JSONL, OTel JSON)."),
            file: str = FILE,
            out: Path = typer.Option(Path("evals/promoted.yaml"), "--out", "-o"),
            only_failures: bool = typer.Option(False, "--failures", help="Only runs that broke a rule or errored."),
            reviewed: bool = typer.Option(False, "--reviewed", help="Only runs you labelled with `evalplane review`."),
            force: bool = typer.Option(False, "--force", help="Overwrite --out if it exists.")):
    """Turn recorded runs into regression cases (review them before committing)."""
    from .promote import promote as _promote

    profile = _load(file) if Path(file).exists() else None
    runs = _traces(profile, traces) if profile else __import__("evalplane").load_traces(traces)
    if not runs:
        _fail("no runs found in the given traces")
    if out.exists() and not force:
        _fail(f"{out} exists; use --force to overwrite or choose another --out")
    existing = set()
    if profile:
        try:
            existing = {c.id for c in load_cases(profile.evals.cases, base_dir=profile.base_dir)
                        if Path(c.source).resolve() != out.resolve()}
        except EvalplaneError:
            pass
    reviews = None
    if reviewed:
        from .review import load_reviews

        base = profile.base_dir / profile.evals.results_dir if profile else Path(".evalplane")
        reviews = load_reviews(base)
        if not reviews:
            _fail("no reviews yet: run `evalplane review <traces>` first")
    text = _promote(runs, profile, only_failures=only_failures, existing_ids=existing, reviews=reviews)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    console.print(f"Wrote {text.count('- id:')} cases to {out}. Review them, then run `evalplane coverage`.")


# --------------------------------------------------------------------------- gate / report


def _latest(profile: AgentProfile, results: str | None):
    path = Path(results) if results and results != "latest" else profile.base_dir / profile.evals.results_dir / "latest.json"
    return load_results(path) if path.exists() else None


@app.command(rich_help_panel="2. The loop")
def gate(stage: str = typer.Option("ci", "--stage", help="design | ci | pre-release"), file: str = FILE,
         results: str | None = typer.Option("latest", "--results"),
         fmt: str = typer.Option("text", "--format", help="text | md | json"),
         allow_stale: bool = typer.Option(False, "--allow-stale"),
         warn_only: bool = typer.Option(False, "--warn-only", help="Never fail (report only)."),
         strict: bool = typer.Option(False, "--strict", help="Treat warnings as failures."),
         github_summary: bool = typer.Option(False, "--github-summary", help="Append the scorecard to $GITHUB_STEP_SUMMARY."),
         verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every unmet requirement."),
         out: Path | None = typer.Option(None, "--out", "-o")):
    """Pass/fail for this stage, using thresholds for the agent's risk tier. Exit 1 on FAIL."""
    stage = _stage(stage)
    profile, p, cases = _context(file)
    res = _latest(profile, results)
    cov = analyse(p, profile, cases)
    cal_file = profile.base_dir / profile.evals.results_dir / "calibration.json"
    calibration = json.loads(cal_file.read_text()) if cal_file.exists() else None
    g = evaluate_gate(p, cov, stage, res, allow_stale=allow_stale, calibration=calibration)
    md = scorecard_md(p, cov, res, g)
    if github_summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md)
    if fmt == "json":
        _emit(g.model_dump_json(indent=2), out)
    elif fmt == "md":
        _emit(md, out)
    else:
        color = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[g.verdict]
        console.print(f"Gate [bold]{stage.value}[/bold] for {p.agent} ({p.tier.value}): [bold {color}]{g.verdict}[/]")
        failing = [c for c in (res.cases if res else []) if c.status in ("fail", "error")]
        if failing:
            console.print("\n[bold]Failing cases[/bold] (fix these first; they cause most requirement failures):")
            for c in failing:
                console.print(f"  [red]✗[/red] {escape(c.case_id)} ({c.c}/{c.n}): {escape(c.reason)}")
        other = [c for c in g.checks if c.verdict != "PASS" and c.kind != "requirement"]
        reqs = [c for c in g.checks if c.verdict != "PASS" and c.kind == "requirement"]
        risk_first = ("policy", "safety", "guard", "injection", "approval", "violation")
        reqs.sort(key=lambda c: (c.verdict != "FAIL", not any(k in c.id for k in risk_first), c.id))
        if other:
            console.print("")
            for c in other:
                col = "red" if c.verdict == "FAIL" else "yellow"
                console.print(f"  [{col}]{c.verdict}[/] {c.id}: {c.detail}")
        if reqs:
            console.print(f"\n[bold]Requirements not met[/bold] ({len(reqs)}):")
            shown = reqs if verbose else reqs[:8]
            for c in shown:
                col = "red" if c.verdict == "FAIL" else "yellow"
                console.print(f"  [{col}]{c.verdict}[/] {c.id}: {c.detail}", markup=True)
            if len(reqs) > len(shown):
                console.print(f"  [dim]... {len(reqs) - len(shown)} more (--verbose)[/dim]")
        n = g.counts()
        console.print(f"[dim]{n['PASS']} pass, {n['WARN']} warn, {n['FAIL']} fail[/dim]")
    if warn_only:
        return
    if g.verdict == "FAIL" or (strict and g.verdict == "WARN"):
        raise typer.Exit(1)


@app.command(rich_help_panel="3. More")
def plan(file: str = FILE, fmt: str = FORMAT, must_only: bool = typer.Option(False, "--must-only"),
         why: bool = typer.Option(False, "--why", help="Show why each eval is needed."),
         out: Path | None = typer.Option(None, "--out", "-o")):
    """Which evals this agent needs, per layer, and why."""
    profile, p, cases = _context(file)
    if must_only:
        p = p.model_copy(update={"requirements": [r for r in p.requirements if r.priority == "must"]})
    if fmt == "json":
        return _emit(p.model_dump_json(indent=2), out)
    if fmt == "md":
        return _emit(plan_md(p), out)
    covered = set(analyse(p, profile, cases).covered)
    must = p.must
    n_cov = sum(1 for r in must if r.id in covered)
    console.print(f"[bold]{escape(p.agent)}[/bold]: tier [bold]{p.tier.value}[/bold] ({TIER_NAMES[p.tier]}), "
                  f"pack {p.pack}: {len(must)} must ({n_cov} covered), "
                  f"{sum(r.priority == 'should' and not r.waived for r in p.requirements)} should")
    for r in p.tier_reasons:
        console.print(f"  [dim]- {escape(r)}[/dim]")
    for layer in Layer:
        reqs = [r for r in p.requirements if r.layer == layer]
        if not reqs:
            continue
        console.print(f"\n[bold]{layer.value} · {LAYER_NAMES[layer]}[/bold]")
        for r in reqs:
            mark = "[dim]〰[/]" if r.waived else ("[green]✓[/]" if r.id in covered else "[red]·[/]")
            tag = "waived" if r.waived else r.priority
            tag_s = f"[dim]{tag}, {r.stage.value}[/dim]" if tag != "must" else f"[dim]{r.stage.value}[/dim]"
            console.print(f" {mark} [cyan]{escape(r.id)}[/cyan]  {escape(r.why if why else r.title)}  {tag_s}")
    console.print("\n[dim]✓ covered · must unless marked · `--why` for reasons · `evalplane explain <id>` "
                  "for how to test one · `evalplane coverage` for the gaps, most important first[/dim]")


@app.command(rich_help_panel="3. More")
def report(file: str = FILE, stage: str = typer.Option("ci", "--stage", help="design | ci | pre-release"),
           results: str | None = typer.Option("latest", "--results"),
           evidence_dir: Path | None = typer.Option(None, "--evidence", help="Write an evidence pack to this directory."),
           fmt: str = typer.Option("md", "--format", help="md | html | junit | cucumber | pr-comment"),
           baseline: str | None = typer.Option(None, "--baseline", help="With pr-comment: base-branch coverage JSON."),
           out: Path | None = typer.Option(None, "--out", "-o")):
    """Scorecard as markdown, a self-contained HTML page, JUnit XML (GitLab/Jenkins) or a PR comment."""
    from . import reporters

    stage = _stage(stage)
    profile, p, cases = _context(file)
    res = _latest(profile, results)
    cov = analyse(p, profile, cases)
    cal_file = profile.base_dir / profile.evals.results_dir / "calibration.json"
    g = evaluate_gate(p, cov, stage, res, calibration=json.loads(cal_file.read_text()) if cal_file.exists() else None)
    md = scorecard_md(p, cov, res, g)
    if fmt == "html":
        runs = []
        if res and res.runs_file and Path(res.runs_file).exists():
            from .trace import read_runs

            runs = list(read_runs(res.runs_file))  # steps for each case, shown as scenario steps
        _emit(reporters.html_report(p, cov, res, g, runs), out)
    elif fmt == "cucumber":
        if res is None:
            _fail("no results: run `evalplane run` first")
        _emit(reporters.cucumber_json(res, p), out)
    elif fmt == "junit":
        if res is None:
            _fail("no results: run `evalplane run` first")
        _emit(reporters.junit_xml(res, p, stage), out)
    elif fmt == "pr-comment":
        d = None
        if baseline:
            from .coverage import CoverageReport, diff

            try:
                d = diff(CoverageReport.model_validate_json(Path(baseline).read_text()), cov)
            except (OSError, ValueError):
                d = None
        _emit(reporters.pr_comment_md(p, cov, res, g, diff=d), out)
    else:
        _emit(md, out)
    if evidence_dir:
        ev = evidence(p, cov, res, g)
        extra = {"plan.md": plan_md(p), "coverage.json": cov.model_dump_json(indent=2), "scorecard.md": md}
        if res:
            extra["results.json"] = res.model_dump_json(indent=2)
        write_evidence(evidence_dir, ev, extra)
        err.print(f"[green]Evidence pack written to {evidence_dir}[/green]")


@app.command(hidden=True)
def badge(file: str = FILE, out: Path = typer.Option(Path(".evalplane/badge.json"), "--out", "-o")):
    """Write a shields.io endpoint badge JSON with the coverage score."""
    profile, p, cases = _context(file)
    cov = analyse(p, profile, cases)
    color = "brightgreen" if cov.score >= 85 else "green" if cov.score >= 70 else "yellow" if cov.score >= 50 else "red"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"schemaVersion": 1, "label": f"eval coverage · {p.tier.value}",
                               "message": f"{cov.score:g}%", "color": color}))
    console.print(f"wrote {out}")


@app.command(hidden=True)
def attest(requirement: str, by: str = typer.Option(..., "--by", help="Who checked it."),
           note: str = typer.Option("", "--note"), evidence: str = typer.Option("", "--evidence", help="Link or path."),
           file: str = FILE):
    """Record a human sign-off (kill switch, review, business metric...) in agent.eval.yaml."""
    import datetime as dt

    import yaml

    file = _find(file)
    profile, p, _ = _context(file)
    req = p.get(requirement)
    if req is None:
        _fail(f"{requirement!r} is not in this agent's plan (see `evalplane plan`)")
    if req.metric != "attested":
        _fail(f"{requirement} is measured by eval cases, not by a sign-off. See `evalplane explain {requirement}`.")
    entry = {"requirement": requirement, "by": by, "date": dt.date.today().isoformat()}
    if evidence:
        entry["evidence"] = evidence
    if note:
        entry["note"] = note
    item = "  - " + yaml.safe_dump(entry, default_flow_style=True, width=1000, sort_keys=False).strip() + "\n"
    text = Path(file).read_text()
    lines = text.splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if ln.startswith("attestations:")), None)
    if idx is None:
        text = text.rstrip("\n") + "\nattestations:\n" + item
    elif lines[idx].strip() in ("attestations: []", "attestations:[]"):
        lines[idx] = "attestations:\n" + item
        text = "".join(lines)
    else:
        lines.insert(idx + 1, item)
        text = "".join(lines)
    Path(file).write_text(text)
    _load(file)  # still valid
    console.print(f"[green]Recorded[/green] {requirement} (by {by}). Re-run `evalplane run` so results match the plan.")


@app.command(hidden=True)
def schema(out: Path | None = typer.Option(None, "--out", "-o"),
           kind: str = typer.Option("profile", "--kind", help="profile (agent.eval.yaml) | cases (evals/*.yaml)")):
    """JSON Schema for agent.eval.yaml or case files (editor autocompletion and validation)."""
    from .cases import Case

    if kind == "cases":
        item = Case.model_json_schema()
        data = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Evalplane cases",
                "type": "object", "properties": {"suite": {"type": "string"}, "defaults": {"type": "object"},
                                                 "cases": {"type": "array", "items": {"$ref": "#/$defs/Case"}}},
                "$defs": {**item.pop("$defs", {}), "Case": item}}
    else:
        data = {"$schema": "https://json-schema.org/draft/2020-12/schema", **AgentProfile.model_json_schema()}
        data["title"] = "Evalplane agent profile (agent.eval.yaml)"
    _emit(json.dumps(data, indent=2), out)


@app.command(hidden=True)
def packs():
    """List available sector packs."""
    t = Table("id", "tier", "description")
    for p in list_packs():
        t.add_row(p["id"], str(p["default_tier"]), p["description"])
    console.print(t)


@app.command(hidden=True)
def version():
    """Show the version."""
    console.print(__version__)


def main() -> None:
    try:
        app()
    except EvalplaneError as e:
        err.print(f"[red]error:[/red] {e}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
