"""Runtime guard: enforce the same policies you test with, before a tool call happens.

    guard = ep.Guard.from_file("agent.eval.yaml")            # or mode="shadow" to only log

    @guard.tool
    def issue_refund(order_id: str, amount: float): ...

    with guard.session(user_input):                          # one conversation / request
        guard.user_said("yes, go ahead")                     # later user turns (for confirmation rules)
        ...your agent runs; issue_refund(amount=900) raises ToolDenied before any money moves

The checks are the policy `check:`s from agent.eval.yaml plus tool scope, `max_calls_per_run` and
`args_schema`. A call is blocked only if *it* would add a violation. In "shadow" mode nothing is blocked:
violations are reported to `on_violation` (default: logged) so you can roll the guard out safely.
Blocked calls are recorded as `denied` steps, so `evalplane audit` and evals see them too.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from .models import AgentProfile, load_profile
from .recorder import SimulatedToolError, ToolDenied, _current, record
from .scorers import Violation, global_violations
from .trace import Run, ToolCall, UserTurn

log = logging.getLogger("evalplane.guard")


class Guard:
    def __init__(self, profile: AgentProfile, mode: Literal["enforce", "shadow"] = "enforce",
                 on_violation: Callable[[Violation, ToolCall], None] | None = None):
        self.profile = profile
        self.mode = mode
        self.on_violation = on_violation or (lambda v, call: log.warning("evalplane guard: %s: %s", v.policy,
                                                                          v.detail))
        self.blocked: list[Violation] = []

    @classmethod
    def from_file(cls, path: str | Path = "agent.eval.yaml", **kw) -> Guard:
        return cls(load_profile(path), **kw)

    # ----------------------------------------------------------------- sessions

    @contextmanager
    def session(self, input: Any = None, **kw):
        """One conversation or request. Reuses the current run if one is already being recorded
        (e.g. inside `evalplane run`), so guards and evals see the same steps."""
        if _current.get() is not None:
            yield _current.get()
            return
        with record(input, **kw) as run:
            yield run

    def user_said(self, text: str) -> None:
        run = _current.get()
        if run is not None:
            run.steps.append(UserTurn(text=text))

    # ----------------------------------------------------------------- checking

    def check(self, name: str, args: dict[str, Any]) -> list[Violation]:
        """Violations this call would add to the current session (empty = allowed)."""
        from collections import Counter

        run = _current.get() or Run()
        before = Counter(_key(v) for v in global_violations(run, self.profile))
        trial = run.model_copy(update={"steps": [*run.steps, ToolCall(name=name, args=args)]})
        new: list[Violation] = []
        for v in global_violations(trial, self.profile):
            if v.detail.startswith("output contains"):  # can't be judged before the answer exists
                continue
            if before[_key(v)] > 0:
                before[_key(v)] -= 1  # this one already existed before the call
            else:
                new.append(v)
        return new

    def tool(self, fn: Callable | None = None, *, name: str | None = None):
        """Decorator: check each call against the policies, record it, and block it if it violates one."""

        def wrap(f: Callable) -> Callable:
            tool_name = name or f.__name__
            sig = inspect.signature(f)

            @functools.wraps(f)
            def inner(*args, **kwargs):
                try:
                    bound = sig.bind_partial(*args, **kwargs)
                    call_args = {k: v for k, v in bound.arguments.items() if k not in ("self", "cls", "context", "ctx")}
                except TypeError:
                    call_args = dict(kwargs)
                violations = self.check(tool_name, call_args)
                run = _current.get()
                for v in violations:
                    self.on_violation(v, ToolCall(name=tool_name, args=call_args))
                if violations and self.mode == "enforce":
                    self.blocked.extend(violations)
                    if run is not None:
                        run.steps.append(ToolCall(name=tool_name, args=call_args, status="denied",
                                                  error="; ".join(f"{v.policy}: {v.detail}" for v in violations)))
                    raise ToolDenied("; ".join(f"{v.policy}: {v.detail}" for v in violations))
                step = ToolCall(name=tool_name, args=call_args)
                if run is not None:
                    run.steps.append(step)
                    if tool_name in (run.metadata.get("fail_tools") or []):  # simulated outage in a test case
                        step.status, step.error = "error", "SimulatedToolError: injected by evalplane"
                        raise SimulatedToolError(f"{tool_name} failed (simulated by evalplane for this case)")
                try:
                    step.result = f(*args, **kwargs)
                    return step.result
                except Exception as e:
                    step.status, step.error = "error", f"{type(e).__name__}: {e}"
                    raise

            return inner

        return wrap(fn) if fn is not None else wrap


def _key(v: Violation) -> tuple[str, str]:
    return (v.policy, v.detail)
