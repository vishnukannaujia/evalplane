"""Run eval cases against a live agent or recorded traces, then roll results up per requirement."""

from __future__ import annotations

import copy
import datetime as dt
import importlib
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable
from math import comb
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import __version__
from .cases import Case
from .coverage import valid_covers
from .errors import ConfigError
from .models import AgentProfile, Stage
from .planner import Plan, Requirement
from .recorder import _current
from .scorers import Judge, Violation, score_run
from .trace import Run, ToolCall, UserTurn, load_traces, read_runs, write_runs

AgentFn = Callable[..., Any]


class CheckResult(BaseModel):
    name: str
    status: str
    reason: str = ""


class AttemptResult(BaseModel):
    run_id: str
    attempt: int
    passed: bool
    checks: list[CheckResult]


class CaseResult(BaseModel):
    case_id: str
    suite: str
    source: str = ""
    status: str  # pass | fail | skip | error
    n: int = 0
    c: int = 0
    covers: list[str] = Field(default_factory=list)
    attempts: list[AttemptResult] = Field(default_factory=list)
    reason: str = ""
    req_c: dict[str, int] = Field(default_factory=dict)  # passes per covered requirement (fine-grained blame)

    @property
    def pass_rate(self) -> float | None:
        return self.c / self.n if self.n else None

    def rate_for(self, req_id: str) -> float | None:
        if not self.n:
            return None
        return self.req_c.get(req_id, self.c) / self.n

    def pass_hat_k(self, k: int) -> float | None:
        if self.n < k:
            return None
        return comb(self.c, k) / comb(self.n, k)


GENERIC_CHECKS = ("status", "python", "judge")


def implied_by_check(name: str, case: Case) -> list[str]:
    """Requirement ids a single check speaks for (so a failure is blamed only where it belongs)."""
    tools = [c.name for c in (case.expect.tools.calls if case.expect.tools else [])]
    if name == "tools.trajectory":
        return ["L4.trajectory.match", *(f"L3.tool.selection@{t}" for t in tools)]
    for prefix, rid in (("tools.args[", "L3.tool.args@"), ("forbidden[", "L3.tool.guard@"),
                        ("policy[", "L4.policy.adherence@")):
        if name.startswith(prefix):
            return [rid + name[len(prefix):-1]]
    if name.startswith("output.json_schema"):
        return ["L1.output.schema", "L4.task.success"]
    if name.startswith(("output.", "state[")):
        return ["L4.task.success", "L1.golden.accuracy"]
    if name == "retrieval.recall":
        return ["L2.retrieval.recall"]
    if name.startswith("budget."):
        return ["L4.efficiency.budget"]
    return []


def requirement_passes(case: Case, checks: list, attempt_passed: bool) -> dict[str, bool]:
    """Per covered requirement: did this attempt pass the checks that relate to it?
    Requirements named explicitly in `covers:` (e.g. an injection test) are judged on the whole case."""
    generic_ok = all(c.ok for c in checks if c.name in GENERIC_CHECKS)
    mapped: dict[str, bool] = {}
    for ch in checks:
        for rid in implied_by_check(ch.name, case):
            mapped[rid] = mapped.get(rid, True) and ch.ok
    out = {}
    for rid in case.all_covers():
        if rid in case.covers or rid not in mapped:
            out[rid] = attempt_passed
        else:
            out[rid] = mapped[rid] and generic_ok
    return out


class ViolationResult(BaseModel):
    policy: str
    rule: str
    detail: str
    run_id: str
    case_id: str | None = None
    effect: Literal["effective", "blocked", "not_applicable"] = "effective"

    @property
    def took_effect(self) -> bool:
        return self.effect != "blocked"


def _irreversible_at_t4(policy, profile: AgentProfile, tier) -> bool:
    """A blocked attempt on an irreversible tool at T4 still fails: the agent tried to move money and only
    an unrelated validation stopped it."""
    from .models import SideEffect, Tier

    if tier is None or tier < Tier.T4 or policy is None or policy.check is None or not policy.check.tool:
        return False
    tool = next((t for t in profile.tools if t.name == policy.check.tool), None)
    return tool is not None and tool.side_effect == SideEffect.irreversible


def resolving_power(threshold: float) -> int:
    """How many covering cases a threshold needs before it can disagree with "all of them must pass".

    With n cases the possible pass rates are k/n, so the highest rate below 1.0 is (n-1)/n. A threshold of
    0.85 only means something once (n-1)/n can fall below it, i.e. n >= 7; 0.98 needs n >= 50. Below that,
    the threshold is exactly "no failures allowed" wearing a decimal point.
    """
    return 1 if threshold >= 1.0 else math.ceil(1 / (1 - threshold))


