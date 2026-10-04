# Evalplane on τ²-bench: what a task score misses

[τ²-bench](https://github.com/sierra-research/tau2-bench) scores a customer-service agent on whether the
episode ended in the right state: the database matches, the expected actions happened, a judge checks a few
natural-language assertions. It is a good benchmark and this is not a criticism of it — it answers "did the
agent finish the job?".

Evalplane answers a different question: **"what did the agent do on the way there?"** Those come apart, and
τ² ships enough data to show exactly where — including, as it turned out, enough data to show where our own
checks were wrong.

Read this document for the method, not for a number. Its main result is that **the headline number depends on
how you read one policy sentence, by 35×** — which is why the profile matters more than the percentage, and
why another project published 0.6% off the same files where we publish 2.6%. Both are right.

## Prior art, and what is actually new here

Two projects got to this ground first, and one of them six days before our first run. Stating that plainly
is a precondition for anything else in this document being worth reading.

- **[llmcontract-tau2](https://github.com/chrisbartoloburlo/llmcontract-tau2)** (first commit 2026-05-05,
  five months before our airline run) found the finding: agents violate τ²'s stated confirm-before-mutating
  rule in trajectories the benchmark scores as solved. It encodes each domain's policy as a session type and
  replays the conversation against it, reporting 0.6% of τ²-passing runs. It also opened
  [tau2-bench#298](https://github.com/sierra-research/tau2-bench/issues/298) about it.
- **Cao, "Policy Loopholes in Agent Evaluation: When Policy Ambiguity Masquerades as Agent Error"**
  ([arXiv 2609.14400](https://arxiv.org/abs/2609.14400), 13 Sep 2026) had the thesis: natural-language
  policies admit "multiple defensible readings that a single gold trajectory cannot capture", and
  "policy specification quality sets the ceiling on evaluation quality". It audits the airline and retail
  domains, catalogues 17 loopholes (L1–L16 airline, R1–R2 retail), finds 7 of 50 airline tasks affected, and
  shows those tasks lower scores unevenly across models and make every model less consistent across repeated
  trials. Its two-factor mechanism — exploitability needs *both* policy ambiguity and tool permissiveness —
  is adjacent to a hypothesis this work pre-registered and then retired, on a different dependent variable.
- **[arXiv 2607.07405](https://www.alphaxiv.org/abs/2607.07405)** (Jul 2026) supplies the intervention:
  deterministic pre-call gates recover +12.4pp task success on τ² airline, with retail as a self-enforcing
  negative control.

**So what is left that is ours?** Narrower than the first draft of this document implied:

1. **A quantification of the spread.** Cao establishes that multiple readings exist and that affected tasks
   score unreliably; it measures score effects and consistency, not compliance rate under alternative
   encodings. The three-point curve below — the *same* rule, the *same* trajectories, three defensible
   readings of how long a confirmation lasts, differing by 35× in retail and 70× in airline — is a different
   measurement, and we have not found it published elsewhere.
2. **Reconciliation with an independent implementation.** We reproduce llmcontract's published numbers
   exactly by reimplementing their semantics in our harness, which is what turns "we disagree" into a
   calibration curve with their result as one of its points.
3. **Telecom.** Neither paper covers it. Our answer there is that it produced no usable signal, which is
   also a result.
4. **Reproducibility of every figure** from one command (see "Check these numbers yourself").

Cao's paper never mentions confirmation, which is llmcontract's rule, and llmcontract never quantifies the
spread. The contribution sits in the gap between them, and it is a contribution rather than a differentiator.

## What was run

τ²-bench publishes its own result files (`data/tau2/results/final/*.json`) — complete trajectories with the
reward the benchmark gave each one. We wrote an `agent.eval.yaml` for three domains from each domain's
published `policy.md` **before opening a single trajectory**, then ran `evalplane audit` over all of them:

**4,448 trajectories** — airline 800, retail 1,824, telecom 1,824 (the `_default` agent, plus `_base` for the
one model that only has that variant; the `no-user` and `op` ablations are excluded because they change the
agent's tool surface, not its policy compliance). No API keys, no model calls, no re-running the benchmark.

## Results

Rates are the share of **τ²-solved** trajectories (reward 1.0) in which the agent broke a rule stated in that
domain's `policy.md`. Evalplane's own always-on checks (`SCOPE`, `LOOP`, `ARGS`) are counted separately, below.

| Domain | Trajectories | τ² solved | Broke a policy | Solved *and* broke one | Lenient rate | Strict rate (`each: true`) |
|---|---:|---:|---:|---:|---:|---:|
| airline | 800 | 431 | 52 | **11** | **2.6%** | 8.6% |
| retail | 1,824 | 1,324 | 88 | **53** | **4.0%** | 18.3% |
| telecom | 1,824 | 773 | 29 | **5** | **0.6%** | 0.6% |

Each violation is labelled by whether the offending call actually happened — a tool that raised changed
nothing, and "tried to refund $900" is not "refunded $900":

| Domain | Took effect | Blocked by the tool |
|---|---:|---:|
| airline | 94 | 11 |
| retail | 122 | 15 |
| telecom | 24 | 13 |

An earlier draft reported airline as 103 effective and 3 blocked. That was wrong, and the error is worth
naming because it is the commonest kind in this document: it asked "did any call of this tool succeed in this
run?" rather than "did *the offending call* succeed?". Runs where the agent retried — one success followed by
three failures — had all their violations counted as effective. Per-call attribution gives 94/11.

**Read the last two columns together.** Whether one "yes" covers several calls, or each call needs its own,
moves the headline by **3–5×**. That single semantic choice matters more than the difference between models,
domains or environments, which is why both numbers are in the table rather than one in a footnote. The lenient
reading is primary here because it is the one that cannot accuse an agent that did ask.

## The finding that matters most

The obvious conclusion — "about 3% of τ²-passing runs break policy" — is **not a property of τ²**. It is a
joint property of the trajectories *and the profile we wrote*. Airline attaches a confirmation check to 3
irreversible tools; retail to 7. Hold that constant and the domains converge:

| Confirmation checks on the 3 nearest-equivalent irreversible tools | Solved *and* broke one | Rate |
|---|---:|---:|
| airline — book / cancel / change | 10 of 431 | **2.3%** |
| retail — cancel / modify-items / exchange | 27 of 1,324 | **2.0%** |

Near-identical. The entire retail-vs-airline gap was check-surface breadth, not agent behaviour. So:

- **The pattern replicates.** Confirmation-before-irreversible-action is the dominant policy failure in both
  domains, at an almost identical per-tool rate, across four models. That is a real cross-domain result.
- **Any single headline percentage is misleading** unless the profile is published next to it. A per-rule or
  per-tool rate is the honest unit.

The recurring shape, in every domain: the agent lists the change and its cost, asks a follow-up, the customer
answers *that question* — a payment method, a cancellation reason, an email address — and the agent treats the
answer as consent. Two retail cases, both τ²-scored 1.0, where `return_delivered_order_items` was called
immediately after the customer's turn, which was:

> `amelia.silva7872@example.com`

> "Are you kidding me? That's f\*\*\*ing stupid. Why can't you jus…"

## An independent implementation, and a 35× calibration curve

**Someone published this finding before us, and that should be stated up front.**
[llmcontract-tau2](https://github.com/chrisbartoloburlo/llmcontract-tau2) (first commit 2026-05-05, five
months before our airline run) audits the same published trajectories by a different method — it encodes each
domain's policy as a session type and replays the conversation against it — and reports **0.6%** of τ²-passing
trajectories violating "confirm before mutating", over 1,755 passing runs (retail 1,324 + airline 431). It
runs offline and reproduces: retail 0.8%, airline 0.2%.

So "agents break stated policies in runs τ² scores as solved" is **not our discovery**. What is ours is the
calibration curve below, which took two independent implementations disagreeing to produce.

**Their 0.6% and our 2.6% are not in conflict.** The difference is one documented modelling choice. Their
session type has no transition back to an idle state, so — in their own words — the first confirmation "flips
a one-way switch": one yes authorises every subsequent mutation in the conversation. Evalplane re-reads the
most recent user turn before each call. We ruled out the other explanations: their retail mutating set is
identical to ours, and their airline set is *broader* than ours (6 tools vs 3), so they check more surface and
still find 10× fewer. Reimplementing their semantics inside Evalplane, with their vocabulary and their tool
sets, reproduces their published numbers exactly.

That gives a calibration curve on identical data, holding tool sets and vocabulary at *theirs* and varying
only how long a confirmation lasts:

| Consent semantics | retail | airline |
|---|---:|---:|
| one yes authorises the rest of the conversation (llmcontract-tau2) | 0.8% | 0.2% |
| one yes lasts until the user speaks again (Evalplane default) | 4.2% | 3.7% |
| one yes per action (`each: true`) | 27.8% | 13.9% |

**A 35× spread in retail and 70× in airline, for three defensible readings of one policy sentence.** (These
use their tool sets, which is why the middle row differs from our 2.6% / 4.0% headline — that is the
check-surface effect again, in the same table.) Their own limitations section identifies this curve and says
the strict reading "would raise the violation rate substantially but is closer to the policy's literal
reading." The author picked a point on it knowingly; so have we. Anyone quoting a single number from either
project without saying which point they picked is quoting noise.

What *is* independently confirmed is the failure mode. Their airline example — the agent summarises the
change, the customer replies "I'd like to use the gift card with $280 available for payment", the agent calls
`update_reservation_passengers` — is the same pattern we found, reached by a different method. **The pattern
replicates; the rate is not comparable.**

One methodological note, since they were explicit about it: they rejected negation handling because a regex
over refusal words produces false positives, citing a reason field containing "no longer needed" tripping
`\bno\b` inside a clear confirmation. Evalplane handles both halves of that case — `Yes, the reason is "no
longer needed"` is consent, `the reason is no longer needed` is not — and there is a regression test with
those literal strings.

## Telecom produced no usable signal, and that is the instructive part

Telecom's first run showed **7.0%**, which would have been the best number in this document. It was wrong.
301 of its 302 `READ-BILL-BEFORE-REQUEST` violations came from one model, and **88% of them were false**: the
agent *had* read the bill, through telecom's generic `get_details_by_id` rather than the
`get_bills_for_customer` we had encoded. Allowing either getter (`prior:` now takes a list) takes that check
from 302 violations to 36, and the domain from 7.0% to **0.6%**.

What is left is 36 ordering violations and one refuel over the cap. **Three checks produced no usable signal**
— which is not evidence for the thesis. Saying so is the point of having written the falsification criterion
down first.

Telecom also killed a hypothesis we had pre-registered: that violation rates track how little the domain's
tools enforce. Telecom is the most permissive domain in τ² — `refuel_data` applies `gb_amount × price_per_gb`
to the customer's bill with no upper bound, and its line-status guard is commented out in the source — and it
has the **lowest** corrected rate. The 2GB cap exists only in prose, and across **538 `refuel_data` calls the
agents exceeded it once**: they respected an unenforced money cap 99.8% of the time. The hypothesis is retired — and there is a better one.

[arXiv 2607.07405](https://www.alphaxiv.org/abs/2607.07405) (Reddy, Challaram & Basu, Jul 2026) re-ran τ²
airline with four deterministic pre-call gates and recovered **+12.4pp task success on gpt-4o-mini** (29.6% →
42.0%, P=0.0012, replicated on a disjoint seed set), reporting that **78% of that agent's failures are silent
wrong-state with no tool error**. Their negative controls classify retail as self-enforcing and airline as
policy-permissive — the same split we reached by counting 26 versus 4 validation guards in the source. Their
numbers are not comparable to ours (different models, and a task-success delta rather than a violation rate),
but the mechanism explains the inversion: in a permissive environment a violation produces a silent wrong
state, and a wrong state tends to **fail the task**. So violations in permissive domains land disproportionately
in *failed* runs, which "solved and violating" excludes by construction. llmcontract's data shows the same
gradient independently — their violation rate among failing trajectories exceeds passing in both domains.

Which reframes what this metric is for: **"solved and violating" measures the procedural violations that
happen to be harmless enough that nothing catches them** — not the total rate, and not how permissive the
environment is. Those are the ones no benchmark and no policy check is looking for, which is the point.

## Where the violations land

Violations per 100 action-tool calls, split by the reward τ² gave the run. Computed against the profiles in
`examples/`, so this reproduces from the repo. Confidence intervals are log-Poisson on the event counts.

| Domain | In solved runs | In failed runs | Ratio failed/solved [95% CI] |
|---|---|---|---|
| airline | 23 / 525 (**4.38**) | 82 / 698 (11.75) | 2.68× [1.69, 4.26] |
| retail | 82 / 2,126 (**3.86**) | 55 / 898 (6.12) | 1.59× [1.13, 2.23] |
| telecom | 5 / 1,163 (0.43) | 32 / 1,891 (1.69) | direction only — 5 events is too few for a ratio |

**Violations are 1.6–3.9× more common in runs the benchmark failed**, in all three domains, which is consistent
with a broken rule corrupting state and costing the task. This replicates the same gradient llmcontract-tau2
found by their different method (retail 1.6% failing vs 0.8% passing; airline 1.4% vs 0.2%) — a replication,
not a discovery.

**And they still occur in passing runs at 3.9–4.4 per 100 action calls.** Those are the ones nothing catches:
not the environment, not the policy, not the benchmark's reward.

What we deliberately do **not** claim: that this establishes a mechanism. Observational data cannot separate
"violations cause failures" from a common cause, and the gating experiment that would settle it needs API keys
and is [arXiv 2607.07405](https://www.alphaxiv.org/abs/2607.07405)'s contribution, not ours. We also do not
rank the domains by how much their tools self-enforce: retail has the shallowest gradient, but that follows
from its rule mix (6 of its 7 contributing rules are confirmation rules, which have a shallower gradient:
1.75× [1.33, 2.30] versus 4.34× [2.27, 8.30] for read-before-write), not from its 26 validation guards. The
guard count predicts whether *gating improves task success*; it does not predict where audit violations land.
Those are two different questions and an earlier draft of this document conflated them.

> **A correction worth recording, because it nearly shipped.** The first version of this analysis concluded
> that confirmation violations are equally likely in solved and failed runs (1.22× [0.93, 1.59]) — a clean
> claim that "solved and violating" is exactly the population an outcome benchmark is blind to. It was wrong:
> the confirmation rate was computed against the all-domain action-call total, and telecom contributes 3,054
> action calls but zero confirmation violations (its only confirmation rule is held out), dragging the pooled
> ratio toward 1. Stratified by domain, confirmation violations are **also** concentrated in failed runs,
> 1.75×. The error surfaced only because the pooled figure disagreed with every per-domain figure.

## What τ²'s reward structurally cannot see

These are Evalplane's own always-on checks, not `policy.md` rules, so they are excluded from the rates above.

- **Excess agency, telecom: 146 attempts by the agent to call the customer's own device tools** —
  `reboot_device`, `toggle_data`, `reseat_sim_card`, `can_send_mms` and others that belong to the user
  simulator. **Every one errored**, so nothing happened, so no reward signal exists for any of them. An agent
  reaching outside its granted tool surface is OWASP ASI02/ASI03 behaviour, and it is visible only because
  Evalplane checks calls against the tools you declared.
- **Repetition loops:** telecom 92, airline 1, retail 0.

## What this exercise got wrong

Four of our own bugs, in order of how badly they would have misled a reader. All are now regression-tested.

1. **One confirmation was consumed per call.** A customer who says "yes, downgrade all five" confirmed five
   updates; the check credited the first and reported four violations — 238 of ~301 hits were this artifact.
   One yes now covers the calls made before the customer speaks again; `each: true` is the strict reading.
2. **A single `prior` could not express a disjunction.** The telecom error above: 88% false positives on one
   check, which by itself would have moved a published figure by more than 10×.
3. **The affirmation vocabulary was too narrow** — "That's fine" and "sounds good" were not agreement, so
   agents that *did* ask were accused of not asking.
4. **Then widening it made refusals read as consent.** "No, do not proceed" matched on `proceed`. That error
   runs the other way: it hides violations, so earlier counts were a floor. Fixing it moved airline from 49
   rule-breaking trajectories to 53 and from 89 violations to 106, and left the headline unchanged.

Worth stating plainly: **every number in the first draft of this document was wrong in a direction that
flattered us, and each correction came from running the checks against more data.** A lexical test on "did the
user agree?" has a false-positive rate you have to measure, not assume. Where the reading is genuinely subtle,
use a judge ([docs/judges.md](judges.md)) rather than a keyword list.

## How much of this would a human agree with?

20 retail confirmation violations from τ²-solved runs were read by hand. **No clear false positives; 5 of 20
(25%) are borderline** — the customer issued a directive ("Please change my address to…", "I'm fine with the
extra cost") rather than confirming details the agent had listed. Under the policy's literal wording ("list
the action details and obtain explicit user confirmation (yes)") those are violations; under a looser reading
they are not. So read the rates with this attached: **roughly a quarter of flagged cases are a user directive
rather than user silence**, which is materially different from what "called without confirmation" suggests.

## Check these numbers yourself

Every figure in this document lives in [benchmark-figures.json](benchmark-figures.json), and one command
recomputes all of them and fails on any disagreement:

```bash
git clone --depth 1 https://github.com/sierra-research/tau2-bench
python scripts/verify-benchmark.py --tau2 ./tau2-bench
```

40 figures, currently all agreeing. It exists because four published figures here turned out wrong when
someone checked them properly, and every one was caught by a human reading rather than by anything
mechanical. The JSON also lists what the script does **not** check — the calibration curve, the confidence
intervals, the hand-verified sample — so the absence of a failure is not a claim about those.

## Reproduce it

```bash
git clone --depth 1 https://github.com/sierra-research/tau2-bench.git
cd /path/to/evalplane/examples/tau2-airline
evalplane audit /path/to/tau2-bench/data/tau2/results/final/claude-3-7-sonnet-20250219_airline_default_gpt-4.1-2025-04-14_4trials.json
```

τ² result files are detected automatically — `audit`, `run --traces`, `review` and `promote` all take them
directly, and `metadata.reward` carries the benchmark's own verdict, so you can slice by it:

```python
from evalplane.integrations import load_tau2

runs = load_tau2("...4trials.json")
solved = [r for r in runs if r.metadata.get("reward") == 1.0]
```

## Limits, stated plainly

- **Airline's 11 policy violations plus 1 repetition loop are the 12 this document reported previously.** The
  split is new; the result has not changed.
- **Not every policy is checkable.** Airline encodes 7 of 9 stated rules; retail 4 of 15; telecom 3 of 20+.
  Six retail rules are enforced by the environment in code, so they cannot be broken silently; others need a
  judge, or a check type that does not exist yet. The violation counts are a floor, not a ceiling.
- **Blocked attempts are labelled, not filtered.** An attempt the tool refused is still a defect in the agent,
  so it is reported — but it fails the gate only when the tool is irreversible and the agent is T4, where
  trying to move money is enough. Everywhere else it warns, because failing a release over damage that
  provably did not occur is how a gate gets switched off.
- **A policy violation is not automatically a failure.** It means a stated rule was broken and nothing in the
  benchmark looks for it. The point is to make it visible and let a human decide.
- τ²'s reward already includes judge-graded assertions; this is not "the benchmark is only a DB check".
- A near-zero rate in a defensively coded domain is **a bound on what this tool can do for you**, not a win.
  Retail's 7 write tools carry 26 validation guards; where the environment enforces a rule, an offline policy
  audit has nothing left to find.
