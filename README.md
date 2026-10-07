# Evalplane

> [!NOTE]
> **This is a research artifact, not a maintained product.** It works, it is tested, and the numbers in it
> are checkable (`scripts/verify-benchmark.py`). But nobody is on call for it, there is no roadmap, and
> issues may go unanswered. Do not build a release process on it without being willing to fork it. If you
> want a maintained tool in this space, [Microsoft ASSERT](https://github.com/responsibleai/ASSERT) is
> actively developed and [Langfuse](https://github.com/langfuse/langfuse) covers the observability side.
> Said plainly because the alternative is what happened to users of eval libraries that went quiet without
> telling anyone.

**Your agent's rules live in a prompt, where they have more than one defensible reading.** "Get explicit
confirmation before charging the customer" can mean one confirmation per conversation, one until the customer
speaks again, or one per charge — and across 2,624 published τ²-bench trajectories those three readings
disagree by **35×** about how often agents actually break the rule.

Evalplane makes each rule a deterministic check in a file you can diff and review, tells you which of your
rules you have not pinned down yet, and fails the build when one fires — in a test run or in a production
trace. It makes the reading you chose explicit. It does not claim to find what other tools cannot.

- **[The study](docs/benchmark-tau2.md): one policy sentence, three defensible readings, 35× apart** —
  4,448 published τ²-bench trajectories across three domains, prior art credited at the top, and five of our
  own wrong numbers recorded as corrections. This is the part worth reading even if you never run the tool.
- **Write the rule down:** `check:` types that run on every test *and* every production trace, so a policy is
  enforced rather than described.
- **See what you have not pinned down:** `evalplane coverage` counts the policies with no code check and the
  action tools with no "should NOT act" test, and refuses to credit a claim a case does not actually assert.
- **Gate CI** by the agent's risk tier, derived from what its tools can do.

It runs offline, needs no API key, makes **zero LLM calls** by default, and works with any Python agent
(LangGraph, OpenAI Agents SDK, Claude Agent SDK, plain Python — CrewAI, Pydantic AI and ADK tools are
recognised by the scanner but those paths are untested) or recorded traces (OpenTelemetry,
Langfuse, LangSmith, Braintrust, τ²-bench). DeepEval and Ragas can be used as judges.

> Status: v0.1.0.dev0 · 361 tests · Apache-2.0

---

## See the finding in 60 seconds (no API key)

```bash
pip install git+https://github.com/vishnukannaujia/evalplane   # not on PyPI
evalplane init --example tau2-airline && cd tau2-airline
evalplane audit traces/
```

Four recorded τ²-bench airline conversations, no test cases written, and it reports four rule violations —
a compensation over the published cap, a cancellation with no confirmation, a booking before the customer was
identified, and a booking over the passenger limit. These are the agent's own stated policies, encoded as
checks, run over recorded traces.

Then the same thing at scale, on a bot with deliberate bugs:

```bash
evalplane init --example support-refund && cd support-refund
evalplane run && evalplane gate     # PASS, 11 cases, 0 LLM calls
BUGGY=1 evalplane run; evalplane gate   # FAIL: refund over limit, injection, PII leak, retry loop
```

## Already have traces? Start there

The best evals come from real failures. If your agent already runs (in dev or production) and you have
traces (Langfuse, LangSmith or Braintrust exports, OpenTelemetry / OpenInference, JSONL or message histories; see [docs/integrations.md](docs/integrations.md)):

```bash
evalplane init                      # describe the tools and rules once
evalplane audit traces/             # rule violations across all runs, no test cases needed
evalplane review traces/            # look at runs (rule breakers first), label what went wrong
evalplane promote traces/ --reviewed    # labelled runs become test cases, grouped by failure mode
evalplane coverage                  # what's still untested, riskiest first
```

`promote` writes cases that remember the run they came from, so `evalplane run --traces traces/` replays them
right away; point `agent.entrypoint` at your agent to run them live instead. `generate` adds temptation cases
tagged `unreviewed`; they count toward coverage once you've checked them and removed the tag.

`review` is error analysis: labels like "made up a policy" or "wrong recipient" are grouped so your most
common failure modes become your first tests. The risk-based plan (next section) is the safety floor for
what you can't wait to see fail: irreversible actions, prompt injection, data leaks.

## Using it on your own agent

The notice above is not a disclaimer, so the full walkthrough lives in
**[docs/using-it.md](docs/using-it.md)** rather than here: describing your tools, recording what the agent
does, writing cases, running the same rules over production traces, the MCP server and CI.

## The model

Every eval sits on four axes:

| Axis | Values |
|---|---|
| **Layer** | L0 model · L1 prompt · L2 retrieval/memory · L3 tools · L4 trajectory · L5 multi-agent · L6 business outcome · L7 governance |
| **Dimension** | quality · safety · reliability (pass^k) · efficiency · compliance |
| **Risk tier** | T1 read-only · T2 a human approves actions · T3 autonomous, reversible · T4 irreversible or regulated |
| **Stage** | design · ci · pre-release · production |

The tier is **derived from what the agent's tools can do**, not from what you say it is for. Higher tiers get
more required evals and stricter thresholds. See [docs/framework.md](docs/framework.md).

## Commands

| Command | What it does |
|---|---|
| `evalplane init` | Create `agent.eval.yaml` from the tools in your code, and starter cases |
| `evalplane audit <traces>` | Check recorded or production traces against every rule — no test cases needed |
| `evalplane review <traces>` | Error analysis: label real runs, see your most common failure modes |
| `evalplane coverage` | What your rules don't pin down, worst first (`--explain <id>` for why one applies) |
| `evalplane run` | Run cases live or by replaying traces (`--traces`, `--repeat`, `--judge`) |
| `evalplane gate` | PASS/WARN/FAIL for a stage, by risk tier. Exit 1 on FAIL |

Also: `plan`, `generate`, `promote`, `compare`, `report`, and eight more kept out of `--help` to keep that
list readable — [docs/reference.md](docs/reference.md) lists them all.

## Docs

- [Framework: the four axes and how to choose metrics](docs/framework.md)
- [Writing cases and checks](docs/cases.md) · [`agent.eval.yaml` reference](docs/reference.md) · [Architecture](docs/architecture.md)
- [Integrations](docs/integrations.md) (Langfuse, LangSmith, Braintrust, OpenAI Agents SDK, Claude Agent SDK, MCP) · [Runtime guard](docs/guard.md) · [CI](docs/ci.md) · [LLM judges](docs/judges.md) · [Changelog](CHANGELOG.md)
- **[The study](docs/benchmark-tau2.md)** and its [checkable figures](docs/benchmark-figures.json) · [Using it on your own agent](docs/using-it.md)
- [Validation log](docs/validation-log.md) — what was tested against real systems, and every number this project got wrong

## License

Apache-2.0. See [LICENSE](LICENSE).