class RequirementResult(BaseModel):
    id: str
    priority: str
    status: str  # pass | fail | warn | missing | skip | waived
    value: float | None = None
    threshold: float | None = None
    metric: str
    cases: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def n(self) -> int:
        return len(self.cases)

    @property
    def threshold_is_meaningful(self) -> bool:
        """False when too few cases cover this for the threshold to differ from all-must-pass.

        Only rate metrics are in scope: a `violations` threshold of 0 means "none", not a proportion.
        """
        if self.threshold is None or self.metric not in ("pass_rate", "pass_hat_k"):
            return True
        if not 0 < self.threshold < 1:
            return True    # 1.0 already *is* all-must-pass, and it says so honestly
        return self.n >= resolving_power(self.threshold)

    def threshold_note(self) -> str:
        if self.threshold is None or self.threshold_is_meaningful:
            return ""
        return (f"{self.n} case{'s' if self.n != 1 else ''}: the {self.threshold:g} threshold acts as "
                f"all-must-pass (needs {resolving_power(self.threshold)})")


class Results(BaseModel):
    schema_version: str = "1"
    evalplane_version: str = __version__
    agent: str
    tier: str
    plan_hash: str
    stage: str | None = None
    started_at: str
    finished_at: str = ""
    git_commit: str | None = None
    cases: list[CaseResult] = Field(default_factory=list)
    violations: list[ViolationResult] = Field(default_factory=list)
    requirements: list[RequirementResult] = Field(default_factory=list)
    runs_file: str | None = None
    unmatched_runs: int = 0  # recorded runs given with --traces that no case used

    @property
    def summary(self) -> dict[str, Any]:
        ran = [c for c in self.cases if c.status != "skip"]
        return {
            "cases": len(self.cases),
            "passed": sum(1 for c in self.cases if c.status == "pass"),
            "failed": sum(1 for c in self.cases if c.status in ("fail", "error")),
            "skipped": sum(1 for c in self.cases if c.status == "skip"),
            "attempts": sum(c.n for c in ran),
            "violations": len(self.violations),
        }


# --------------------------------------------------------------------------- agent loading


def load_agent(ref: str, base_dir: Path | None = None) -> AgentFn:
    if ":" not in ref:
        raise ConfigError(f"agent entrypoint must look like 'module:function', got {ref!r}")
    mod_name, fn_name = ref.split(":", 1)
    for p in filter(None, [base_dir, Path.cwd()]):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        raise ConfigError(f"cannot import agent module {mod_name!r}: {e}") from e
    fn = getattr(mod, fn_name, None)
    if fn is None or not callable(fn):
        raise ConfigError(f"{ref!r}: no callable named {fn_name!r}")
    return fn


def _absorb(run: Run, out: Any, case: Case, attempt: int, first_turn: bool = True, n_before: int = 0) -> Run:
    """Fold one agent return value (str, dict, Run or message history) into the run being recorded."""
    if isinstance(out, Run):
        steps = run.steps
        new = out.model_copy(update={"case_id": case.id, "attempt": attempt})
        new.steps = steps + list(out.steps) if not first_turn else (list(out.steps) or steps)
        return new
    if isinstance(out, list) or (isinstance(out, dict) and "messages" in out):
        # a message history (OpenAI / Anthropic / LangChain): derive tool calls from it, unless @ep.tool
        # already recorded them this turn. In multi-turn cases, return only this turn's messages.
        from .messages import from_messages

        msgs = out if isinstance(out, list) else out["messages"]
        converted = from_messages(msgs, input=case.input)
        recorded_this_turn = any(isinstance(s, ToolCall) for s in run.steps[n_before:])
        if not recorded_this_turn:
            # the runner already recorded this turn's user message; keep the agent's steps only
            run.steps = run.steps + [s for s in converted.steps if first_turn or not isinstance(s, UserTurn)]
        run.output = out.get("output", converted.output) if isinstance(out, dict) else converted.output
        if isinstance(out, dict) and out.get("final_state") is not None:
            run.final_state = out["final_state"]
        return run
    if isinstance(out, dict) and ("output" in out or "final_state" in out):
        run.output = out.get("output")
        if out.get("final_state") is not None:
            run.final_state = out["final_state"]
        return run
    run.output = out
    return run


