import textwrap

import pytest

from evalplane.cases import _MISSING, Case, get_path, load_cases, match_value
from evalplane.errors import ConfigError
from evalplane.models import Stage


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def test_yaml_loading_with_defaults_and_shorthand(tmp_path):
    write(tmp_path / "evals" / "orders.yaml", """
        suite: orders
        defaults:
          context: {user: ana}
          tags: [smoke]
          stage: ci
        cases:
          - id: one
            input: hi
            expect:
              tools: [lookup, {name: refund, args: {amount: 5}}]
          - id: two
            tags: [own]
            context: {user: bob}
            stage: pre_release
            suite: custom
    """)
    one, two = load_cases(["evals"], base_dir=tmp_path)
    assert one.suite == "orders" and one.context == {"user": "ana"} and one.tags == ["smoke"]
    assert one.source.endswith("orders.yaml")
    assert one.expect.tools.mode == "in_order"
    assert [(c.name, c.args) for c in one.expect.tools.calls] == [("lookup", {}), ("refund", {"amount": 5})]
    assert two.tags == ["own"] and two.context == {"user": "bob"} and two.stage == Stage.pre_release
    assert two.suite == "custom"


def test_case_defaults():
    c = Case(id="x")
    assert c.suite == "default" and c.repeat == 1 and c.stage == Stage.ci and c.expect.status == "ok"
    with pytest.raises(Exception):
        Case(id="x", repeat=0)


def test_yaml_top_level_list_and_skips(tmp_path):
    write(tmp_path / "evals" / "list.yml", "- {id: a}\n- {id: b}\n")
    write(tmp_path / "evals" / "nocases.yaml", "foo: bar\n")
    write(tmp_path / "evals" / "empty.yaml", "")
    write(tmp_path / "evals" / "agent.eval.yaml", "cases: [{id: should-not-load}]\n")
    write(tmp_path / "evals" / "notes.txt", "cases: [{id: nope}]\n")
    cases = load_cases([tmp_path / "evals", tmp_path / "missing-dir"])
    assert [(c.id, c.suite) for c in cases] == [("a", "list"), ("b", "list")]


def test_invalid_case_is_config_error(tmp_path):
    write(tmp_path / "bad.yaml", "cases:\n  - {id: broken, expect: {nonsense: 1}}\n")
    with pytest.raises(ConfigError, match=r"bad.yaml: case broken"):
        load_cases([tmp_path / "bad.yaml"])


def test_duplicate_ids_error(tmp_path):
    write(tmp_path / "evals" / "a.yaml", "cases: [{id: same}]\n")
    write(tmp_path / "evals" / "b.yaml", "cases: [{id: same}]\n")
    with pytest.raises(ConfigError, match="duplicate case ids: \\['same'\\]"):
        load_cases([tmp_path / "evals"])


def test_python_case_discovery(tmp_path):
    write(tmp_path / "evals" / "helper_mod.py", "VALUE = 'from-helper'\nraise_if_imported_as_case = True\n")
    write(tmp_path / "evals" / "not_a_case_file.py", "raise RuntimeError('must not be imported')\n")
    write(tmp_path / "evals" / "privacy_eval.py", """
        import evalplane as ep
        from helper_mod import VALUE

        @ep.case("py-one", input=VALUE, policies=["NO-PII"], repeat=2,
                 expect={"tools": ["lookup"], "forbidden_tools": ["refund"]})
        def py_one(run):
            ep.expect(run).called("lookup")

        @ep.case("py-two", stage="pre_release", tags=["t"])
        def py_two(run):
            pass
    """)
    write(tmp_path / "evals" / "eval_more.py", """
        import evalplane as ep

        @ep.case("py-three")
        def three(run):
            pass
    """)
    cases = {c.id: c for c in load_cases(["evals"], base_dir=tmp_path)}
    assert set(cases) == {"py-one", "py-two", "py-three"}
    one = cases["py-one"]
    assert one.input == "from-helper" and one.suite == "privacy_eval" and one.repeat == 2
    assert one.fn is not None and one.fn.__name__ == "py_one"
    assert one.source.endswith("privacy_eval.py:py_one")
    assert one.expect.tools.calls[0].name == "lookup" and one.expect.forbidden_tools == ["refund"]
    assert cases["py-two"].stage == Stage.pre_release
    assert cases["py-three"].suite == "eval_more"
    # loading again gives the same cases, not duplicates
    assert len(load_cases(["evals"], base_dir=tmp_path)) == 3


