# τ²-bench telecom, seen by Evalplane

The [τ²-bench](https://github.com/sierra-research/tau2-bench) **telecom** agent (13 agent tools, the published
`main_policy.md`) described in `agent.eval.yaml`, written from the policy document before any trajectory was
opened.

```bash
evalplane plan        # T4: refuel_data charges the customer, send_payment_request cannot be unsent
evalplane audit /path/to/tau2-bench/data/tau2/results/final/*telecom_default*.json
```

**Only 3 of 20+ stated rules are encodable, and this example is in the repo because that is the useful part.**

Telecom is the opposite of retail: **policy-permissive**. Four guards across six write tools, one of them
commented out in the source. `refuel_data` applies `gb_amount × price_per_gb` to the customer's bill with no
upper bound, so its 2GB cap exists only in prose. `send_payment_request`'s own docstring says it does not check
whether the bill is already paid.

That should make it the richest hunting ground in τ². It isn't:

- Across **538 `refuel_data` calls the agents exceeded the unenforced 2GB cap once.**
- The domain's two highest-value rules are unencodable. "Lift the suspension only after all overdue bills are
  paid" is satisfied by the *customer's* `make_payment`, which is a user-simulator tool, so a `requires_prior_tool`
  would fire on every legitimate run. "Never lift the suspension if the contract has ended" needs database state.
- 507 lines of tech-support workflow run through the customer's device, not the agent.

Two things this example is genuinely good for. First, the correction recorded in the profile: with a single
`prior` the bill-reading check reported 302 violations of which **88% were false**, because agents read bills
through the generic `get_details_by_id`. Second, what Evalplane's always-on checks find that the benchmark's
reward structurally cannot: **146 attempts by the agent to call the customer's own device tools**, every one
rejected by the environment, so no reward signal exists for any of them.

See [docs/benchmark-tau2.md](../../docs/benchmark-tau2.md).
