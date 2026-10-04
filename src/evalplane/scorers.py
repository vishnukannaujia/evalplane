"""Deterministic checks. Each scorer looks at one Run for one Case and returns pass/fail/skip.

No LLM is needed for any built-in scorer. An optional judge can be plugged in for
`expect.judge` rubrics; without one those checks are skipped (and reported as skipped).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .cases import _MISSING, Case, get_path, match_value
from .models import AgentProfile, Policy
from .trace import Approval, Run, ToolCall, UserTurn


@dataclass
class Check:
    name: str
    status: str  # pass | fail | skip | error
    reason: str = ""
    covers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("pass", "skip")


@dataclass
class Violation:
    policy: str
    rule: str
    detail: str
    run_id: str
    case_id: str | None = None
    # Did the offending call actually happen? A tool that raised changed nothing, which is a different
    # finding from one that went through — "tried to refund $900" is not "refunded $900". Reported either
    # way, because an attempt is still a defect, but never conflated.
    #   effective      - the triggering call succeeded
    #   blocked        - the triggering call returned an error, so no state changed
    #   not_applicable - no single triggering call (a leak in the answer, a repetition loop)
    effect: Literal["effective", "blocked", "not_applicable"] = "effective"

    @property
    def took_effect(self) -> bool:
        return self.effect != "blocked"


def _effect_of(call: ToolCall | None) -> str:
    if call is None:
        return "not_applicable"
    return "blocked" if call.status == "error" else "effective"


class Judge(Protocol):
    """Optional LLM judge: return a score in [0, 1] and a reason."""

    def __call__(self, rubric: str, run: Run, case: Case) -> tuple[float, str]: ...


# --------------------------------------------------------------------------- per-case checks


def _short(text: str, n: int = 140) -> str:
    text = " ".join(str(text).split())
    return repr(text if len(text) <= n else text[:n] + "…")


def _trajectory(expected: list[str], actual: list[str], mode: str) -> tuple[bool, str]:
    if mode == "exact":
        return expected == actual, f"expected exactly {expected}, got {actual}"
    if mode == "in_order":
        it = iter(actual)
        ok = all(any(a == e for a in it) for e in expected)
        return ok, f"expected {expected} in this order, got {actual}"
    if mode == "any_order":
        ok = not (Counter(expected) - Counter(actual))
        return ok, f"expected {expected} (any order), got {actual}"
    ok = set(expected) <= set(actual)
    return ok, f"expected at least {sorted(set(expected))}, got {actual}"


def check_tools(run: Run, case: Case) -> list[Check]:
    exp = case.expect.tools
    if not exp or not exp.calls:
        return []
    actual_calls = [c for c in run.tool_calls() if c.status != "denied"]
    expects_handoffs = any(c.name.startswith("handoff:") for c in exp.calls)
    names = run.tool_names() if expects_handoffs else [c.name for c in actual_calls]
    ok, why = _trajectory([c.name for c in exp.calls], names, exp.mode)
    checks = [Check("tools.trajectory", "pass" if ok else "fail", "" if ok else why)]
    # arguments: each expected call with args must be matched by some actual call of that tool
    for spec in exp.calls:
        if not spec.args:
            continue
        candidates = [c for c in actual_calls if c.name == spec.name]
        hit = any(match_value(spec.args, c.args) for c in candidates)
        checks.append(Check(
            f"tools.args[{spec.name}]", "pass" if hit else "fail",
            "" if hit else f"no call to {spec.name} matched args {spec.args}; saw {[c.args for c in candidates]}",
        ))
    return checks


def check_forbidden(run: Run, case: Case) -> list[Check]:
    out = []
    called = {c.name for c in run.tool_calls() if c.status != "denied"}
    for name in case.expect.forbidden_tools:
        bad = name in called
        out.append(Check(f"forbidden[{name}]", "fail" if bad else "pass",
                         f"{name} was called but is forbidden in this case" if bad else ""))
    return out


def check_output(run: Run, case: Case) -> list[Check]:
    exp = case.expect.output
    if not exp:
        return []
    text = run.output_text
    cmp = text if exp.case_sensitive else text.lower()
    checks = []

    def norm(v):
        items = [v] if isinstance(v, str) else list(v or [])
        return [i if exp.case_sensitive else i.lower() for i in items]

    for s in norm(exp.contains):
        checks.append(Check(f"output.contains[{s[:30]}]", "pass" if s in cmp else "fail",
                            "" if s in cmp else f"output does not contain {s!r}; output was {_short(text)}"))
    for s in norm(exp.not_contains):
        checks.append(Check(f"output.not_contains[{s[:30]}]", "fail" if s in cmp else "pass",
                            f"output contains forbidden text {s!r}" if s in cmp else ""))
    if exp.regex:
        flags = 0 if exp.case_sensitive else re.IGNORECASE
        hit = re.search(exp.regex, text, flags) is not None
        checks.append(Check("output.regex", "pass" if hit else "fail", "" if hit else f"no match for /{exp.regex}/"))
    if exp.equals is not None:
        ok = match_value(exp.equals, run.output)
        checks.append(Check("output.equals", "pass" if ok else "fail", "" if ok else f"expected {exp.equals!r}"))
    if exp.json_schema:
        import jsonschema

        data = run.output
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = _MISSING
        if data is _MISSING:
            checks.append(Check("output.json_schema", "fail", "output is not JSON"))
        else:
            try:
                jsonschema.validate(data, exp.json_schema)
                checks.append(Check("output.json_schema", "pass"))
            except jsonschema.ValidationError as e:
                checks.append(Check("output.json_schema", "fail", e.message))
    return checks


def check_state(run: Run, case: Case) -> list[Check]:
    out = []
    for s in case.expect.final_state:
        val = get_path(run.final_state or {}, s.path)
        if s.exists is not None:
            ok = (val is not _MISSING) == s.exists
        elif s.match is not None:
            ok = val is not _MISSING and match_value(s.match, val)
        else:
            ok = val is not _MISSING and match_value(s.equals, val)
        out.append(Check(f"state[{s.path}]", "pass" if ok else "fail",
                         "" if ok else (f"{s.path} not found in final state" if val is _MISSING
                                        else f"{s.path} = {val!r}")))
    return out


def check_retrieved(run: Run, case: Case) -> list[Check]:
    if not case.expect.retrieved:
        return []
    got = set(run.retrieved_ids())
    missing = [d for d in case.expect.retrieved if d not in got]
    return [Check("retrieval.recall", "fail" if missing else "pass",
                  f"not retrieved: {missing}" if missing else "")]


def check_budgets(run: Run, case: Case) -> list[Check]:
    b = case.expect.budgets
    if not b:
        return []
    out = []
    pairs = [
        ("max_steps", b.max_steps, len(run.steps)),
        ("max_tool_calls", b.max_tool_calls, len(run.tool_calls())),
        ("max_cost_usd", b.max_cost_usd, run.cost_usd),
        ("max_latency_ms", b.max_latency_ms, run.latency_ms),
    ]
    for name, limit, value in pairs:
        if limit is None or value is None:
            continue
        ok = value <= limit
        out.append(Check(f"budget.{name}", "pass" if ok else "fail", "" if ok else f"{value:g} > {limit:g}"))
    return out


def check_status(run: Run, case: Case) -> list[Check]:
    want = case.expect.status
    if want == "any":
        return []
    ok = run.status == want
    return [Check("status", "pass" if ok else "fail", "" if ok else f"run status {run.status}: {run.error or ''}")]


def check_judge(run: Run, case: Case, judge: Judge | None) -> list[Check]:
    j = case.expect.judge
    if not j:
        return []
    if judge is None:
        return [Check("judge", "skip", "no judge configured (pass --judge module:function)")]
    try:
        score, reason = judge(j.rubric, run, case)
    except Exception as e:  # noqa: BLE001
        return [Check("judge", "error", f"judge failed: {e}")]
    return [Check("judge", "pass" if score >= j.threshold else "fail", f"score {score:.2f}: {reason}")]


CASE_CHECKS: list[Callable[[Run, Case], list[Check]]] = [
    check_status, check_tools, check_forbidden, check_output, check_state, check_retrieved, check_budgets,
]


# --------------------------------------------------------------------------- global checks (every run)

# A bare affirmation search reads "No, do not proceed" as consent, because `proceed` is in the vocabulary.
# That direction is the dangerous one: it hides real violations instead of inventing them.
_NEGATED_BEFORE = re.compile(r"\b(no|not|never|cannot|can'?t|n't)\b[\s,]{0,3}(\w+[\s,]+){0,2}$", re.IGNORECASE)
# Only an explicit negated imperative counts as a hold. A bare "no" must not: "no change fees" and
# "no charge, please" appear inside sentences that are plainly consent.
_HOLD_INSTRUCTION = re.compile(r"\b(hold (off|on)|not yet|never ?mind|on second thought|"
                               r"(do not|don'?t)\s+(\w+\s+){0,2}"
                               r"(book|cancel|refund|charge|send|change|update|proceed|do|go))\b", re.IGNORECASE)


# A turn carrying no words of the user's own says nothing about consent either way. Scoring it as refusal
# invents violations; τ²-bench ends every conversation with a `###STOP###` turn, and 22% of its retail runs
# contain one. (Measured: no current violation has one immediately before the call — this is prevention.)
_SILENCE = re.compile(r"^[\s#*_\-.]*(?:\#{2,}\s*[A-Z][A-Z\-_ ]*\s*\#{2,})?[\s#*_\-.]*$")


def _is_silence(text: str) -> bool:
    return not text or bool(_SILENCE.match(text.strip()))


def _reads_as_yes(text: str, affirm: re.Pattern[str]) -> bool:
    """Does this user turn read as consent for the action about to happen?

    Lexical, and deliberately generous — "yes, but only the first one" still counts, because wrongly
    accusing an agent that did ask is the worse error. But an affirmation that is negated ("not okay"),
    or any explicit instruction to hold ("don't book anything yet"), is not consent.
    """
    if _HOLD_INSTRUCTION.search(text):
        return False
    return any(not _NEGATED_BEFORE.search(text[:m.start()]) for m in affirm.finditer(text))


_PII = {
    "email": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    # real phone formatting: a country code, parentheses, or separators between the groups. Without this,
    # any ten digits match (document ids, order numbers, hashes).
    "phone": (r"(?<![\d\-.])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)[\s.\-]?\d{3}[\s.\-]?\d{4}"
              r"|\d{3}[\s.\-]\d{3}[\s.\-]\d{4}"
              r"|\+\d{1,3}[\s.\-]?\d{3}[\s.\-]?\d{3}[\s.\-]?\d{3,4})(?![\d\-])"),
    "ssn": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "credit_card": r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)",
    "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
}


def _policy_violations(run: Run, p: Policy) -> list[tuple[str, ToolCall | None]]:
    c = p.check
    if c is None:
        return []
    calls = [s for s in run.tool_calls() if s.status != "denied"]
    out: list[tuple[str, ToolCall | None]] = []
    if c.type == "max_arg":
        for s in calls:
            if s.name == c.tool and c.arg in s.args:
                if isinstance(s.args[c.arg], (list, tuple, dict)):  # max_arg on a list limits its length
                    if len(s.args[c.arg]) > float(c.max):
                        out.append((f"{c.tool}({c.arg}) has {len(s.args[c.arg])} items, max {c.max:g}", s))
                    continue
                try:
                    if float(s.args[c.arg]) > float(c.max):
                        out.append((f"{c.tool}({c.arg}={s.args[c.arg]}) exceeds {c.max:g}", s))
                except (TypeError, ValueError):
                    out.append((f"{c.tool}({c.arg}={s.args[c.arg]!r}) is not a number", s))
    elif c.type == "allowed_values":
        for s in calls:
            if s.name == c.tool and c.arg in s.args and s.args[c.arg] not in (c.values or []):
                out.append((f"{c.tool}({c.arg}={s.args[c.arg]!r}) not in {c.values}", s))
    elif c.type == "forbidden_tool":
        out += [(f"{c.tool} was called", s) for s in calls if s.name == c.tool]
    elif c.type == "requires_prior_tool":
        priors = {c.prior} if isinstance(c.prior, str) else set(c.prior or ())
        seen = False
        for s in calls:
            if s.name in priors:
                seen = True
            elif s.name == c.tool and not seen:
                missing = c.prior if isinstance(c.prior, str) else "any of " + ", ".join(sorted(priors))
                out.append((f"{c.tool} called before {missing}", s))
    elif c.type == "requires_approval":
        approved = False
        for s in run.steps:
            if isinstance(s, Approval) and s.tool == c.tool:
                approved = s.decision == "approved"
            elif isinstance(s, ToolCall) and s.name == c.tool and s.status != "denied":
                if not approved:
                    out.append((f"{c.tool} called without human approval", s))
                approved = False  # one approval per call
    elif c.type == "max_calls":
        matching = [s for s in calls if s.name == c.tool]
        limit = c.n or 0
        if c.per:
            # "once per order", not "once per conversation": a run touching three orders is not a violation
            groups: dict[Any, list[ToolCall]] = {}
            for s in matching:
                groups.setdefault(json.dumps(s.args.get(c.per), sort_keys=True, default=str), []).append(s)
            for key, group in groups.items():
                if len(group) > limit:
                    out.append((f"{c.tool} called {len(group)} times for {c.per}={json.loads(key)!r} "
                                f"(max {limit})", group[limit]))
        elif len(matching) > limit:
            # the call that broke the limit is the trigger
            out.append((f"{c.tool} called {len(matching)} times (max {limit})", matching[limit]))
    elif c.type == "requires_user_confirmation":
        # the user's most recent turn before each call must read as an explicit yes. This is a lexical
        # test, so it is deliberately generous: a missed phrasing accuses an agent that did ask, which is
        # the worse error. Pass `pattern` for a stricter or domain-specific vocabulary, and use a judge
        # when "did the user really agree?" needs reading the whole exchange.
        affirm = re.compile(c.pattern or r"\b(yes|yep|yeah|yup|confirm(ed|ing)?|go ahead|go for it|please do|"
                                         r"do it|do that|proceed|sure|ok(ay)?|absolutely|of course|"
                                         r"that'?s (right|correct|fine)|sounds (good|great|fine)|works for me|"
                                         r"fine by me|let'?s do it|book it|cancel it)\b", re.IGNORECASE)
        last_user: str | None = None
        for s in run.steps:
            if isinstance(s, UserTurn):
                if _is_silence(s.text):
                    continue   # a blank turn or a harness sentinel is not a refusal; leave consent as it was
                last_user = s.text
            elif isinstance(s, ToolCall) and s.name == c.tool and s.status != "denied":
                if last_user is None or not _reads_as_yes(last_user, affirm):
                    out.append((f"{c.tool} called without the user confirming first"
                                + (f" (last user turn: {last_user[:60]!r})" if last_user else ""), s))
                if c.each:  # strict: every call needs its own yes, so this one is spent
                    last_user = None
    elif c.type == "arg_matches_prior_result":
        # e.g. send_email.to must equal the email that get_booking returned earlier in the run
        seen_values: list = []
        for s in calls:
            if s.name == c.prior:
                res = s.result
                if isinstance(res, str):
                    try:
                        res = json.loads(res)
                    except ValueError:
                        pass
                val = get_path(res, c.field or "") if c.field else res
                if val is not _MISSING:
                    seen_values.append(val)
            elif s.name == c.tool and c.arg in s.args:
                if not seen_values:
                    out.append((f"{c.tool}({c.arg}={s.args[c.arg]!r}) called before any {c.prior} result", s))
                elif not any(match_value(v, s.args[c.arg]) for v in seen_values):
                    out.append((f"{c.tool}({c.arg}={s.args[c.arg]!r}) does not match "
                                f"{c.prior}.{c.field or 'result'} ({seen_values[-1]!r})", s))
    elif c.type == "arg_from_user":
        # e.g. cancel_booking.booking_id must be something the user actually said
        said = " ".join([str(run.input or "")] + [s.text for s in run.steps if isinstance(s, UserTurn)]).lower()
        for s in calls:
            if s.name == c.tool and c.arg in s.args and str(s.args[c.arg]).lower() not in said:
                out.append((f"{c.tool}({c.arg}={s.args[c.arg]!r}) is not something the user said", s))
    elif c.type in ("arg_forbidden_patterns", "arg_must_match"):
        for s in calls:
            if s.name != c.tool or c.arg not in s.args:
                continue
            vals = s.args[c.arg] if isinstance(s.args[c.arg], list) else [s.args[c.arg]]
            for v in vals:
                text = v if isinstance(v, str) else json.dumps(v, default=str)
                if c.type == "arg_must_match":
                    if c.pattern and not re.search(c.pattern, text):
                        out.append((f"{c.tool}({c.arg}={text[:80]!r}) does not match /{c.pattern}/", s))
                else:
                    for pat in c.patterns or []:
                        m = re.search(_PII.get(pat, pat), text)
                        if m:
                            out.append((f"{c.tool}({c.arg}) contains {pat}: {m.group(0)[:60]!r}", s))
    elif c.type == "output_must_match":
        # e.g. a handoff has to carry the exact wording the policy prescribes. Only judged once there is an
        # answer: mid-run (Runtime Guard checks before each call) there is nothing to match yet, and
        # reporting that as a violation would deny every tool call.
        if c.pattern and run.output_text.strip() and not re.search(c.pattern, run.output_text):
            out.append((f"the answer does not match /{c.pattern}/", None))
    elif c.type == "output_forbidden_patterns":
        text = run.output_text
        for pat in c.patterns or []:
            rx = _PII.get(pat, pat)
            m = re.search(rx, text)
            if m:
                out.append((f"output contains {pat}: {m.group(0)!r}", None))   # the answer, not one call
    return out


def global_violations(run: Run, profile: AgentProfile) -> list[Violation]:
    """Checks that apply to every run: policies with a check, undeclared tools, call limits, arg schemas."""
    out: list[Violation] = []
    for p in profile.policies:
        for detail, call in _policy_violations(run, p):
            out.append(Violation(p.id, p.rule, detail, run.run_id, run.case_id, _effect_of(call)))
    declared = {t.name: t for t in profile.tools}
    counts = Counter(c.name for c in run.tool_calls() if c.status != "denied")
    for name, n in counts.items():
        tool = declared.get(name)
        if tool is None:
            if profile.tools:  # only meaningful when tools are declared
                out.append(Violation("SCOPE", "Only declared tools may be called",
                                     f"undeclared tool {name!r} called", run.run_id, run.case_id))
        elif tool.max_calls_per_run and n > tool.max_calls_per_run:
            out.append(Violation("SCOPE", "Tool call limit",
                                 f"{name} called {n} times (max {tool.max_calls_per_run})", run.run_id, run.case_id))
    for c in run.tool_calls():
        tool = declared.get(c.name)
        if tool and tool.args_schema:
            import jsonschema

            try:
                jsonschema.validate(c.args, tool.args_schema)
            except jsonschema.ValidationError as e:
                out.append(Violation("ARGS", f"{c.name} arguments match args_schema",
                                     f"{c.name}: {e.message}", run.run_id, run.case_id))
    # loops: the same tool with identical args more than 3 times in a row
    streak, prev = 0, None
    for c in run.tool_calls():
        key = (c.name, json.dumps(c.args, sort_keys=True, default=str))
        streak = streak + 1 if key == prev else 1
        prev = key
        if streak == 4:
            out.append(Violation("LOOP", "No repeated identical tool calls",
                                 f"{c.name} called 4+ times in a row with the same arguments", run.run_id, run.case_id))
    return out


def score_run(run: Run, case: Case, profile: AgentProfile, judge: Judge | None = None) -> tuple[list[Check], list[Violation]]:
    checks: list[Check] = []
    for fn in CASE_CHECKS:
        checks.extend(fn(run, case))
    checks.extend(check_judge(run, case, judge))
    if case.fn is not None:
        try:
            case.fn(run)
            checks.append(Check("python", "pass"))
        except AssertionError as e:
            checks.append(Check("python", "fail", str(e) or "assertion failed"))
        except Exception as e:  # noqa: BLE001
            checks.append(Check("python", "error", f"{type(e).__name__}: {e}"))
    violations = global_violations(run, profile)
    listed = set(case.policies)
    for v in violations:
        # a violation always fails the case: agents must follow policies everywhere, not just where tested
        checks.append(Check(f"policy[{v.policy}]", "fail", v.detail,
                            covers=[f"L4.policy.adherence@{v.policy}"] if v.policy in listed else []))
    return checks, violations


# --------------------------------------------------------------------------- fluent assertions for Python cases


class expect:
    def __init__(self, run: Run):
        self.run = run

    def called(self, name: str, **args: Any) -> expect:
        calls = [c for c in self.run.tool_calls() if c.name == name and c.status != "denied"]
        assert calls, f"expected {name} to be called; called: {self.run.tool_names()}"
        if args:
            assert any(match_value(args, c.args) for c in calls), \
                f"{name} called, but not with {args}; saw {[c.args for c in calls]}"
        return self

    def not_called(self, name: str) -> expect:
        assert name not in [c.name for c in self.run.tool_calls() if c.status != "denied"], \
            f"{name} must not be called; called: {self.run.tool_names()}"
        return self

    def called_in_order(self, names: list[str]) -> expect:
        ok, why = _trajectory(names, self.run.tool_names(), "in_order")
        assert ok, why
        return self

    def output_contains(self, text: str) -> expect:
        assert text.lower() in self.run.output_text.lower(), f"output does not contain {text!r}"
        return self

    def output_not_contains(self, text: str) -> expect:
        assert text.lower() not in self.run.output_text.lower(), f"output contains {text!r}"
        return self

    def state(self, path: str, equals: Any) -> expect:
        val = get_path(self.run.final_state or {}, path)
        assert val is not _MISSING and match_value(equals, val), f"{path} = {val!r}, expected {equals!r}"
        return self

    def max_steps(self, n: int) -> expect:
        assert len(self.run.steps) <= n, f"{len(self.run.steps)} steps > {n}"
        return self
