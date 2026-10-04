"""Error analysis on real traces: look at runs, label failures, see which failure modes are common.

    evalplane review traces/           # label runs: g(ood) / b(ad) <label> / s(kip) / q(uit)
    evalplane promote traces/ --reviewed

Runs that broke a rule (policy checks, scope, loops) or errored are shown first. Labels are free text
("wrong recipient", "made up a policy"); the summary groups them so the most common failure modes
become your first test cases.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from pathlib import Path

from .models import AgentProfile
from .scorers import global_violations
from .trace import Run, UserTurn

REVIEW_FILE = "review.jsonl"


def load_reviews(results_dir: Path) -> dict[str, dict]:
    f = results_dir / REVIEW_FILE
    if not f.exists():
        return {}
    out = {}
    for line in f.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["run_id"]] = r  # the latest verdict for a run wins
    return out


def save_review(results_dir: Path, entry: dict) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    with (results_dir / REVIEW_FILE).open("a") as f:
        f.write(json.dumps(entry) + "\n")


def order_for_review(runs: list[Run], profile: AgentProfile | None, sample: int, seed: int = 0) -> list[Run]:
    """Rule breakers and errors first, then a random sample of the rest."""
    flagged, rest = [], []
    for r in runs:
        bad = r.status != "ok" or (profile is not None and global_violations(r, profile))
        (flagged if bad else rest).append(r)
    random.Random(seed).shuffle(rest)
    return (flagged + rest)[:sample]


def summarize_run(run: Run, profile: AgentProfile | None, width: int = 110) -> list[str]:
    def cut(x) -> str:
        s = x if isinstance(x, str) else json.dumps(x, default=str)
        s = " ".join(s.split())
        return s if len(s) <= width else s[: width - 1] + "…"

    lines = [f"input:  {cut(run.input)}"]
    for s in run.steps:
        if isinstance(s, UserTurn):
            lines.append(f"  user: {cut(s.text)}")
        elif getattr(s, "type", "") == "tool":
            status = "" if s.status == "ok" else f" [{s.status}]"
            lines.append(f"  tool: {s.name}({cut(s.args)}){status}")
            if s.result is not None:
                lines.append(f"     -> {cut(s.result)}")
    lines.append(f"output: {cut(run.output_text)}")
    if run.status != "ok":
        lines.append(f"ERROR:  {cut(run.error or run.status)}")
    for v in (global_violations(run, profile) if profile else []):
        lines.append(f"RULE:   {v.policy}: {cut(v.detail)}")
    return lines


_STOP = {"the", "a", "an", "to", "of", "on", "in", "for", "and", "or", "without", "with", "by", "was", "is", "it",
         "user", "agent", "wrong"}


def _tokens(label: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", label.lower())
    stem = lambda w: re.sub(r"(ing|ed|es|s)$", "", w) if len(w) > 4 else w  # noqa: E731
    return {stem(w) for w in words if w not in _STOP} or set(words)


def failure_modes(reviews: dict[str, dict]) -> list[tuple[str, int]]:
    """Bad-run labels grouped into failure modes: near-duplicate wordings ("booked without confirmation",
    "booked w/o confirming") count as one mode, named after its most common wording."""
    labels = [(r.get("label") or "unlabelled").strip().lower() for r in reviews.values() if r.get("verdict") == "bad"]
    groups: list[list[str]] = []
    for lab in sorted(labels, key=labels.count, reverse=True):
        t = _tokens(lab)
        for g in groups:
            gt = _tokens(g[0])
            if t and gt and len(t & gt) / len(t | gt) >= 0.5:
                g.append(lab)
                break
        else:
            groups.append([lab])
    out = [(Counter(g).most_common(1)[0][0], len(g)) for g in groups]
    return sorted(out, key=lambda x: -x[1])
