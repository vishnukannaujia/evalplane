from helpers import make_profile

from evalplane.cases import Case
from evalplane.compare import compare, comparison_md
from evalplane.planner import build_plan
from evalplane.runner import run_cases

P = make_profile(tools=[{"name": "t"}], policies=[
    {"id": "NO-SECRET", "rule": "never say secret", "check": {"type": "output_forbidden_patterns", "patterns": ["secret"]}}])
CASES = [Case(id="a", input="hi", expect={"output": {"contains": "hello"}}),
         Case(id="b", input="x", expect={"output": {"contains": "ok"}})]


def run(agent):
    return run_cases(P, build_plan(P), CASES, agent=agent)[0]


def test_compare_finds_regressions_and_fixes():
    before = run(lambda i, c: "hello" if i == "hi" else "nope")      # a passes, b fails
    after = run(lambda i, c: "the secret is 42" if i == "hi" else "ok")  # a fails + violation, b fixed
    c = compare(before, after)
    assert [r.id for r in c.regressions] == ["a"] and [f.id for f in c.fixes] == ["b"]
    assert c.new_violations and "NO-SECRET" in c.new_violations[0]
    assert c.regressed and "Regressions found" in comparison_md(c)
    same = compare(before, before)
    assert not same.regressed and "No regressions" in comparison_md(same)
