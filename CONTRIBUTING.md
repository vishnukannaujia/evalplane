# Contributing

Thanks for helping. Evalplane is small on purpose: every feature should help someone find or fix a real
agent failure within minutes.

## Setup

```bash
uv sync
uv run pytest -q
cd examples/support-refund && ../../.venv/bin/evalplane run && ../../.venv/bin/evalplane gate
```

## Good first contributions

- **A sector pack**: a directory under `src/evalplane/packs/<id>/` with `pack.yaml` (sector rules, with sources),
  `agent.eval.yaml` (a template profile) and `cases.yaml` (starter cases). `evalplane init --pack <id>` must work,
  and `validate`, `plan` and `coverage` must run cleanly on the result.
- **A rule** in `src/evalplane/rules/core.yaml`: say why it matters, how to test it, and which OWASP Agentic
  risks or framework controls it maps to. Prefer rules that can be checked by code.
- **A trace importer** for a framework or platform (convert to `evalplane.trace.Run`).
- **A policy check type** in `scorers.py` (with tests).

## Rules of the road

- No network calls and no LLM calls in the core. Integrations are optional extras.
- Deterministic output: same inputs, same plan, same coverage, same gate.
- Tests for every behaviour change (`uv run pytest -q`).
- Sign off your commits (`git commit -s`, Developer Certificate of Origin).
