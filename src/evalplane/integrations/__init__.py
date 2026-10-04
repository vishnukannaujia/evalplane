"""Integrations: import traces from Langfuse, LangSmith and Braintrust, and record runs from the OpenAI
Agents SDK and the Claude Agent SDK.

None of these modules imports a third-party library at import time; the SDK-backed helpers
(`fetch_langfuse`, `fetch_langsmith`, `openai_agents.install`, `claude_agent_sdk.evalplane_hooks`) import it
when called and raise an ImportError naming the pip package if it is missing. The file importers need no
third-party package at all.

    from evalplane.integrations import load_any
    runs = load_any("exports/langfuse-traces.json")   # native JSONL, OTel, Langfuse, LangSmith or Braintrust
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..trace import Run, load_otel_data, read_runs
from ._common import as_list, read_records
from .braintrust import braintrust_to_runs, is_braintrust, load_braintrust
from .langfuse import fetch_langfuse, is_langfuse, langfuse_to_runs, load_langfuse
from .langsmith import fetch_langsmith, is_langsmith, langsmith_to_runs, load_langsmith
from .tau2 import is_tau2, load_tau2, tau2_to_runs

__all__ = [
    "braintrust_to_runs",
    "detect_format",
    "fetch_langfuse",
    "fetch_langsmith",
    "langfuse_to_runs",
    "langsmith_to_runs",
    "load_any",
    "load_braintrust",
    "load_langfuse",
    "load_langsmith",
    "load_tau2",
    "tau2_to_runs",
]


def detect_format(data: Any) -> str:
    """Name the format: 'otel', 'braintrust', 'langsmith', 'langfuse', 'tau2' or 'evalplane'."""
    if is_tau2(data):
        return "tau2"
    if isinstance(data, dict) and ("resourceSpans" in data or isinstance(data.get("spans"), list)):
        return "otel"
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        return "braintrust"
    records = as_list(data, "runs", "data", "traces", "observations", "events")
    if is_braintrust(records):
        return "braintrust"
    if is_langsmith(records):
        return "langsmith"
    if is_langfuse(records):
        return "langfuse"
    return "evalplane"  # native runs, or flat OTel span lists: trace.read_runs handles both


def load_any(path: str | Path) -> list[Run]:
    """Load runs from a file of any supported format (or every *.jsonl / *.json file in a directory)."""
    path = Path(path)
    if path.is_dir():
        files = sorted(path.rglob("*.jsonl")) + sorted(path.rglob("*.json"))
        return [r for f in files for r in load_any(f)]
    try:
        data = read_records(path)
    except ValueError:
        return list(read_runs(path))  # not JSON we can sniff: let the native reader report the error
    fmt = detect_format(data)
    if fmt == "otel":
        return load_otel_data(data)
    if fmt == "braintrust":
        return braintrust_to_runs(data)
    if fmt == "langsmith":
        return langsmith_to_runs(data)
    if fmt == "langfuse":
        return langfuse_to_runs(data)
    if fmt == "tau2":
        return tau2_to_runs(data)
    return list(read_runs(path))
