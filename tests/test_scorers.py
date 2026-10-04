import pytest
from helpers import Approval, Handoff, LLMCall, Retrieval, make_profile, mk_case, mk_run, tc

from evalplane.scorers import (
    check_budgets,
    check_forbidden,
    check_judge,
    check_output,
    check_retrieved,
    check_state,
    check_status,
    check_tools,
    expect,
    global_violations,
    score_run,
)


def statuses(checks):
    return {c.name: c.status for c in checks}


# --------------------------------------------------------------------------- trajectory


@pytest.mark.parametrize("mode, expected, actual, ok", [
    ("exact", ["a", "b"], ["a", "b"], True),
    ("exact", ["a", "b"], ["a", "b", "c"], False),
    ("exact", ["a", "b"], ["b", "a"], False),
    ("in_order", ["a", "c"], ["a", "b", "c"], True),
    ("in_order", ["c", "a"], ["a", "b", "c"], False),
    ("in_order", ["a", "a"], ["a", "b"], False),
    ("any_order", ["b", "a"], ["a", "b", "c"], True),
    ("any_order", ["a", "a"], ["a", "b"], False),
    ("subset", ["a", "a", "b"], ["b", "a"], True),
    ("subset", ["d"], ["a"], False),
])
def test_trajectory_modes(mode, expected, actual, ok):
    case = mk_case(expect={"tools": {"mode": mode, "calls": [{"name": n} for n in expected]}})
    run = mk_run(*[tc(n) for n in actual])
    checks = check_tools(run, case)
    assert checks[0].name == "tools.trajectory"
    assert checks[0].status == ("pass" if ok else "fail")
    if not ok:
        assert "expected" in checks[0].reason


def test_no_tool_expectation_no_checks():
    assert check_tools(mk_run(tc("a")), mk_case()) == []


def test_denied_calls_do_not_count_for_trajectory():
    case = mk_case(expect={"tools": {"mode": "exact", "calls": [{"name": "a"}]}})
    assert check_tools(mk_run(tc("a"), tc("b", status="denied")), case)[0].status == "pass"


def test_handoffs_in_trajectory():
    case = mk_case(expect={"tools": ["lookup", "handoff:billing"]})
    run = mk_run(tc("lookup"), Handoff(to_agent="billing"))
    assert check_tools(run, case)[0].status == "pass"
    assert check_tools(mk_run(tc("lookup"), Handoff(to_agent="sales")), case)[0].status == "fail"


def test_args_matching():
    case = mk_case(expect={"tools": [{"name": "refund", "args": {"order_id": "A1", "amount": {"lte": 40}}},
                                     {"name": "email"}]})
    good = mk_run(tc("refund", order_id="A1", amount=30), tc("email"))
    assert statuses(check_tools(good, case)) == {"tools.trajectory": "pass", "tools.args[refund]": "pass"}
    bad = mk_run(tc("refund", order_id="A1", amount=50), tc("email"))
    checks = statuses(check_tools(bad, case))
    assert checks["tools.args[refund]"] == "fail"
    reason = check_tools(bad, case)[1].reason
    assert "no call to refund matched" in reason and "'amount': 50" in reason
    # any matching call among several satisfies the spec
    multi = mk_run(tc("refund", order_id="B", amount=1), tc("refund", order_id="A1", amount=1), tc("email"))
    assert statuses(check_tools(multi, case))["tools.args[refund]"] == "pass"


def test_forbidden():
    case = mk_case(expect={"forbidden_tools": ["refund", "email"]})
    s = statuses(check_forbidden(mk_run(tc("refund"), tc("email", status="denied")), case))
    assert s == {"forbidden[refund]": "fail", "forbidden[email]": "pass"}


# --------------------------------------------------------------------------- output / state / retrieval


def test_output_contains_and_not_contains():
    case = mk_case(expect={"output": {"contains": ["REFUNDED", "order"], "not_contains": "secret"}})
    assert all(c.status == "pass" for c in check_output(mk_run(output="I refunded your Order"), case))
    s = [c.status for c in check_output(mk_run(output="Secret order"), case)]
    assert s == ["fail", "pass", "fail"]
    sensitive = mk_case(expect={"output": {"contains": "Order", "case_sensitive": True}})
    assert check_output(mk_run(output="order"), sensitive)[0].status == "fail"
    assert check_output(mk_run(output="Order"), sensitive)[0].status == "pass"


