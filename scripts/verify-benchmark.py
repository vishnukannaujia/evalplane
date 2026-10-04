#!/usr/bin/env python
"""Recompute every figure published in docs/benchmark-tau2.md and fail on any disagreement.

    python scripts/verify-benchmark.py --tau2 ~/src/tau2-bench

Needs a clone of https://github.com/sierra-research/tau2-bench (its published `data/tau2/results/final`).
Without one it reports what it would have checked and exits 0, so CI stays green.

Four published figures in that document turned out wrong when someone checked them properly, each caught by
a human reading rather than by anything mechanical. This is the mechanical part.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from evalplane.integrations import load_any  # noqa: E402
from evalplane.models import load_profile  # noqa: E402
from evalplane.runner import audit_traces  # noqa: E402

FIGURES = ROOT / "docs" / "benchmark-figures.json"
GLOBAL_CHECKS = {"SCOPE", "LOOP", "ARGS"}


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checked = 0

    def expect(self, what: str, got: object, want: object) -> None:
        self.checked += 1
        if got != want:
            self.failures.append(f"{what}: doc says {want!r}, recomputed {got!r}")
            print(f"  FAIL  {what}: doc says {want!r}, recomputed {got!r}")
        else:
            print(f"  ok    {what} = {got!r}")

    def close(self, rate: object, want: object, what: str) -> None:
        """A rate the doc prints to one decimal place."""
        self.checked += 1
        if abs(float(rate) - float(want)) > 0.05:  # one decimal place, so half of the last digit
            self.failures.append(f"{what}: doc says {want}, recomputed {rate:.2f}")
            print(f"  FAIL  {what}: doc says {want}, recomputed {rate:.2f}")
        else:
            print(f"  ok    {what} = {rate:.1f} (doc: {want})")


def load_domain(tau2: Path, figures: dict, domain: str) -> list:
    base = tau2 / figures["corpus"]["path"]
    files: list[str] = []
    for pattern in figures["corpus"][domain]:
        files += glob.glob(str(base / pattern))
    if not files:
        raise SystemExit(f"no {domain} result files under {base} (looked for {figures['corpus'][domain]})")
    return [r for f in sorted(set(files)) for r in load_any(f)]


def policy_violations(profile, runs, held_out: list[str], *, strict: bool = False) -> list:
    if strict:
        profile = profile.model_copy(deep=True)
        for pol in profile.policies:
            if pol.check and pol.check.type == "requires_user_confirmation":
                pol.check.each = True
    skip = GLOBAL_CHECKS | set(held_out)
    return [v for v in audit_traces(profile, runs) if v.policy not in skip]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau2", type=Path, help="Path to a tau2-bench clone.")
    args = ap.parse_args()
    figures = json.loads(FIGURES.read_text())

    if args.tau2 is None or not (args.tau2 / figures["corpus"]["path"]).is_dir():
        print(f"No τ²-bench corpus given, so nothing was verified. Clone it and re-run:\n"
              f"  git clone --depth 1 {figures['corpus']['source']}\n"
              f"  python scripts/verify-benchmark.py --tau2 ./tau2-bench\n\n"
              f"It would check {len(figures['results_table'])} domains, the effect split, the like-for-like\n"
              f"rates, the solved/failed gradient and {len(figures['scalars'])} scalars.")
        for why in figures["not_machine_checked"].values():
            print(f"  not covered: {why}")
        return 0

    r = Report()
    held = figures["corpus"]["held_out_policies"]
    cache: dict[str, tuple] = {}

    for domain, want in figures["results_table"].items():
        print(f"\n{domain}:")
        runs = load_domain(args.tau2, figures, domain)
        profile = load_profile(ROOT / "examples" / f"tau2-{domain}" / "agent.eval.yaml")
        cache[domain] = (runs, profile)
        solved = {x.run_id for x in runs if x.metadata.get("reward") == 1.0}
        v = policy_violations(profile, runs, held.get(domain, []))
        breaking = {x.run_id for x in v}

        r.expect(f"{domain} trajectories", len(runs), want["trajectories"])
        r.expect(f"{domain} τ²-solved", len(solved), want["solved"])
        r.expect(f"{domain} broke a policy", len(breaking), want["breaking"])
        r.expect(f"{domain} solved and breaking", len(breaking & solved), want["solved_and_breaking"])
        r.close(100 * len(breaking & solved) / len(solved), want["lenient_rate_pct"], f"{domain} lenient rate %")

        effect = Counter(x.effect for x in v)
        we = figures["effect_split"][domain]
        r.expect(f"{domain} took effect", effect["effective"], we["effective"])
        r.expect(f"{domain} blocked by the tool", effect["blocked"], we["blocked"])

    print("\nstrict (each: true) rates:")
    for domain, want in (("airline", 8.6), ("retail", 18.3)):
        runs, profile = cache[domain]
        solved = {x.run_id for x in runs if x.metadata.get("reward") == 1.0}
        v = policy_violations(profile, runs, held.get(domain, []), strict=True)
        r.close(100 * len({x.run_id for x in v} & solved) / len(solved), want, f"{domain} strict rate %")

    print("\nlike-for-like, confirmation checks on three irreversible tools:")
    for domain, want in figures["like_for_like_three_tools"].items():
        runs, profile = cache[domain]
        solved = {x.run_id for x in runs if x.metadata.get("reward") == 1.0}
        v = [x for x in policy_violations(profile, runs, held.get(domain, []))
             if "confirming first" in x.detail and any(t in x.detail for t in want["tools"])]
        hits = {x.run_id for x in v} & solved
        r.expect(f"{domain} like-for-like solved and breaking", len(hits), want["solved_and_breaking"])
        r.close(100 * len(hits) / len(solved), want["rate_pct"], f"{domain} like-for-like rate %")

    print("\nviolations per 100 action calls, by reward:")
    for domain, want in figures["gradient_per_100_action_calls"].items():
        runs, profile = cache[domain]
        actions = {t.name for t in profile.tools if t.is_action}
        v = policy_violations(profile, runs, held.get(domain, []))
        per_run = Counter(x.run_id for x in v)
        calls = Counter()
        hits = Counter()
        for run in runs:
            bucket = "solved" if run.metadata.get("reward") == 1.0 else "failed"
            calls[bucket] += sum(1 for s in run.steps if s.type == "tool" and s.name in actions)
            hits[bucket] += per_run.get(run.run_id, 0)
        for bucket in ("solved", "failed"):
            r.expect(f"{domain} {bucket} [violations, action calls]", [hits[bucket], calls[bucket]], want[bucket])

    print("\nscalars:")
    s = figures["scalars"]
    runs, profile = cache["telecom"]
    v_all = audit_traces(profile, runs)
    refuels = [c for run in runs for c in run.steps if c.type == "tool" and c.name == "refuel_data"]
    r.expect("telecom READ-BILL violations (any-of prior)",
             sum(1 for x in v_all if x.policy == "READ-BILL-BEFORE-REQUEST"),
             s["telecom_read_bill_violations_with_any_of_prior"])
    r.expect("telecom refuel_data calls", len(refuels), s["telecom_refuel_calls"])
    r.expect("telecom refuels over the 2GB cap",
             sum(1 for x in v_all if x.policy == "REFUEL-MAX-2GB"), s["telecom_refuel_over_cap"])
    r.expect("telecom undeclared-tool attempts",
             sum(1 for x in v_all if x.policy == "SCOPE"), s["telecom_undeclared_tool_attempts"])
    for domain, want in s["loops"].items():
        runs, profile = cache[domain]
        r.expect(f"{domain} repetition loops",
                 sum(1 for x in audit_traces(profile, runs) if x.policy == "LOOP"), want)

    print(f"\n{r.checked} figures checked, {len(r.failures)} disagreed with the document.")
    for f in r.failures:
        print(f"  - {f}")
    if r.failures:
        print("\nEither the code changed behaviour or the write-up is stale. Fix whichever is wrong, and if it is\n"
              "the write-up, say in it why the figure moved — that record is the point.")
    return 1 if r.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
