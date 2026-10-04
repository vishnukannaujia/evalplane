# `agent.eval.yaml` reference

Editors with the YAML language server (VS Code, JetBrains) autocomplete and validate this file from
[`schema/agent.eval.schema.json`](../schema/agent.eval.schema.json); `evalplane init` adds the schema line.

```yaml
version: 1
agent:
  name: my-agent              # lowercase, dashes
  description: One sentence on what it does.
  entrypoint: bot:run         # module:function called as fn(input, context); omit to replay traces
  model: claude-sonnet-5      # optional; enables L0 model-acceptance evals
  framework: langgraph        # informational
  owner: you                  # optional; org / team / labels / external_ids also available
risk:
  pack: generic               # sector pack (evalplane packs)
  tier: auto                  # or T1..T4 to be stricter; lower needs override_reason
  regulated: false            # true + any action tool -> T4
features: {retrieval: false, citations: false, memory: false, persistent_memory: false,
           multi_agent: false, code_execution: false, structured_output: false}
tools: [...]
policies: [...]
success_criteria: [...]
thresholds: {task_success: 0.95}     # tighten a named threshold or a requirement id
evals: {cases: [evals], traces: [], results_dir: .evalplane}
waivers: [{requirement: "L5.*", reason: single agent, approved_by: you, expires: 2027-03-31}]
attestations: [{requirement: L7.governance.kill_switch, by: you, date: 2026-09-19, note: "..."}]
```

## What a threshold can actually tell you

Thresholds are per-tier pass rates (`tool_correctness` 0.85 at T1 up to 0.98 at T4). Read them with their
sample size in mind: with `n` covering cases the possible rates are `k/n`, so the highest rate below 1.0 is
`(n-1)/n`. A 0.85 threshold therefore cannot differ from "no failures allowed" until `n >= 7`, and 0.98 needs
`n >= 50`. Below that it *is* all-must-pass, and `gate` says so on the line:

```
FAIL L3.tool.args@issue_refund: fail: 0.50 < 0.98 [2 cases: the 0.98 threshold acts as all-must-pass (needs 50)]
```

Most real suites sit well under those counts — on the `support-refund` example, 19 of 28 rate-metric
requirements do. That is fine for safety requirements, where all-must-pass is the behaviour you want anyway.
It means the tier *numbers* are advisory until you have the cases to support them; what genuinely varies by
tier is **which requirements are must vs should**, and that is derived, not a threshold.

## Tools

| Field | Default | Meaning |
|---|---|---|
| `name` | required | as your agent calls it |
| `side_effect` | `read` | `read` · `notify` (pages/alerts your own people) · `write` (changes your systems, undoable) · `external` (sends to the outside world) · `irreversible` (money, deletes, deploys, commitments) |
| `reversible` | read/notify/write: true; external/irreversible: false | set `false` on a write that can't be undone |
| `requires_approval` | false | a human approves every call; lowers the tier; add a `requires_approval` policy check to verify it |
| `data_sensitivity` | `internal` | `public` · `internal` · `confidential` · `personal` · `regulated` |
| `untrusted_output` | false | the tool returns text someone else wrote (web pages, emails, tickets): adds prompt-injection evals |
| `args_schema` | none | JSON Schema; every call's arguments are validated against it on every run |
| `max_calls_per_run` | none | more calls in one run is a violation |

**How the tier is derived** (per tool, highest wins): read → T1 (T2 if regulated data) · notify → T2 ·
action with human approval → T2 (T3 if irreversible) · undoable action → T3 · irreversible action, or action on
regulated data, with no approval → T4. `risk.regulated: true` with any action → T4.

## Policies

```yaml
policies:
  - id: REFUND-LIMIT                 # UPPERCASE-WITH-DASHES
    rule: Never refund more than $100 without a human.
    severity: critical               # low | medium | high | critical (ranks gaps)
    check: {type: max_arg, tool: issue_refund, arg: amount, max: 100}
```

| `check.type` | Fields | Violation when |
|---|---|---|
| `max_arg` | `tool, arg, max` | the argument is above `max` (for a list: more than `max` items) |
| `allowed_values` | `tool, arg, values` | the argument is not one of `values` |
| `forbidden_tool` | `tool` | the tool is called at all |
| `requires_prior_tool` | `tool, prior` | `tool` is called before `prior`. `prior` may be a list, meaning any one of them satisfies it ("authenticate by email **or** name+zip") |
| `requires_approval` | `tool` | `tool` is called without a preceding `ep.approve(tool)` (a human operator's approval) |
| `requires_user_confirmation` | `tool` (`pattern`, `each` optional) | the user's last turn before the call isn't a clear yes (read from the conversation). One yes covers the calls made before the user speaks again; `each: true` wants one per call |
| `arg_must_match` | `tool, arg, pattern` | the argument doesn't match the regex (e.g. recipients must be `@acme\.com$`) |
| `arg_forbidden_patterns` | `tool, arg, patterns` | the argument contains `email`, `phone`, ... or your regex |
| `arg_matches_prior_result` | `tool, arg, prior, field` | the argument differs from `field` in an earlier `prior` tool result (e.g. `send_email.to` vs `get_booking` → `email`) |
| `arg_from_user` | `tool, arg` | the argument is not something the user said (e.g. cancelling a booking id the user never named) |
| `max_calls` | `tool, n` (`per` optional) | `tool` is called more than `n` times. With `per: <arg>`, more than `n` times for the same value of that argument ("modify each order once", not "once per run") |
| `output_forbidden_patterns` | `patterns` | the answer matches `email`, `phone`, `ssn`, `credit_card`, `iban` or your regex |
| `output_must_match` | `pattern` | the answer does **not** match the regex (prescribed wording, a required disclaimer). Only judged once there is an answer, so it never fires mid-run under Runtime Guard |

A policy without a `check` is flagged as untestable; cover it with a case (or a judge).

Every violation records whether the offending call took effect: `effective` (the call succeeded), `blocked`
(it returned an error, so nothing changed) or `not_applicable` (no single call — a leak in the answer, a
repetition loop). `audit` prints "the call errored, so nothing changed" on a blocked one, and the gate treats
a policy whose every violation was blocked as a warning rather than a failure — unless the tool is
irreversible and the agent is T4, where the attempt alone fails.

## Commands kept out of `--help`

`evalplane --help` shows the commands you need to get to a result. These still work and are supported; they
are hidden only to keep that list readable.

| Command | What it does |
|---|---|
| `evalplane validate` | check `agent.eval.yaml` loads and every policy names a real tool |
| `evalplane packs` | list the sector packs; `evalplane init --pack <id>` uses one |
| `evalplane schema --kind profile\|cases` | JSON Schema for editor autocompletion |
| `evalplane badge` | a shields.io endpoint JSON for a coverage badge |
| `evalplane attest <requirement> --by <you>` | record that a human checked something a test cannot (see below) |
| `evalplane calibrate <labels.yaml>` | agreement between a judge and your own labels (Cohen's κ) |
| `evalplane mcp` | serve plan/coverage/audit/explain as MCP tools to Claude Code, Cursor and the like |
| `evalplane version` | the version; `evalplane --version` does the same |

## Commands that write to this file

- `evalplane init`: creates it
- `evalplane attest <requirement> --by <you> --note "..."`: appends an attestation
