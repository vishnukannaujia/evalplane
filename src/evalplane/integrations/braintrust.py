"""Braintrust logs / experiment spans -> Evalplane runs (one per root span).

Input: Braintrust span rows as returned by the API (`POST /v1/project_logs/{id}/fetch`,
`/v1/experiment/{id}/fetch` -> `{"events": [...]}`), BTQL queries or the UI's JSON export; a JSON list,
the `{"events": [...]}` envelope, or JSONL. Each row has `span_id`, `root_span_id`, `span_parents`,
`span_attributes: {name, type}`, `input`, `output`, `error`, `metadata`, `metrics: {start, end,
prompt_tokens, completion_tokens, ...}`.

Mapping by `span_attributes.type`: `tool` -> `tool` step (name, input -> args, output -> result, error ->
status error); `llm` -> `llm` step (`metadata.model`, token metrics, `metrics.estimated_cost` if present);
a `task` span named "Handoff" with `metadata.to_agent` (what Braintrust's OpenAI Agents integration logs) ->
`handoff`. The root span gives the run's name, input, output, error, `expected` and `scores` (kept in
metadata) and latency. Plain `@traced` spans (type `function`) and `score`/`eval` spans are not steps. If a
trace has no tool spans, tool calls requested in LLM outputs are used instead (without results).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..trace import Handoff, LLMCall, Run, ToolCall
from ._common import (
    as_args,
    as_list,
    case_id_from,
    first_user_text,
    maybe_json,
    pick,
    read_records,
    tool_calls_from_output,
    unwrap,
)


def is_braintrust(records: list[Any]) -> bool:
    for r in records[:5]:
        if not isinstance(r, dict):
            return False
        if "span_attributes" in r or ("root_span_id" in r and "span_id" in r):
            return True
    return False


def _attrs(r: dict[str, Any]) -> dict[str, Any]:
    a = r.get("span_attributes")
    return a if isinstance(a, dict) else {}


def _metrics(r: dict[str, Any]) -> dict[str, Any]:
    m = r.get("metrics")
    return m if isinstance(m, dict) else {}


def _meta(r: dict[str, Any]) -> dict[str, Any]:
    m = r.get("metadata")
    return m if isinstance(m, dict) else {}


def _dur(r: dict[str, Any]) -> float | None:
    m = _metrics(r)
    if isinstance(m.get("start"), (int, float)) and isinstance(m.get("end"), (int, float)):
        return round((m["end"] - m["start"]) * 1000, 3)  # Braintrust metrics are epoch seconds
    return None


def _error_text(e: Any) -> str | None:
    if e is None or e == "" or e is False:
        return None
    if isinstance(e, dict):
        return str(pick(e, "message", "error", default=e))
    return str(e)


def braintrust_to_runs(data: Any) -> list[Run]:
    """Convert already-parsed Braintrust span rows to Evalplane Runs, one per root span."""
    rows = [r for r in as_list(data, "events", "data", "rows") if isinstance(r, dict)]
    traces: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        traces.setdefault(str(r.get("root_span_id") or r.get("span_id") or r.get("id")), []).append(r)

    runs = []
    for root_id, spans in traces.items():
        spans.sort(key=lambda r: (_metrics(r).get("start") or 0.0, str(r.get("created") or "")))
        root = next((s for s in spans if str(s.get("span_id")) == root_id), None) or \
            next((s for s in spans if not s.get("span_parents")), spans[0])
        run = Run(run_id=root_id, agent=_attrs(root).get("name"), metadata={"source": "braintrust"})
        run.case_id = case_id_from(_meta(root), *(_meta(s) for s in spans))
        run.input = first_user_text(unwrap(root.get("input"), "input", "query", "question", "message"))
        run.output = unwrap(root.get("output"), "output", "answer", "response")
        err = _error_text(root.get("error"))
        if err:
            run.status, run.error = "error", err
        run.latency_ms = _dur(root)
        for key in ("expected", "scores", "tags"):
            if root.get(key) is not None:
                run.metadata[key] = root[key]
        if root.get("experiment_id"):
            run.metadata["experiment_id"] = root["experiment_id"]

        has_tools = any(_attrs(s).get("type") == "tool" for s in spans)
        for s in spans:
            if s is root:
                continue
            a, m, meta = _attrs(s), _metrics(s), _meta(s)
            kind = a.get("type")
            if kind == "tool":
                e = _error_text(s.get("error"))
                run.steps.append(ToolCall(
                    name=a.get("name") or "?", args=as_args(s.get("input")), result=maybe_json(s.get("output")),
                    status="error" if e else "ok", error=e, duration_ms=_dur(s),
                ))
            elif kind == "llm":
                cost = pick(m, "estimated_cost", "cost")
                run.steps.append(LLMCall(
                    model=pick(meta, "model"), input_tokens=pick(m, "prompt_tokens"),
                    output_tokens=pick(m, "completion_tokens"),
                    cost_usd=float(cost) if cost is not None else None, duration_ms=_dur(s),
                ))
                if not has_tools:
                    run.steps.extend(tool_calls_from_output(s.get("output")))
            elif kind == "task" and a.get("name") == "Handoff" and meta.get("to_agent"):
                run.steps.append(Handoff(to_agent=str(meta["to_agent"]), from_agent=meta.get("from_agent")))
        runs.append(run)
    return runs


def load_braintrust(path: str | Path) -> list[Run]:
    """Load exported Braintrust logs / experiment spans (JSON or JSONL) as Evalplane runs."""
    return braintrust_to_runs(read_records(path))
