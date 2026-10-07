# Evalplane in CI

## GitHub Action (recommended)

One step runs your cases, gates the PR on the agent's risk tier, writes the scorecard to the job summary,
keeps one sticky PR comment up to date, and uploads JUnit XML and an HTML report.

```yaml
# .github/workflows/evals.yml
name: evals
on: [pull_request]
permissions:
  contents: read
  pull-requests: write        # for the PR comment
jobs:
  evals:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: vishnukannaujia/evalplane@main
        with:
          install: pip install -e . evalplane   # your agent's deps + evalplane
          # run-args: --traces traces/          # replay recorded traces: no API keys in CI
```

A copy-ready version is in `.github/workflows/example-pr.yml` (not published in this repo — see the note above).

| Input | Default | What it does |
|---|---|---|
| `working-directory` | `.` | Directory with `agent.eval.yaml` |
| `stage` | `ci` | Gate stage: `design`, `ci`, `pre-release` |
| `python-version` | `3.12` | Python for `actions/setup-python` (`uv` is installed too) |
| `install` | `pip install evalplane` | Install command. If it creates `./.venv` (e.g. `uv sync`), that venv goes on `PATH` |
| `run-args` | | Extra `evalplane run` arguments, e.g. `--traces traces/` |
| `baseline` | `true` | On PRs, compute coverage on the base branch (in a `git worktree`) and list new gaps |
| `comment` | `true` | On PRs, create or update one comment (found by its `<!-- evalplane -->` marker) |
| `fail-on-new-gaps` | `false` | Fail the job if the PR adds a must-priority coverage gap |
| `github-token` | `github.token` | Token for the comment |

Outputs: `verdict` (PASS/WARN/FAIL), `gate-exit-code`, `junit` and `html` (report paths).

What it does, in order:

1. `evalplane run -q`. Failing cases don't stop the job here; the gate decides.
2. `evalplane gate --stage <stage> --format md`, appended to the job summary.
3. On pull requests: checks out the base branch in a worktree and runs `evalplane coverage --format json` there.
   If the base branch has no `agent.eval.yaml` yet (the PR that adds Evalplane), the new-gap report is skipped.
4. `evalplane report --format junit` and `--format html` into `.evalplane/reports/`, and the PR comment:
   verdict, counts, up to 10 failing cases and "New gaps introduced by this PR".
5. Uploads `.evalplane/results/`, `.evalplane/reports/` and `.evalplane/latest.json` as the
   `evalplane-results` artifact. `.evalplane/runs/` (full traces with tool arguments) is not uploaded.
6. Exits with the gate's exit code (and 1 on new must gaps when `fail-on-new-gaps: true`).

Pushes and other non-PR events skip the baseline and the comment. On PRs from forks the token is read-only,
so the comment step logs a warning instead of failing the job.

## Without the Action

```yaml
name: evals
on: [pull_request]
jobs:
  evals:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync
      # Run cases. Replay recorded traces in CI (--traces) so no API key is needed,
      # or call the live agent if your CI has the keys.
      - run: uv run evalplane run -q
        continue-on-error: true      # let the gate decide
      - run: uv run evalplane gate --stage ci --github-summary
      - if: always()
        uses: actions/upload-artifact@v4
        with: {name: evalplane-results, path: .evalplane/results/}
        # .evalplane/runs/ holds full run traces (inputs, tool arguments, your fake state). Upload it only
        # if your test data contains nothing sensitive.
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | PASS or WARN (use `--strict` to fail on WARN) |
| 1 | gate FAIL, or failing cases in `run` |
| 2 | invalid `agent.eval.yaml`, cases or arguments |

## "New gaps introduced by this PR"

Compare coverage against the base branch, so a PR that adds a risky tool without tests is caught:

```yaml
      - run: |
          git fetch origin ${{ github.base_ref }} --depth=1
          git worktree add /tmp/base origin/${{ github.base_ref }}
          (cd /tmp/base && uv run --project $GITHUB_WORKSPACE evalplane coverage --format json -o /tmp/base.json) || echo '{}' > /tmp/base.json
      - run: uv run evalplane coverage --baseline /tmp/base.json --fail-on-new-gaps >> $GITHUB_STEP_SUMMARY
```

Output:

```
Eval coverage: 93.3% → 84.9% (▼ -8.4)
New gaps introduced by this change:
- cancel_subscription: no case proves cancel_subscription is NOT called when it shouldn't be
- cancel_subscription: no case exercises cancel_subscription (T4)
```

## Recording traces once, replaying in CI

1. Locally (with API keys), run your agent inside `ep.record(save_to="traces/cases.jsonl", case_id=...)`,
   or run `evalplane run`, which writes every run to `.evalplane/runs/<timestamp>.jsonl`.
2. Commit the JSONL under `traces/`.
3. In CI: `evalplane run --traces traces/`. Cases are matched to runs by `case_id`.

Re-record when the model, prompt or tools change. The `L7.governance.recertify` requirement is there to remind you.

## Coverage badge

```bash
evalplane badge --out badges/evalplane.json
```

Commit the JSON (or publish it from CI), then use
`https://img.shields.io/endpoint?url=<raw URL of evalplane.json>` in your README.

## JUnit XML (GitLab, Jenkins, Azure DevOps)

```bash
evalplane report --format junit -o .evalplane/reports/junit.xml
```

One `<testsuite>` per eval suite with a `<testcase>` per case (`classname` = suite, `name` = case id). A failing
or crashed case gets a `<failure>` with its reason and the failing checks; a skipped case gets `<skipped>`.
A `requirements` suite has one test case per must requirement, failing when it failed, has no covering eval
(`missing`) or is `unverified`. Waived requirements are reported as skipped.

GitLab (shows failures in the merge request widget):

```yaml
evals:
  image: python:3.12
  script:
    - pip install evalplane -e .
    - evalplane run -q || true              # let the gate decide
    - evalplane report --format junit -o .evalplane/reports/junit.xml
    - evalplane report --format html -o .evalplane/reports/report.html
    - evalplane gate --stage ci
  artifacts:
    when: always
    reports:
      junit: .evalplane/reports/junit.xml
    paths:
      - .evalplane/reports/
```

Jenkins: `junit '.evalplane/reports/junit.xml'` in a `post { always { ... } }` block.

## HTML report

```bash
evalplane report --format html -o report.html
```

One self-contained file (inline CSS, no scripts, no external assets, works offline, follows the viewer's
light/dark setting): the gate verdict, coverage counts, failing cases with reasons, top gaps, and every
requirement grouped by layer with its result and gate verdict. Attach it as a CI artifact or send it to a
reviewer.

## Cucumber JSON

`evalplane report --format cucumber -o cucumber.json` writes standard Cucumber JSON (suites become features,
cases become scenarios, checks become steps). Feed it to the Jenkins `cucumber-reports` plugin,
`cucumber-html-reporter`, ReportPortal or any tool that already reads Cucumber output.

## pre-commit

Validate `agent.eval.yaml` and the cases before each commit:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/vishnukannaujia/evalplane
    rev: main            # pin a tag or commit
    hooks:
      - id: evalplane-validate
```

The hook runs `evalplane validate` (in pre-commit's own environment) when `agent.eval.yaml` or anything under
`evals/` changes. If your cases import your agent's code, use a local hook so it runs in your environment:

```yaml
- repo: local
  hooks:
    - id: evalplane-validate
      name: evalplane validate
      entry: evalplane validate
      language: system
      files: ^(agent\.eval\.yaml|evals/)
      pass_filenames: false
```