def test_all_covers_implications():
    c = Case.model_validate({
        "id": "x", "covers": ["L0.model.acceptance"], "policies": ["P1"], "repeat": 3,
        "expect": {
            "tools": ["a", "b"], "forbidden_tools": ["c"],
            "output": {"contains": "ok", "json_schema": {"type": "object"}},
            "retrieved": ["d1"], "budgets": {"max_steps": 3},
        },
    })
    assert c.exercised_tools() == {"a", "b"}
    assert c.all_covers() == sorted({
        "L0.model.acceptance", "L3.tool.selection@a", "L3.tool.selection@b",  # args only when asserted
        "L3.tool.guard@c", "L4.policy.adherence@P1", "L4.task.success", "L1.golden.accuracy",
        "L2.retrieval.recall", "L1.output.schema", "L4.efficiency.budget", "L4.trajectory.match",
        "L4.reliability.pass_k",
    })


@pytest.mark.parametrize("expect, covered", [
    ({"output": {"not_contains": "x"}}, []),
    ({"output": {"equals": 0}}, ["L1.golden.accuracy", "L4.task.success"]),
    ({"output": {"regex": "a+"}}, ["L1.golden.accuracy", "L4.task.success"]),
    ({"final_state": [{"path": "a", "equals": 1}]}, ["L1.golden.accuracy", "L4.task.success"]),
    ({"tools": ["only"]}, ["L3.tool.selection@only"]),
    ({"tools": [{"name": "only", "args": {"x": 1}}]}, ["L3.tool.args@only", "L3.tool.selection@only"]),
])
def test_all_covers_partial(expect, covered):
    assert Case.model_validate({"id": "x", "expect": expect}).all_covers() == covered


@pytest.mark.parametrize("expected, actual, ok", [
    (5, 5, True), (5, 5.0, True), (5, 6, False), ("a", "a", True), ("a", "b", False),
    ({"eq": 3}, 3, True), ({"ne": 3}, 4, True), ({"ne": 3}, 3, False),
    ({"lt": 3}, 2, True), ({"lt": 3}, 3, False), ({"lte": 3}, 3, True),
    ({"gt": 3}, 4, True), ({"gt": 3}, 3, False), ({"gte": 3}, 3, True),
    ({"gt": 3}, None, False), ({"lt": 3}, "str", False),
    ({"in": ["a", "b"]}, "a", True), ({"in": ["a", "b"]}, "c", False),
    ({"regex": r"^A\d+$"}, "A100", True), ({"regex": r"^A\d+$"}, "B1", False), ({"regex": "x"}, None, False),
    ({"contains": "bc"}, "abcd", True), ({"contains": 2}, [1, 2], True), ({"contains": "z"}, "abc", False),
    ({"contains": "a"}, 5, False),
    ({"any": True}, None, True),
    ({"gte": 1, "lte": 10}, 5, True), ({"gte": 1, "lte": 10}, 11, False),
    ({"id": "A1", "amount": {"lte": 40}}, {"id": "A1", "amount": 40, "extra": 1}, True),
    ({"id": "A1"}, {"other": 1}, False),
    ({"nested": {"deep": {"gt": 1}}}, {"nested": {"deep": 2}}, True),
    ([1, 2], [1, 2], True),
])
def test_match_value(expected, actual, ok):
    assert match_value(expected, actual) is ok


def test_get_path():
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}, "x.y": 0}
    assert get_path(data, "a.b[1].c") == 2
    assert get_path(data, "a.b[0]") == {"c": 1}
    assert get_path(data, "a") == data["a"]
    assert get_path(data, "a.b[5]") is _MISSING
    assert get_path(data, "a.zz") is _MISSING
    assert get_path(data, "a.b.c") is _MISSING
    assert get_path({"a": None}, "a") is None
