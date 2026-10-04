"""τ²-bench simulation results -> Evalplane runs (one per simulation).

Input: a τ²-bench results file (`data/tau2/results/**.json`, or the output of `tau2 run`), which holds
`{"info": ..., "tasks": [...], "simulations": [...]}`. Each simulation is one episode: `messages` in order,
`task_id`, `trial`, and `reward_info.reward` (the benchmark's own verdict, 1.0 = solved).

Mapping: assistant `tool_calls` -> `tool` steps, with the result taken from the `tool` message that carries
the same id (`error: true` -> status error); assistant text -> an `llm` step (model from `info.agent_info`,
tokens and cost when the record has them); `user` messages -> `user` steps, so conversation-aware checks
(`requires_user_confirmation`) see what the customer actually said. The first user message is the run's
input and the last assistant text its output. `case_id` is the τ² task id, so runs group by task across
trials, and `metadata` keeps `reward`, `trial`, `termination_reason` and the models, which lets you compare
what Evalplane finds against the benchmark's own score:

    runs = load_tau2("results/claude-3-7-sonnet_airline_default_4trials.json")
    solved = [r for r in runs if r.metadata.get("reward") == 1.0]

Only tool calls whose `requestor` is the assistant become steps: in domains where the user simulator has
tools of its own (telecom), those are the customer's actions, not the agent's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..trace import LLMCall, Run, ToolCall, UserTurn
from ._common import as_args, maybe_json, pick, read_records


def is_tau2(data: Any) -> bool:
    if isinstance(data, dict) and isinstance(data.get("simulations"), list):
        return True
    records = data if isinstance(data, list) else []
    return any(isinstance(r, dict) and "reward_info" in r and isinstance(r.get("messages"), list)
               for r in records[:5])


def _simulations(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("simulations"), list):
        return [s for s in data["simulations"] if isinstance(s, dict)]
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict) and isinstance(s.get("messages"), list)]
    return []


def _models(data: Any) -> tuple[str | None, str | None]:
    """(agent model, user-simulator model) from the run's `info` block, when it is there."""
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None, None

    def llm_of(key: str) -> str | None:
        block = info.get(key)
        return block.get("llm") if isinstance(block, dict) else None

    return llm_of("agent_info"), llm_of("user_info")


def _text(m: dict[str, Any]) -> str:
    c = m.get("content")
    if isinstance(c, list):  # content blocks
        return "".join(b.get("text", "") for b in c if isinstance(b, dict))
    return c if isinstance(c, str) else ""


def _results_by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(m.get("id")): m for m in messages if m.get("role") == "tool" and m.get("id")}


def _simulation_to_run(sim: dict[str, Any], agent_model: str | None, user_model: str | None) -> Run:
    messages = [m for m in sim.get("messages") or [] if isinstance(m, dict)]
    results = _results_by_id(messages)
    reward_info = sim.get("reward_info") if isinstance(sim.get("reward_info"), dict) else {}

    run = Run(run_id=str(sim.get("id") or f"{sim.get('task_id')}-{sim.get('trial')}"),
              case_id=str(sim["task_id"]) if sim.get("task_id") is not None else None,
              attempt=int(sim.get("trial") or 0), agent="tau2-agent",
              metadata={"source": "tau2"})
    for key, value in (("reward", reward_info.get("reward")), ("trial", sim.get("trial")),
                       ("termination_reason", sim.get("termination_reason")),
                       ("agent_cost", sim.get("agent_cost")), ("agent_model", agent_model),
                       ("user_model", user_model)):
        if value is not None:
            run.metadata[key] = value
    if sim.get("duration") is not None:
        run.latency_ms = float(sim["duration"]) * 1000

    first_user = True
    for m in messages:
        role, text = m.get("role"), _text(m)
        if role == "user":
            if first_user:  # the opening request is the run's input; later turns are steps
                run.input, first_user = text, False
            else:
                run.steps.append(UserTurn(text=text))
        elif role == "assistant":
            if text:
                run.output = text
            calls = [c for c in (m.get("tool_calls") or []) if isinstance(c, dict)]
            usage = m.get("usage") if isinstance(m.get("usage"), dict) else {}
            run.steps.append(LLMCall(
                model=agent_model, output=text or None,
                input_tokens=pick(usage, "prompt_tokens", "input_tokens"),
                output_tokens=pick(usage, "completion_tokens", "output_tokens"),
                cost_usd=float(m["cost"]) if m.get("cost") is not None else None,
            ))
            for call in calls:
                if call.get("requestor") not in (None, "assistant"):
                    continue  # a tool the simulated user called, not an action the agent took
                result = results.get(str(call.get("id")))
                errored = bool(result and result.get("error"))
                run.steps.append(ToolCall(
                    name=str(call.get("name") or "?"),
                    args=as_args(maybe_json(call.get("arguments"))),
                    result=maybe_json(_text(result)) if result else None,
                    status="error" if errored else "ok",
                    error=_text(result) if errored else None,
                ))
    if sim.get("termination_reason") in ("too_many_errors", "max_steps"):
        run.status = "error"
        run.error = str(sim["termination_reason"])
    return run


def tau2_to_runs(data: Any) -> list[Run]:
    """Convert already-parsed τ²-bench results to Evalplane runs, one per simulation."""
    agent_model, user_model = _models(data)
    return [_simulation_to_run(s, agent_model, user_model) for s in _simulations(data)]


def load_tau2(path: str | Path) -> list[Run]:
    """Load a τ²-bench results file (JSON or JSONL) as Evalplane runs, one per simulation."""
    return tau2_to_runs(read_records(path))
