import json

import pytest

from evalplane.trace import (
    Approval,
    Handoff,
    LLMCall,
    Retrieval,
    Run,
    ToolCall,
    load_otel_data,
    load_traces,
    read_runs,
    write_runs,
)


def sample_run(**kw) -> Run:
    return Run(case_id="c1", input="hi", output={"a": 1}, steps=[
        ToolCall(name="lookup", args={"id": 1}, result={"ok": True}),
        Handoff(to_agent="billing", from_agent="triage"),
        LLMCall(model="m", cost_usd=0.01, input_tokens=3),
        Retrieval(query="q", doc_ids=["d1", "d2"]),
        LLMCall(model="m", cost_usd=0.02),
        LLMCall(model="m"),
        Approval(tool="refund", decision="rejected", approver="bob"),
        ToolCall(name="refund", args={"amount": 5}, status="denied", error="blocked"),
    ], **kw)


def test_run_helpers():
    r = sample_run()
    assert r.tool_names() == ["lookup", "handoff:billing", "refund"]
    assert [c.name for c in r.tool_calls()] == ["lookup", "refund"]
    assert r.retrieved_ids() == ["d1", "d2"]
    assert r.cost_usd == pytest.approx(0.03)
    assert json.loads(r.output_text) == {"a": 1}
    assert Run().output_text == "" and Run(output="plain").output_text == "plain"
    assert Run().run_id != Run().run_id


def test_jsonl_roundtrip(tmp_path):
    runs = [sample_run(), Run(case_id="c2", status="error", error="boom", final_state={"x": [1]})]
    path = tmp_path / "sub" / "runs.jsonl"
    write_runs(path, runs)
    back = list(read_runs(path))
    assert [r.model_dump() for r in back] == [r.model_dump() for r in runs]
    assert isinstance(back[0].steps[1], Handoff) and isinstance(back[0].steps[6], Approval)


def test_jsonl_skips_blank_and_comment_lines_and_reports_bad_lines(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text("// comment\n\n" + Run(case_id="ok").model_dump_json() + "\n{not json}\n")
    it = read_runs(p)
    assert next(it).case_id == "ok"
    with pytest.raises(ValueError, match=r"r.jsonl:4"):
        next(it)


def test_json_list_of_runs_and_load_traces_dir(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps([Run(case_id="x").model_dump(mode="json"),
                                                 Run(case_id="y").model_dump(mode="json")]))
    (tmp_path / "nested").mkdir()
    write_runs(tmp_path / "nested" / "b.jsonl", [Run(case_id="z")])
    (tmp_path / "ignored.txt").write_text("junk")
    runs = load_traces([tmp_path])
    assert sorted(r.case_id for r in runs) == ["x", "y", "z"]
    single = load_traces([tmp_path / "a.json"])
    assert [r.case_id for r in single] == ["x", "y"]


def test_otel_otlp_json_import(fixtures_dir):
    runs = list(read_runs(fixtures_dir / "otel_resource_spans.json"))
    assert len(runs) == 1
    r = runs[0]
    assert r.run_id == "trace-aaa" and r.case_id == "case-7" and r.agent == "support"
    assert r.input == "Refund order A1" and r.output == {"answer": "done"}
    assert r.latency_ms == pytest.approx(500.0)
    assert r.metadata["source"] == "otel"
    assert [type(s).__name__ for s in r.steps] == ["LLMCall", "ToolCall", "ToolCall", "Handoff"]
    llm, lookup, refund, handoff = r.steps
    assert llm.model == "gpt-x" and llm.input_tokens == 12 and llm.output_tokens == 5
    assert llm.cost_usd == pytest.approx(0.25) and llm.duration_ms == pytest.approx(100.0)
    assert lookup.name == "lookup_order" and lookup.args == {"order_id": "A1"} and lookup.result == {"total": 40}
    assert lookup.status == "ok"
    assert refund.status == "error" and refund.args == {"input": "not json"}
    assert handoff.to_agent == "billing"
    assert r.tool_names() == ["lookup_order", "issue_refund", "handoff:billing"]


def test_openinference_flat_span_import(fixtures_dir):
    runs = {r.run_id: r for r in read_runs(fixtures_dir / "openinference_spans.json")}
    assert set(runs) == {"t-1", "t-2"}
    r = runs["t-1"]
    assert r.agent == "agent" and r.input == "What is the refund policy?"
    assert r.output == "Refunds up to $100 are automatic."
    assert [s.type for s in r.steps] == ["retrieval", "llm", "tool", "handoff"]
    ret, llm, tool, hand = r.steps
    assert ret.doc_ids == ["refund-policy", "42"] and ret.query == "refund policy"
    assert llm.model == "claude-x" and llm.input_tokens == 30 and llm.output_tokens == 7
    assert r.cost_usd == pytest.approx(0.5)
    assert tool.name == "search_policy" and tool.args == {"query": "refund"} and tool.result == [1, 2]
    assert tool.status == "error"
    assert hand.to_agent == "escalation-agent"
    other = runs["t-2"]
    assert other.steps[0].name == "ping" and other.steps[0].args == {"input": "hello"}


def test_flat_spans_dict_wrapper():
    runs = load_otel_data({"spans": [{"trace_id": "t", "span_id": "s", "name": "x",
                                      "attributes": {"openinference.span.kind": "TOOL", "tool.name": "a"}}]})
    assert runs[0].tool_names() == ["a"]
    assert runs[0].steps[0].duration_ms is None


def test_openinference_iso_timestamps():
    runs = load_otel_data([
        {"trace_id": "t", "span_id": "s", "parent_id": None, "name": "tool",
         "start_time": "2026-01-01T00:00:00.000+00:00", "end_time": "2026-01-01T00:00:00.250+00:00",
         "attributes": {"openinference.span.kind": "TOOL", "tool.name": "a"}},
    ])
    assert runs[0].tool_names() == ["a"]
    assert runs[0].steps[0].duration_ms == pytest.approx(250.0)
