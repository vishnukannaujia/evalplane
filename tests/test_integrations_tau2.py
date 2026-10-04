"""τ²-bench results -> runs. The shapes here match real files in tau2-bench's data/tau2/results."""

from evalplane.integrations import detect_format, load_any, tau2_to_runs
from evalplane.trace import write_runs

RESULTS = {
    "info": {"num_trials": 2,
             "agent_info": {"llm": "claude-3-7-sonnet-20250219"},
             "user_info": {"implementation": "user_simulator", "llm": "gpt-4.1-2025-04-14"}},
    "tasks": [{"id": "39", "description": {"purpose": "cancellation"}}],
    "simulations": [{
        "id": "sim-1", "task_id": "39", "trial": 0, "duration": 12.5,
        "termination_reason": "user_stop", "agent_cost": 0.04,
        "reward_info": {"reward": 1.0, "db_check": {"db_match": True}},
        "messages": [
            {"role": "assistant", "content": "Hi! How can I help you today?", "tool_calls": None,
             "cost": 0.0, "usage": None},
            {"role": "user", "content": "Cancel reservation EHGLP3 please.", "tool_calls": None},
            {"role": "assistant", "content": "Let me look that up.", "cost": 0.01,
             "usage": {"prompt_tokens": 400, "completion_tokens": 20},
             "tool_calls": [{"id": "t1", "name": "get_reservation_details", "requestor": "assistant",
                             "arguments": {"reservation_id": "EHGLP3"}}]},
            {"id": "t1", "role": "tool", "content": '{"reservation_id": "EHGLP3"}', "requestor": "assistant",
             "error": False},
            {"role": "user", "content": "Yes, cancel it."},
            {"role": "assistant", "content": "Cancelled.", "cost": 0.02, "usage": None,
             "tool_calls": [{"id": "t2", "name": "cancel_reservation", "requestor": "assistant",
                             "arguments": {"reservation_id": "EHGLP3"}}]},
            {"id": "t2", "role": "tool", "content": "Error: already cancelled", "requestor": "assistant",
             "error": True},
        ],
    }],
}


def _run():
    return tau2_to_runs(RESULTS)[0]


def test_messages_become_steps_with_results_and_user_turns():
    r = _run()
    assert r.case_id == "39" and r.attempt == 0 and r.latency_ms == 12500
    assert r.input == "Cancel reservation EHGLP3 please."   # the opening request, not a step
    assert r.output == "Cancelled."
    kinds = [s.type for s in r.steps]
    assert kinds.count("user") == 1 and kinds.count("tool") == 2   # the later "Yes, cancel it." is a step

    tools = [s for s in r.steps if s.type == "tool"]
    assert [t.name for t in tools] == ["get_reservation_details", "cancel_reservation"]
    assert tools[0].args == {"reservation_id": "EHGLP3"} and tools[0].result == {"reservation_id": "EHGLP3"}
    assert tools[1].status == "error" and "already cancelled" in tools[1].error

    llm = [s for s in r.steps if s.type == "llm"]
    assert llm[0].model == "claude-3-7-sonnet-20250219"
    assert llm[1].input_tokens == 400 and llm[1].output_tokens == 20 and llm[1].cost_usd == 0.01


def test_metadata_keeps_the_benchmark_verdict():
    """The τ² reward is what makes "solved by the benchmark, yet broke a policy" answerable."""
    r = _run()
    assert r.metadata["reward"] == 1.0 and r.metadata["source"] == "tau2"
    assert r.metadata["agent_model"] == "claude-3-7-sonnet-20250219"
    assert r.metadata["user_model"] == "gpt-4.1-2025-04-14"


def test_user_simulator_tool_calls_are_not_the_agents_actions():
    data = {**RESULTS, "simulations": [{**RESULTS["simulations"][0], "messages": [
        {"role": "user", "content": "check my bill",
         "tool_calls": [{"id": "u1", "name": "open_app", "requestor": "user", "arguments": {}}]},
    ]}]}
    assert [s.type for s in tau2_to_runs(data)[0].steps] == []


def test_detected_and_loaded_from_a_file(tmp_path):
    import json

    p = tmp_path / "results.json"
    p.write_text(json.dumps(RESULTS))
    assert detect_format(RESULTS) == "tau2"
    assert len(load_any(p)) == 1

    # and the converted runs round-trip through the native format, which is how you'd hand them to `audit`
    out = tmp_path / "runs.jsonl"
    write_runs(out, tau2_to_runs(RESULTS))
    assert [r.case_id for r in load_any(out)] == ["39"]
