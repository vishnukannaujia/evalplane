"""Compare two result files, e.g. before and after a model, prompt or tool change (re-certification).

    evalplane run -q && cp .evalplane/latest.json before.json
    # ...change the model or prompt...
    evalplane run -q && evalplane compare before.json .evalplane/latest.json
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

from .runner import Results

_BAD = ("fail", "error")


class Change(BaseModel):
    id: str
    before: str
    after: str
    detail: str = ""


class Comparison(BaseModel):
    before_commit: str | None = None
    after_commit: str | None = None
    plan_changed: bool = False
    regressions: list[Change] = Field(default_factory=list)  # passed before, fails now
    fixes: list[Change] = Field(default_factory=list)  # failed before, passes now
    requirement_regressions: list[Change] = Field(default_factory=list)
    requirement_fixes: list[Change] = Field(default_factory=list)
    new_violations: list[str] = Field(default_factory=list)
    gone_violations: list[str] = Field(default_factory=list)
    pass_rate_before: float | None = None
    pass_rate_after: float | None = None

    @property
    def regressed(self) -> bool:
        return bool(self.regressions or self.requirement_regressions or self.new_violations)


def _rate(r: Results) -> float | None:
    ran = [c for c in r.cases if c.n]
    return round(sum(c.c for c in ran) / sum(c.n for c in ran), 4) if ran else None


def compare(before: Results, after: Results) -> Comparison:
    out = Comparison(before_commit=before.git_commit, after_commit=after.git_commit,
                     plan_changed=before.plan_hash != after.plan_hash,
                     pass_rate_before=_rate(before), pass_rate_after=_rate(after))
    b = {c.case_id: c for c in before.cases}
    for c in after.cases:
        old = b.get(c.case_id)
        if old is None or c.status == "skip" or old.status == "skip":
            continue
        if old.status == "pass" and c.status in _BAD:
            out.regressions.append(Change(id=c.case_id, before=old.status, after=c.status, detail=c.reason))
        elif old.status in _BAD and c.status == "pass":
            out.fixes.append(Change(id=c.case_id, before=old.status, after=c.status))
        elif c.n and old.n and (c.c / c.n) < (old.c / old.n) - 1e-9:  # still passing overall? (pass^k repeats)
            out.regressions.append(Change(id=c.case_id, before=f"{old.c}/{old.n}", after=f"{c.c}/{c.n}",
                                          detail="fewer passing attempts"))
    rb = {r.id: r for r in before.requirements}
    for r in after.requirements:
        old = rb.get(r.id)
        if old is None:
            continue
        if old.status == "pass" and r.status == "fail":
            out.requirement_regressions.append(Change(id=r.id, before=old.status, after=r.status, detail=r.reason))
        elif old.status == "fail" and r.status == "pass":
            out.requirement_fixes.append(Change(id=r.id, before=old.status, after=r.status))
    key = lambda v: f"{v.policy} in {v.case_id}: {v.detail}"  # noqa: E731
    vb, va = Counter(key(v) for v in before.violations), Counter(key(v) for v in after.violations)
    out.new_violations = sorted((va - vb).elements())
    out.gone_violations = sorted((vb - va).elements())
    return out


def comparison_md(c: Comparison) -> str:
    def pct(x):
        return "n/a" if x is None else f"{x * 100:.0f}%"

    head = "❌ **Regressions found**" if c.regressed else "✅ **No regressions**"
    lines = [f"{head} · attempts passing {pct(c.pass_rate_before)} → {pct(c.pass_rate_after)}"]
    if c.before_commit or c.after_commit:
        lines.append(f"commits `{(c.before_commit or '?')[:8]}` → `{(c.after_commit or '?')[:8]}`")
    if c.plan_changed:
        lines.append("_agent.eval.yaml changed between the runs; requirement results may not be comparable._")
    for title, items in (("Cases that now fail", c.regressions), ("Requirements that now fail", c.requirement_regressions)):
        if items:
            lines += ["", f"**{title}:**", *[f"- `{i.id}` ({i.before} → {i.after}){': ' + i.detail if i.detail else ''}"
                                          for i in items]]
    if c.new_violations:
        lines += ["", "**New rule violations:**", *[f"- {v}" for v in c.new_violations[:20]]]
    if c.fixes or c.requirement_fixes or c.gone_violations:
        lines += ["", f"Fixed: {len(c.fixes)} cases, {len(c.requirement_fixes)} requirements, "
                      f"{len(c.gone_violations)} violations gone."]
    return "\n".join(lines) + "\n"
