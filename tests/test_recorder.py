import pytest

import evalplane as ep
from evalplane.recorder import current_run
from evalplane.trace import Approval, Handoff, LLMCall, Retrieval, read_runs


@ep.tool
def add(a, b=2):
    return a + b


@ep.tool(name="custom.name")
def renamed(x):
    return x


@ep.tool
def explode(why):
    raise ValueError(why)


@ep.tool
def guarded(amount):
    raise ep.ToolDenied(f"amount {amount} blocked")


class Svc:
    @ep.tool
    def method(self, q):
        return q.upper()


def test_noop_outside_record():
    assert current_run() is None
    assert add(1) == 3
    ep.approve("x")
    ep.retrieved(["d"])
    ep.handoff("b")
    ep.llm(model="m")
    assert current_run() is None


def test_records_tool_calls_inside_record():
    with ep.record("hello", case_id="c9", agent="bot") as run:
        assert current_run() is run
        assert add(1, b=5) == 6
        renamed("v")
        Svc().method("q")
        run.output = "done"
    assert current_run() is None
    assert run.input == "hello" and run.case_id == "c9" and run.agent == "bot" and run.status == "ok"
    assert run.tool_names() == ["add", "custom.name", "method"]
    first = run.steps[0]
    assert first.args == {"a": 1, "b": 5} and first.result == 6 and first.status == "ok"
    assert first.duration_ms is not None and first.duration_ms >= 0
    assert run.steps[2].args == {"q": "q"}  # self is dropped
    assert run.latency_ms is not None


def test_tool_error_is_recorded_and_reraised():
    with pytest.raises(ValueError), ep.record() as run:
        explode("boom")
    step = run.steps[0]
    assert step.status == "error" and step.error == "ValueError: boom"
    assert run.status == "error" and "ValueError: boom" in run.error


def test_tool_denied_is_recorded_as_denied():
    with ep.record() as run, pytest.raises(ep.ToolDenied):
        guarded(500)
    assert run.steps[0].status == "denied" and "500 blocked" in run.steps[0].error
    assert run.status == "ok"


def test_approve_retrieved_handoff_llm_steps():
    with ep.record() as run:
        ep.approve("pay", approver="alice")
        ep.approve("pay", decision="rejected")
        ep.retrieved([1, "b"], query="q")
        ep.handoff("billing", from_agent="triage")
        ep.llm(model="m", input_tokens=3, output_tokens=4, cost_usd=0.1, output="hi")
    a1, a2, ret, hand, llm = run.steps
    assert isinstance(a1, Approval) and a1.tool == "pay" and a1.decision == "approved" and a1.approver == "alice"
    assert a2.decision == "rejected"
    assert isinstance(ret, Retrieval) and ret.doc_ids == ["1", "b"] and ret.query == "q"
    assert isinstance(hand, Handoff) and hand.to_agent == "billing" and hand.from_agent == "triage"
    assert isinstance(llm, LLMCall) and llm.cost_usd == 0.1
    assert run.tool_names() == ["handoff:billing"]
    assert run.retrieved_ids() == ["1", "b"]


def test_record_save_to_appends(tmp_path):
    path = tmp_path / "out" / "runs.jsonl"
    for i in range(2):
        with ep.record(i, save_to=path) as run:
            add(i)
            run.output = i
    runs = list(read_runs(path))
    assert [r.input for r in runs] == [0, 1]
    assert runs[1].tool_names() == ["add"]


def test_nested_records_are_isolated():
    with ep.record() as outer:
        add(1)
        with ep.record() as inner:
            add(2)
        add(3)
    assert [s.args["a"] for s in outer.steps] == [1, 3]
    assert [s.args["a"] for s in inner.steps] == [2]
