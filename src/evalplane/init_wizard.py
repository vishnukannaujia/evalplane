"""`evalplane init`: build agent.eval.yaml from your code (scan) and a few questions."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from .errors import ConfigError
from .models import TIER_NAMES, AgentProfile
from .packs import list_packs, pack_dir
from .scan import FoundTool, find_entrypoint, find_model, scan, to_profile_tools
from .tiering import derive_tier

console = Console()

SIDE_EFFECTS = {
    "r": ("read", "reads or looks things up"),
    "w": ("write", "changes something in your systems that can be undone"),
    "n": ("notify", "notifies your own people (page on-call, post to your team channel)"),
    "e": ("external", "sends something to the outside world (email a customer, post publicly, call an API)"),
    "i": ("irreversible", "moves money, deletes data, deploys, or makes a commitment"),
}
_BY_NAME = {v[0]: k for k, v in SIDE_EFFECTS.items()}

SCHEMA_HINT = ("# yaml-language-server: $schema=https://raw.githubusercontent.com/vishnukannaujia/evalplane/main/"
               "schema/agent.eval.schema.json\n")

# A profile with action tools and no policies has nothing agent-specific to break, so the gap list collapses
# into the generic starter requirements any eval tool would hand you. Leave the next edit written out.
POLICY_STUB = """
# Rules this agent must never break. Each `check:` is verified on every run AND on production traces
# (`evalplane audit`), so one line here is worth more than a dozen test cases.
# Types: max_arg · allowed_values · forbidden_tool · requires_prior_tool · requires_approval
#        requires_user_confirmation · max_calls · arg_must_match · arg_forbidden_patterns
#        arg_matches_prior_result · arg_from_user · output_forbidden_patterns  (see docs/reference.md)
policies: []
# policies:
#   - id: CONFIRM-FIRST
#     rule: Never call {tool} before the user has confirmed it.
#     check: {{type: requires_user_confirmation, tool: {tool}}}
"""


def _fail(msg: str) -> None:
    Console(stderr=True).print(f"[red]error:[/red] {msg}")
    raise typer.Exit(2)


def copy_example(example: str, directory: Path, force: bool, examples_root: Path) -> None:
    src = examples_root / example
    if not (src / "agent.eval.yaml").exists():
        avail = sorted(d.name for d in examples_root.iterdir() if (d / "agent.eval.yaml").exists()) \
            if examples_root.exists() else []
        _fail(f"unknown example {example!r}. Available: {', '.join(avail) or 'none'}")
    dest = directory / example if directory == Path(".") else directory
    if dest.exists() and any(dest.iterdir()) and not force:
        _fail(f"{dest} exists and is not empty (use --force)")
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".evalplane", "__pycache__"))
    profile = yaml.safe_load((src / "agent.eval.yaml").read_text()) or {}
    if (profile.get("agent") or {}).get("entrypoint"):
        steps = "  evalplane plan\n  evalplane run && evalplane gate\n  BUGGY=1 evalplane run; evalplane gate"
    else:  # a described agent with recorded traces but no code to run
        steps = "  evalplane plan\n  evalplane coverage\n  evalplane audit traces/\n  evalplane promote traces/ --failures"
    console.print(f"[green]Copied example[/green] to {dest}. Try:\n  cd {dest}\n{steps}")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "my-agent")[:40].strip("-") or "my-agent"


def run_init(*, pack: str | None, name: str | None, non_interactive: bool, directory: Path, force: bool,
             scan_dir: Path | None) -> None:
    target = directory / "agent.eval.yaml"
    if target.exists() and not force:
        _fail(f"{target} already exists (use --force to overwrite)")

    code_dir = scan_dir or directory
    # Always scan. `-y` used to skip it, so `init -y` next to a tool that deletes records produced the pack's
    # read-only example tools and announced "T1 (read-only)" — a confident wrong answer about the one thing
    # this tool exists to get right. Guessed side effects need review; inventing tools is worse.
    found: list[FoundTool] = scan(code_dir)

    # 1. pack
    pid = pack
    if pid is None and non_interactive:
        pid = "generic"
    if pid is None:
        packs = sorted(list_packs(), key=lambda p: (p["id"] != "generic", p["id"]))
        console.print("[bold]Which kind of agent is closest?[/bold] (adds sector-specific checks)")
        for i, p in enumerate(packs, 1):
            console.print(f"  {i}. [cyan]{p['id']}[/cyan]: {p['description']}")
        choice = typer.prompt("Pick a number", default="1")
        pid = packs[int(choice) - 1]["id"] if choice.isdigit() and 0 < int(choice) <= len(packs) else choice
    try:
        pdir = pack_dir(pid)
    except ConfigError as e:
        _fail(str(e))
    template = yaml.safe_load((pdir / "agent.eval.yaml").read_text())
    template.setdefault("risk", {})["pack"] = pid

    # 2. start from the pack template only if we found no tools in the code
    if found:
        data = {"version": 1, "agent": {"name": "my-agent", "description": ""}, "risk": {"pack": pid},
                "tools": to_profile_tools(found), "policies": []}
        fw = next((t.framework for t in found if t.framework), "")
        if fw:
            data["agent"]["framework"] = fw
        data["success_criteria"] = [{"id": "SC1", "kind": "task", "description": "Completes its core tasks correctly",
                                     "metric": "pass_rate", "target": 0.9}]
    else:
        data = template

    if found:
        console.print(f"\n[bold]Found {len(found)} tools in {code_dir.resolve().name}/[/bold]. "
                      "What each tool can do sets the risk tier:")
        for k, (se, desc) in SIDE_EFFECTS.items():
            console.print(f"  [cyan]{k}[/cyan] = {se}: {desc}")
        for t, entry in zip(found, data["tools"], strict=True):
            loc = f"[dim]{t.file}:{t.line}[/dim]"
            if non_interactive:
                console.print(f"  {t.name:<28} [cyan]{t.side_effect:<12}[/cyan] (guessed) {loc}")
                continue
            ans = typer.prompt(f"  {t.name} ({t.file}:{t.line})", default=_BY_NAME[t.side_effect])
            se = SIDE_EFFECTS.get(ans.strip().lower()[:1], (t.side_effect, ""))[0]
            entry["side_effect"] = se
            if se == "read":
                if typer.confirm(f"    does {t.name} return text other people wrote (emails, web pages, tickets)?",
                                 default=bool(entry.get("untrusted_output"))):
                    entry["untrusted_output"] = True
                else:
                    entry.pop("untrusted_output", None)
            if se not in ("read", "notify") and typer.confirm(f"    does a human approve every {t.name} call?",
                                                              default=False):
                entry["requires_approval"] = True

    if not non_interactive:
        desc = typer.prompt("In one sentence, what does your agent do?", default="", show_default=False)
        data["agent"]["description"] = desc
        folder = _slug(directory.resolve().name)
        data["agent"]["name"] = name or typer.prompt("Agent name", default=folder if found or len(folder) > 1
                                                     else data["agent"]["name"])
        if not found and not typer.confirm(
                f"No tools found in {code_dir}. Use the {len(data.get('tools', []))} example tools from the "
                f"'{pid}' pack as a starting point?", default=True):
            data["tools"], data["policies"] = [], []
        sens = typer.prompt("Does it handle personal data (p), regulated data like money or health (r), or neither (n)?",
                            default="n")
        if sens.lower().startswith(("p", "r")):
            level = "regulated" if sens.lower().startswith("r") else "personal"
            for t in data.get("tools", []):
                t["data_sensitivity"] = level if level == "regulated" else t.get("data_sensitivity", level)
            data["risk"]["regulated"] = level == "regulated"
        else:
            data["risk"].pop("regulated", None)
        if found or not data.get("policies"):
            console.print("\nRules your agent must never break, in plain words (blank line to finish), e.g. "
                          "'never refund more than $100 without a human'. They become policies; add a `check:` to "
                          "each so every run is verified (docs/reference.md).")
            rules = list(data.get("policies") or [])
            while len(rules) < 10:
                rule = typer.prompt(f"  rule {len(rules) + 1}", default="", show_default=False).strip()
                if not rule:
                    break
                rules.append({"id": f"RULE-{len(rules) + 1}", "rule": rule})
            data["policies"] = rules
        guess = find_entrypoint(code_dir) if found else None
        ans = typer.prompt(
            "Python entrypoint called as fn(input, context), as module:function ('-' if you'll only replay traces)",
            default=guess or data["agent"].get("entrypoint") or "-", show_default=True).strip()
        data["agent"]["entrypoint"] = None if ans in ("-", "") else ans
    else:
        if name:
            data["agent"]["name"] = name
        if found:
            data["agent"]["entrypoint"] = find_entrypoint(code_dir)
    if found:
        model = find_model(code_dir)
        if model:
            data["agent"]["model"] = model
    ep_ref = data["agent"].get("entrypoint")
    if ep_ref:
        mod = ep_ref.split(":")[0].replace(".", "/")
        if not ((directory / f"{mod}.py").exists() or (directory / mod / "__init__.py").exists()):
            console.print(f"[dim]note: {ep_ref} not found in {directory}; set agent.entrypoint when your agent "
                          "exists (or replay traces with `evalplane run --traces`)[/dim]")
            ep_ref = None
    if ep_ref:
        data["agent"]["entrypoint"] = ep_ref
    else:
        data["agent"].pop("entrypoint", None)

    try:
        profile = AgentProfile.model_validate(data)
        decision = derive_tier(profile)
    except (ValidationError, ConfigError) as e:
        _fail(f"generated profile is invalid: {e}")
    directory.mkdir(parents=True, exist_ok=True)
    header = (SCHEMA_HINT + "# Evalplane agent profile. Docs: https://github.com/vishnukannaujia/evalplane\n"
              f"# Risk tier is derived from your tools: currently {decision.tier.value} ({TIER_NAMES[decision.tier]}).\n")
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    actions = [t for t in profile.tools if t.is_action]
    if actions and not data.get("policies"):
        body = body.replace("policies: []\n", POLICY_STUB.format(tool=actions[0].name))
    target.write_text(header + body)

    evals_dir = directory / "evals"
    evals_dir.mkdir(exist_ok=True)
    starter_out = evals_dir / "starter.yaml"
    if not starter_out.exists():
        if found:
            cases = [{"id": f"{t.name.replace('_', '-')}-happy-path",
                      "input": f"TODO: a request where {t.name} is the right tool",
                      "expect": {"tools": [t.name]}} for t in found[:10]]
            starter_out.write_text("# Starter cases: replace every TODO input with a real request, then run "
                                   "`evalplane coverage` for what's still missing.\n"
                                   + yaml.safe_dump({"suite": "starter", "cases": cases}, sort_keys=False))
        elif (pdir / "cases.yaml").exists():
            shutil.copy(pdir / "cases.yaml", starter_out)
    console.print(f"\n[green]Created[/green] {target} and {starter_out}")
    console.print(f"Risk tier: [bold]{decision.tier.value}[/bold] ({TIER_NAMES[decision.tier]})")
    # the tool that set the tier first: under a T4 headline, a read-only tool is a confusing first line
    for r in sorted(decision.reasons, key=lambda r: f": {decision.tier.value} (" not in r):
        console.print(f"  [dim]- {r}[/dim]")
    console.print("\nNext:\n  [cyan]evalplane coverage[/cyan]   your to-do list: what is untested, most important first\n"
                  "  [cyan]evalplane plan[/cyan]       every eval this agent needs, and why\n"
                  "  [cyan]evalplane run[/cyan]        run the cases in evals/\n"
                  "[dim]using Claude Code, Cursor or another MCP client? "
                  "`evalplane mcp` exposes plan/coverage/audit as tools[/dim]")
