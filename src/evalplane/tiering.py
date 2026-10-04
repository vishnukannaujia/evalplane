"""Derive an agent's risk tier from what its tools can do.

The tier is based on the agent's power, not its purpose: a read-only FAQ bot is T1,
an agent that can move money without a human is T4.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ConfigError
from .models import AgentProfile, Sensitivity, SideEffect, Tier, Tool


@dataclass
class TierDecision:
    tier: Tier
    derived: Tier
    declared: str
    per_tool: dict[str, Tier] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    overridden: bool = False


def tool_tier(tool: Tool) -> tuple[Tier, str]:
    """Tier for one tool, with a one-line reason. First matching rule wins."""
    se = tool.side_effect
    irreversible = se == SideEffect.irreversible or (tool.is_action and tool.reversible is False)
    if se == SideEffect.read:
        if tool.data_sensitivity >= Sensitivity.regulated:
            return Tier.T2, "reads regulated data"
        return Tier.T1, "read-only"
    if se == SideEffect.notify:
        return Tier.T2, "notifies your own people"
    if tool.requires_approval:
        if irreversible:
            return Tier.T3, "irreversible, but a human approves each call"
        return Tier.T2, "a human approves each call"
    if irreversible:
        return Tier.T4, f"{se.value} action that cannot be undone, with no human approval"
    if tool.data_sensitivity >= Sensitivity.regulated:
        return Tier.T4, "acts on regulated data with no human approval"
    return Tier.T3, "autonomous action that can be undone"


def derive_tier(profile: AgentProfile) -> TierDecision:
    per_tool: dict[str, Tier] = {}
    reasons: list[str] = []
    derived = Tier.T1
    for t in profile.tools:
        tier, why = tool_tier(t)
        per_tool[t.name] = tier
        reasons.append(f"{t.name}: {tier.value} ({why})")
        derived = max(derived, tier)
    if not profile.tools:
        reasons.append("no tools declared: treated as read-only (T1)")
    if profile.risk.regulated and any(t.is_action for t in profile.tools):
        if derived < Tier.T4:
            reasons.append("regulated domain with actions: raised to T4")
        derived = Tier.T4
    if profile.features.persistent_memory and derived < Tier.T2:
        reasons.append("persistent memory: raised to T2")
        derived = Tier.T2

    declared = profile.risk.tier
    tier = derived
    overridden = False
    if declared != "auto":
        d = Tier(declared)
        if d >= derived:
            tier = d
            if d > derived:
                reasons.append(f"declared {d.value} is stricter than derived {derived.value}: using {d.value}")
        else:
            if not profile.risk.override_reason:
                raise ConfigError(
                    f"risk.tier is {d.value} but the tools imply {derived.value}. "
                    "Set risk.override_reason to explain, or remove risk.tier to use the derived tier."
                )
            tier = d
            overridden = True
            reasons.append(f"OVERRIDE: declared {d.value} below derived {derived.value}: {profile.risk.override_reason}")
    return TierDecision(tier=tier, derived=derived, declared=declared, per_tool=per_tool,
                        reasons=reasons, overridden=overridden)
