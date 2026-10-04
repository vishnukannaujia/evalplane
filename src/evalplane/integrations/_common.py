"""Helpers shared by the third-party importers (no third-party imports here)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..trace import ToolCall


def require(module: str, pip_name: str, what: str) -> Any:
    """Import an optional dependency lazily, with an install hint if it is missing."""
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise ImportError(f"{what} needs the `{pip_name}` package: pip install {pip_name}") from e


def read_records(path: str | Path) -> Any:
    """Parse a .json file (any JSON value) or a JSONL file (list of the objects on each line)."""
    text = Path(path).read_text()
    stripped = text.lstrip()
    if not stripped:
        return []
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError as e:
            raise ValueError(f"{path}:{n}: not valid JSON: {e}") from e
    return out


def as_list(data: Any, *keys: str) -> list[Any]:
    """Unwrap `{"data": [...]}`-style API envelopes; wrap a single object in a list.

    A dict with an `id` is a record itself (e.g. one Langfuse trace with its `observations`), not an envelope.
    """
    if isinstance(data, dict):
        for k in keys if "id" not in data else ():
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return list(data or [])


def pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First present, non-None value among `keys` (handles camelCase / snake_case exports)."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in ("{", "[", '"'):
            try:
                return json.loads(s)
            except ValueError:
                return v
    return v


def as_args(v: Any) -> dict[str, Any]:
    v = maybe_json(v)
    if isinstance(v, dict):
        return v
    if v is None or v == "":
        return {}
    return {"input": v}


def to_seconds(v: Any) -> float | None:
    """Timestamps as epoch seconds: accepts ISO-8601 strings, epoch seconds/ms/ns numbers, datetimes."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.timestamp()
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str) and v.replace(".", "", 1).isdigit():
        x = float(v)
    else:
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if x > 1e17:  # ns
        return x / 1e9
    if x > 1e14:  # us
        return x / 1e6
    if x > 1e11:  # ms
        return x / 1e3
    return x


def duration_ms(start: Any, end: Any) -> float | None:
    s, e = to_seconds(start), to_seconds(end)
    if s is None or e is None:
        return None
    return round((e - s) * 1000, 3)


def tool_calls_from_output(output: Any) -> list[ToolCall]:
    """Tool calls an LLM *requested*, from a generation's output in any common provider shape.

    Handles OpenAI chat messages / choices (`tool_calls[].function`), LangChain AIMessage dicts
    (`tool_calls[] = {name, args}`, or `additional_kwargs.tool_calls`), Anthropic `tool_use` content blocks
    and OpenAI Responses `function_call` items. Results are unknown here (they are not in the output).
    """
    out: list[ToolCall] = []
    output = maybe_json(output)

    def visit(o: Any) -> None:
        if isinstance(o, list):
            for x in o:
                visit(x)
            return
        if not isinstance(o, dict):
            return
        t = o.get("type")
        if t == "tool_use" and o.get("name"):
            out.append(ToolCall(name=o["name"], args=as_args(o.get("input"))))
            return
        if t == "function_call" and o.get("name"):
            out.append(ToolCall(name=o["name"], args=as_args(o.get("arguments"))))
            return
        calls = o.get("tool_calls")
        if not calls and isinstance(o.get("additional_kwargs"), dict):
            calls = o["additional_kwargs"].get("tool_calls")
        if isinstance(calls, list) and calls:
            for c in calls:
                if not isinstance(c, dict):
                    continue
                fn = c.get("function") if isinstance(c.get("function"), dict) else None
                if fn and fn.get("name"):
                    out.append(ToolCall(name=fn["name"], args=as_args(fn.get("arguments"))))
                elif c.get("name"):
                    out.append(ToolCall(name=c["name"], args=as_args(c.get("args", c.get("arguments")))))
            return
        for key in ("choices", "message", "content", "output", "generations", "messages", "kwargs"):
            if key in o and isinstance(o[key], (list, dict)):
                visit(o[key])

    visit(output)
    return out


def doc_ids(output: Any) -> list[str]:
    """Document ids from a retriever's output (LangChain Documents, lists of dicts or ids)."""
    output = maybe_json(output)
    if isinstance(output, dict):
        output = pick(output, "documents", "docs", "results", "output", default=[])
    ids: list[str] = []
    for d in output if isinstance(output, list) else []:
        if isinstance(d, (str, int)):
            ids.append(str(d))
        elif isinstance(d, dict):
            meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
            v = pick(d, "id", "doc_id", "document_id") or pick(meta, "id", "doc_id", "document_id", "source")
            if v is not None:
                ids.append(str(v))
    return ids


def query_text(inp: Any) -> str | None:
    inp = maybe_json(inp)
    if isinstance(inp, dict):
        v = pick(inp, "query", "question", "input")
        return v if isinstance(v, str) else (json.dumps(v, default=str) if v is not None else None)
    return inp if isinstance(inp, str) else None


def unwrap(v: Any, *keys: str) -> Any:
    """`{"input": "text"}` -> "text" when the dict holds exactly one of `keys` and nothing else."""
    v = maybe_json(v)
    if isinstance(v, dict) and v.get("role") and isinstance(v.get("content"), str):
        return v["content"]  # a single chat message
    if isinstance(v, dict) and len(v) == 1:
        (k, inner), = v.items()
        if k in keys:
            return inner
    return v


def first_user_text(v: Any) -> Any:
    """For a chat-message list input, the first user message's text; anything else unchanged."""
    v = maybe_json(v)
    if isinstance(v, list) and v and all(isinstance(m, dict) and "role" in m for m in v):
        for m in v:
            if m.get("role") in ("user", "human") and isinstance(m.get("content"), str):
                return m["content"]
    return v


def case_id_from(*metas: Any) -> str | None:
    for m in metas:
        if isinstance(m, dict):
            v = pick(m, "evalplane.case_id", "evalplane_case_id", "case_id")
            if v is not None:
                return str(v)
    return None
