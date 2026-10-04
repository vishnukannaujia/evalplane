"""Turn recorded runs (from tracing you already have) into regression eval cases.

Most teams have traces but no eval set. `evalplane promote traces/` writes one case per run,
with the observed tool trajectory as the expected one. Review each case: keep the good runs
as regression tests, and turn bad runs into guard cases by moving the wrong tool to
`forbidden_tools`.
"""

from __future__ import annotations

import re

import yaml

from .models import AgentProfile
from .scorers import global_violations
from .trace import Run, UserTurn


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:n].strip("-") or "run"


def promote(runs: list[Run], profile: AgentProfile | None = None, *, suite: str = "promoted",
            only_failures: bool = False, existing_ids: set[str] | None = None,
            reviews: dict[str, dict] | None = None) -> str:
    """reviews (from `evalplane review`): only reviewed runs are promoted; bad ones are named after their label."""
    cases = []
    seen: set[str] = set(existing_ids or ())
    if reviews is not None:
        runs = [r for r in runs if r.run_id in reviews]
    seen_inputs: set[str] = set()
    for run in runs:
        violations = global_violations(run, profile) if profile else []
        bad = run.status != "ok" or bool(violations)
        if only_failures and not bad:
            continue
        key = f"{run.input!r}|{bad}"
        if key in seen_inputs:  # the same request recorded several times: keep one
            continue
        seen_inputs.add(key)
        verdict = (reviews or {}).get(run.run_id, {})
        if verdict.get("verdict") == "bad":
            bad = True
        base = "promoted-" + (run.case_id or _slug(run.input if isinstance(run.input, str) else run.run_id))
        if verdict.get("label"):
            base = f"{_slug(verdict['label'], 30)}-{_slug(run.input if isinstance(run.input, str) else run.run_id, 24)}"
        cid, i = base, 2
        while cid in seen:
            cid, i = f"{base}-{i}", i + 1
        seen.add(cid)
        names = [c.name for c in run.tool_calls() if c.status != "denied"]
        case: dict = {"id": cid, "input": run.input, "from_run": run.run_id}
        user_turns = [s.text for s in run.steps if isinstance(s, UserTurn)]
        if user_turns:  # later user messages (e.g. "yes, book it") so a live re-run gets the same conversation
            case["turns"] = user_turns
        if run.metadata.get("context"):
            case["context"] = run.metadata["context"]
        said = " ".join([str(run.input or ""), *user_turns]).lower()
        expect: dict = {}
        if bad:
            if violations:
                case["description"] = "REVIEW: this run broke a rule: " + "; ".join(
                    f"{v.policy}: {v.detail}" for v in violations)
            elif verdict.get("label"):
                case["description"] = (f"Failure mode '{verdict['label']}' (from review). Add the expectation that "
                                       "fails on this behaviour, e.g. forbidden_tools, output.not_contains or args.")
            else:
                case["description"] = f"REVIEW: this run ended with {run.status}: {run.error}"
            if verdict.get("label"):
                case["tags"] = [_slug(verdict["label"], 30)]
            guard_types = ("forbidden_tool", "requires_prior_tool", "requires_approval")
            guarded = {p.id: p.check.tool for p in (profile.policies if profile else [])
                       if p.check and p.check.type in guard_types}
            offending = sorted({guarded[v.policy] for v in violations if v.policy in guarded} & set(names))
            if offending:
                expect["forbidden_tools"] = offending
            pol = sorted({v.policy for v in violations if v.policy not in ("SCOPE", "ARGS", "LOOP")})
            if pol:
                case["policies"] = pol
        else:
            case["description"] = f"Promoted from run {run.run_id[:12]}: review the expectations."
            if names:
                calls = []
                for c in run.tool_calls():
                    if c.status == "denied":
                        continue
                    # pin only arguments the user actually said (ids, amounts); values the backend produced
                    # (generated ids, looked-up emails) would make the case brittle
                    args = {k: v for k, v in (c.args or {}).items()
                            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).lower() in said}
                    calls.append({"name": c.name, "args": args} if args else {"name": c.name})
                expect["tools"] = {"mode": "in_order", "calls": calls}
        if expect:
            case["expect"] = expect
        cases.append(case)
    header = ("# Cases promoted from recorded runs by `evalplane promote`.\n"
              "# Review every case: tighten expectations, delete noise, and keep the ones worth guarding.\n")
    return header + yaml.safe_dump({"suite": suite, "cases": cases}, sort_keys=False, allow_unicode=True, width=110)
