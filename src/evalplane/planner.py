"""Turn an agent profile into an eval plan: which evals it needs, at which layer, and why."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .errors import ConfigError
from .models import (
    AgentProfile,
    Dimension,
    Layer,
    Method,
    Sensitivity,
    Stage,
    Tier,
    Waiver,
)
from .tiering import TierDecision, derive_tier

TIER_KEYS = ["T1", "T2", "T3", "T4"]


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    layer: Layer
    dimension: Dimension
    stage: Stage
    method: Method
    metric: str = "pass_rate"
    threshold: Any = None
    required: list[Tier] = Field(default_factory=list)
    recommended: list[Tier] = Field(default_factory=list)
    when: dict[str, Any] = Field(default_factory=dict)
    for_each: str | None = None
    where: dict[str, Any] = Field(default_factory=dict)
    must_where: dict[str, Any] = Field(default_factory=dict)  # per-tool: only matching tools are must, others should
    why: str = ""
    how: str = ""
    stub: str = "golden"
    owasp: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)
    recertify_on: list[str] = Field(default_factory=list)


class Requirement(BaseModel):
    id: str
    rule_id: str
    target: str | None = None
    title: str
    layer: Layer
    dimension: Dimension
    stage: Stage
    method: Method
    metric: str
    threshold: float | None = None
    k: int | None = None
    priority: str  # must | should
    why: str
    how: str
    stub: str
    owasp: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)
    waived: Waiver | None = None


class Plan(BaseModel):
    agent: str
    tier: Tier
    tier_reasons: list[str]
    pack: str
    catalog_version: str
    plan_hash: str
    requirements: list[Requirement]
    owasp_applicable: list[str]

    def get(self, req_id: str) -> Requirement | None:
        return next((r for r in self.requirements if r.id == req_id), None)

    @property
    def must(self) -> list[Requirement]:
        return [r for r in self.requirements if r.priority == "must" and not r.waived]


# --------------------------------------------------------------------------- catalog


def _rules_dir():
    return resources.files("evalplane") / "rules"


@lru_cache(maxsize=1)
def load_thresholds() -> dict[str, Any]:
    return yaml.safe_load((_rules_dir() / "thresholds.yaml").read_text())


@lru_cache(maxsize=1)
def load_owasp() -> dict[str, str]:
    return yaml.safe_load((_rules_dir() / "owasp_agentic_2026.yaml").read_text())


def load_core_catalog() -> tuple[str, list[Rule]]:
    data = yaml.safe_load((_rules_dir() / "core.yaml").read_text())
    return str(data["version"]), [Rule.model_validate(r) for r in data["rules"]]


def load_catalog(pack_id: str = "generic", base_dir: Path | None = None) -> tuple[str, list[Rule], dict]:
    """Core rules plus the pack's rules (a pack rule with the same id replaces the core rule)."""
    from .packs import get_pack

    version, rules = load_core_catalog()
    pack = get_pack(pack_id, base_dir=base_dir)
    by_id = {r.id: r for r in rules}
    for raw in pack.get("rules", []) or []:
        rule = Rule.model_validate(raw)
        by_id[rule.id] = rule
    return f"{version}+{pack['id']}", sorted(by_id.values(), key=lambda r: r.id), pack


# --------------------------------------------------------------------------- conditions


def _tool_matches(tool, flt: dict[str, Any]) -> bool:
    for key, want in flt.items():
        if key == "action":
            if tool.is_action != bool(want):
                return False
        elif key == "side_effect":
            wants = want if isinstance(want, list) else [want]
            if tool.side_effect.value not in wants:
                return False
        elif key == "sensitivity_at_least":
            if tool.data_sensitivity < Sensitivity(want):
                return False
        elif key in ("requires_approval", "untrusted_output", "reversible"):
            if bool(getattr(tool, key)) != bool(want):
                return False
        else:
            raise ConfigError(f"unknown tool filter '{key}'")
    return True


def _when(rule: Rule, profile: AgentProfile, tier: Tier) -> bool:
    for key, val in rule.when.items():
        if key == "tier_at_least":
            if tier < Tier(val):
                return False
        elif key == "features":
            names = val if isinstance(val, list) else [val]
            if not any(getattr(profile.features, n) for n in names):
                return False
        elif key == "any_tool":
            if not any(_tool_matches(t, val or {}) for t in profile.tools):
                return False
        elif key == "has_policies":
            if bool(profile.policies) != bool(val):
                return False
        elif key == "governance":
            if profile.risk.governance != bool(val):
                return False
        elif key == "has_model":
            if bool(profile.agent.model) != bool(val):
                return False
        elif key == "user_facing":
            if profile.agent.user_facing != bool(val):
                return False
        else:
            raise ConfigError(f"rule {rule.id}: unknown condition '{key}'")
    return True