def _call_agent(agent: AgentFn, case: Case, attempt: int) -> Run:
    context = copy.deepcopy(case.context)
    context["attempt"] = attempt
    context["case_id"] = case.id
    run = Run(input=case.input, case_id=case.id, attempt=attempt,
              metadata={"source": "live", "context": copy.deepcopy(case.context),
                        "fail_tools": list(case.context.get("fail_tools") or [])})
    token = _current.set(run)
    start = time.perf_counter()
    try:
        history: list[dict] = []
        user_msg = case.input
        simulator = load_agent(case.simulator) if case.simulator else None
        max_turns = 1 + (len(case.turns) if case.turns else (case.max_turns - 1 if simulator else 0))
        for turn in range(max_turns):
            if turn:
                run.steps.append(UserTurn(text=str(user_msg)))
            context["turn"] = turn
            context["history"] = list(history)
            n_before = len(run.steps)
            out = agent(user_msg, context)
            run = _absorb(run, out, case, attempt, first_turn=turn == 0, n_before=n_before)
            _current.set(run)
            history += [{"role": "user", "content": user_msg}, {"role": "assistant", "content": run.output_text}]
            if turn + 1 >= max_turns:
                break
            user_msg = case.turns[turn] if case.turns else simulator(history, context)
            if not user_msg:  # the simulated user is done
                break
    except Exception as e:  # noqa: BLE001 - an agent crash is a failed attempt, not a runner crash
        import traceback

        run.status = "error"
        tb = traceback.extract_tb(e.__traceback__)
        pkg = str(Path(__file__).parent)
        where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb) if not f.filename.startswith(pkg)), "")
        run.error = f"{type(e).__name__}: {e}" + (f" (at {where})" if where else "")
        run.metadata["traceback"] = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    finally:
        _current.reset(token)
    if run.latency_ms is None:
        run.latency_ms = (time.perf_counter() - start) * 1000
    if run.final_state is None:
        run.final_state = context.get("state")
    return run


def _run_live_parallel(agent: AgentFn, cases: list[Case], traces: list[Run] | None, stage: Stage | None,
                       repeat: int | None, jobs: int, timeout: float | None) -> dict[str, list[Run]]:
    """Run every live attempt (cases with no recorded trace) in a thread pool, with an optional timeout."""
    import concurrent.futures as cf

    recorded = {r.case_id for r in traces or [] if r.case_id}
    run_ids = {r.run_id for r in traces or []}
    todo = [(c, i) for c in cases
            if not (stage is not None and c.stage > stage) and not c.is_placeholder and c.external_status is None
            and not c.trace and c.id not in recorded and not (c.from_run and c.from_run in run_ids)
            for i in range(repeat or c.repeat)]
    out: dict[str, list[Run | None]] = {c.id: [None] * (repeat or c.repeat) for c, _ in todo}
    pool = cf.ThreadPoolExecutor(max_workers=max(1, jobs))
    futures = {pool.submit(_call_agent, agent, c, i): (c, i) for c, i in todo}
    for fut, (c, i) in futures.items():
        try:
            out[c.id][i] = fut.result(timeout=timeout)
        except cf.TimeoutError:
            out[c.id][i] = Run(input=c.input, case_id=c.id, attempt=i, status="timeout",
                               error=f"no answer within {timeout:g}s", metadata={"source": "live"})
    pool.shutdown(wait=False, cancel_futures=True)
    return {k: [r for r in v if r is not None] for k, v in out.items()}


# --------------------------------------------------------------------------- running


