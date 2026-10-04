# Validation log: what was tested against reality, and what it got wrong

This project's claims were checked against real systems rather than only against its own tests, and every
check found something. This log records what, including the parts that were wrong — the corrections are the
most useful thing here, so they are not quietly folded into a changelog.

## Checked against real systems

| What it was run against | What that found |
|---|---|
| **τ²-bench**, 4,448 published trajectories, three domains | The study in [benchmark-tau2.md](benchmark-tau2.md), and four bugs in our own checks (below) |
| **A real LangSmith project** (40 traces) | Traces whose root *is* the retriever imported as zero-step runs; `list_runs` deprecated in langsmith 0.13 |
| **The GitHub Action, on a real pull request** | `pip install -e .` ran inside `working-directory`, so the install silently targeted the wrong directory |
| **A real LangGraph `StateGraph`** | Confirmed `from_messages` works on live LangChain objects, not just recorded JSON |
| **Agents using the tool for the first time**, three rounds | ~25 friction points, all fixed: `init` now scans your code for tools; each requirement is blamed only on the checks that relate to it; injection checks gate CI rather than pre-release |
| **A skeptical review told to find what to cut** | A credibility bug: coverage could be claimed without being tested. See "honest coverage" below |
| **A PM review told to find overclaims** | Five documentation claims the code did not support, and the `init -y` bug below |

## Bugs in our own checks, found by running them on more data

Each of these produced a published number that was wrong. All are regression-tested.

1. **One confirmation consumed per call.** A customer saying "yes, downgrade all five" confirmed five
   updates; the check credited the first and reported four violations — 238 of ~301 hits were this artifact.
   One yes now covers the calls made before the customer speaks again; `each: true` is the strict reading.
2. **The affirmation vocabulary was too narrow.** "That's fine" and "sounds good" were not read as
   agreement, so agents that *did* ask were accused of not asking.
3. **Widening it made refusals read as consent.** "No, do not proceed" matched on `proceed`. That error hides
   violations rather than inventing them, so the published counts had been a floor.
4. **A single `prior` could not express a disjunction.** On τ² telecom, agents read bills through a generic
   getter rather than the one encoded, so the check reported 302 violations of which **88% were false**.
5. **Blocked attempts counted as completed actions.** The first effect split was measured by asking "did any
   call of this tool succeed in this run?" instead of "did the offending call succeed?", so retry runs
   counted every violation as effective. Per-call attribution moved airline from 103/3 to 94/11.

The response to this pattern is [benchmark-figures.json](benchmark-figures.json) plus
`scripts/verify-benchmark.py`, which recomputes all 40 published figures from the corpus and fails on any
disagreement. It exists because every one of the above was caught by a human reading, not by anything
mechanical.

## Design decisions that came from being wrong

- **Honest coverage.** An explicit `covers:` claim counts only if the case has the matching kind of
  assertion; judge-graded requirements read "unverified" unless a judge actually ran; `pass^k` needs
  `repeat >= k`. Before this, a suite could claim coverage it did not have.
- **Counts instead of a grade.** Four plain counts (must-have evals covered, action tools with no
  "should NOT act" test, policies no case tries to break, policies with no code check) replaced a letter
  grade that flattered the result.
- **Thresholds state their own resolving power.** With `n` covering cases the best rate below 1.0 is
  `(n-1)/n`, so a 0.98 threshold needs 50 cases to differ from "all must pass". On the bundled example, 19
  of 28 rate requirements are below that, and each line now says so.
- **Precise blame.** A failing injection case no longer marks tool selection and budgets as failed.
- **`init` always scans.** `init -y` used to skip the scan and fall back to the pack's example tools: run
  beside an agent whose tools include `delete_record`, it reported "Risk tier: T1 (read-only)".
- **Output-level checks audit, they do not enforce.** Runtime Guard runs before each call, when there is no
  answer yet, so `output_must_match` and `output_forbidden_patterns` can only be checked afterwards. The
  naive version reported "the answer does not match" on every call and would have denied all of them.

## What is not claimed

- The τ² finding is not ours. [llmcontract-tau2](https://github.com/chrisbartoloburlo/llmcontract-tau2)
  published it five months earlier and [Cao's paper](https://arxiv.org/abs/2609.14400) had the ambiguity
  thesis six days before our first run. [benchmark-tau2.md](benchmark-tau2.md) states what is left that is ours.
- `fetch_langfuse` has never been run against a live Langfuse account; the file importers are the tested path.
- No judge calibration figures are published here, because no judge has been calibrated against expert
  labels on a real suite.