def test_output_regex_and_equals():
    case = mk_case(expect={"output": {"regex": r"ticket ESC-\d+"}})
    assert check_output(mk_run(output="Your TICKET esc-12"), case)[0].status == "pass"
    assert check_output(mk_run(output="none"), case)[0].status == "fail"
    eq = mk_case(expect={"output": {"equals": {"total": {"gte": 10}, "ok": True}}})
    assert check_output(mk_run(output={"total": 12, "ok": True}), eq)[0].status == "pass"
    assert check_output(mk_run(output={"total": 2, "ok": True}), eq)[0].status == "fail"
    assert check_output(mk_run(output="x"), mk_case(expect={"output": {"equals": "x"}}))[0].status == "pass"


def test_output_json_schema():
    schema = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
    case = mk_case(expect={"output": {"json_schema": schema}})
    assert check_output(mk_run(output={"id": 1}), case)[0].status == "pass"
    assert check_output(mk_run(output='{"id": 2}'), case)[0].status == "pass"
    bad = check_output(mk_run(output={"id": "x"}), case)[0]
    assert bad.status == "fail" and "integer" in bad.reason
    notjson = check_output(mk_run(output="hello"), case)[0]
    assert notjson.status == "fail" and notjson.reason == "output is not JSON"


def test_no_output_expectation():
    assert check_output(mk_run(output="x"), mk_case()) == []


def test_final_state():
    case = mk_case(expect={"final_state": [
        {"path": "refunds.A1", "equals": 40},
        {"path": "refunds.A1", "match": {"gt": 10}},
        {"path": "escalations", "exists": False},
        {"path": "refunds", "exists": True},
        {"path": "missing.path", "equals": None},
    ]})
    s = [c.status for c in check_state(mk_run(final_state={"refunds": {"A1": 40.0}}), case)]
    assert s == ["pass", "pass", "pass", "pass", "fail"]
    s2 = [c.status for c in check_state(mk_run(final_state=None), case)]
    assert s2 == ["fail", "fail", "pass", "fail", "fail"]


def test_retrieved():
    case = mk_case(expect={"retrieved": ["d1", "d2"]})
    assert check_retrieved(mk_run(Retrieval(doc_ids=["d2", "d1", "d3"])), case)[0].status == "pass"
    miss = check_retrieved(mk_run(Retrieval(doc_ids=["d1"])), case)[0]
    assert miss.status == "fail" and "d2" in miss.reason
    assert check_retrieved(mk_run(), mk_case()) == []


def test_budgets():
    case = mk_case(expect={"budgets": {"max_steps": 3, "max_tool_calls": 1, "max_cost_usd": 0.05,
                                       "max_latency_ms": 100}})
    run = mk_run(tc("a"), LLMCall(cost_usd=0.03), LLMCall(cost_usd=0.03), latency_ms=50)
    assert statuses(check_budgets(run, case)) == {
        "budget.max_steps": "pass", "budget.max_tool_calls": "pass", "budget.max_cost_usd": "fail",
        "budget.max_latency_ms": "pass",
    }
    over = mk_run(tc("a"), tc("b"), tc("c"), tc("d"), latency_ms=None)
    s = statuses(check_budgets(over, case))
    assert s["budget.max_steps"] == "fail" and s["budget.max_tool_calls"] == "fail"
    assert "budget.max_latency_ms" not in s  # no latency recorded: skipped
    assert check_budgets(run, mk_case()) == []


def test_status():
    assert check_status(mk_run(), mk_case())[0].status == "pass"
    bad = check_status(mk_run(status="error", error="Boom"), mk_case())[0]
    assert bad.status == "fail" and "Boom" in bad.reason
    assert check_status(mk_run(status="error"), mk_case(expect={"status": "any"})) == []
    assert check_status(mk_run(status="error"), mk_case(expect={"status": "error"}))[0].status == "pass"


def test_judge():
    case = mk_case(expect={"judge": {"rubric": "be kind", "threshold": 0.7}})
    run = mk_run(output="hi")
    skipped = check_judge(run, case, None)[0]
    assert skipped.status == "skip" and skipped.ok
    seen = {}

    def judge(rubric, r, c):
        seen["rubric"] = rubric
        return (0.9 if r.output == "hi" else 0.1), "because"

    passed = check_judge(run, case, judge)[0]
    assert passed.status == "pass" and "0.90" in passed.reason and seen["rubric"] == "be kind"
    assert check_judge(mk_run(output="rude"), case, judge)[0].status == "fail"

    def broken(*a):
        raise RuntimeError("api down")

    err = check_judge(run, case, broken)[0]
    assert err.status == "error" and "api down" in err.reason
    assert check_judge(run, mk_case(), judge) == []


