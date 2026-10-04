"""LangSmith runs -> Evalplane runs (one per root trace).

Input: LangSmith run records as returned by the API / SDK (`Client.list_runs(...)`, `run.dict()` /
`run.model_dump()`), as JSON (a list, `{"runs": [...]}`, or root runs with nested `child_runs`) or JSONL.
Runs are grouped by `trace_id` (falling back to walking `parent_run_id`) and ordered by `dotted_order`
(or `start_time`).

Mapping by `run_type`: `tool` -> `tool` step (name, `inputs` -> args, `outputs.output` -> result, `error` ->
status error); `llm` -> `llm` step (model from `extra.metadata.ls_model_name` / invocation params,
`prompt_tokens` / `completion_tokens` / `total_cost`); `retriever` -> `retrieval` (`inputs.query`,
document ids from `outputs.documents[].metadata.id|source`). The root run gives the run's name (agent),
input, output, status and latency; `chain`, `prompt`, `parser` and `embedding` runs are not steps. If a trace
has no tool runs, the tool calls requested in LLM outputs are used instead (without results).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ..trace import LLMCall, Retrieval, Run, ToolCall
from ._common import (
    as_args,
    as_list,
    case_id_from,
    doc_ids,
    duration_ms,
    maybe_json,
    pick,
    query_text,
    read_records,
    require,
    to_seconds,
    tool_calls_from_output,
    unwrap,
)

RUN_TYPES = {"tool", "llm", "chain", "retriever", "prompt", "parser", "embedding"}


def is_langsmith(records: list[Any]) -> bool:
    for r in records[:5]:
        if not isinstance(r, dict):
            return False
        if r.get("run_type") in RUN_TYPES and ("trace_id" in r or "parent_run_id" in r or "dotted_order" in r
                                               or "inputs" in r):
            return True
    return False


def _flatten(records: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    stack = [r for r in records if isinstance(r, dict)]
    while stack:
        r = stack.pop(0)
        out.append(r)
        for child in r.get("child_runs") or []:
            if isinstance(child, dict):
                child = {**child}
                child["parent_run_id"] = child.get("parent_run_id") or r.get("id")
                child["trace_id"] = child.get("trace_id") or r.get("trace_id")
                stack.append(child)
    return out


def _message_text(m: Any) -> Any:
    """Text of a serialised LangChain message (`{"lc": 1, "kwargs": {...}}` or `{"type": "ai", "content"}`)."""
    if isinstance(m, dict):
        body = m.get("kwargs") if isinstance(m.get("kwargs"), dict) else m
        c = body.get("content")
        if isinstance(c, list):
            return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        return c
    if isinstance(m, (list, tuple)) and len(m) == 2 and isinstance(m[1], str):  # ("user", "hi") tuples
        return m[1]
    return m


def _msg_role(m: Any) -> str:
    if isinstance(m, dict):
        ident = m.get("id")
        if isinstance(ident, list) and ident:
            return str(ident[-1]).lower()
        return str(m.get("type") or m.get("role") or "").lower()
    if isinstance(m, (list, tuple)) and m:
        return str(m[0]).lower()
    return ""


def _io(v: Any, *, first_human: bool) -> Any:
    v = maybe_json(v)
    if isinstance(v, dict) and isinstance(v.get("messages"), list) and v["messages"]:
        msgs = v["messages"]
        if msgs and isinstance(msgs[0], list):  # llm-run style [[...]]
            msgs = msgs[0]
        if first_human:
            for m in msgs:
                if _msg_role(m) in ("human", "humanmessage", "user"):
                    return _message_text(m)
            return _message_text(msgs[0])
        for m in reversed(msgs):
            if _msg_role(m) in ("ai", "aimessage", "assistant") and _message_text(m):
                return _message_text(m)
        return _message_text(msgs[-1])
    return unwrap(v, "input", "query", "question", "output", "answer", "result")


def _tool_result(outputs: Any) -> tuple[Any, bool]:
    out = unwrap(outputs, "output")
    if isinstance(out, dict) and ("kwargs" in out or out.get("type") == "tool"):  # serialised ToolMessage
        body = out.get("kwargs") if isinstance(out.get("kwargs"), dict) else out
        return maybe_json(body.get("content")), body.get("status") == "error"
    return out, False


def _tool_args(inputs: Any) -> dict[str, Any]:
    inputs = maybe_json(inputs)
    if isinstance(inputs, dict) and set(inputs) == {"input"}:
        raw = inputs["input"]  # LangChain may log the input string, sometimes str(dict): "{'id': 1}"
        if isinstance(raw, str) and raw.strip().startswith("{") and not isinstance(maybe_json(raw), dict):
            try:
                parsed = ast.literal_eval(raw.strip())
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, SyntaxError):
                pass
        return as_args(raw)
    return as_args(inputs)


def _llm_step(r: dict[str, Any]) -> LLMCall:
    extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
    meta = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
    params = extra.get("invocation_params") if isinstance(extra.get("invocation_params"), dict) else {}
    outputs = r.get("outputs") if isinstance(r.get("outputs"), dict) else {}
    llm_out = outputs.get("llm_output") if isinstance(outputs.get("llm_output"), dict) else {}
    usage = llm_out.get("token_usage") or llm_out.get("usage") or outputs.get("usage_metadata") or {}
    cost = r.get("total_cost")
    return LLMCall(
        model=pick(meta, "ls_model_name") or pick(params, "model", "model_name") or llm_out.get("model_name"),
        input_tokens=pick(r, "prompt_tokens") or pick(usage, "prompt_tokens", "input_tokens"),
        output_tokens=pick(r, "completion_tokens") or pick(usage, "completion_tokens", "output_tokens"),
        cost_usd=float(cost) if cost is not None else None,
        duration_ms=duration_ms(r.get("start_time"), r.get("end_time")),
    )


def _trace_id(r: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    if r.get("trace_id"):
        return str(r["trace_id"])
    seen = set()
    while r.get("parent_run_id") and str(r["parent_run_id"]) in by_id and r["id"] not in seen:
        seen.add(r["id"])
        r = by_id[str(r["parent_run_id"])]
    return str(r.get("id"))


def langsmith_to_runs(data: Any) -> list[Run]:
    """Convert already-parsed LangSmith run records to Evalplane Runs, one per trace."""
    records = _flatten(as_list(data, "runs", "data"))
    by_id = {str(r.get("id")): r for r in records}
    traces: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        traces.setdefault(_trace_id(r, by_id), []).append(r)

    runs = []
    for tid, items in traces.items():
        items.sort(key=lambda r: (str(r.get("dotted_order") or ""), to_seconds(r.get("start_time")) or 0.0))
        root = next((r for r in items if not r.get("parent_run_id")), None) or \
            next((r for r in items if str(r.get("id")) == tid), items[0])
        extra = root.get("extra") if isinstance(root.get("extra"), dict) else {}
        run = Run(run_id=tid, agent=root.get("name"), metadata={"source": "langsmith"})
        run.case_id = case_id_from(extra.get("metadata"), *(
            (r.get("extra") or {}).get("metadata") for r in items if isinstance(r.get("extra"), dict)))
        if root.get("session_id"):
            run.metadata["project_id"] = str(root["session_id"])
        if root.get("tags"):
            run.metadata["tags"] = root["tags"]
        run.input = _io(root.get("inputs"), first_human=True)
        run.output = _io(root.get("outputs"), first_human=False)
        if root.get("error"):
            run.status = "error"
            run.error = str(root["error"])
        run.latency_ms = duration_ms(root.get("start_time"), root.get("end_time"))

        has_tools = any(r.get("run_type") == "tool" for r in items)
        # a trace whose root *is* the tool / retriever / llm (a directly traced call, no wrapping chain)
        # still has that one call as its step
        root_is_step = root.get("run_type") in ("tool", "retriever", "llm")
        for r in items:
            if r is root and not root_is_step:
                continue
            kind = r.get("run_type")
            dur = duration_ms(r.get("start_time"), r.get("end_time"))
            if kind == "tool":
                result, errored = _tool_result(r.get("outputs"))
                err = r.get("error")
                run.steps.append(ToolCall(
                    name=r.get("name") or "?", args=_tool_args(r.get("inputs")), result=result,
                    status="error" if (err or errored) else "ok", error=str(err) if err else None,
                    duration_ms=dur,
                ))
            elif kind == "llm":
                run.steps.append(_llm_step(r))
                if not has_tools:
                    run.steps.extend(tool_calls_from_output(r.get("outputs")))
            elif kind == "retriever":
                run.steps.append(Retrieval(query=query_text(r.get("inputs")), doc_ids=doc_ids(r.get("outputs")),
                                           duration_ms=dur))
        runs.append(run)
    return runs


def load_langsmith(path: str | Path) -> list[Run]:
    """Load exported LangSmith runs (JSON or JSONL) as Evalplane runs, one per root trace."""
    return langsmith_to_runs(read_records(path))


def fetch_langsmith(project: str, limit: int = 50, **client_kwargs: Any) -> list[Run]:
    """Fetch the latest `limit` root traces of a LangSmith project (`pip install langsmith`) and convert them.

    `client_kwargs` go to `langsmith.Client(...)` (api_key, api_url); by default the SDK reads
    LANGSMITH_API_KEY / LANGSMITH_ENDPOINT from the environment.
    """
    ls = require("langsmith", "langsmith", "fetch_langsmith()")
    client = ls.Client(**client_kwargs)

    def query(**kw: Any):
        # langsmith >= 0.13 deprecates list_runs in favour of client.runs.query()
        runs_api = getattr(client, "runs", None)
        if runs_api is not None and hasattr(runs_api, "query"):
            try:
                return runs_api.query(**kw)
            except TypeError:
                pass
        return client.list_runs(**kw)

    records: list[dict[str, Any]] = []
    for root in query(project_name=project, is_root=True, limit=limit):
        for r in query(project_name=project, trace_id=getattr(root, "trace_id", None) or root.id):
            records.append(r.model_dump() if hasattr(r, "model_dump") else dict(r))
    return langsmith_to_runs(records)