# --------------------------------------------------------------------------- planning


def _resolve_threshold(rule: Rule, req_id: str, tier: Tier, profile: AgentProfile, pack: dict) -> float | None:
    for key in (req_id, rule.id, rule.threshold if isinstance(rule.threshold, str) else None):
        if key and key in profile.thresholds:
            return float(profile.thresholds[key])
    th = rule.threshold
    if isinstance(th, str):
        overrides = (pack.get("thresholds") or {}).get(th)
        table = overrides or load_thresholds().get(th)
        if table is None:
            raise ConfigError(f"rule {rule.id}: unknown named threshold '{th}'")
        return float(table[tier.value])
    if isinstance(th, dict):
        return float(th[tier.value])
    if isinstance(th, (int, float)):
        return float(th)
    return None


def build_plan(profile: AgentProfile, decision: TierDecision | None = None) -> Plan:
    decision = decision or derive_tier(profile)
    tier = decision.tier
    version, rules, pack = load_catalog(profile.risk.pack, base_dir=profile.base_dir)
    active_waivers = [w for w in profile.waivers if w.active()]
    reqs: list[Requirement] = []

    for rule in rules:
        if tier in rule.required:
            priority = "must"
        elif tier in rule.recommended:
            priority = "should"
        else:
            continue
        if not _when(rule, profile, tier):
            continue
        if rule.for_each == "tool":
            targets = [t.name for t in profile.tools if _tool_matches(t, rule.where)]
        elif rule.for_each == "policy":
            targets = [p.id for p in profile.policies]
        else:
            targets = [None]
        for target in targets:
            prio = priority
            if rule.must_where and rule.for_each == "tool" and prio == "must":
                tool = profile.tool(target)
                if tool is not None and not _tool_matches(tool, rule.must_where):
                    prio = "should"
            req_id = f"{rule.id}@{target}" if target else rule.id
            threshold = _resolve_threshold(rule, req_id, tier, profile, pack)
            k = load_thresholds()["pass_k_k"][tier.value] if rule.metric == "pass_hat_k" else None
            waiver = next((w for w in active_waivers if fnmatch.fnmatch(req_id, w.requirement)), None)
            reqs.append(
                Requirement(
                    id=req_id, rule_id=rule.id, target=target, title=rule.title, layer=rule.layer,
                    dimension=rule.dimension, stage=rule.stage, method=rule.method, metric=rule.metric,
                    threshold=threshold, k=k, priority=prio,
                    why=rule.why.replace("{name}", target or ""), how=rule.how.replace("{name}", target or ""),
                    stub=rule.stub, owasp=rule.owasp, controls=rule.controls, waived=waiver,
                )
            )

    reqs.sort(key=lambda r: (r.layer, r.id))
    owasp = sorted({o for r in reqs if r.priority == "must" for o in r.owasp})
    payload = json.dumps(
        {"profile": profile.model_dump(mode="json"), "catalog": version}, sort_keys=True, default=str
    )
    plan_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return Plan(
        agent=profile.agent.name, tier=tier, tier_reasons=decision.reasons, pack=pack["id"],
        catalog_version=version, plan_hash=plan_hash, requirements=reqs, owasp_applicable=owasp,
    )


def explain(req_id: str, plan: Plan) -> str:
    r = plan.get(req_id)
    if r is None:
        return f"{req_id} is not in this agent's plan."
    lines = [
        f"{r.id} [{r.priority.upper()}] {r.title}",
        f"  layer {r.layer.value} · {r.dimension.value} · stage {r.stage.value} · method {r.method.value}",
        f"  metric {r.metric}" + (f" (threshold {r.threshold:g})" if r.threshold is not None else ""),
        f"  why: {r.why}",
        f"  how: {r.how}",
    ]
    if r.metric == "attested":
        lines.append(f"  record it: evalplane attest {r.id} --by <you> --note \"<what you checked>\" "
                     "[--evidence <link>]")
    if r.owasp:
        names = load_owasp()
        lines.append("  OWASP Agentic: " + ", ".join(f"{o} {names.get(o, '')}" for o in r.owasp))
    if r.controls:
        lines.append("  controls: " + ", ".join(r.controls))
    return "\n".join(lines)