def test_python_case_assertion_via_score_run():
    profile = make_profile()

    def ok_fn(run):
        expect(run).called("a")

    def fail_fn(run):
        expect(run).called("zzz")

    def err_fn(run):
        raise KeyError("x")

    for fn, want in [(ok_fn, "pass"), (fail_fn, "fail"), (err_fn, "error")]:
        case = mk_case()
        case.fn = fn
        checks, _ = score_run(mk_run(tc("a")), case, profile)
        py = [c for c in checks if c.name == "python"][0]
        assert py.status == want
    fail_case = mk_case()
    fail_case.fn = fail_fn
    checks, _ = score_run(mk_run(tc("a")), fail_case, profile)
    assert "expected zzz to be called" in [c for c in checks if c.name == "python"][0].reason


# --------------------------------------------------------------------------- global violations


def prof(policies, tools=None):
    tools = tools if tools is not None else [{"name": "lookup"}, {"name": "refund", "side_effect": "write"}]
    return make_profile(tools=tools, policies=policies)


def details(run, profile):
    return [(v.policy, v.detail) for v in global_violations(run, profile)]


def test_max_arg():
    p = prof([{"id": "LIMIT", "rule": "r", "check": {"type": "max_arg", "tool": "refund", "arg": "amount",
                                                      "max": 100}}])
    assert details(mk_run(tc("refund", amount=100)), p) == []
    assert details(mk_run(tc("refund", amount=150.5)), p) == [("LIMIT", "refund(amount=150.5) exceeds 100")]
    assert details(mk_run(tc("refund", amount="lots")), p) == [("LIMIT", "refund(amount='lots') is not a number")]
    assert details(mk_run(tc("refund", other=1)), p) == []
    assert details(mk_run(tc("refund", status="denied", amount=999)), p) == []


def test_allowed_values():
    p = prof([{"id": "CUR", "rule": "r", "check": {"type": "allowed_values", "tool": "refund", "arg": "currency",
                                                    "values": ["USD", "EUR"]}}])
    assert details(mk_run(tc("refund", currency="USD")), p) == []
    assert details(mk_run(tc("refund", currency="BTC")), p)[0][0] == "CUR"


def test_forbidden_tool():
    p = prof([{"id": "NO-REFUND", "rule": "r", "check": {"type": "forbidden_tool", "tool": "refund"}}])
    assert details(mk_run(tc("refund"), tc("refund")), p) == [("NO-REFUND", "refund was called")] * 2
    assert details(mk_run(tc("lookup")), p) == []


def test_requires_prior_tool():
    p = prof([{"id": "VERIFY", "rule": "r", "check": {"type": "requires_prior_tool", "tool": "refund",
                                                       "prior": "lookup"}}])
    assert details(mk_run(tc("refund"), tc("lookup")), p) == [("VERIFY", "refund called before lookup")]
    assert details(mk_run(tc("lookup"), tc("refund"), tc("refund")), p) == []


def test_requires_approval():
    p = prof([{"id": "HITL", "rule": "r", "check": {"type": "requires_approval", "tool": "refund"}}])
    assert details(mk_run(Approval(tool="refund"), tc("refund")), p) == []
    assert details(mk_run(tc("refund")), p) == [("HITL", "refund called without human approval")]
    assert len(details(mk_run(Approval(tool="refund", decision="rejected"), tc("refund")), p)) == 1
    assert len(details(mk_run(Approval(tool="lookup"), tc("refund")), p)) == 1
    # one approval covers one call
    assert len(details(mk_run(Approval(tool="refund"), tc("refund"), tc("refund")), p)) == 1
    assert details(mk_run(tc("refund", status="denied")), p) == []


def test_max_calls():
    p = prof([{"id": "ONCE", "rule": "r", "check": {"type": "max_calls", "tool": "lookup", "n": 1}}])
    assert details(mk_run(tc("lookup", id=1)), p) == []
    assert details(mk_run(tc("lookup", id=1), tc("lookup", id=2)), p) == [("ONCE", "lookup called 2 times (max 1)")]


def test_output_forbidden_patterns():
    p = prof([{"id": "NO-PII", "rule": "r", "check": {"type": "output_forbidden_patterns",
                                                       "patterns": ["email", "phone", "ssn", r"SECRET-\d+"]}}])
    assert details(mk_run(output="All good, order A100 shipped."), p) == []
    found = details(mk_run(output="Mail bob@example.com or call 555-201-3344, ssn 123-45-6789, SECRET-9"), p)
    joined = " ".join(d for _, d in found)
    assert "email" in joined and "phone" in joined and "ssn" in joined and "SECRET-9" in joined
    assert {pid for pid, _ in found} == {"NO-PII"}


