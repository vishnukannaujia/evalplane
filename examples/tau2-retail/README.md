# τ²-bench retail, seen by Evalplane

The [τ²-bench](https://github.com/sierra-research/tau2-bench) **retail** agent (16 tools, the published
`policy.md`) described in `agent.eval.yaml`. Written from the policy document **before any trajectory was
opened**, which is what makes the numbers in [docs/benchmark-tau2.md](../../docs/benchmark-tau2.md) a
replication rather than a fit.

```bash
evalplane plan        # T4: cancel_pending_order refunds money with no human in the loop
evalplane audit /path/to/tau2-bench/data/tau2/results/final/*retail_default*.json
```

**4 of 15 stated rules are encoded.** That is the honest ratio, and the reasons matter more than the number:

- **6 rules are enforced by the environment in code** — 26 `ValueError` guards across retail's 7 write tools
  re-check order status, item correspondence, gift-card balance, refund destination and payment count. A rule
  the tools enforce cannot be broken silently, so a check for it would only ever flag blocked attempts.
- **4 need a judge**: don't invent information, no subjective recommendations, remind the customer they have
  listed all items, transfer only if out of scope.
- **2 need an any-of prior over argument shapes** (authenticate by email *or* name+zip).
- **2 need a check type that does not exist**: one tool call at a time, and an exact handoff message.

What is left is the two procedural families no environment can enforce — **confirm before you act** and
**read before you write** — and they are where the violations are: 53 of 1,324 τ²-solved trajectories, with
the agent treating an answer to its own follow-up question as consent.

`CANCEL-REASON` is held out of the headline rate: the environment enforces the enum, so only an attempt can
ever be flagged.
