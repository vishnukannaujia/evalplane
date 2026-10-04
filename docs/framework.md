# The Evalplane Framework

No single framework says *which* evals to run at *which* layer of an agentic system.
Governance frameworks (NIST AI RMF / AI 600-1, ISO/IEC 42001, EU AI Act) say *that* you must measure risk.
Eval tools supply metrics. Evalplane connects the two with a matrix of four axes.

## Axis 1: Layer (what is being tested)

| Layer | What you test | Example metrics | Main method |
|---|---|---|---|
| **L0 Model** | The underlying LLM when you adopt or upgrade it | Capability on a domain test set, refusal rate, cost/latency per token | Private "model acceptance" suite |
| **L1 Prompt / component** | One LLM call (classifier, extractor, summariser) | Accuracy/F1, schema validity, format adherence | Code checks + golden datasets |
| **L2 Retrieval / knowledge** | RAG and memory | Context precision/recall, faithfulness, citation accuracy, staleness | Labelled question-to-document pairs |
| **L3 Tool / action** | Each tool the agent can call | Tool-selection accuracy, argument correctness, error recovery, permission compliance | Recorded tool calls, contract tests against mocks |
| **L4 Agent trajectory** | One agent working through a task | Task success, step efficiency, loop rate, policy adherence, pass^k | Sandboxed simulation, trajectory judge, final-state checks |
| **L5 Multi-agent / system** | Orchestration and handoffs | Handoff accuracy, which agent caused a failure, end-to-end latency, cost per task | End-to-end simulation with user simulators |
| **L6 Business outcome** | The agent inside a real workflow | Resolution rate, escalation rate, CSAT, cost per outcome vs. a human | Online A/B tests, canaries, expert review |
| **L7 Org / governance** | The whole portfolio of agents | Eval coverage, incidents that slipped past evals, judge–expert agreement, audit readiness | Dashboards, evidence packs |

Lower layers are cheap, fast and fixed, so run them on every commit. Higher layers are expensive and noisy, so run them before release and continuously in production.
Failures travel upward: an L3 argument bug shows up as "task failed" at L4. Without the lower-layer evals you can't diagnose the higher-layer ones.

## Axis 2: Dimension (what "good" means)

1. **Quality**: correctness, grounding, completeness
2. **Safety & security**: injection resistance, data leakage, harmful actions, excess agency
3. **Reliability**: pass^k consistency, recovery when a tool fails, robustness to odd inputs
4. **Efficiency**: cost, latency, steps, tokens
5. **Compliance**: following policies/SOPs, PII handling, logging, human approval where required

## Axis 3: Risk tier (how much evidence you need), based on what the agent can *do*

| Tier | Agent's power | Evals required |
|---|---|---|
| **T1** Informational | Read-only | L1–L2, basic safety, online quality sampling |
| **T2** Assistive | Drafts; a human approves | + L3–L4, human-review calibration |
| **T3** Autonomous, reversible | Writes to systems; changes can be undone | + pass^k thresholds, permission evals, red-teaming, canary |
| **T4** Irreversible / regulated | Money, legal, health, commitments | + independent review, policy-as-code on 100% of actions, evidence pack, kill switch, re-certify on any model change |

## Axis 4: Lifecycle stage (when it runs)

| Stage | Evals | Gate |
|---|---|---|
| Design | Failure-mode analysis, risk tiering, success criteria written before building | Eval plan approved |
| Development | L1–L3 unit evals, error analysis on traces | Developer feedback loop |
| CI | L1–L4 regression suite, safety smoke tests | Block the merge on regression |
| Pre-release | L4–L5 simulation, red-teaming, pass^k | Release sign-off by tier |
| Production | Online scoring, guardrails, drift, L6 KPIs | Alerts and automatic rollback |
| Change events | Model upgrade, prompt change, new tool | Re-run the full suite and re-certify |

## How to choose metrics

1. Define success in business terms first.
2. Do error analysis: read 50–100 traces and group the failures into categories.
3. Map each failure category to a layer.
4. Pick the cheapest reliable check: code assertion → reference comparison → LLM judge → human review.
5. Calibrate every LLM judge against expert labels (e.g. Cohen's κ) before trusting it.
6. Set thresholds by risk tier.
7. Feed production back in: every incident or escalation becomes a new test case.

## Who owns what

- **Central AI platform team:** tracing standard (OpenTelemetry), eval infrastructure, shared judges, simulated environments, dashboards
- **Product / agent teams:** domain test sets, failure analysis, L1–L6 evals, thresholds
- **Risk / compliance:** tier definitions, T3–T4 release gates, audit evidence
- **Domain experts:** labelling, judge calibration, reviewing sampled production output

**Maturity levels:** 1 ad-hoc → 2 golden test sets in CI → 3 trajectory simulation + online scoring → 4 tier gates, calibrated judges, production failures become tests → 5 organisation-wide coverage, audit-ready, re-certification on model change.
