"""Eval cases: what to send the agent and what must (or must not) happen.

Cases are declared in YAML files under `evals/` or with the `@evalplane.case` decorator
in Python files named `*_eval.py` / `eval_*.py`.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from .errors import ConfigError
from .models import Stage


class ToolCallSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["exact", "in_order", "any_order", "subset"] = "in_order"
    calls: list[ToolCallSpec] = Field(default_factory=list)


class OutputExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contains: str | list[str] | None = None
    not_contains: str | list[str] | None = None
    regex: str | None = None
    equals: Any = None
    json_schema: dict[str, Any] | None = None
    case_sensitive: bool = False


class StateExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    equals: Any = None
    match: Any = None
    exists: bool | None = None


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_cost_usd: float | None = None
    max_latency_ms: float | None = None


class JudgeExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rubric: str
    threshold: float = 0.7


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: ToolExpect | None = None
    forbidden_tools: list[str] = Field(default_factory=list)
    output: OutputExpect | None = None
    final_state: list[StateExpect] = Field(default_factory=list)
    retrieved: list[str] = Field(default_factory=list)
    budgets: Budgets | None = None
    judge: JudgeExpect | None = None
    status: Literal["ok", "error", "any"] = "ok"

    @field_validator("tools", mode="before")
    @classmethod
    def _tools_shorthand(cls, v):
        # `tools: [a, b]` is shorthand for in-order calls by name
        if isinstance(v, list):
            return {"mode": "in_order", "calls": [c if isinstance(c, dict) else {"name": c} for c in v]}
        return v


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    id: str
    suite: str = "default"
    description: str = ""
    input: Any = None
    context: dict[str, Any] = Field(default_factory=dict)
    covers: list[str] = Field(default_factory=list)
    policies: list[str] = Field(default_factory=list)
    owasp: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    stage: Stage = Stage.ci
    repeat: int = Field(default=1, ge=1)
    trace: str | None = None
    from_run: str | None = None  # run_id this case was promoted from: `run --traces` replays that run
    turns: list[str] = Field(default_factory=list)  # scripted user replies after `input` (multi-turn)
    simulator: str | None = None  # module:function(history, context) -> next user message, or None to stop
    max_turns: int = Field(default=6, ge=1)  # with a simulator: at most this many user turns
    external_status: str | None = None  # set for tests run elsewhere (pytest): pass | fail | skip
    external_reason: str = ""
    expect: Expectations = Field(default_factory=Expectations)
    source: str = ""
    fn: SkipJsonSchema[Callable[..., Any] | None] = Field(default=None, exclude=True)

    @property
    def is_placeholder(self) -> bool:
        """Starter/suggested cases whose input was never filled in (TODO or <...>)."""
        text = self.input if isinstance(self.input, str) else ""
        return text.strip().upper().startswith("TODO") or bool(re.fullmatch(r"\s*<[^>]*>\s*", text or "x"))

    def exercised_tools(self) -> set[str]:
        names = set()
        if self.expect.tools:
            names |= {c.name for c in self.expect.tools.calls}
        return names

    def all_covers(self) -> list[str]:
        """Requirement ids this case covers: explicit ones plus the ones implied by its expectations."""
        out = list(self.covers)
        for t in self.exercised_tools():
            out.append(f"L3.tool.selection@{t}")
        for spec in (self.expect.tools.calls if self.expect.tools else []):
            if spec.args:  # only a case that asserts arguments tests them
                out.append(f"L3.tool.args@{spec.name}")
        for t in self.expect.forbidden_tools:
            out.append(f"L3.tool.guard@{t}")
        for p in self.policies:
            out.append(f"L4.policy.adherence@{p}")
        e = self.expect
        if e.output and (e.output.contains or e.output.equals is not None or e.output.regex) or e.final_state:
            out += ["L4.task.success", "L1.golden.accuracy"]
        if self.expect.retrieved:
            out.append("L2.retrieval.recall")
        if self.expect.output and self.expect.output.json_schema:
            out.append("L1.output.schema")
        if self.expect.budgets:
            out.append("L4.efficiency.budget")
        if self.expect.tools and len(self.expect.tools.calls) > 1:
            out.append("L4.trajectory.match")
        if self.context.get("fail_tools"):
            out.append("L3.tool.error_recovery")
        if self.repeat > 1:
            out.append("L4.reliability.pass_k")
        return sorted(set(out))


# --------------------------------------------------------------------------- matchers

_OPS = {"eq", "ne", "lt", "lte", "gt", "gte", "in", "regex", "contains", "any"}


def match_value(expected: Any, actual: Any) -> bool:
    """Literal equality, or a dict of operators: eq, ne, lt, lte, gt, gte, in, regex, contains, any."""
    if isinstance(expected, dict) and expected and set(expected) <= _OPS:
        for op, want in expected.items():
            try:
                ok = {
                    "eq": lambda: actual == want,
                    "ne": lambda: actual != want,
                    "lt": lambda: actual is not None and actual < want,
                    "lte": lambda: actual is not None and actual <= want,
                    "gt": lambda: actual is not None and actual > want,
                    "gte": lambda: actual is not None and actual >= want,
                    "in": lambda: actual in want,
                    "regex": lambda: actual is not None and re.search(want, str(actual)) is not None,
                    "contains": lambda: actual is not None and want in actual,
                    "any": lambda: True,
                }[op]()
            except TypeError:
                ok = False
            if not ok:
                return False
        return True
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(k in actual and match_value(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)) and not isinstance(expected, bool):
        return float(expected) == float(actual)
    return expected == actual


_MISSING = object()


def get_path(data: Any, path: str) -> Any:
    cur = data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return _MISSING
            cur = cur[idx]
        elif isinstance(cur, list) and part.isdigit():  # `sent.0.to` works like `sent[0].to`
            if int(part) >= len(cur):
                return _MISSING
            cur = cur[int(part)]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return _MISSING
            cur = cur[part]
    return cur


# --------------------------------------------------------------------------- python cases

_PY_CASES: list[Case] = []


def case(id: str, *, input: Any = None, covers: list[str] | None = None, policies: list[str] | None = None,
         repeat: int = 1, context: dict | None = None, tags: list[str] | None = None,
         owasp: list[str] | None = None, stage: str = "ci", expect: dict | None = None):
    """Decorator for a Python eval case. The function receives the recorded Run and asserts on it,
    e.g. with `evalplane.expect(run).called("issue_refund")`. Raise AssertionError to fail."""

    def wrap(fn: Callable) -> Callable:
        _PY_CASES.append(Case(
            id=id, input=input, covers=covers or [], policies=policies or [], repeat=repeat,
            context=context or {}, tags=tags or [], owasp=owasp or [], stage=Stage(stage),
            expect=Expectations.model_validate(expect or {}), fn=fn,
            source=f"{fn.__module__}:{fn.__name__}",
        ))
        return fn

    return wrap


def _load_python_file(path: Path) -> list[Case]:
    _PY_CASES.clear()
    mod_name = f"_evalplane_cases_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    parent = str(path.parent.resolve())
    added = parent not in sys.path
    if added:
        sys.path.insert(0, parent)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(parent)
    found = list(_PY_CASES)
    for c in found:
        c.suite = path.stem
        c.source = f"{path}:{c.source.split(':')[-1]}"
    _PY_CASES.clear()
    return found


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _load_yaml_file(path: Path) -> list[Case]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        raise ConfigError(f"{path} is not valid YAML{where}: {getattr(e, 'problem', e)}") from e
    if isinstance(data, list):
        data = {"cases": data}
    if "cases" not in data:
        return []
    suite = data.get("suite", path.stem)
    defaults = data.get("defaults", {}) or {}
    out = []
    for raw in data["cases"]:
        merged = {**defaults, **raw}
        if isinstance(defaults.get("context"), dict) and isinstance(raw.get("context"), dict):
            merged["context"] = _deep_merge(defaults["context"], raw["context"])  # override only what differs
        merged.setdefault("suite", suite)
        try:
            c = Case.model_validate(merged)
        except Exception as e:
            from pydantic import ValidationError

            if isinstance(e, ValidationError):
                msgs = "; ".join(f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors())
            else:
                msgs = str(e)
            raise ConfigError(f"{path}: case {raw.get('id', '?')}: {msgs}") from e
        c.source = str(path)
        out.append(c)
    return out


def load_cases(paths: list[str | Path], base_dir: Path | None = None) -> list[Case]:
    cases: list[Case] = []
    for p in paths:
        p = Path(p)
        if base_dir is not None and not p.is_absolute():
            p = base_dir / p
        if not p.exists():
            continue
        files = [p] if p.is_file() else sorted(p.rglob("*"))
        for f in files:
            if f.suffix in (".yaml", ".yml") and f.name != "agent.eval.yaml":
                cases.extend(_load_yaml_file(f))
            elif f.suffix == ".py" and (f.stem.endswith("_eval") or f.stem.startswith("eval_")):
                cases.extend(_load_python_file(f))
    if base_dir is not None:
        cases.extend(load_pytest_results(base_dir))
    ids = [c.id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ConfigError(f"duplicate case ids: {dupes}")
    return cases


def load_pytest_results(base_dir: Path) -> list[Case]:
    """Tests marked @pytest.mark.evalplane, as recorded by the pytest plugin (nearest .evalplane/pytest.json)."""
    import json

    for d in [base_dir, *base_dir.resolve().parents]:
        f = d / ".evalplane" / "pytest.json"
        if f.exists():
            break
        if (d / ".git").exists():
            return []
    else:
        return []
    out = []
    data = json.loads(f.read_text())
    when = data.get("finished_at", "unknown time")
    for t in data.get("tests", []):
        if t.get("status") == "fail":
            t["reason"] = f"{t.get('reason') or 'pytest test failed'} (pytest run at {when}; re-run pytest after fixes)"
        expect = {"forbidden_tools": t.get("forbidden_tools", [])}
        if t.get("tools"):
            expect["tools"] = t["tools"]
        out.append(Case(id=f"pytest::{t['id']}", suite="pytest", covers=t.get("covers", []),
                        policies=t.get("policies", []), owasp=t.get("owasp", []),
                        expect=Expectations.model_validate(expect), source=str(f),
                        external_status=t.get("status", "pass"), external_reason=t.get("reason", "")))
    return out
