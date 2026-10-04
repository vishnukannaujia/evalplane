"""Langfuse, LangSmith and Braintrust exports -> Runs (fixtures follow the real export formats)."""

from pathlib import Path

import pytest

from evalplane.integrations import detect_format, load_any
from evalplane.integrations.braintrust import load_braintrust
from evalplane.integrations.langfuse import load_langfuse
from evalplane.integrations.langsmith import load_langsmith
from evalplane.trace import load_traces

FIX = Path(__file__).parent / "fixtures" / "integrations"


def test_langfuse_traces_export():
    runs = load_langfuse(FIX / "langfuse_traces.json")
    assert len(runs) == 2
    r = next(r for r in runs if r.case_id == "refund-over-limit")
    assert r.tool_names() == ["lookup_order", "handoff:refunds_agent", "issue_refund", "escalate_to_human"]
    assert r.output_text.startswith("I've asked a human")


def test_langfuse_observations_jsonl():
    runs = load_langfuse(FIX / "langfuse_observations.jsonl")
    assert runs[0].tool_names() == ["get_weather"] and "Paris" in runs[0].output_text


def test_langsmith_runs_export():
    runs = load_langsmith(FIX / "langsmith_runs.jsonl")
    r = next(r for r in runs if r.case_id == "refund-small")
    assert r.tool_names() == ["lookup_order", "issue_refund"] and "$40" in r.output_text
    assert r.tool_calls()[0].args


def test_braintrust_logs_export():
    runs = load_braintrust(FIX / "braintrust_logs.json")
    r = next(r for r in runs if r.case_id == "refund-damaged")
    assert r.tool_names() == ["lookup_order", "handoff:refunds_agent", "issue_refund"]


@pytest.mark.parametrize("name", ["langfuse_traces.json", "langsmith_runs.jsonl", "braintrust_logs.json"])
def test_load_any_and_load_traces_detect_format(name):
    assert load_any(FIX / name) and load_traces([FIX / name])


def test_native_formats_still_load(tmp_path):
    from evalplane.trace import Run, ToolCall, write_runs

    write_runs(tmp_path / "r.jsonl", [Run(case_id="x", steps=[ToolCall(name="t")])])
    assert [r.case_id for r in load_traces([tmp_path])] == ["x"]
    assert detect_format([{"run_id": "a", "steps": []}]) != "langfuse"


def test_root_run_that_is_itself_a_tool_or_retriever():
    """A directly traced retriever/tool call (no wrapping chain) is still a step.
    Found against a real LangSmith project, where every trace was a bare VectorStoreRetriever run."""
    from evalplane.integrations.langsmith import langsmith_to_runs

    records = [{
        "id": "0199-a", "trace_id": "0199-a", "parent_run_id": None, "run_type": "retriever",
        "name": "VectorStoreRetriever", "start_time": "2026-09-08T10:00:00Z", "end_time": "2026-09-08T10:00:01Z",
        "inputs": {"query": "Who leads the championship?"},
        "outputs": {"documents": [{"page_content": "...", "metadata": {"id": "doc-1"}}]},
    }, {
        "id": "0199-b", "trace_id": "0199-b", "parent_run_id": None, "run_type": "tool", "name": "lookup_order",
        "inputs": {"order_id": "A1"}, "outputs": {"output": {"total": 40}},
    }]
    runs = {r.run_id: r for r in langsmith_to_runs(records)}
    assert runs["0199-a"].retrieved_ids() == ["doc-1"]
    assert runs["0199-b"].tool_names() == ["lookup_order"]
    assert runs["0199-b"].tool_calls()[0].args == {"order_id": "A1"}
