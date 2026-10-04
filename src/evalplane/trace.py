"""Normalised trace model (Run -> steps) plus JSONL and OpenTelemetry/OpenInference loaders.

Every eval runs over the same `Run` object, whatever framework produced it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"


class _Step(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent: str | None = None
    duration_ms: float | None = None


class LLMCall(_Step):
    type: Literal["llm"] = "llm"
    model: str | None = None
    input: Any = None
    output: Any = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class ToolCall(_Step):
    type: Literal["tool"] = "tool"
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    status: Literal["ok", "error", "denied"] = "ok"
    error: str | None = None


class Retrieval(_Step):
    type: Literal["retrieval"] = "retrieval"
    query: str | None = None
    doc_ids: list[str] = Field(default_factory=list)


class Handoff(_Step):
    type: Literal["handoff"] = "handoff"
    to_agent: str
    from_agent: str | None = None


class Approval(_Step):
    type: Literal["approval"] = "approval"
    tool: str
    decision: Literal["approved", "rejected"] = "approved"
    approver: str | None = None


class UserTurn(_Step):
    """Something the user said mid-run (multi-turn conversations), e.g. a confirmation."""

    type: Literal["user"] = "user"
    text: str = ""


Step = Annotated[LLMCall | ToolCall | Retrieval | Handoff | Approval | UserTurn, Field(discriminator="type")]


class Run(BaseModel):
    """One execution of an agent on one input."""

    model_config = ConfigDict(extra="allow")
    schema_version: str = SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    case_id: str | None = None
    attempt: int = 0
    agent: str | None = None
    input: Any = None
    output: Any = None
    final_state: dict[str, Any] | None = None
    status: Literal["ok", "error", "timeout"] = "ok"
    error: str | None = None
    latency_ms: float | None = None
    steps: list[Step] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def tool_calls(self) -> list[ToolCall]:
        return [s for s in self.steps if isinstance(s, ToolCall)]

    def tool_names(self) -> list[str]:
        """Tool names in order, with handoffs as `handoff:<agent>` so trajectories can include routing."""
        out = []
        for s in self.steps:
            if isinstance(s, ToolCall):
                out.append(s.name)
            elif isinstance(s, Handoff):
                out.append(f"handoff:{s.to_agent}")
        return out

    def retrieved_ids(self) -> list[str]:
        return [d for s in self.steps if isinstance(s, Retrieval) for d in s.doc_ids]

    @property
    def cost_usd(self) -> float:
        return sum((s.cost_usd or 0.0) for s in self.steps if isinstance(s, LLMCall))

    @property
    def output_text(self) -> str:
        if self.output is None:
            return ""
        return self.output if isinstance(self.output, str) else json.dumps(self.output, default=str)


# --------------------------------------------------------------------------- JSONL


def write_runs(path: str | Path, runs: Iterable[Run]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in runs:
            f.write(r.model_dump_json(exclude_none=True) + "\n")


def read_runs(path: str | Path) -> Iterator[Run]:
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict) and ("resourceSpans" in data or "spans" in data):
            yield from load_otel_data(data)
            return
        items = data if isinstance(data, list) else [data]
        if items and isinstance(items[0], dict) and "span_id" in items[0]:
            yield from load_otel_data(items)
            return
        for item in items:
            yield Run.model_validate(item)
        return
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            yield Run.model_validate_json(line)
        except Exception as e:
            raise ValueError(f"{path}:{n}: not a valid run: {e}") from e


def load_traces(paths: Iterable[str | Path]) -> list[Run]:
    """Load runs from files or directories. The format is detected per file: native JSONL, OTel/OpenInference,
    or Langfuse / LangSmith / Braintrust exports."""
    from .integrations import load_any

    runs: list[Run] = []
    for p in paths:
        p = Path(p)
        files = sorted(p.rglob("*.jsonl")) + sorted(p.rglob("*.json")) if p.is_dir() else [p]
        for f in files:
            runs.extend(load_any(f))
    return runs


# --------------------------------------------------------------------------- OpenTelemetry / OpenInference


def _attr_value(v: Any) -> Any:
    if isinstance(v, dict):
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in v:
                return int(v[key]) if key == "intValue" else v[key]
        if "arrayValue" in v:
            return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    return v


def _to_ns(v: Any) -> int:
    """Span times as integer nanoseconds: accepts ns integers, numeric strings or ISO-8601 strings."""
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v)
    if s.isdigit():
        return int(s)
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1e9)
    except ValueError:
        return 0


def _flatten_spans(data: Any) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    if isinstance(data, dict) and "resourceSpans" in data:
        for rs in data["resourceSpans"]:
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    attrs = {a["key"]: _attr_value(a["value"]) for a in sp.get("attributes", [])}
                    spans.append({
                        "trace_id": sp.get("traceId"), "span_id": sp.get("spanId"),
                        "parent_id": sp.get("parentSpanId") or None, "name": sp.get("name"),
                        "start": int(sp.get("startTimeUnixNano", 0)), "end": int(sp.get("endTimeUnixNano", 0)),
                        "error": (sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR"),
                        "attributes": attrs,
                    })
        return spans
    items = data["spans"] if isinstance(data, dict) else data
    for sp in items:
        spans.append({
            "trace_id": sp.get("trace_id"), "span_id": sp.get("span_id"),
            "parent_id": sp.get("parent_id") or sp.get("parent_span_id"), "name": sp.get("name"),
            "start": _to_ns(sp.get("start_time")), "end": _to_ns(sp.get("end_time")),
            "error": str(sp.get("status", "")).upper() in ("ERROR", "STATUS_CODE_ERROR"),
            "attributes": sp.get("attributes") or {},
        })
    return spans


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def load_otel_data(data: Any) -> list[Run]:
    """Convert OTel GenAI semantic-convention or OpenInference spans into Runs (one per trace)."""
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for sp in _flatten_spans(data):
        by_trace.setdefault(sp["trace_id"] or "trace", []).append(sp)
    runs = []
    for trace_id, spans in by_trace.items():
        spans.sort(key=lambda s: (s["start"], s["span_id"] or ""))
        run = Run(run_id=str(trace_id), metadata={"source": "otel"})
        for sp in spans:
            a = sp["attributes"]
            op = a.get("gen_ai.operation.name")
            kind = str(a.get("openinference.span.kind", "")).upper()
            dur = (sp["end"] - sp["start"]) / 1e6 if sp["end"] and sp["start"] else None
            if a.get("evalplane.case_id") and not run.case_id:
                run.case_id = a["evalplane.case_id"]
            if op == "execute_tool" or kind == "TOOL":
                args = _maybe_json(a.get("gen_ai.tool.call.arguments") or a.get("tool.parameters") or a.get("input.value") or {})
                run.steps.append(ToolCall(
                    name=a.get("gen_ai.tool.name") or a.get("tool.name") or sp["name"],
                    args=args if isinstance(args, dict) else {"input": args},
                    result=_maybe_json(a.get("gen_ai.tool.call.result") or a.get("output.value")),
                    status="error" if sp["error"] else "ok", duration_ms=dur,
                ))
            elif op in ("chat", "text_completion", "generate_content") or kind == "LLM":
                run.steps.append(LLMCall(
                    model=a.get("gen_ai.request.model") or a.get("gen_ai.response.model") or a.get("llm.model_name"),
                    input_tokens=a.get("gen_ai.usage.input_tokens") or a.get("llm.token_count.prompt"),
                    output_tokens=a.get("gen_ai.usage.output_tokens") or a.get("llm.token_count.completion"),
                    cost_usd=a.get("llm.cost.total") or a.get("gen_ai.usage.cost"), duration_ms=dur,
                ))
            elif kind == "RETRIEVER":
                ids = [v for k, v in a.items() if k.startswith("retrieval.documents.") and k.endswith(".document.id")]
                run.steps.append(Retrieval(query=a.get("input.value"), doc_ids=[str(i) for i in ids]))
            elif op == "invoke_agent" or kind == "AGENT":
                name = a.get("gen_ai.agent.name") or sp["name"]
                if sp["parent_id"] is None:
                    run.agent = name
                    run.input = _maybe_json(a.get("input.value"))
                    run.output = _maybe_json(a.get("output.value"))
                    if dur is not None:
                        run.latency_ms = dur
                else:
                    run.steps.append(Handoff(to_agent=name))
        runs.append(run)
    return runs
