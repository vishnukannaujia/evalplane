"""Evalplane: plan, cover, gate and prove evals for AI agents."""

__version__ = "0.1.0.dev0"

from .cases import Case, case, load_cases
from .guard import Guard
from .messages import from_messages
from .models import AgentProfile, Layer, Stage, Tier, load_profile
from .planner import Plan, Requirement, build_plan, explain
from .recorder import ToolDenied, approve, handoff, llm, record, retrieved, tool
from .scorers import expect
from .tiering import derive_tier
from .trace import (
    Approval,
    Handoff,
    LLMCall,
    Retrieval,
    Run,
    ToolCall,
    UserTurn,
    load_traces,
    read_runs,
    write_runs,
)

__all__ = [
    "AgentProfile",
    "Approval",
    "Case",
    "Guard",
    "Handoff",
    "LLMCall",
    "Layer",
    "Plan",
    "Requirement",
    "Retrieval",
    "Run",
    "Stage",
    "Tier",
    "ToolCall",
    "ToolDenied",
    "UserTurn",
    "__version__",
    "approve",
    "build_plan",
    "case",
    "derive_tier",
    "expect",
    "explain",
    "from_messages",
    "handoff",
    "llm",
    "load_cases",
    "load_profile",
    "load_traces",
    "read_runs",
    "record",
    "retrieved",
    "tool",
    "write_runs",
]