def _git_commit(cwd: Path) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def run_cases(
    profile: AgentProfile,
    plan: Plan,
    cases: list[Case],
    *,
    agent: AgentFn | None = None,
    traces: list[Run] | None = None,
    judge: Judge | None = None,
    stage: Stage | None = None,
    repeat: int | None = None,
    jobs: int = 1,
    timeout: float | None = None,
) -> tuple[Results, list[Run]]:
    """jobs > 1 runs live attempts in parallel threads (your agent must be thread-safe);
    timeout (seconds) marks an attempt as `timeout` if the agent takes longer (the thread is not killed)."""
    live: dict[str, list[Run]] = {}
    if agent is not None and (jobs > 1 or timeout):
        live = _run_live_parallel(agent, cases, traces, stage, repeat, jobs, timeout)
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    by_case: dict[str, list[Run]] = {}
    by_run: dict[str, Run] = {}
    for r in traces or []:
        by_run[r.run_id] = r
        if r.case_id:
            by_case.setdefault(r.case_id, []).append(r)
    matched: set[str] = set()

    results = Results(agent=profile.agent.name, tier=plan.tier.value, plan_hash=plan.plan_hash,
                      stage=stage.value if stage else None, started_at=started,
                      git_commit=_git_commit(profile.base_dir))
    all_runs: list[Run] = []
    for case in cases:
        if stage is not None and case.stage > stage:
            continue
        if case.is_placeholder:
            results.cases.append(CaseResult(case_id=case.id, suite=case.suite, source=case.source, status="skip",
                                            reason="placeholder input: replace the TODO/<...> with a real request"))
            continue
        if case.external_status is not None:  # a test that ran elsewhere (pytest): use its recorded outcome
            passed = case.external_status == "pass"
            results.cases.append(CaseResult(
                case_id=case.id, suite=case.suite, source=case.source,
                status={"pass": "pass", "skip": "skip"}.get(case.external_status, "fail"),
                n=0 if case.external_status == "skip" else 1, c=int(passed), covers=valid_covers(case, plan),
                reason=case.external_reason))
            continue
        n = repeat or case.repeat
        runs: list[Run] = []
        if case.trace:
            path = Path(case.trace)
            if not path.is_absolute():
                path = profile.base_dir / path
            runs = [r for r in read_runs(path) if r.case_id in (None, case.id)][:n]
            for r in runs:
                r.case_id = case.id
        elif case.id in by_case:
            runs = by_case[case.id][:n]
        elif case.from_run and case.from_run in by_run:  # a case promoted from this recorded run
            runs = [by_run[case.from_run]]
        elif agent is not None:
            runs = live.get(case.id) or [_call_agent(agent, case, i) for i in range(n)]
        matched |= {r.run_id for r in runs}
        if not runs:
            why = ("no recorded run matches this case (give the run a case_id, or the case a from_run)"
                   if traces else "no agent entrypoint and no recorded trace for this case")
            results.cases.append(CaseResult(case_id=case.id, suite=case.suite, source=case.source, status="skip",
                                            covers=valid_covers(case, plan), reason=why))
            continue
        attempts = []
        req_c: dict[str, int] = {}
        for i, run in enumerate(runs):
            checks, violations = score_run(run, case, profile, judge)
            for v in violations:
                results.violations.append(ViolationResult(**v.__dict__))
            passed = all(c.ok for c in checks)
            for rid, ok in requirement_passes(case, checks, passed).items():
                req_c[rid] = req_c.get(rid, 0) + int(ok)
            attempts.append(AttemptResult(run_id=run.run_id, attempt=i, passed=passed,
                                          checks=[CheckResult(name=c.name, status=c.status, reason=c.reason)
                                                  for c in checks]))
            all_runs.append(run)
        c = sum(a.passed for a in attempts)
        errored = any(ch.status == "error" for a in attempts for ch in a.checks)
        crashed = any(r.status == "error" for r in runs) and c == 0
        status = "pass" if c == len(attempts) else ("error" if (errored or crashed) and c == 0 else "fail")
        first_fail = next((ch for a in attempts for ch in a.checks if ch.status not in ("pass", "skip")), None)
        results.cases.append(CaseResult(
            case_id=case.id, suite=case.suite, source=case.source, status=status, n=len(attempts), c=c,
            covers=valid_covers(case, plan), attempts=attempts, req_c=req_c,
            reason=f"{first_fail.name}: {first_fail.reason}" if first_fail else "",
        ))

    results.unmatched_runs = len([r for r in traces or [] if r.run_id not in matched])
    results.requirements = aggregate(plan, results, profile)
    results.finished_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return results, all_runs


def audit_traces(profile: AgentProfile, runs: list[Run]) -> list[Violation]:
    """Check arbitrary (e.g. production) traces against every policy and tool rule, with no cases."""
    from .scorers import global_violations

    out: list[Violation] = []
    for r in runs:
        out.extend(global_violations(r, profile))
    return out


# --------------------------------------------------------------------------- aggregation


def _covering(req: Requirement, results: Results) -> list[CaseResult]:
    return [c for c in results.cases if req.id in c.covers]


def _cmp(value: float, threshold: float | None, lower_is_better: bool) -> bool:
    if threshold is None:
        return True
    return value <= threshold if lower_is_better else value >= threshold - 1e-9


