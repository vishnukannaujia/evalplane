# Runtime guard: the same rules in tests and in production

The policy `check:`s in `agent.eval.yaml` are evaluated on every eval run. `evalplane.Guard` applies the
same checks **before each tool call in production**, and blocks the call if it would break a rule.

```python
import evalplane as ep

guard = ep.Guard.from_file("agent.eval.yaml")          # mode="shadow" to only report

@guard.tool                                            # use instead of @ep.tool
def issue_refund(order_id: str, amount: float): ...

def handle(user_input: str):
    with guard.session(user_input):                    # one conversation or request
        ...                                            # your agent loop
        guard.user_said(next_user_message)             # later user turns, for confirmation rules
```

- A blocked call raises `evalplane.ToolDenied` **before** the function runs, with the rule(s) it broke.
  Catch it in your agent loop and tell the model or the user ("I can't refund more than $100 without a human").
- Checked before each call: every policy check that looks at tool calls (`max_arg`, `allowed_values`,
  `forbidden_tool`, `requires_prior_tool`, `requires_approval`, `requires_user_confirmation`, `max_calls`,
  `arg_must_match`, `arg_forbidden_patterns`, `arg_matches_prior_result`, `arg_from_user`), undeclared tools,
  `max_calls_per_run` and `args_schema`. Rules about the final answer (`output_forbidden_patterns`,
  `output_must_match`) can't be judged before the answer exists; check those with `evalplane audit` on your
  traces.
- A call is blocked only if **it** adds a violation.
- **So argument- and ordering-level checks enforce; output-level ones only audit.** Guard runs before the
  call, when there is no answer yet, so `output_forbidden_patterns` and `output_must_match` cannot stop
  anything — they are checked after the fact by `evalplane run` and `evalplane audit`. If a rule about the
  final answer has to be enforced, that belongs in your own output filter, not here.
- **Shadow mode** (`mode="shadow"`) blocks nothing and reports every would-be violation to `on_violation`
  (default: a warning log). Run it in shadow for a while, check the reports, then switch to `enforce`.
- Blocked calls are recorded as `denied` steps, so the same session can be saved (`ep.record(..., save_to=...)`)
  and audited, reviewed and promoted like any other trace.
- Inside `evalplane run`, the guard uses the run being recorded, so your eval cases exercise the guard too:
  a case can check that a refund over the limit is denied (`forbidden_tools` ignores denied calls).
