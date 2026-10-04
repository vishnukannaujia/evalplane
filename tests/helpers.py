"""Small builders shared by the test modules (import with `from helpers import ...`)."""

from __future__ import annotations

from typing import Any

from evalplane.cases import Case
from evalplane.models import AgentProfile
from evalplane.trace import Approval, Handoff, LLMCall, Retrieval, Run, ToolCall


def make_profile(tools: list | None = None, policies: list | None = None, name: str = "test-agent",
                 **extra: Any) -> AgentProfile:
    data: dict[str, Any] = {"agent": {"name": name, "model": "test-model"}, "tools": tools or [],
                            "policies": policies or [], "risk": {"governance": True}}  # full plan in tests
    agent_extra = extra.pop("agent", None)
    if agent_extra:
        data["agent"].update(agent_extra)
    risk_extra = extra.pop("risk", None)
    if risk_extra:
        data["risk"].update(risk_extra)
    data.update(extra)
    return AgentProfile.model_validate(data)


def t1_profile(**extra: Any) -> AgentProfile:
    """Read-only agent: one plain read tool and one read tool whose output is untrusted."""
    return make_profile(
        tools=[{"name": "search"}, {"name": "fetch_web", "untrusted_output": True}],
        policies=[{"id": "NO-PII", "rule": "No PII", "check": {"type": "output_forbidden_patterns",
                                                                "patterns": ["email"]}}],
        **extra,
    )


def t3_profile(**extra: Any) -> AgentProfile:
    """Autonomous reversible actions: a read tool with untrusted output and a reversible write."""
    return make_profile(
        tools=[{"name": "lookup", "untrusted_output": True}, {"name": "update_ticket", "side_effect": "write"}],
        policies=[
            {"id": "VERIFY", "rule": "Look up first", "check": {"type": "requires_prior_tool",
                                                                 "tool": "update_ticket", "prior": "lookup"}},
            {"id": "BE-NICE", "rule": "Be nice", "severity": "low"},
        ],
        **extra,
    )


def t4_profile(**extra: Any) -> AgentProfile:
    return make_profile(
        tools=[{"name": "lookup"}, {"name": "pay", "side_effect": "irreversible"}],
        policies=[{"id": "LIMIT", "rule": "Max 100", "check": {"type": "max_arg", "tool": "pay",
                                                                "arg": "amount", "max": 100}}],
        **extra,
    )


def tc(name: str, status: str = "ok", **args: Any) -> ToolCall:
    return ToolCall(name=name, args=args, status=status)


def mk_run(*steps, output: Any = None, final_state: dict | None = None, status: str = "ok",
           case_id: str | None = None, latency_ms: float | None = None, error: str | None = None) -> Run:
    return Run(steps=list(steps), output=output, final_state=final_state, status=status, case_id=case_id,
               latency_ms=latency_ms, error=error)


def mk_case(id: str = "c1", **fields: Any) -> Case:
    return Case.model_validate({"id": id, **fields})


__all__ = [
    "Approval",
    "Handoff",
    "LLMCall",
    "Retrieval",
    "Run",
    "ToolCall",
    "make_profile",
    "mk_case",
    "mk_run",
    "t1_profile",
    "t3_profile",
    "t4_profile",
    "tc",
]
