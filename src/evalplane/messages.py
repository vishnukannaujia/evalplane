"""Build a Run from a chat message history, so any framework works without decorating tools.

Supported shapes (mixed lists are fine):
- OpenAI Chat Completions: {"role": "assistant", "tool_calls": [{"id", "function": {"name", "arguments"}}]}
  and {"role": "tool", "tool_call_id", "content"}
- OpenAI Responses / Agents SDK items: {"type": "function_call", "name", "arguments", "call_id"}
  and {"type": "function_call_output", "call_id", "output"}
- Anthropic Messages: content blocks {"type": "tool_use", "id", "name", "input"} and
  {"type": "tool_result", "tool_use_id", "content", "is_error"}
- LangChain / LangGraph message objects (AIMessage.tool_calls, ToolMessage), e.g. `state["messages"]`

    run = ep.from_messages(result["messages"])
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .trace import LLMCall, Run, ToolCall, UserTurn


def _parse_args(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v) if v.strip() else {}
            return parsed if isinstance(parsed, dict) else {"input": parsed}
        except ValueError:
            return {"input": v}
    return {} if v is None else {"input": v}


def _as_dict(m: Any) -> dict[str, Any]:
    """Normalise LangChain message objects (and pydantic models) to dicts."""
    if isinstance(m, dict):
        return m
    cls = type(m).__name__
    d: dict[str, Any] = {}
    for attr in ("content", "tool_calls", "tool_call_id", "name", "status", "type", "role", "response_metadata",
                 "usage_metadata"):
        if hasattr(m, attr):
            d[attr] = getattr(m, attr)
    if cls in ("AIMessage", "AIMessageChunk"):
        d["role"] = "assistant"
        d["_lc_tool_calls"] = d.pop("tool_calls", None) or []
    elif cls in ("ToolMessage",):
        d["role"] = "tool"
    elif cls in ("HumanMessage",):
        d["role"] = "user"
    elif cls in ("SystemMessage",):
        d["role"] = "system"
    elif hasattr(m, "model_dump"):
        d = m.model_dump()
    return d


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "output_text"):
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return "" if content is None else str(content)


def from_messages(messages: Iterable[Any], *, input: Any = None, case_id: str | None = None,
                  output: Any = None) -> Run:
    """Convert a message history to a Run. The output defaults to the last assistant text."""
    run = Run(input=input, case_id=case_id, metadata={"source": "messages"})
    pending: dict[str, ToolCall] = {}
    last_text = ""
    first_user: Any = None
    for raw in messages:
        m = _as_dict(raw)
        role = m.get("role")
        mtype = m.get("type")

        if role == "user":
            said = _text(m.get("content"))
            if first_user is None:
                first_user = said
            elif said:  # later user turns (e.g. "yes, go ahead") are kept for confirmation checks
                run.steps.append(UserTurn(text=said))
            # Anthropic: tool results arrive inside user messages
        if role in ("user", "tool") or mtype == "function_call_output":
            if mtype == "function_call_output":
                tc = pending.get(m.get("call_id", ""))
                if tc:
                    tc.result = m.get("output")
                continue
            if role == "tool":
                tc = pending.get(m.get("tool_call_id", ""))
                if tc:
                    tc.result = _text(m.get("content")) or m.get("content")
                    if m.get("status") == "error":
                        tc.status = "error"
                continue
            if isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tc = pending.get(b.get("tool_use_id", ""))
                        if tc:
                            tc.result = _text(b.get("content")) or b.get("content")
                            if b.get("is_error"):
                                tc.status = "error"
            continue

        if mtype == "function_call":
            tc = ToolCall(name=m.get("name", "?"), args=_parse_args(m.get("arguments")))
            pending[m.get("call_id") or tc.id] = tc
            run.steps.append(tc)
            continue

        if role == "assistant" or mtype == "message":
            usage = m.get("usage_metadata") or {}
            run.steps.append(LLMCall(input_tokens=usage.get("input_tokens"),
                                     output_tokens=usage.get("output_tokens")))
            for call in m.get("_lc_tool_calls") or []:  # LangChain: {"name", "args", "id"}
                tc = ToolCall(name=call.get("name", "?"), args=_parse_args(call.get("args")))
                pending[call.get("id") or tc.id] = tc
                run.steps.append(tc)
            for call in m.get("tool_calls") or []:  # OpenAI chat
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                tc = ToolCall(name=fn.get("name", "?"), args=_parse_args(fn.get("arguments")))
                pending[call.get("id") or tc.id] = tc
                run.steps.append(tc)
            content = m.get("content")
            if isinstance(content, list):  # Anthropic content blocks
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tc = ToolCall(name=b.get("name", "?"), args=_parse_args(b.get("input")))
                        pending[b.get("id") or tc.id] = tc
                        run.steps.append(tc)
            text = _text(content)
            if text:
                last_text = text
    run.input = input if input is not None else first_user
    run.output = output if output is not None else last_text
    return run
