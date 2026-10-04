"""Record OpenAI Agents SDK runs (`pip install openai-agents`) into Evalplane, with no tool decorators.

    from evalplane.integrations.openai_agents import install
    install()                                     # once, at startup

    with ep.record(question, case_id="refund-1") as run:
        result = await Runner.run(agent, question)
        run.output = result.final_output

`EvalplaneProcessor` implements the SDK's `TracingProcessor` interface (on_trace_start / on_trace_end /
on_span_start / on_span_end / shutdown / force_flush) and maps spans onto the current Evalplane run:

- `function` spans (`FunctionSpanData`: name, input = JSON arguments, output) -> `tool` steps. A span error
  becomes status `error`; a tool call rejected by a human approval becomes status `denied`.
- `handoff` spans (`HandoffSpanData`: from_agent, to_agent) -> `handoff` steps.
- `generation` / `response` spans -> `llm` steps with model and token usage.
- the first `agent` span names the run's agent.

The run is taken from `ep.record()` / `evalplane run` (the `_current` context variable) when the SDK trace
starts, so spans that finish on other tasks still land in the right run. Tool arguments and results are
only present when the SDK's `trace_include_sensitive_data` is on (the default). If tracing is disabled
(`set_tracing_disabled(True)` / OPENAI_AGENTS_DISABLE_TRACING=1), no spans are emitted and nothing is recorded.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from ..recorder import _current
from ..trace import Handoff, LLMCall, Run, ToolCall
from ._common import as_args, duration_ms, require


def _require_agents() -> Any:
    return require("agents", "openai-agents", "evalplane.integrations.openai_agents")


def _result(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    if hasattr(v, "model_dump"):
        try:
            return v.model_dump()
        except Exception:
            return str(v)
    return v


class EvalplaneProcessor:
    """An OpenAI Agents SDK trace processor that writes tool calls and handoffs into Evalplane runs.

    Pass `run` to always record into that run; otherwise the run active when each SDK trace starts is used
    (falling back to the run active when the span ends). Spans outside any run are ignored.
    """

    def __init__(self, run: Run | None = None) -> None:
        self._run = run
        self._lock = threading.Lock()
        self._trace_runs: dict[str, Run] = {}
        self._open_tools: dict[str, tuple[Run, ToolCall]] = {}

    # -- run lookup
    def _run_for(self, trace_id: str | None) -> Run | None:
        if self._run is not None:
            return self._run
        with self._lock:
            run = self._trace_runs.get(trace_id or "")
        return run if run is not None else _current.get()

    # -- TracingProcessor interface
    def on_trace_start(self, trace: Any) -> None:
        run = self._run or _current.get()
        if run is not None:
            with self._lock:
                self._trace_runs[trace.trace_id] = run

    def on_trace_end(self, trace: Any) -> None:
        with self._lock:
            self._trace_runs.pop(trace.trace_id, None)

    def on_span_start(self, span: Any) -> None:
        data = span.span_data
        if getattr(data, "type", None) != "function":
            return
        run = self._run_for(span.trace_id)
        if run is None:
            return
        step = ToolCall(name=data.name, args=as_args(data.input))
        run.steps.append(step)  # appended at start so nested calls keep their order
        with self._lock:
            self._open_tools[span.span_id] = (run, step)

    def on_span_end(self, span: Any) -> None:
        try:
            self._on_span_end(span)
        except Exception:  # a tracing processor must never break the agent
            pass

    def _on_span_end(self, span: Any) -> None:
        data = span.span_data
        kind = getattr(data, "type", None)
        dur = duration_ms(span.started_at, span.ended_at)
        if kind == "function":
            with self._lock:
                opened = self._open_tools.pop(span.span_id, None)
            if opened is None:  # span started before this processor was installed
                run = self._run_for(span.trace_id)
                if run is None:
                    return
                step = ToolCall(name=data.name)
                run.steps.append(step)
            else:
                step = opened[1]
            if data.input is not None:
                step.args = as_args(data.input)
            step.result = _result(data.output)
            step.duration_ms = dur
            if data.mcp_data:
                step.mcp = data.mcp_data  # type: ignore[attr-defined]  # extra field (ToolCall allows extras)
            err = span.error
            if err:
                detail = (err.get("data") or {}).get("error") if isinstance(err.get("data"), dict) else None
                text = " ".join(str(x) for x in (err.get("message"), detail) if x)
                step.status = "denied" if "rejected" in text.lower() else "error"
                step.error = text or "error"
            return

        run = self._run_for(span.trace_id)
        if run is None:
            return
        if kind == "handoff":
            if data.to_agent:
                run.steps.append(Handoff(to_agent=data.to_agent, from_agent=data.from_agent))
        elif kind == "agent":
            if run.agent is None:
                run.agent = data.name
        elif kind in ("generation", "response"):
            usage = data.usage or {}
            model = getattr(data, "model", None)
            response = getattr(data, "response", None)
            if response is not None:
                model = model or getattr(response, "model", None)
                if not usage and getattr(response, "usage", None) is not None:
                    u = response.usage
                    usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}
            run.steps.append(LLMCall(model=model, input_tokens=usage.get("input_tokens"),
                                     output_tokens=usage.get("output_tokens"), duration_ms=dur))

    def shutdown(self) -> None:
        with self._lock:
            self._trace_runs.clear()
            self._open_tools.clear()

    def force_flush(self) -> None:
        pass


def install(run: Run | None = None, *, replace: bool = False) -> EvalplaneProcessor:
    """Register an `EvalplaneProcessor` with the Agents SDK and return it.

    By default it is added next to the SDK's own exporter (`add_trace_processor`); `replace=True` makes it the
    only processor (`set_trace_processors`), so nothing is sent to the OpenAI traces dashboard.
    """
    agents = _require_agents()
    processor = EvalplaneProcessor(run)
    try:  # register as a virtual subclass so isinstance checks against the SDK's ABC pass
        agents.tracing.TracingProcessor.register(EvalplaneProcessor)
    except Exception:
        pass
    if replace:
        agents.set_trace_processors([processor])
    else:
        agents.add_trace_processor(processor)
    return processor
