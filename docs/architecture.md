# Architecture (v0.1)

Evalplane is a small, flat Python package. Everything is offline and deterministic; nothing calls an LLM
unless you plug in a judge.

```
agent.eval.yaml ──► models.py (AgentProfile) ──► tiering.py (risk tier from tool power)
                                                   │
rules/core.yaml + packs/<id>/pack.yaml ──► planner.py ──► Plan (requirements with ids, thresholds, why)
                                                   │
evals/*.yaml, *_eval.py, pytest marks ──► cases.py ──► coverage.py ──► CoverageReport (+ gaps, suggestions)
                                                   │
live agent (@ep.tool, messages) or traces ──► runner.py + scorers.py ──► Results (per case, per requirement)
                                                   │
                                   gate.py ──► GateResult ──► report.py (scorecard, evidence pack)
```

| Module | Responsibility |
|---|---|
| `models.py` | Enums (layer, tier, stage, side effect...) and the `agent.eval.yaml` schema (pydantic, `extra=forbid`) |
| `tiering.py` | Derives T1–T4 from each tool's side effect, reversibility, approval and data sensitivity |
| `rules/core.yaml` | The rule catalog: declarative YAML, no code. Conditions: `tier_at_least`, `features`, `any_tool`, `user_facing`; `for_each: tool/policy` |
| `rules/thresholds.yaml` | Named thresholds by tier, pass^k k by tier, coverage floors by stage × tier |
| `packs/` | Sector packs: extra rules + a template profile + starter cases. Third-party packs via the `evalplane.packs` entry point |
| `planner.py` | Profile + catalog → `Plan`. Requirement ids are `rule.id` or `rule.id@target`. `plan_hash` = sha256(profile + catalog version) |
| `trace.py` | `Run` → steps (`llm`, `tool`, `retrieval`, `handoff`, `approval`). JSONL and OpenTelemetry GenAI / OpenInference importers |
| `recorder.py` | `@ep.tool`, `ep.record()`, `ep.approve()`...: records steps into the current run (a no-op outside a run) |
| `messages.py` | `from_messages()`: OpenAI chat, OpenAI Responses/Agents SDK, Anthropic, LangChain message histories → `Run` |
| `cases.py` | YAML and Python (`@ep.case`) cases, matchers, implied coverage (`all_covers`), pytest results as cases |
| `scorers.py` | Deterministic checks per case, plus global checks on every run (policy checks, scope, call limits, args schema, loops) |
| `runner.py` | Runs cases live / from traces, pass^k, aggregates requirement results, saves `.evalplane/` |
| `coverage.py` | 5 weighted dimensions (requirements .4, tools .2, policies .2, OWASP .1, layers .1), gaps, suggestions, diff |
| `gate.py` | Pure function: plan + coverage + results + stage → PASS/WARN/FAIL. Missing evidence fails at T3+/pre-release |
| `report.py` | Plan/coverage/scorecard markdown, evidence pack (controls → requirements → results, sha256 manifest) |
| `promote.py` | Recorded runs → review-ready regression cases |
| `scan.py` | AST scan for tool definitions (decorators and raw SDK schema dicts) |
| `pytest_plugin.py` | `@pytest.mark.evalplane(...)` → `.evalplane/pytest.json` |
| `cli.py` | Typer CLI |

## Key semantics

- **Coverage is declared; results are measured.** A case covers requirements (explicitly or by implication);
  the gate then checks the covering cases' results against the tier's threshold.
- **Policy checks are global.** A policy with a `check:` is evaluated on every run of every case and in `audit`.
  A case that lists the policy is the "temptation" test that makes coverage complete.
- **Missing vs failing.** `missing` = nothing tests it; `fail` = tested and below threshold. At T1/T2 in CI,
  missing is a warning; at T3/T4 or pre-release, it blocks.
- **Staleness.** Results carry the `plan_hash`; if `agent.eval.yaml` changes, the gate refuses old results.

## Extension points

- Rules and packs: YAML (`pack.yaml` rules use the same schema as `rules/core.yaml`)
- Judges: `--judge module:function` returning `(score, reason)`
- Trace sources: anything that produces `Run` objects (JSONL is the interchange format)
- Existing tests: the pytest marker

## Deliberately not in v0.1

Built-in LLM providers or judges, a hosted service, signing, adapters to Inspect and promptfoo (they can
already be used inside Python cases).

Shipped after this list was first written: judge calibration (`evalplane calibrate`), DeepEval and Ragas judge
adapters (`adapters.py`), a multi-turn user simulator, and framework-specific recording for the OpenAI Agents
SDK and the Claude Agent SDK (`integrations/`).
The core keeps stable ids, versioned schemas and content-hashed plans so that other tools can build on its outputs.
