"""Find tool definitions in a codebase so `evalplane init` can pre-fill agent.eval.yaml.

Static (AST) scan, nothing is imported or executed. Recognises functions decorated with
LangChain/LangGraph `@tool`, OpenAI Agents SDK `@function_tool`, MCP `@mcp.tool()` / `@server.tool()`,
CrewAI `@tool("name")`, Pydantic AI `@agent.tool` / `@agent.tool_plain`, Google ADK `FunctionTool(fn)`,
and Evalplane's own `@ep.tool`. The side effect is guessed from the tool name and docstring;
always review the guess, because it decides the risk tier.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

TOOL_DECORATORS = {"tool", "function_tool", "tool_plain"}
SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "__pycache__", "site-packages", ".evalplane", "build", "dist",
             "tests", "test"}

# verb -> side effect (first match on the name's leading word, then anywhere in the name)
IRREVERSIBLE = ("delete", "remove", "drop", "destroy", "purge", "refund", "pay", "charge", "transfer", "withdraw",
                "deposit", "purchase", "buy", "sell", "order", "cancel", "terminate", "revoke", "deploy", "merge",
                "publish", "approve", "deny", "reject", "sign", "execute", "run_sql", "wipe", "trash", "book")
NOTIFY = ("page", "alert", "notify", "escalate", "ping", "remind")
EXTERNAL = ("send", "email", "mail", "post", "tweet", "message", "sms", "call", "slack", "invite",
            "share", "reply", "forward", "comment", "webhook", "upload", "submit")
UNTRUSTED = ("fetch", "browse", "scrape", "web", "url", "http", "page_content", "read_email", "get_email", "inbox",
             "ticket", "document", "doc", "file", "issue", "comment", "message", "review")
WRITE = ("create", "update", "set", "write", "add", "edit", "modify", "save", "insert", "upsert", "put", "patch",
         "move", "rename", "assign", "schedule", "draft", "commit", "apply", "store", "tag", "label", "archive",
         "spawn", "launch", "start", "delegate", "trigger", "enqueue", "dispatch", "change", "switch", "enable",
         "disable", "restart", "scale", "resize", "reboot", "stop", "pause", "resume", "retry", "rotate", "reset")
READ = ("get", "list", "search", "find", "read", "fetch", "lookup", "look", "query", "retrieve", "check", "view",
        "show", "describe", "count", "load", "browse", "scrape", "summarize", "calculate", "compute")
SENSITIVE = ("customer", "user", "patient", "account", "email", "phone", "address", "profile", "contact", "ssn",
             "payment", "card", "salary", "medical", "health")


@dataclass
class FoundTool:
    name: str
    side_effect: str
    file: str
    line: int
    description: str = ""
    sensitive: bool = False
    framework: str = ""
    args_schema: dict | None = None


def guess_side_effect(name: str, doc: str = "") -> str:
    words = [w.lower() for w in re.split(r"[_\W]+|(?<=[a-z])(?=[A-Z])", name) if w]
    head = words[0] if words else ""
    groups = ((IRREVERSIBLE, "irreversible"), (NOTIFY, "notify"), (EXTERNAL, "external"), (WRITE, "write"),
              (READ, "read"))
    for group, label in groups:
        if head in group:
            return label
    for group, label in groups[:-1]:
        if any(w in group for w in words):
            return label
    text = doc.lower()
    if any(k in text for k in ("permanently", "cannot be undone", "irreversib", "payment", "refund", "delete")):
        return "irreversible"
    if any(k in text for k in ("sends", "send an", "posts to", "emails")):
        return "external"
    return "read"


def _decorator_name(d: ast.expr) -> tuple[str, str | None]:
    """(decorator attr/name, explicit tool name if given as first string arg)."""
    explicit = None
    if isinstance(d, ast.Call):
        if d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
            explicit = d.args[0].value
        for kw in d.keywords:
            if kw.arg in ("name", "name_override") and isinstance(kw.value, ast.Constant):
                explicit = str(kw.value.value)
        d = d.func
    if isinstance(d, ast.Attribute):
        return d.attr, explicit
    if isinstance(d, ast.Name):
        return d.id, explicit
    return "", explicit


def _framework(src: str) -> str:
    for needle, fw in (("langgraph", "langgraph"), ("langchain", "langgraph"), ("agents import", "openai-agents"),
                       ("from agents", "openai-agents"), ("claude_agent_sdk", "claude-agent-sdk"), ("crewai", "crewai"),
                       ("pydantic_ai", "pydantic-ai"), ("google.adk", "google-adk"), ("mcp", "mcp"),
                       ("evalplane", "custom")):
        if needle in src:
            return fw
    return ""


def scan(root: str | Path = ".", max_files: int = 2000) -> list[FoundTool]:
    root = Path(root)
    found: dict[str, FoundTool] = {}
    files = [p for p in root.rglob("*.py") if not (set(p.relative_to(root).parts[:-1]) & SKIP_DIRS)][:max_files]
    for path in files:
        try:
            src = path.read_text(errors="ignore")
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        fw = _framework(src)
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in funcs.values():
            for d in node.decorator_list:
                dname, explicit = _decorator_name(d)
                if dname in TOOL_DECORATORS:
                    _add(found, explicit or node.name, node, path, root, fw)
                    break
        # raw SDK tool schemas: {"name": ..., "input_schema": {...}} (Anthropic) or {"name": ..., "parameters": {...}}
        # (OpenAI, also inside {"type": "function", "function": {...}})
        for d in (n for n in ast.walk(tree) if isinstance(n, ast.Dict)):
            keys = {k.value: v for k, v in zip(d.keys, d.values, strict=True) if isinstance(k, ast.Constant)}
            name_node = keys.get("name")
            schema_node = keys.get("input_schema") or keys.get("parameters")
            if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str) and schema_node):
                continue
            name = name_node.value
            if name in found or not re.match(r"^[A-Za-z_][\w.\-]*$", name):
                continue
            desc_node = keys.get("description")
            desc = desc_node.value if isinstance(desc_node, ast.Constant) and isinstance(desc_node.value, str) else ""
            try:
                schema = ast.literal_eval(schema_node)
            except ValueError:
                schema = None
            found[name] = FoundTool(name=name, side_effect=guess_side_effect(name, desc), file=str(path.relative_to(root)),
                                    line=d.lineno, description=desc.strip().splitlines()[0][:120] if desc.strip() else "",
                                    sensitive=any(k in f"{name} {desc}".lower() for k in SENSITIVE),
                                    framework=fw, args_schema=schema if isinstance(schema, dict) else None)
        # FunctionTool(fn) / Tool.from_function(fn) / StructuredTool.from_function(func=fn)
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            cname, _ = _decorator_name(call)
            if cname in ("FunctionTool", "from_function"):
                args = list(call.args) + [k.value for k in call.keywords if k.arg in ("func", "fn")]
                for a in args:
                    if isinstance(a, ast.Name) and a.id in funcs:
                        _add(found, a.id, funcs[a.id], path, root, fw)
    return sorted(found.values(), key=lambda t: (t.file, t.line))


def _add(found: dict[str, FoundTool], name: str, node, path: Path, root: Path, fw: str) -> None:
    if name in found or not re.match(r"^[A-Za-z_][\w.\-]*$", name):
        return
    doc = ast.get_docstring(node) or ""
    arg_names = " ".join(a.arg for a in node.args.args)
    found[name] = FoundTool(
        name=name, side_effect=guess_side_effect(name, doc), file=str(path.relative_to(root)), line=node.lineno,
        description=doc.strip().splitlines()[0][:120] if doc.strip() else "",
        sensitive=any(k in f"{name} {arg_names} {doc}".lower() for k in SENSITIVE), framework=fw,
    )


MODEL_RX = re.compile(r"^(claude-[\w.\-]+|gpt-[\w.\-]+|o\d(?:-[\w.\-]+)?|gemini-[\w.\-]+|llama[\w.\-:]*|"
                      r"mistral-[\w.\-]+|anthropic\.[\w.\-:]+|us\.anthropic\.[\w.\-:]+)$")


def find_model(root: str | Path = ".") -> str | None:
    """The most frequently mentioned model id string in the code, if any."""
    from collections import Counter

    root = Path(root)
    seen: Counter[str] = Counter()
    for path in [p for p in root.rglob("*.py") if not (set(p.relative_to(root).parts[:-1]) & SKIP_DIRS)][:2000]:
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and MODEL_RX.match(n.value):
                seen[n.value] += 1
    return seen.most_common(1)[0][0] if seen else None


def find_entrypoint(root: str | Path = ".") -> str | None:
    """A top-level function that looks like the agent's entrypoint (run/main/invoke/...), as module:function."""
    root = Path(root)
    preferred = ("run", "run_agent", "agent", "invoke", "chat", "respond", "answer", "handle", "main")
    best: tuple[int, str] | None = None
    for path in [p for p in root.rglob("*.py") if not (set(p.relative_to(root).parts[:-1]) & SKIP_DIRS)][:2000]:
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in preferred and len(n.args.args) >= 1:
                mod = ".".join(path.relative_to(root).with_suffix("").parts)
                rank = preferred.index(n.name)
                if best is None or rank < best[0]:
                    best = (rank, f"{mod}:{n.name}")
    return best[1] if best else None


def to_profile_tools(tools: list[FoundTool]) -> list[dict]:
    out = []
    for t in tools:
        d: dict = {"name": t.name, "side_effect": t.side_effect}
        low = t.name.lower()
        if t.side_effect == "read" and any(k in low for k in UNTRUSTED):
            d["untrusted_output"] = True  # returns text someone else wrote: test for injected instructions
        if t.description:
            d["description"] = t.description
        if t.sensitive:
            d["data_sensitivity"] = "personal"
        if t.args_schema and t.args_schema.get("properties"):
            d["args_schema"] = t.args_schema
        out.append(d)
    return out