def test_policy_without_check_never_violates():
    p = prof([{"id": "SOFT", "rule": "Be nice"}])
    assert details(mk_run(tc("refund"), output="rude"), p) == []


def test_scope_undeclared_tool():
    p = prof([])
    assert details(mk_run(tc("delete_db")), p) == [("SCOPE", "undeclared tool 'delete_db' called")]
    assert details(mk_run(tc("delete_db", status="denied")), p) == []
    assert details(mk_run(tc("anything")), make_profile()) == []  # no tools declared: nothing to compare


def test_max_calls_per_run():
    p = prof([], tools=[{"name": "email", "side_effect": "external", "max_calls_per_run": 1}])
    assert details(mk_run(tc("email", to="a")), p) == []
    assert details(mk_run(tc("email", to="a"), tc("email", to="b")), p) == [
        ("SCOPE", "email called 2 times (max 1)")]


def test_args_schema():
    schema = {"type": "object", "required": ["order_id"],
              "properties": {"order_id": {"type": "string", "pattern": "^[A-Z][0-9]{3,}$"}}}
    p = prof([], tools=[{"name": "lookup", "args_schema": schema}])
    assert details(mk_run(tc("lookup", order_id="A100")), p) == []
    bad = details(mk_run(tc("lookup", order_id="nope"), tc("lookup")), p)
    assert [pid for pid, _ in bad] == ["ARGS", "ARGS"]
    assert "required" in bad[1][1]


def test_loop_detection():
    p = prof([])
    same = [tc("lookup", id=1) for _ in range(4)]
    assert details(mk_run(*same), p) == [("LOOP", "lookup called 4+ times in a row with the same arguments")]
    assert details(mk_run(*same, tc("lookup", id=1), tc("lookup", id=1)), p).count(
        ("LOOP", "lookup called 4+ times in a row with the same arguments")) == 1
    assert details(mk_run(*same[:3]), p) == []
    assert details(mk_run(*[tc("lookup", id=i) for i in range(5)]), p) == []
    assert details(mk_run(tc("lookup", id=1), tc("lookup", id=1), tc("refund"), tc("lookup", id=1),
                          tc("lookup", id=1)), p) == []


def test_violations_carry_run_and_case_ids():
    p = prof([])
    run = mk_run(tc("ghost"), case_id="c-1")
    v = global_violations(run, p)[0]
    assert v.run_id == run.run_id and v.case_id == "c-1"


def test_score_run_adds_violation_checks():
    p = prof([{"id": "NO-REFUND", "rule": "r", "check": {"type": "forbidden_tool", "tool": "refund"}}])
    listed = mk_case(policies=["NO-REFUND"])
    checks, violations = score_run(mk_run(tc("refund")), listed, p)
    pol = [c for c in checks if c.name == "policy[NO-REFUND]"][0]
    assert pol.status == "fail" and pol.covers == ["L4.policy.adherence@NO-REFUND"]
    assert len(violations) == 1
    # a violation fails every case, even ones that don't list the policy
    checks2, _ = score_run(mk_run(tc("refund")), mk_case(), p)
    pol2 = [c for c in checks2 if c.name == "policy[NO-REFUND]"][0]
    assert pol2.status == "fail" and pol2.covers == []


# --------------------------------------------------------------------------- fluent expect


def test_fluent_expect_passes_and_chains():
    run = mk_run(tc("lookup", order_id="A1"), Handoff(to_agent="b"), tc("refund", amount=10),
                 output="Refunded your order", final_state={"refunds": {"A1": 10}})
    e = expect(run)
    assert e.called("lookup").called("refund", amount={"lte": 10}).not_called("email") is e
    e.called_in_order(["lookup", "handoff:b", "refund"]).output_contains("REFUNDED").output_not_contains("secret")
    e.state("refunds.A1", 10).state("refunds.A1", {"gt": 5}).max_steps(3)


@pytest.mark.parametrize("assertion", [
    lambda e: e.called("email"),
    lambda e: e.called("refund", amount=99),
    lambda e: e.called("denied_tool"),  # denied calls do not count as called
    lambda e: e.not_called("lookup"),
    lambda e: e.called_in_order(["refund", "lookup"]),
    lambda e: e.output_contains("missing"),
    lambda e: e.output_not_contains("refunded"),
    lambda e: e.state("refunds.A1", 11),
    lambda e: e.state("nope", None),
    lambda e: e.max_steps(1),
])
def test_fluent_expect_failures(assertion):
    run = mk_run(tc("lookup"), tc("refund", amount=10), tc("denied_tool", status="denied"),
                 output="Refunded", final_state={"refunds": {"A1": 10}})
    with pytest.raises(AssertionError):
        assertion(expect(run))
