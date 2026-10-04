# Traces: getting runs into Evalplane

Evalplane works on **runs**: one execution of your agent on one input, with the steps it took. You can
produce runs in four ways; all commands that take traces (`audit`, `review`, `promote`, `run --traces`,
`coverage --traces`) accept files or directories of any of these.

## 1. JSONL (the native format)

One run per line:

```json
{"run_id": "t-001", "case_id": "cancel-booking", "input": "Cancel booking B77.",
 "output": "Booking B77 is cancelled.",
 "steps": [
   {"type": "tool", "name": "get_booking", "args": {"booking_id": "B77"}, "result": {"status": "confirmed"}},
   {"type": "user", "text": "Yes, cancel it."},
   {"type": "tool", "name": "cancel_booking", "args": {"booking_id": "B77"}}
 ]}
```

| Field | Meaning |
|---|---|
| `run_id` | unique id (generated if missing) |
| `case_id` | optional: which eval case this run belongs to (used by `run --traces` to match runs to cases) |
| `input`, `output` | the user's first message and the agent's final answer |
| `status`, `error` | `ok` (default), `error` or `timeout` |
| `final_state` | optional dict, checked by `expect.final_state` |
| `steps[]` | in order. `type` is one of: |
| &nbsp;&nbsp;`tool` | `name`, `args`, `result`, `status` (`ok`/`error`/`denied`) |
| &nbsp;&nbsp;`user` | `text`: a later user turn (e.g. a confirmation) |
| &nbsp;&nbsp;`llm` | `model`, `input_tokens`, `output_tokens`, `cost_usd` (for budgets) |
| &nbsp;&nbsp;`retrieval` | `query`, `doc_ids` |
| &nbsp;&nbsp;`handoff` | `to_agent` (multi-agent) |
| &nbsp;&nbsp;`approval` | `tool`, `decision` (`approved`/`rejected`): a human operator's approval |

A `.json` file may hold one run or a list of runs.

## 2. Message histories (no conversion code)

If your agent returns (or you logged) a chat history, `evalplane.from_messages(messages)` builds the run:
OpenAI Chat Completions (`tool_calls` / `role: tool`), OpenAI Responses and Agents SDK items
(`function_call` / `function_call_output`), Anthropic (`tool_use` / `tool_result` blocks) and LangChain /
LangGraph message objects. Later user turns become `user` steps.

```python
import json, evalplane as ep
runs = [ep.from_messages(conv["messages"], case_id=conv["id"]) for conv in my_logged_conversations]
ep.write_runs("traces/prod.jsonl", runs)
```

An entrypoint can also simply `return messages` (or a LangGraph state with `messages`); `evalplane run`
converts it.

## 3. OpenTelemetry / OpenInference exports

OTLP JSON (`resourceSpans`) or flat span lists are converted automatically, one run per trace:
GenAI semantic conventions (`gen_ai.operation.name = execute_tool / chat / invoke_agent`, `gen_ai.tool.name`,
`gen_ai.tool.call.arguments`, `gen_ai.usage.*`) and OpenInference span kinds (`TOOL`, `LLM`, `RETRIEVER`,
`AGENT`). Set a span attribute `evalplane.case_id` to tie a trace to a case.

## 4. Record live runs

`evalplane run` writes every run to `.evalplane/runs/<timestamp>.jsonl`. You can also record from your own
code:

```python
with ep.record(user_input, case_id="demo", save_to="traces/dev.jsonl") as run:
    run.output = my_agent(user_input)       # calls to @ep.tool functions are captured
```

## Privacy

Runs contain whatever your agent saw and said: inputs, tool arguments and results. Keep production traces
out of git unless they are scrubbed, and don't upload `.evalplane/runs/` as a CI artifact if they contain
real data.
