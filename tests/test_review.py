import shutil
from pathlib import Path

import yaml
from typer.testing import CliRunner

from evalplane.cli import app

TAU = Path(__file__).parent.parent / "examples" / "tau2-airline"


def test_review_then_promote_reviewed(tmp_path, monkeypatch):
    d = tmp_path / "tau"
    shutil.copytree(TAU, d, ignore=shutil.ignore_patterns(".evalplane", "__pycache__"))
    monkeypatch.chdir(d)
    cli = CliRunner()
    # rule breakers are shown first: label two as bad (one mode), skip one, mark one good
    r = cli.invoke(app, ["review", "traces/"], input="b skipped confirmation\nb skipped confirmation\ns\ng\n")
    assert r.exit_code == 0, r.output
    assert "2 × skipped confirmation" in r.output and "1 good" in r.output
    again = cli.invoke(app, ["review", "traces/", "--summary"])
    assert "Reviewed 3 runs" in again.output
    p = cli.invoke(app, ["promote", "traces/", "--reviewed"])
    assert p.exit_code == 0, p.output
    data = yaml.safe_load((d / "evals" / "promoted.yaml").read_text().split("\n", 2)[2])
    assert len(data["cases"]) == 3
    assert sum(1 for c in data["cases"] if c.get("tags") == ["skipped-confirmation"]) == 2
