"""Adapters are tested against fake deepeval/ragas modules with the real (checked) API shapes."""

import sys
import types
from dataclasses import dataclass, field

from evalplane.adapters import deepeval_judge, deepeval_rubric_judge, ragas_judge
from evalplane.cases import Case
from evalplane.trace import Run, ToolCall


@dataclass
class _TC:
    input: str
    actual_output: str
    expected_output: str | None = None
    retrieval_context: list | None = None
    tools_called: list | None = None


@dataclass
class _DETool:
    name: str
    input_parameters: dict = field(default_factory=dict)
    output: object = None


def _install_fake_deepeval(monkeypatch):
    tc_mod = types.ModuleType("deepeval.test_case")
    tc_mod.LLMTestCase, tc_mod.ToolCall = _TC, _DETool
    tc_mod.LLMTestCaseParams = types.SimpleNamespace(INPUT="input", ACTUAL_OUTPUT="actual_output")
    metrics = types.ModuleType("deepeval.metrics")

    class GEval:
        def __init__(self, name, criteria, evaluation_params, threshold, **kw):
            self.criteria = criteria

        def measure(self, tc):
            self.score, self.reason = (1.0 if "refund" in tc.actual_output else 0.0), f"criteria: {self.criteria}"

    metrics.GEval = GEval
    monkeypatch.setitem(sys.modules, "deepeval", types.ModuleType("deepeval"))
    monkeypatch.setitem(sys.modules, "deepeval.test_case", tc_mod)
    monkeypatch.setitem(sys.modules, "deepeval.metrics", metrics)


RUN = Run(input="refund A1", output="I issued a refund of $20.",
          steps=[ToolCall(name="lookup", args={"id": "A1"}, result={"total": 20})])
CASE = Case(id="c", expect={"output": {"contains": ["refund"]}})


def test_deepeval_metric_wrapper(monkeypatch):
    _install_fake_deepeval(monkeypatch)
    seen = {}

    class FakeMetric:
        def measure(self, tc):
            seen["tc"] = tc
            self.score, self.reason = 0.9, "grounded"

    score, reason = deepeval_judge(FakeMetric())("rubric", RUN, CASE)
    assert (score, reason) == (0.9, "grounded")
    tc = seen["tc"]
    assert tc.input == "refund A1" and tc.expected_output == "refund"
    assert tc.tools_called[0].name == "lookup" and '"total": 20' in tc.retrieval_context[0]


def test_deepeval_rubric_judge_uses_case_rubric(monkeypatch):
    _install_fake_deepeval(monkeypatch)
    judge = deepeval_rubric_judge()
    score, reason = judge("Mentions the refund", RUN, CASE)
    assert score == 1.0 and "Mentions the refund" in reason


def test_ragas_wrapper(monkeypatch):
    ds = types.ModuleType("ragas.dataset_schema")

    @dataclass
    class SingleTurnSample:
        user_input: str
        response: str
        retrieved_contexts: list | None = None
        reference: str | None = None

    ds.SingleTurnSample = SingleTurnSample
    monkeypatch.setitem(sys.modules, "ragas", types.ModuleType("ragas"))
    monkeypatch.setitem(sys.modules, "ragas.dataset_schema", ds)

    class Faithfulness:
        name = "faithfulness"

        def single_turn_score(self, sample):
            assert sample.user_input == "refund A1" and sample.retrieved_contexts
            return 0.75

    score, reason = ragas_judge(Faithfulness())("", RUN, CASE)
    assert score == 0.75 and "faithfulness" in reason
