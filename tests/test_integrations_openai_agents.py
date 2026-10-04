"""OpenAI Agents SDK trace processor. Uses the real SDK (skipped if `openai-agents` isn't installed) with a
scripted in-process Model, so no network and no LLM calls."""

from __future__ import annotations

import asyncio
import json

import pytest

import evalplane as ep
from evalplane.integrations.openai_agents import EvalplaneProcessor
from evalplane.trace import Handoff, LLMCall, ToolCall

agents = pytest.importorskip("agents")
from agents import tracing  # noqa: E402


@pytest.fixture
def processor():
    """Make our processor the only one (so nothing is exported to OpenAI), restore afterwards."""
    provider = tracing.get_trace_provider()
    before = provider._multi_processor._processors
    proc = EvalplaneProcessor()
    agents.set_trace_processors([proc])
    agents.set_tracing_disabled(False)
    yield proc
    provider._multi_processor._processors = before


def test_spans_built_directly(processor):
    with ep.record("refund A200") as run:
        with tracing.trace("support"):
            with tracing.agent_span(name="triage"):
                with tracing.generation_span(model="gpt-4o-mini", usage={"input_tokens": 50, "output_tokens": 7}):
                    pass
                with tracing.handoff_span(from_agent="triage") as h:
                    h.span_data.to_agent = "refunds"
            with tracing.agent_span(name="refunds"):
                with tracing.function_span("lookup_order", input='{"order_id": "A200"}') as f:
                    f.span_data.output = {"amount": 40}
                with tracing.function_span("issue_refund", input='{"order_id": "A200", "amount": 40}') as f:
                    f.set_error(tracing.SpanError(message="Error running tool",
                                                  data={"tool_name": "issue_refund", "error": "gateway down"}))
                with tracing.function_span("delete_account", input="{}") as f:
                    f.set_error(tracing.SpanError(
                        message="Tool execution rejected",
                        data={"tool_name": "delete_account",
                              "error": "Tool execution for call_9 was manually rejected by user."}))
    assert run.agent == "triage"
    assert run.tool_names() == ["handoff:refunds", "lookup_order", "issue_refund", "delete_account"]
    llm = [s for s in run.steps if isinstance(s, LLMCall)]
    assert llm[0].model == "gpt-4o-mini" and llm[0].input_tokens == 50 and llm[0].output_tokens == 7
    hand = [s for s in run.steps if isinstance(s, Handoff)][0]
    assert (hand.from_agent, hand.to_agent) == ("triage", "refunds")
    lookup, refund, delete = run.tool_calls()
    assert lookup.args == {"order_id": "A200"} and lookup.result == {"amount": 40} and lookup.status == "ok"
    assert refund.status == "error" and "gateway down" in refund.error
    assert delete.status == "denied"


def test_outside_a_run_nothing_is_recorded(processor):
    with tracing.trace("no-run"):
        with tracing.function_span("lookup_order", input="{}"):
            pass
    assert processor._open_tools == {}


def test_explicit_run(processor):
    from evalplane.trace import Run

    target = Run()
    proc = EvalplaneProcessor(target)
    agents.set_trace_processors([proc])
    with tracing.trace("t"):
        with tracing.function_span("ping", input='{"x": 1}') as f:
            f.span_data.output = "pong"
    assert [(s.name, s.args, s.result) for s in target.tool_calls()] == [("ping", {"x": 1}, "pong")]


def test_full_runner_with_scripted_model(processor):
    """A real Runner.run: handoff + two function tools (one failing), driven by a scripted Model."""
    from agents import Agent, ModelResponse, Runner, Usage, function_tool
    from agents.models.interface import Model
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

    @function_tool
    def lookup_order(order_id: str) -> dict:
        """Look up an order."""
        return {"order_id": order_id, "amount": 40}

    @function_tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        raise RuntimeError("payments gateway down")

    def call(name, args, n):
        return ResponseFunctionToolCall(type="function_call", name=name, arguments=json.dumps(args),
                                        call_id=f"call_{n}", id=f"fc_{n}", status="completed")

    def text(t):
        return ResponseOutputMessage(id="msg_1", type="message", role="assistant", status="completed",
                                     content=[ResponseOutputText(type="output_text", text=t, annotations=[])])

    class Scripted(Model):
        def __init__(self, outputs):
            self.outputs = list(outputs)

        async def get_response(self, *args, **kwargs):
            with tracing.generation_span(model="scripted", usage={"input_tokens": 10, "output_tokens": 2}):
                pass
            return ModelResponse(output=[self.outputs.pop(0)], usage=Usage(), response_id=None)

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    refunds = Agent(name="refunds", tools=[lookup_order, issue_refund], model=Scripted([
        call("lookup_order", {"order_id": "A200"}, 2),
        call("issue_refund", {"order_id": "A200", "amount": 40}, 3),
        text("Sorry, the refund failed; a human will follow up."),
    ]))
    triage = Agent(name="triage", handoffs=[refunds], model=Scripted([call("transfer_to_refunds", {}, 1)]))

    with ep.record("Refund A200") as run:
        result = asyncio.run(Runner.run(triage, "Refund A200"))
        run.output = result.final_output

    assert run.agent == "triage"
    assert run.tool_names() == ["handoff:refunds", "lookup_order", "issue_refund"]
    lookup, refund = run.tool_calls()
    assert isinstance(lookup, ToolCall) and lookup.args == {"order_id": "A200"}
    assert lookup.result == {"order_id": "A200", "amount": 40}
    assert refund.args == {"order_id": "A200", "amount": 40}
    assert refund.status == "error" and "payments gateway down" in (refund.error or "")
    assert sum(isinstance(s, LLMCall) for s in run.steps) == 4
    assert run.output.startswith("Sorry")


def test_install_registers(processor):
    from evalplane.integrations.openai_agents import install

    proc = install(replace=True)
    assert isinstance(proc, tracing.TracingProcessor)
    assert tracing.get_trace_provider()._multi_processor._processors == (proc,)