def aggregate(plan: Plan, results: Results, profile: AgentProfile) -> list[RequirementResult]:
    out: list[RequirementResult] = []
    attested = {a.requirement for a in profile.attestations}
    any_runs = any(c.n for c in results.cases)
    for req in plan.requirements:
        base = dict(id=req.id, priority=req.priority, metric=req.metric, threshold=req.threshold)
        if req.waived:
            out.append(RequirementResult(**base, status="waived", reason=req.waived.reason))
            continue
        if req.metric == "attested":
            ok = req.id in attested or (req.rule_id == "L6.outcome.defined" and profile.has_business_metric)
            out.append(RequirementResult(**base, status="pass" if ok else "missing", value=1.0 if ok else 0.0,
                                         reason="" if ok else "no attestation"))
            continue
        cov = _covering(req, results)
        ran = [c for c in cov if c.n]
        if req.metric == "violations":
            policy = req.target or ("SCOPE" if req.rule_id == "L4.safety.scope" else None)
            count = sum(1 for v in results.violations if v.policy == policy)
            pol = next((p for p in profile.policies if p.id == req.target), None) if req.target else None
            if pol is not None and pol.check is None:
                out.append(RequirementResult(**base, status="missing",
                                             reason=f"policy {pol.id} has no check, so nothing enforces it"))
            elif not any_runs:
                out.append(RequirementResult(**base, status="missing", reason="no runs"))
            else:
                ok = _cmp(count, req.threshold, lower_is_better=True)
                hits = [v for v in results.violations if v.policy == policy]
                blocked = [v for v in hits if not v.took_effect]
                status, why = ("pass" if ok else "fail"), ("" if ok else f"{count} violation(s)")
                if not ok and blocked and len(blocked) == len(hits) and not _irreversible_at_t4(pol, profile, plan.tier):
                    # every attempt was refused by the tool, so nothing changed. Still a defect in the agent,
                    # but failing a release over damage that provably did not occur gets the gate switched off.
                    status = "warn"
                    why = f"{count} violation(s), all blocked by the tool (nothing changed)"
                out.append(RequirementResult(**base, status=status, value=float(count),
                                             cases=[c.case_id for c in cov], reason=why))
            continue
        if not cov:
            why = f"needs a case with repeat >= {req.k}" if req.metric == "pass_hat_k" else "no eval case covers this"
            out.append(RequirementResult(**base, status="missing", reason=why))
            continue
        if not ran:
            out.append(RequirementResult(**base, status="skip", cases=[c.case_id for c in cov],
                                         reason="covering cases were skipped"))
            continue
        if req.method.value == "judge":
            # only cases where a judge (or a Python check) actually graded the output count
            graded = [c for c in ran if c.source.endswith("pytest.json") or any(
                ch.name in ("judge", "python") and ch.status in ("pass", "fail")
                for a in c.attempts for ch in a.checks)]
            if not graded:
                out.append(RequirementResult(**base, status="unverified", cases=[c.case_id for c in ran],
                                             reason="needs a judge: no judge graded these cases (run with --judge)"))
                continue
            ran = graded
        if req.metric == "pass_hat_k":
            k = req.k or 1
            vals = [c.pass_hat_k(k) for c in ran]
            vals = [v for v in vals if v is not None]
            if not vals:
                out.append(RequirementResult(**base, status="missing", cases=[c.case_id for c in ran],
                                             reason=f"needs cases with repeat >= {k}"))
                continue
            value = sum(vals) / len(vals)
        else:
            value = sum(c.rate_for(req.id) or 0.0 for c in ran) / len(ran)
        ok = _cmp(value, req.threshold, lower_is_better=False)
        out.append(RequirementResult(**base, status="pass" if ok else "fail", value=round(value, 4),
                                     cases=[c.case_id for c in ran],
                                     reason="" if ok else f"{value:.2f} < {req.threshold:g}"))
    return out


# --------------------------------------------------------------------------- persistence


def save_results(results: Results, runs: list[Run], results_dir: Path) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / ".gitignore").write_text("*\n")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs_path = results_dir / "runs" / f"{stamp}.jsonl"
    write_runs(runs_path, runs)
    results.runs_file = str(runs_path)
    data = results.model_dump_json(indent=2)
    out = results_dir / "results" / f"{stamp}-{results.plan_hash[:8]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(data)
    (results_dir / "latest.json").write_text(data)
    return out


def load_results(path: str | Path) -> Results:
    return Results.model_validate(json.loads(Path(path).read_text()))


def load_trace_paths(profile: AgentProfile, paths: list[str] | None = None) -> list[Run]:
    """Paths given explicitly (CLI) resolve against the current directory and must exist.
    Paths from agent.eval.yaml resolve against its folder and are skipped if missing."""
    if paths is not None:
        resolved = []
        for p in paths:
            cand = [Path(p), profile.base_dir / p] if not Path(p).is_absolute() else [Path(p)]
            hit = next((c for c in cand if c.exists()), None)
            if hit is None:
                raise ConfigError(f"trace path not found: {p}")
            resolved.append(str(hit))
        return load_traces(resolved)
    resolved = [p if Path(p).is_absolute() else str(profile.base_dir / p) for p in profile.evals.traces]
    return load_traces([p for p in resolved if Path(p).exists()])
