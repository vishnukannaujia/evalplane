"""Langfuse traces -> Evalplane runs.

Accepts what the Langfuse public API / SDK return and what the UI's JSON export produces:

- traces with nested `observations` (`GET /api/public/traces/{id}`, `langfuse.api.trace.get(...)`,
  the "download trace" JSON), optionally wrapped in `{"data": [...]}`;
- a flat list of observations (`GET /api/public/observations`, the observations table export, blob-storage
  exports), grouped into runs by `traceId`;
- JSON or JSONL, camelCase (API) or snake_case (SDK `model_dump()`) keys.

Observation types (Langfuse v3+): GENERATION -> `llm` step (model, tokens, cost); TOOL -> `tool` step
(name, input -> args, output -> result, level ERROR -> status error); RETRIEVER -> `retrieval`; AGENT -> sets
the run's agent, later different AGENT observations -> `handoff`. SPAN/CHAIN/EVENT/EVALUATOR/EMBEDDING/
GUARDRAIL are skipped, except that in traces without TOOL observations (older SDKs) a SPAN whose name matches
a tool the model requested is treated as that tool call; if there are no tool observations at all, the
tool calls requested in GENERATION outputs (`tool_calls` / `tool_use`) are used instead (without results).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..trace import Handoff, LLMCall, Retrieval, Run, ToolCall
from ._common import (
    as_args,
    as_list,
    case_id_from,
    doc_ids,
    duration_ms,
    first_user_text,
    maybe_json,
    pick,
    query_text,
    read_records,
    require,
    to_seconds,
    tool_calls_from_output,
    unwrap,
)

OBSERVATION_TYPES = {"SPAN", "GENERATION", "EVENT", "AGENT", "TOOL", "CHAIN", "RETRIEVER", "EVALUATOR",
                     "EMBEDDING", "GUARDRAIL"}


def is_langfuse(records: list[Any]) -> bool:
    """Heuristic used by `load_any`: does this look like a Langfuse trace or observation export?"""
    for r in records[:5]:
        if not isinstance(r, dict):
            return False
        if isinstance(r.get("observations"), list) and ("id" in r) and "steps" not in r:
            return True
        if str(r.get("type", "")).upper() in OBSERVATION_TYPES and (
                "traceId" in r or ("trace_id" in r and ("start_time" in r or "startTime" in r))) \
                and "span_attributes" not in r and "run_type" not in r:
            return True
    return False


def _obs(o: dict[str, Any]) -> dict[str, Any]:
    usage = pick(o, "usageDetails", "usage_details") or {}
    legacy = o.get("usage") if isinstance(o.get("usage"), dict) else {}
    costs = pick(o, "costDetails", "cost_details") or {}
    return {
        "id": o.get("id"),
        "trace_id": pick(o, "traceId", "trace_id"),
        "parent": pick(o, "parentObservationId", "parent_observation_id"),
        "type": str(o.get("type") or "SPAN").upper(),
        "name": o.get("name") or "",
        "start": pick(o, "startTime", "start_time"),
        "end": pick(o, "endTime", "end_time"),
        "input": maybe_json(o.get("input")),
        "output": maybe_json(o.get("output")),
        "model": o.get("model"),
        "input_tokens": pick(usage, "input", "prompt_tokens", "input_tokens")
        or pick(legacy, "input") or pick(o, "promptTokens", "prompt_tokens"),
        "output_tokens": pick(usage, "output", "completion_tokens", "output_tokens")
        or pick(legacy, "output") or pick(o, "completionTokens", "completion_tokens"),
        "cost": pick(o, "calculatedTotalCost", "calculated_total_cost", "totalCost", "total_cost")
        or pick(costs, "total"),
        "level": str(o.get("level") or "DEFAULT").upper(),
        "status_message": pick(o, "statusMessage", "status_message"),
        "metadata": o.get("metadata") if isinstance(o.get("metadata"), dict) else {},
    }


def _run_from(trace: dict[str, Any], observations: list[dict[str, Any]]) -> Run:
    obs = sorted((_obs(o) for o in observations if isinstance(o, dict)),
                 key=lambda o: (to_seconds(o["start"]) or 0.0, str(o["id"])))
    meta = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
    trace_id = trace.get("id") or (obs[0]["trace_id"] if obs else None)
    run = Run(metadata={"source": "langfuse"})
    if trace_id:
        run.run_id = str(trace_id)
    for key, src in (("trace_name", "name"), ("session_id", "sessionId"), ("user_id", "userId"),
                     ("tags", "tags"), ("environment", "environment"), ("release", "release")):
        v = pick(trace, src, src.replace("Id", "_id"))
        if v:
            run.metadata[key] = v
    run.case_id = case_id_from(meta, *(o["metadata"] for o in obs))

    tools = [o for o in obs if o["type"] == "TOOL"]
    requested = {tc.name for o in obs if o["type"] == "GENERATION" for tc in tool_calls_from_output(o["output"])}
    if not tools:
        tools = [o for o in obs if o["type"] == "SPAN" and o["name"] in requested]
    tool_ids = {o["id"] for o in tools}

    current_agent: str | None = None
    for o in obs:
        dur = duration_ms(o["start"], o["end"])
        t = o["type"]
        if o["id"] in tool_ids:
            error = o["level"] == "ERROR"
            run.steps.append(ToolCall(
                name=o["name"] or "?", args=as_args(o["input"]), result=o["output"],
                status="error" if error else "ok", error=o["status_message"] if error else None,
                duration_ms=dur, agent=current_agent,
            ))
        elif t == "GENERATION":
            run.steps.append(LLMCall(model=o["model"], input_tokens=o["input_tokens"],
                                     output_tokens=o["output_tokens"], cost_usd=o["cost"], duration_ms=dur,
                                     agent=current_agent))
            if not tool_ids:
                for tc in tool_calls_from_output(o["output"]):
                    tc.agent = current_agent
                    run.steps.append(tc)
        elif t == "RETRIEVER":
            run.steps.append(Retrieval(query=query_text(o["input"]), doc_ids=doc_ids(o["output"]),
                                       duration_ms=dur, agent=current_agent))
        elif t == "AGENT":
            if current_agent is None:
                current_agent = o["name"] or None
                run.agent = run.agent or current_agent
            elif o["name"] and o["name"] != current_agent:
                run.steps.append(Handoff(to_agent=o["name"], from_agent=current_agent))
                current_agent = o["name"]
        if o["parent"] is None and o["level"] == "ERROR":
            run.status = "error"
            run.error = o["status_message"]

    roots = [o for o in obs if o["parent"] is None]
    inp = trace.get("input")
    out = trace.get("output")
    if inp is None and roots:
        inp = roots[0]["input"]
    if out is None and roots:
        out = roots[-1]["output"]
    run.input = first_user_text(unwrap(inp, "input", "query", "question", "message"))
    run.output = unwrap(out, "output", "answer", "content", "response")

    latency = trace.get("latency")  # seconds in the Langfuse API
    if isinstance(latency, (int, float)):
        run.latency_ms = latency * 1000
    elif obs:
        starts = [s for s in (to_seconds(o["start"]) for o in obs) if s is not None]
        ends = [e for e in (to_seconds(o["end"]) for o in obs) if e is not None]
        if starts and ends:
            run.latency_ms = round((max(ends) - min(starts)) * 1000, 3)
    return run


def langfuse_to_runs(data: Any) -> list[Run]:
    """Convert already-parsed Langfuse JSON (traces with observations, or flat observations) to Runs."""
    records = as_list(data, "data", "traces", "observations")
    traces: dict[str, dict[str, Any]] = {}
    observations: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("observations"), list):  # a trace
            tid = str(r.get("id"))
            traces[tid] = r
            if tid not in order:
                order.append(tid)
            observations.setdefault(tid, []).extend(o for o in r["observations"] if isinstance(o, dict))
        else:  # a bare observation
            tid = str(pick(r, "traceId", "trace_id", default="trace"))
            if tid not in order:
                order.append(tid)
            observations.setdefault(tid, []).append(r)
    return [_run_from(traces.get(tid, {"id": tid}), observations.get(tid, [])) for tid in order]


def load_langfuse(path: str | Path) -> list[Run]:
    """Load a Langfuse export (JSON or JSONL) as Evalplane runs, one per trace."""
    return langfuse_to_runs(read_records(path))


def fetch_langfuse(limit: int = 50, *, name: str | None = None, session_id: str | None = None,
                   user_id: str | None = None, tags: list[str] | None = None, **client_kwargs: Any) -> list[Run]:
    """Fetch recent traces with the Langfuse SDK (`pip install langfuse`) and convert them.

    `client_kwargs` go to `langfuse.Langfuse(...)` (public_key, secret_key, host/base_url); by default the
    SDK reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST from the environment.
    """
    lf = require("langfuse", "langfuse", "fetch_langfuse()")
    client = lf.Langfuse(**client_kwargs)
    filters = {k: v for k, v in {"name": name, "session_id": session_id, "user_id": user_id,
                                 "tags": tags}.items() if v is not None}
    listing = client.api.trace.list(limit=limit, **filters)
    full = [client.api.trace.get(t.id).dict() for t in listing.data]  # camelCase, with observations
    return langfuse_to_runs(full)
