"""Core enums and the agent profile (`agent.eval.yaml`) schema."""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


class _OrderedEnum(str, Enum):
    @classmethod
    def _order(cls) -> list:
        return list(cls)

    def __lt__(self, other):  # type: ignore[override]
        if isinstance(other, type(self)):
            return self._order().index(self) < self._order().index(other)
        return NotImplemented

    def __le__(self, other):  # type: ignore[override]
        return self == other or self < other

    def __gt__(self, other):  # type: ignore[override]
        if isinstance(other, type(self)):
            return other < self
        return NotImplemented

    def __ge__(self, other):  # type: ignore[override]
        return self == other or self > other

    def __str__(self) -> str:
        return self.value


class Layer(_OrderedEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"
    L6 = "L6"
    L7 = "L7"


LAYER_NAMES = {
    Layer.L0: "Model",
    Layer.L1: "Prompt / component",
    Layer.L2: "Retrieval / memory",
    Layer.L3: "Tools / actions",
    Layer.L4: "Agent trajectory",
    Layer.L5: "Multi-agent",
    Layer.L6: "Business outcome",
    Layer.L7: "Governance",
}


class Dimension(_OrderedEnum):
    quality = "quality"
    safety = "safety"
    reliability = "reliability"
    efficiency = "efficiency"
    compliance = "compliance"


class Tier(_OrderedEnum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"


TIER_NAMES = {
    Tier.T1: "Informational (read-only)",
    Tier.T2: "Assistive (a human approves actions)",
    Tier.T3: "Autonomous, reversible actions",
    Tier.T4: "Irreversible or regulated actions",
}


class Stage(_OrderedEnum):
    design = "design"
    dev = "dev"
    ci = "ci"
    pre_release = "pre_release"
    production = "production"


class SideEffect(_OrderedEnum):
    read = "read"
    notify = "notify"  # alerts your own people (page on-call, post to your team's channel)
    write = "write"
    external = "external"
    irreversible = "irreversible"


class Sensitivity(_OrderedEnum):
    public = "public"
    internal = "internal"
    confidential = "confidential"
    personal = "personal"
    regulated = "regulated"


class Method(_OrderedEnum):
    code = "code"
    reference = "reference"
    judge = "judge"
    human = "human"


class Severity(_OrderedEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


_STRICT = ConfigDict(extra="forbid")


class Tool(BaseModel):
    model_config = _STRICT
    name: str = Field(pattern=r"^[A-Za-z_][\w.\-]*$")
    description: str = ""
    side_effect: SideEffect = SideEffect.read
    reversible: bool | None = None
    requires_approval: bool = False
    data_sensitivity: Sensitivity = Sensitivity.internal
    untrusted_output: bool | None = None
    args_schema: dict[str, Any] | None = None
    max_calls_per_run: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _defaults(self) -> Tool:
        if self.side_effect == SideEffect.irreversible and self.reversible:
            raise ValueError(f"tool '{self.name}': an irreversible tool cannot be reversible: true")
        if self.reversible is None:
            self.reversible = self.side_effect in (SideEffect.read, SideEffect.write, SideEffect.notify)
        if self.untrusted_output is None:
            # set true for tools that return text someone else wrote (web pages, emails, tickets, documents)
            self.untrusted_output = False
        return self

    @property
    def is_action(self) -> bool:
        return self.side_effect != SideEffect.read


class PolicyCheck(BaseModel):
    """A deterministic check that makes a natural-language policy testable.

    type is one of: max_arg, allowed_values, forbidden_tool, requires_prior_tool,
    requires_approval, max_calls, output_forbidden_patterns.
    """

    model_config = _STRICT
    type: Literal[
        "max_arg",
        "allowed_values",
        "forbidden_tool",
        "requires_prior_tool",
        "requires_approval",
        "max_calls",
        "output_forbidden_patterns",
        "output_must_match",
        "arg_forbidden_patterns",
        "arg_must_match",
        "requires_user_confirmation",
        "arg_matches_prior_result",
        "arg_from_user",
    ]
    field: str | None = None  # for arg_matches_prior_result: path into the prior tool's result, e.g. "email"
    pattern: str | None = None
    tool: str | None = None
    arg: str | None = None
    max: float | None = None
    values: list[Any] | None = None
    # a list means any one of them satisfies the requirement: real rules are disjunctions
    # ("authenticate by email *or* name+zip"), and one check per alternative fires on the unused paths
    prior: str | list[str] | None = None
    n: int | None = None
    # max_calls: count the limit per distinct value of this argument ("once per order_id") instead of
    # per run, which a multi-order conversation would otherwise trip
    per: str | None = None
    patterns: list[str] | None = None
    # requires_user_confirmation: by default one "yes" covers the calls the agent makes before the user
    # speaks again (a user who says "cancel all three" confirmed three calls). each: true demands a
    # separate confirmation for every call.
    each: bool = False


class Policy(BaseModel):
    model_config = _STRICT
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_\-]{1,31}$")
    rule: str
    severity: Severity = Severity.high
    check: PolicyCheck | None = None


class SuccessCriterion(BaseModel):
    model_config = _STRICT
    id: str
    description: str
    kind: Literal["task", "business"] = "task"
    metric: str | None = None
    target: float | None = None


class Waiver(BaseModel):
    model_config = _STRICT
    requirement: str
    reason: str
    approved_by: str
    expires: _dt.date | None = None

    def active(self, today: _dt.date | None = None) -> bool:
        return self.expires is None or self.expires >= (today or _dt.date.today())


class Attestation(BaseModel):
    model_config = _STRICT
    requirement: str
    by: str
    date: _dt.date
    evidence: str = ""
    note: str = ""


class AgentInfo(BaseModel):
    model_config = _STRICT
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{1,63}$")
    description: str = ""
    owner: str | None = None
    org: str = "local"
    team: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    external_ids: dict[str, str] = Field(default_factory=dict)
    framework: str = "custom"
    entrypoint: str | None = None
    model: str | None = None
    user_facing: bool = True


class RiskInfo(BaseModel):
    model_config = _STRICT
    tier: Literal["auto", "T1", "T2", "T3", "T4"] = "auto"
    override_reason: str | None = None
    pack: str = "generic"
    regulated: bool = False
    governance: bool = False  # add release governance: business metric, kill switch, re-certification, review


class Features(BaseModel):
    model_config = _STRICT
    retrieval: bool = False
    citations: bool = False
    memory: bool = False
    persistent_memory: bool = False
    multi_agent: bool = False
    code_execution: bool = False
    structured_output: bool = False


class EvalPaths(BaseModel):
    model_config = _STRICT
    cases: list[str] = Field(default_factory=lambda: ["evals"])
    traces: list[str] = Field(default_factory=list)
    results_dir: str = ".evalplane"


class AgentProfile(BaseModel):
    """The contents of `agent.eval.yaml`."""

    model_config = _STRICT
    version: Literal[1] = 1
    agent: AgentInfo
    risk: RiskInfo = Field(default_factory=RiskInfo)
    features: Features = Field(default_factory=Features)
    tools: list[Tool] = Field(default_factory=list)
    policies: list[Policy] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)
    evals: EvalPaths = Field(default_factory=EvalPaths)
    waivers: list[Waiver] = Field(default_factory=list)
    attestations: list[Attestation] = Field(default_factory=list)
    _base_dir: Path = PrivateAttr(default_factory=lambda: Path("."))

    @field_validator("tools")
    @classmethod
    def _unique_tools(cls, tools: list[Tool]) -> list[Tool]:
        names = [t.name for t in tools]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate tool names: {sorted(dupes)}")
        return tools

    @model_validator(mode="after")
    def _cross_refs(self) -> AgentProfile:
        ids = [p.id for p in self.policies]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate policy ids: {sorted(dupes)}")
        names = {t.name for t in self.tools}
        for p in self.policies:
            c = p.check
            if c is None:
                continue
            priors = [c.prior] if isinstance(c.prior, str) else list(c.prior or ())
            for ref in (c.tool, *priors):
                if ref and ref not in names:
                    raise ValueError(f"policy {p.id}: unknown tool '{ref}' (declare it under tools:)")
        return self

    @property
    def has_business_metric(self) -> bool:
        return any(s.kind == "business" and s.metric and s.target is not None for s in self.success_criteria)

    def tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    @property
    def base_dir(self) -> Path:
        return self._base_dir


def load_profile(path: str | Path = "agent.eval.yaml") -> AgentProfile:
    path = Path(path)
    if not path.exists():
        from .errors import ConfigError

        raise ConfigError(f"{path} not found. Run `evalplane init` to create one.")
    data = yaml.safe_load(path.read_text()) or {}
    profile = AgentProfile.model_validate(data)
    profile._base_dir = path.resolve().parent
    return profile


def dump_profile(profile: AgentProfile) -> str:
    data = profile.model_dump(mode="json", exclude_defaults=True)
    data = {"version": 1, **data}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
