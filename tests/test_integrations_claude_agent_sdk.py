"""Claude Agent SDK hooks. The callback logic is tested without the SDK; the SDK-shaped parts (HookMatcher,
ClaudeAgentOptions, the Query control-request path) are skipped if `claude-agent-sdk` isn't installed."""

from __future__ import annotations

import asyncio
import json

import pytest

import evalplane as ep
from evalplane.integrations.claude_agent_sdk import hook_callbacks, merge_hooks
from evalplane.trace import Run


def _pre(name, tool_input, tid, **kw):
    return {"session_id": "s1", "transcript_path": "/tmp/t.jsonl", "cwd": "/repo", "hook_event_name": "PreToolUse",
            "tool_name": name, "tool_input": tool_input, "tool_use_id": tid, **kw}


def _post(name, tool_input, tid, response):
    return {**_pre(name, tool_input, tid), "hook_event_name": "PostToolUse", "tool_response": response}


def _fail(name, tool_input, tid, error, **kw):
    return {**_pre(name, tool_input, tid), "hook_event_name": "PostToolUseFailure", "error": error, **kw}


def test_callbacks_record_into_current_run():
    cb = hook_callbacks()

    async def session():
        with ep.record("fix the test") as run:
            assert await cb["PreToolUse"](_pre("Read", {"file_path": "a.py"}, "toolu_1"), "toolu_1",
                                          {"signal": None}) == {}
            await cb["PreToolUse"](_pre("Bash", {"command": "pytest"}, "toolu_2", agent_id="a1",
                                        agent_type="test-runner"), "toolu_2", {"signal": None})
            await cb["PostToolUse"](_post("Read", {"file_path": "a.py"}, "toolu_1", {"content": "x = 1"}),
                                    "toolu_1", {"signal": None})
            await cb["PostToolUseFailure"](_fail("Bash", {"command": "pytest"}, "toolu_2", "exit code 1"),
                                           "toolu_2", {"signal": None})
            await cb["PostToolUseFailure"](_fail("Bash", {"command": "sleep 99"}, "toolu_3", "cancelled",
                                                 is_interrupt=True), "toolu_3", {"signal": None})
        return run

    run = asyncio.run(session())
    assert run.tool_names() == ["Read", "Bash", "Bash"]
    read, bash, sleep = run.tool_calls()
    assert read.args == {"file_path": "a.py"} and read.result == {"content": "x = 1"} and read.status == "ok"
    assert bash.agent == "test-runner" and bash.status == "error" and bash.error == "exit code 1"
    assert sleep.status == "error" and sleep.error.endswith("(interrupted)")  # no PreToolUse seen: still recorded


def test_no_run_no_recording_and_explicit_run():
    cb = hook_callbacks()
    asyncio.run(cb["PreToolUse"](_pre("Read", {}, "t1"), "t1", {"signal": None}))  # outside a run: ignored

    target = Run()
    cb = hook_callbacks(target)
    asyncio.run(cb["PreToolUse"](_pre("Write", {"file_path": "b.py", "content": "y"}, "t2"), "t2", None))
    asyncio.run(cb["PostToolUse"](_post("Write", {}, "t2", {"ok": True}), "t2", None))
    assert [(s.name, s.args, s.result) for s in target.tool_calls()] == [
        ("Write", {"file_path": "b.py", "content": "y"}, {"ok": True})]


def test_merge_hooks():
    assert merge_hooks({"PreToolUse": [1]}, None, {"PreToolUse": [2], "Stop": [3]}) == {
        "PreToolUse": [1, 2], "Stop": [3]}


def test_sdk_shapes_and_control_request_path():
    sdk = pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    from evalplane.integrations.claude_agent_sdk import evalplane_hooks

    hooks = evalplane_hooks(matcher="Bash|Write")
    assert set(hooks) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
    assert all(isinstance(m, HookMatcher) and m.matcher == "Bash|Write" for ms in hooks.values() for m in ms)
    ClaudeAgentOptions(hooks=hooks)  # accepted by the real options type

    # Drive the callbacks through the SDK's own hook_callback control-request handler (no CLI, no network).
    from claude_agent_sdk._internal.query import Query
    from claude_agent_sdk.types import _hooks_to_internal_format

    class Transport:
        def __init__(self):
            self.sent = []

        async def write(self, data):
            self.sent.append(json.loads(data))

    internal = _hooks_to_internal_format(hooks)
    transport = Transport()
    q = Query(transport=transport, is_streaming_mode=True, hooks=internal)
    ids = {}
    for event, matchers in internal.items():
        for m in matchers:
            for fn in m["hooks"]:
                ids[event] = f"hook_{len(q.hook_callbacks)}"
                q.hook_callbacks[ids[event]] = fn

    async def session():
        with ep.record("run the tests") as run:
            for n, (event, payload) in enumerate([
                ("PreToolUse", _pre("Bash", {"command": "pytest -q"}, "toolu_9")),
                ("PostToolUse", _post("Bash", {"command": "pytest -q"}, "toolu_9", {"stdout": "3 passed"})),
            ]):
                await q._handle_control_request({"type": "control_request", "request_id": f"r{n}", "request": {
                    "subtype": "hook_callback", "callback_id": ids[event], "input": payload,
                    "tool_use_id": "toolu_9"}})
        return run

    run = asyncio.run(session())
    assert [r["response"]["subtype"] for r in transport.sent] == ["success", "success"]
    (bash,) = run.tool_calls()
    assert (bash.name, bash.args, bash.result) == ("Bash", {"command": "pytest -q"}, {"stdout": "3 passed"})
    assert sdk.__version__
