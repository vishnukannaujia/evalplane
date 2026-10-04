"""MCP server: let coding agents (Claude Code, Cursor, ...) ask Evalplane about the agent they are building.

    pip install "mcp>=1.2"
    claude mcp add evalplane -- evalplane mcp

Tools are read-only: plan, coverage (with paste-ready cases), explain a requirement, validate, audit traces,
and preview generated temptation cases. Nothing is written to disk and no LLM is called.
"""

from __future__ import annotations

import json
from pathlib import Path

from .cases import load_cases
from .coverage import analyse
from .errors import EvalplaneError
from .models import load_profile
from .planner import build_plan, explain


def _ctx(profile_path: str):
    profile = load_profile(profile_path)
    plan = build_plan(profile)
    cases = load_cases(profile.evals.cases, base_dir=profile.base_dir)
    return profile, plan, cases


def tool_plan(profile_path: str = "agent.eval.yaml") -> str:
    """The evals this agent needs: risk tier (with reasons) and must/should requirements with why."""
    _, plan, _ = _ctx(profile_path)
    return json.dumps({
        "agent": plan.agent, "tier": plan.tier.value, "tier_reasons": plan.tier_reasons,
        "requirements": [{"id": r.id, "priority": r.priority, "title": r.title, "why": r.why, "how": r.how}
                         for r in plan.requirements if not r.waived],
    }, indent=2)


def tool_coverage(profile_path: str = "agent.eval.yaml", max_gaps: int = 10) -> str:
    """What the eval cases don't test yet, riskiest first, each with a paste-ready YAML case."""
    profile, plan, cases = _ctx(profile_path)
    cov = analyse(plan, profile, cases)
    return json.dumps({
        "counts": cov.counts, "score": cov.score,
        "gaps": [{"priority": g.priority, "ref": g.ref, "message": g.message, "suggested_case": g.suggestion}
                 for g in cov.gaps[:max_gaps]],
    }, indent=2)


def tool_explain(requirement: str, profile_path: str = "agent.eval.yaml") -> str:
    """Why one requirement applies to this agent and how to test it."""
    _, plan, _ = _ctx(profile_path)
    return explain(requirement, plan)


def tool_validate(profile_path: str = "agent.eval.yaml") -> str:
    """Check agent.eval.yaml and the eval cases; returns 'ok' or the problems."""
    try:
        profile, plan, cases = _ctx(profile_path)
    except (EvalplaneError, ValueError) as e:
        return f"invalid: {e}"
    return f"ok: {profile.agent.name}, tier {plan.tier.value}, {len(profile.tools)} tools, {len(cases)} cases"


def tool_audit(traces: str, profile_path: str = "agent.eval.yaml") -> str:
    """Check recorded runs (JSONL / OTel files or a directory) against every policy and tool rule."""
    from .runner import audit_traces, load_trace_paths

    profile = load_profile(profile_path)
    runs = load_trace_paths(profile, [traces])
    violations = audit_traces(profile, runs)
    return json.dumps({"runs": len(runs), "violations": [
        {"policy": v.policy, "detail": v.detail, "run_id": v.run_id, "case_id": v.case_id} for v in violations]},
        indent=2)


def tool_generate_preview(profile_path: str = "agent.eval.yaml") -> str:
    """Temptation cases (YAML) for untested policies and action tools; review before saving to evals/."""
    from .generate import generate

    profile, plan, cases = _ctx(profile_path)
    text, _ = generate(profile, analyse(plan, profile, cases), existing_ids={c.id for c in cases})
    return text


TOOLS = [tool_plan, tool_coverage, tool_explain, tool_validate, tool_audit, tool_generate_preview]


def build_server():
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        try:  # mcp 1.x
            from mcp.server.fastmcp import FastMCP as Server
        except ImportError as e:
            raise ImportError("the MCP server needs the `mcp` package: pip install 'mcp>=1.2'") from e
    server = Server("evalplane", instructions=(
        "Evalplane plans and checks evals for AI agents described in agent.eval.yaml. Use evalplane_coverage to "
        "find untested risky behaviour and paste its suggested cases into evals/; use evalplane_plan to see "
        "what an agent needs and why."))
    for fn in TOOLS:
        server.tool(name="evalplane_" + fn.__name__.removeprefix("tool_"), description=fn.__doc__)(fn)
    return server


def main(cwd: str | None = None) -> None:
    if cwd:
        import os

        os.chdir(Path(cwd))
    build_server().run("stdio")
