"""Record what an agent does, from any Python agent, with no framework dependency.

    import evalplane as ep

    @ep.tool
    def issue_refund(order_id: str, amount: float): ...

Inside `evalplane run` (or an `ep.record()` block), every call to a decorated tool is
recorded as a ToolCall step. Outside of those, the decorator does nothing.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .trace import Approval, Handoff, LLMCall, Retrieval, Run, ToolCall, write_runs

_current: contextvars.ContextVar[Run | None] = contextvars.ContextVar("evalplane_run", default=None)


def current_run() -> Run | None:
    return _current.get()


@contextmanager
def record(input: Any = None, *, case_id: str | None = None, agent: str | None = None,
           save_to: str | Path | None = None) -> Iterator[Run]:
    """Record one run. Set `run.output` (and optionally `run.final_state`) inside the block."""
    run = Run(input=input, case_id=case_id, agent=agent)
    token = _current.set(run)
    start = time.perf_counter()
    try:
        yield run
    except Exception as e:
        run.status = "error"
        run.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        run.latency_ms = (time.perf_counter() - start) * 1000
        _current.reset(token)
        if save_to:
            path = Path(save_to)
            existing = path.read_text() if path.exists() else ""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(existing + run.model_dump_json(exclude_none=True) + "\n")


def tool(fn: Callable | None = None, *, name: str | None = None):
    """Decorator: record calls to this function as tool calls when a run is being recorded."""

    def wrap(f: Callable) -> Callable:
        tool_name = name or f.__name__
        sig = inspect.signature(f)

        @functools.wraps(f)
        def inner(*args, **kwargs):
            run = _current.get()
            if run is None:
                return f(*args, **kwargs)
            try:
                bound = sig.bind_partial(*args, **kwargs)
                call_args = {k: v for k, v in bound.arguments.items() if k not in ("self", "cls", "context", "ctx")}
            except TypeError:
                call_args = {"args": list(args), **kwargs}
            step = ToolCall(name=tool_name, args=call_args)
            run.steps.append(step)
            start = time.perf_counter()
            if tool_name in (run.metadata.get("fail_tools") or []):
                # simulated outage from the case's `context: {fail_tools: [...]}`
                step.status = "error"
                step.error = "SimulatedToolError: injected by evalplane (context.fail_tools)"
                step.duration_ms = 0.0
                raise SimulatedToolError(f"{tool_name} failed (simulated by evalplane for this case)")
            try:
                result = f(*args, **kwargs)
                step.result = result
                return result
            except ToolDenied as e:
                step.status = "denied"
                step.error = str(e)
                raise
            except Exception as e:
                step.status = "error"
                step.error = f"{type(e).__name__}: {e}"
                raise
            finally:
                step.duration_ms = (time.perf_counter() - start) * 1000

        return inner

    return wrap(fn) if fn is not None else wrap


class SimulatedToolError(RuntimeError):
    """Raised by an @ep.tool listed in the case's `context.fail_tools`, to test error recovery."""


class ToolDenied(Exception):
    """Raise inside a tool to record that the call was blocked (e.g. by a guardrail)."""


def _append(step) -> None:
    run = _current.get()
    if run is not None:
        run.steps.append(step)


def approve(tool: str, decision: str = "approved", approver: str | None = None) -> None:
    """Record a human approval (or rejection) for a tool call."""
    _append(Approval(tool=tool, decision=decision, approver=approver))


def retrieved(doc_ids: list[str], query: str | None = None) -> None:
    """Record which documents a retrieval step returned."""
    _append(Retrieval(query=query, doc_ids=[str(d) for d in doc_ids]))


def handoff(to_agent: str, from_agent: str | None = None) -> None:
    """Record a handoff to another agent."""
    _append(Handoff(to_agent=to_agent, from_agent=from_agent))


def llm(model: str | None = None, input_tokens: int | None = None, output_tokens: int | None = None,
        cost_usd: float | None = None, output: Any = None) -> None:
    """Record an LLM call (for cost and step budgets)."""
    _append(LLMCall(model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                    cost_usd=cost_usd, output=output))


def save_runs(path: str | Path, runs: list[Run]) -> None:
    write_runs(path, runs)
