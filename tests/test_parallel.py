import threading
import time

from helpers import make_profile

from evalplane.cases import Case
from evalplane.planner import build_plan
from evalplane.runner import run_cases

P = make_profile()


def test_parallel_runs_all_attempts_in_order():
    seen = set()

    def agent(inp, ctx):
        seen.add(threading.get_ident())
        time.sleep(0.05)
        return f"{inp}-{ctx['attempt']}"

    cases = [Case(id=f"c{i}", input=f"q{i}", repeat=3, expect={"output": {"contains": f"q{i}"}}) for i in range(4)]
    start = time.perf_counter()
    results, runs = run_cases(P, build_plan(P), cases, agent=agent, jobs=6)
    assert time.perf_counter() - start < 0.4  # 12 attempts x 50 ms, in parallel
    assert all(c.status == "pass" and c.n == 3 for c in results.cases)
    assert [r.output for r in runs if r.case_id == "c1"] == ["q1-0", "q1-1", "q1-2"]
    assert len(seen) > 1


def test_timeout_marks_attempt():
    def slow(inp, ctx):
        time.sleep(1.0)
        return "late"

    results, runs = run_cases(P, build_plan(P), [Case(id="s", input="x")], agent=slow, timeout=0.1)
    assert runs[0].status == "timeout" and results.cases[0].status in ("fail", "error")
