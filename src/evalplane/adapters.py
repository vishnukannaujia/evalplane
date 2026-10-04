"""Use DeepEval or Ragas metrics as Evalplane judges (optional; install the library yourself).

    # my_judges.py
    from deepeval.metrics import FaithfulnessMetric
    from evalplane.adapters import deepeval_judge, deepeval_rubric_judge

    faithfulness = deepeval_judge(FaithfulnessMetric(threshold=0.7))
    rubric = deepeval_rubric_judge()          # uses each case's `expect.judge.rubric` (DeepEval GEval)

    evalplane run --judge my_judges:rubric

Checked against DeepEval 4.2 and Ragas 0.4 (Ragas 0.4.3 currently needs `langchain-community<0.4` to import).
Both call an LLM: set up the provider keys those libraries expect.
"""

from __future__ import annotations

import json
from typing import Any

from .cases import Case
from .trace import Retrieval, Run, ToolCall


def _contexts(run: Run) -> list[str]:
    """Text the agent had in front of it: tool results (and retrieved document ids)."""
    out = []
    for s in run.steps:
        if isinstance(s, ToolCall) and s.result is not None:
            out.append(s.result if isinstance(s.result, str) else json.dumps(s.result, default=str))
        elif isinstance(s, Retrieval) and s.doc_ids:
            out.append("retrieved: " + ", ".join(s.doc_ids))
    return out


def _expected_text(case: Case) -> str | None:
    o = case.expect.output
    if o is None:
        return None
    if isinstance(o.equals, str):
        return o.equals
    if o.contains:
        return " ".join([o.contains] if isinstance(o.contains, str) else o.contains)
    return None


def deepeval_test_case(run: Run, case: Case) -> Any:
    from deepeval.test_case import LLMTestCase
    from deepeval.test_case import ToolCall as DEToolCall

    tools = [DEToolCall(name=t.name, input_parameters=t.args or {}, output=t.result)
             for t in run.tool_calls()]
    return LLMTestCase(input=str(run.input or ""), actual_output=run.output_text,
                       expected_output=_expected_text(case), retrieval_context=_contexts(run) or None,
                       tools_called=tools or None)


def deepeval_judge(metric: Any):
    """Wrap one DeepEval metric instance (FaithfulnessMetric, AnswerRelevancyMetric, ToolCorrectnessMetric, ...)."""

    def judge(rubric: str, run: Run, case: Case) -> tuple[float, str]:
        metric.measure(deepeval_test_case(run, case))
        return float(metric.score or 0.0), str(getattr(metric, "reason", "") or "")

    judge.__name__ = f"deepeval_{type(metric).__name__}"
    return judge


def deepeval_rubric_judge(model: Any = None, threshold: float = 0.5):
    """A DeepEval GEval per rubric: each case's `expect.judge.rubric` becomes the GEval criteria."""
    cache: dict[str, Any] = {}

    def judge(rubric: str, run: Run, case: Case) -> tuple[float, str]:
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCaseParams

        if rubric not in cache:
            kwargs = {"name": "evalplane-rubric", "criteria": rubric, "threshold": threshold,
                      "evaluation_params": [LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT]}
            if model is not None:
                kwargs["model"] = model
            cache[rubric] = GEval(**kwargs)
        metric = cache[rubric]
        metric.measure(deepeval_test_case(run, case))
        return float(metric.score or 0.0), str(getattr(metric, "reason", "") or "")

    return judge


def ragas_sample(run: Run, case: Case) -> Any:
    from ragas.dataset_schema import SingleTurnSample

    return SingleTurnSample(user_input=str(run.input or ""), response=run.output_text,
                            retrieved_contexts=_contexts(run) or None, reference=_expected_text(case))


def ragas_judge(metric: Any):
    """Wrap one Ragas single-turn metric (Faithfulness, ResponseRelevancy, ...), already configured with an LLM."""

    def judge(rubric: str, run: Run, case: Case) -> tuple[float, str]:
        score = metric.single_turn_score(ragas_sample(run, case))
        return float(score), f"ragas {getattr(metric, 'name', type(metric).__name__)} = {float(score):.2f}"

    judge.__name__ = f"ragas_{type(metric).__name__}"
    return judge
