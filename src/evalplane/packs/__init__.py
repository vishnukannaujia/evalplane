"""Sector packs: starter profiles, extra rules and starter cases for common kinds of agents.

A pack is a directory with `pack.yaml` (id, name, description, default_tier, rules, thresholds),
`agent.eval.yaml` (a template profile) and `cases.yaml` (starter eval cases).
Built-in packs live next to this file. Third-party packs can register through the
`evalplane.packs` entry-point group (pointing at a directory path), or be referenced by path.
"""

from __future__ import annotations

from importlib import metadata, resources
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError


def _builtin_root() -> Path:
    return Path(str(resources.files("evalplane") / "packs"))


def _pack_dirs() -> dict[str, Path]:
    dirs: dict[str, Path] = {}
    root = _builtin_root()
    for d in sorted(root.iterdir()):
        if (d / "pack.yaml").exists():
            dirs[d.name] = d
    try:
        eps = metadata.entry_points(group="evalplane.packs")
    except TypeError:  # pragma: no cover - old importlib
        eps = []
    for ep in eps:
        try:
            target = ep.load()
            path = Path(target() if callable(target) else target)
            if (path / "pack.yaml").exists():
                dirs.setdefault(ep.name, path)
        except Exception:  # noqa: BLE001 - a broken plugin must not break the CLI
            continue
    return dirs


def list_packs() -> list[dict[str, Any]]:
    out = []
    for pid, d in _pack_dirs().items():
        meta = yaml.safe_load((d / "pack.yaml").read_text()) or {}
        out.append({"id": pid, "name": meta.get("name", pid), "description": meta.get("description", ""),
                    "default_tier": meta.get("default_tier", ""), "path": str(d)})
    return out


def pack_dir(pack_id: str, base_dir: Path | None = None) -> Path:
    candidate = Path(pack_id)
    if base_dir is not None and not candidate.is_absolute():
        candidate = base_dir / candidate
    if (candidate / "pack.yaml").exists():
        return candidate
    dirs = _pack_dirs()
    if pack_id not in dirs:
        raise ConfigError(f"unknown pack '{pack_id}'. Available: {', '.join(sorted(dirs))}")
    return dirs[pack_id]


def get_pack(pack_id: str = "generic", base_dir: Path | None = None) -> dict[str, Any]:
    d = pack_dir(pack_id, base_dir)
    meta = yaml.safe_load((d / "pack.yaml").read_text()) or {}
    meta.setdefault("id", d.name)
    meta["_dir"] = str(d)
    return meta
