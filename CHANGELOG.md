# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/). The `agent.eval.yaml` schema (`version: 1`) and the JSONL run
format (`schema_version: "1"`) are versioned separately.

## [Unreleased] — 0.1.0.dev0

### Added
- Runtime guard (`evalplane.Guard`): the same policies block violating tool calls in production (enforce or shadow).
- Integrations: Langfuse, LangSmith and Braintrust trace exports; OpenAI Agents SDK processor; Claude Agent SDK hooks;
  DeepEval/Ragas judges; MCP server (`evalplane mcp`); GitHub Action; JUnit/HTML/PR-comment reports; pre-commit hook.
- `compare` (regressions between runs), multi-turn cases (`turns`, `simulator`), `run --jobs/--timeout`,
  `review`, `generate`, `calibrate`, new argument checks.
- `agent.eval.yaml` profile with risk tier derived from what the tools can do (T1–T4), policies with
  deterministic checks, success criteria, waivers and attestations; JSON Schemas for editors.
- Rule catalog (core + 7 sector packs: generic, support-agent, coding-agent, knowledge-assistant,
  data-analyst, personal-assistant, claims-processing) → `evalplane plan`, `explain`.
- `evalplane coverage`: coverage score and a ranked gap list with paste-ready cases; `--baseline` and
  `--fail-on-new-gaps` for pull requests.
- `evalplane run`: live (`@ep.tool`, or any agent returning an OpenAI/Anthropic/LangChain message history),
  or replaying JSONL / OpenTelemetry GenAI / OpenInference traces; pass^k; precise per-requirement blame;
  simulated tool failures (`context.fail_tools`); optional LLM judges.
- `evalplane gate` (design / ci / pre-release, thresholds by tier), `report` (scorecard, evidence pack mapped to
  OWASP Agentic Top 10, NIST AI RMF, ISO/IEC 42001, EU AI Act), `badge`.
- `evalplane audit` (policy checks on any traces, no cases needed), `promote` (runs → regression cases),
  `attest`, `schema`, `packs`, `init` (scans code for tools; `--example`).
- pytest plugin: `@pytest.mark.evalplane(...)` makes existing tests count.
- Examples: support-refund (T4, `BUGGY=1`), faq-messages (T1, messages), langgraph-ops (LangGraph),
  tau2-airline (τ²-bench airline domain described).
