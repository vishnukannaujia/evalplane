# LLM judges (optional)

Evalplane's built-in checks are code: they never call a model. Some things only a model (or a person)
can grade: tone, faithfulness to retrieved documents, whether a summary is right. For those, add a
`judge` expectation to a case and pass a judge function with `--judge module:function`.

```yaml
cases:
  - id: grounded-answer
    input: What is your refund policy?
    covers: [L2.grounding.faithfulness]
    expect:
      judge: {rubric: "Every claim in the answer is supported by the retrieved policy text.", threshold: 0.8}
```

```bash
evalplane run --judge my_judges:claude_judge
```

Without `--judge`, judge checks are reported as **skipped**, never passed.

## Writing a judge

A judge is any function `(rubric, run, case) -> (score between 0 and 1, reason)`.
Example with the Anthropic SDK (`pip install anthropic`, `ANTHROPIC_API_KEY` set):

```python
# my_judges.py
import json

import anthropic

client = anthropic.Anthropic()

PROMPT = """You are grading an AI agent's answer against a rubric.
Rubric: {rubric}
User request: {input}
Tool calls the agent made: {tools}
Agent's final answer: {output}

Reply with JSON only: {{"score": <0 to 1>, "reason": "<one sentence>"}}"""


def claude_judge(rubric, run, case):
    tools = [{"name": c.name, "args": c.args, "result": str(c.result)[:500]} for c in run.tool_calls()]
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=200,
        messages=[{"role": "user", "content": PROMPT.format(
            rubric=rubric, input=run.input, tools=json.dumps(tools)[:4000], output=run.output_text[:4000])}],
    )
    data = json.loads(msg.content[0].text)
    return float(data["score"]), data["reason"]
```

The same shape works with any provider.

## DeepEval and Ragas metrics

`evalplane.adapters` turns their metrics into judges (install the library and its LLM keys yourself):

```python
# my_judges.py
from deepeval.metrics import FaithfulnessMetric
from evalplane.adapters import deepeval_judge, deepeval_rubric_judge, ragas_judge

faithfulness = deepeval_judge(FaithfulnessMetric(threshold=0.7))
rubric = deepeval_rubric_judge()      # each case's expect.judge.rubric becomes a DeepEval GEval
```

```bash
evalplane run --judge my_judges:rubric
```

The agent's tool results are passed as the retrieval context, and the case's `expect.output` (if any)
as the expected output. Checked against DeepEval 4.2 and Ragas 0.4. Ragas 0.4.3 currently needs
`pip install "langchain-community<0.4"` to import (an upstream dependency issue).

## Trusting a judge

An uncalibrated judge is a random number generator with opinions. Before a judge-graded requirement
gates anything at T3/T4:

1. Label 30–50 real outputs yourself (pass/fail against the rubric).
2. Run the judge on the same outputs and measure agreement (Cohen's κ ≥ 0.6 is a reasonable floor).
3. Pin the judge model version; re-check agreement when you change it.

`evalplane calibrate labels.yaml` does steps 2 and 3 for you. `labels.yaml` holds your own verdicts
(`labels: {case-id: pass, other-case: fail}`); it compares them with the judge's verdicts from the latest
`evalplane run --judge ...`, prints agreement, Cohen's kappa and the disagreements, and saves the result.
At T3/T4 the gate warns about judge-graded requirements until the kappa is at least 0.6.

Prefer code checks wherever a rule can be expressed as one (`forbidden_tools`, `args`, policy `check:`s):
they are free, deterministic and never drift.
