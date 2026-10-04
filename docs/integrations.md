# Integrations

Evalplane needs none of these libraries. Each integration imports its library only when you use it.

## Traces you already have (no code)

Every command that takes traces (`audit`, `review`, `promote`, `run --traces`, `coverage --traces`) detects the
format of each file:

| Source | What to export | Notes |
|---|---|---|
| **Langfuse** | Trace JSON (UI "download", `GET /api/public/traces/{id}`) or an observations list/JSONL | TOOL, GENERATION, RETRIEVER, AGENT observations; older SDKs' tool SPANs; camelCase or snake_case keys |
| **LangSmith** | Runs as JSON/JSONL (`Client.list_runs(...)`, `run.dict()`) | grouped by `trace_id`, ordered by `dotted_order`; `tool`, `llm`, `retriever` runs |
| **Braintrust** | Log or experiment spans (`/fetch` → `{"events": [...]}`, BTQL, UI JSON export) | `tool`, `llm` spans; handoffs from its OpenAI Agents integration |
| **OpenTelemetry** | OTLP JSON, GenAI semantic conventions or OpenInference spans (Phoenix, Arize, Traceloop, ...) | see [traces.md](traces.md) |
| **Message histories** | OpenAI / Anthropic / LangChain messages | `ep.from_messages(...)` |
| **τ²-bench** | a results file (`data/tau2/results/**.json`, or `tau2 run` output) | one run per simulation; `metadata.reward` keeps the benchmark's own verdict. See [benchmark-tau2.md](benchmark-tau2.md) |

To tie an imported trace to an eval case, put `case_id` (or `evalplane_case_id`) in the trace's metadata.

Pull recent traces straight from the platform (uses its SDK and your API keys):

```python
from evalplane.integrations.langfuse import fetch_langfuse
from evalplane.integrations.langsmith import fetch_langsmith
import evalplane as ep

ep.write_runs("traces/langfuse.jsonl", fetch_langfuse(limit=100))
ep.write_runs("traces/langsmith.jsonl", fetch_langsmith("my-project", limit=100))
```

`fetch_langsmith` has been run against a real LangSmith project (40 traces; it is what found the
retriever-root and `list_runs` bugs). **`fetch_langfuse` has only been tested against recorded payloads, not
a live account** — the file importers are the tested path. Tell us if it breaks.


## Recording live runs from agent frameworks (no tool decorators)

**OpenAI Agents SDK** (verified with `openai-agents` 0.22.3):

```python
from evalplane.integrations.openai_agents import install
install()                                   # once: adds a tracing processor

def run(user_input, context):               # your Evalplane entrypoint
    result = Runner.run_sync(agent, user_input)
    return result.final_output              # tool calls and handoffs were recorded from the SDK's spans
```

**Claude Agent SDK** (verified with `claude-agent-sdk` 0.2.157):

```python
from claude_agent_sdk import ClaudeAgentOptions, query
from evalplane.integrations.claude_agent_sdk import evalplane_hooks, merge_hooks

options = ClaudeAgentOptions(hooks=merge_hooks(your_hooks, evalplane_hooks()))
```

The hooks only observe (PreToolUse / PostToolUse / PostToolUseFailure); they never block. To block calls
that break a rule, use the [runtime guard](guard.md).

**LangGraph / LangChain:** return the graph state or message list from your entrypoint (see
`examples/langgraph-ops`).

## Eval libraries

- **DeepEval / Ragas** metrics as judges: [judges.md](judges.md) (`evalplane.adapters`).
- **pytest**: existing tests count toward coverage with `@pytest.mark.evalplane(...)` ([cases.md](cases.md)).

## Coding agents

`evalplane mcp` is an MCP server for Claude Code, Cursor and other MCP clients (`pip install "evalplane[mcp]"`,
then `claude mcp add evalplane -- evalplane mcp`).

## CI

GitHub Action, JUnit (GitLab, Jenkins), HTML report and pre-commit: [ci.md](ci.md).
