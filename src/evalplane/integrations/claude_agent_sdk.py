"""Record Claude Agent SDK tool calls (`pip install claude-agent-sdk`) into Evalplane via hooks.

    from claude_agent_sdk import ClaudeAgentOptions, query
    from evalplane.integrations.claude_agent_sdk import evalplane_hooks

    options = ClaudeAgentOptions(hooks=evalplane_hooks())
    with ep.record(prompt, case_id="fix-bug-1") as run:
        async for message in query(prompt=prompt, options=options):
            ...
        run.output = final_text

The SDK calls hooks as `async def hook(input_data, tool_use_id, context) -> dict`. Mapping:

- `PreToolUse` (tool_name, tool_input, tool_use_id) -> a `tool` step is added, in call order.
- `PostToolUse` (tool_response) -> the step's result.
- `PostToolUseFailure` (error, is_interrupt) -> status `error` with the error text.

Steps inside a Task sub-agent carry `agent` = the hook input's `agent_type` (or `agent_id`). The hooks only
observe: they return `{}`, so they never block or change a tool call. Merge them with your own hooks with
`merge_hooks(yours, evalplane_hooks())`.

The SDK runs hooks on its own reader task, which sees the Evalplane run that was active when the
`query()` / `ClaudeSDKClient` session started. Start the session inside `ep.record()`, or pass the run
explicitly: `evalplane_hooks(run)`.
"""

from __future__ import annotations

import threading
from typing import Any

from ..recorder import _current
from ..trace import Run, ToolCall
from ._common import as_args, require

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


class _Recorder:
    def __init__(self, run: Run | None) -> None:
        self.run = run
        self.lock = threading.Lock()
        self.calls: dict[str, tuple[Run, ToolCall]] = {}

    def _target(self) -> Run | None:
        return self.run if self.run is not None else _current.get()

    def _step(self, data: dict[str, Any], tool_use_id: str | None) -> ToolCall | None:
        key = tool_use_id or data.get("tool_use_id") or ""
        with self.lock:
            found = self.calls.get(key) if key else None
        if found is not None:
            return found[1]
        run = self._target()
        if run is None:
            return None
        step = ToolCall(name=data.get("tool_name") or "?", args=as_args(data.get("tool_input")),
                        agent=data.get("agent_type") or data.get("agent_id"))
        run.steps.append(step)
        if key:
            with self.lock:
                self.calls[key] = (run, step)
        return step

    async def pre_tool_use(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict:
        self._step(input_data, tool_use_id)
        return {}

    async def post_tool_use(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict:
        step = self._step(input_data, tool_use_id)
        if step is not None:
            step.result = input_data.get("tool_response")
        return {}

    async def post_tool_use_failure(self, input_data: dict[str, Any], tool_use_id: str | None,
                                    context: Any) -> dict:
        step = self._step(input_data, tool_use_id)
        if step is not None:
            step.status = "error"
            step.error = str(input_data.get("error") or "tool failed")
            if input_data.get("is_interrupt"):
                step.error += " (interrupted)"
        return {}


def hook_callbacks(run: Run | None = None) -> dict[str, Any]:
    """The raw callbacks, keyed by hook event: {"PreToolUse": fn, "PostToolUse": fn, "PostToolUseFailure": fn}.

    Each is `async (input_data, tool_use_id, context) -> {}`, the SDK's HookCallback signature. With no `run`,
    calls are recorded into the run active in `ep.record()` / `evalplane run`.
    """
    rec = _Recorder(run)
    return {"PreToolUse": rec.pre_tool_use, "PostToolUse": rec.post_tool_use,
            "PostToolUseFailure": rec.post_tool_use_failure}


def evalplane_hooks(run: Run | None = None, *, matcher: str | None = None) -> dict[str, list[Any]]:
    """Hooks for `ClaudeAgentOptions(hooks=...)`: {event: [HookMatcher(matcher, hooks=[callback])]}.

    `matcher` limits recording to some tools (e.g. "Bash|Write|mcp__billing__.*"); None records all of them.
    """
    sdk = require("claude_agent_sdk", "claude-agent-sdk", "evalplane_hooks()")
    return {event: [sdk.HookMatcher(matcher=matcher, hooks=[fn])] for event, fn in hook_callbacks(run).items()}


def merge_hooks(*hook_dicts: dict[str, list[Any]] | None) -> dict[str, list[Any]]:
    """Combine several `hooks=` dicts (matchers for the same event are concatenated)."""
    out: dict[str, list[Any]] = {}
    for d in hook_dicts:
        for event, matchers in (d or {}).items():
            out.setdefault(event, []).extend(matchers)
    return out
