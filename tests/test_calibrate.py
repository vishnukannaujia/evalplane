import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from evalplane.cli import app, cohen_kappa


def test_cohen_kappa():
    assert cohen_kappa([(True, True), (False, False)] * 5) == pytest.approx(1.0)
    assert cohen_kappa([(True, False), (False, True)] * 5) == pytest.approx(-1.0)
    mixed = [(True, True)] * 6 + [(False, False)] * 2 + [(True, False)] * 2
    assert 0 < cohen_kappa(mixed) < 1


def test_calibrate_with_a_fake_judge(tmp_path, monkeypatch):
    src = Path(__file__).parent.parent / "examples" / "faq-messages"
    d = tmp_path / "faq"
    shutil.copytree(src, d, ignore=shutil.ignore_patterns(".evalplane", "__pycache__"))
    cases = [{"id": f"q{i}", "input": "How long does shipping take?",
              "expect": {"judge": {"rubric": "correct", "threshold": 0.5}}} for i in range(6)]
    (d / "evals" / "judged.yaml").write_text(yaml.safe_dump({"cases": cases}))
    # judge: passes everything except q5
    (d / "my_judge.py").write_text("def judge(rubric, run, case):\n    return (0.0 if case.id == 'q5' else 1.0), 'ok'\n")
    monkeypatch.chdir(d)
    cli = CliRunner()
    assert cli.invoke(app, ["run", "-q", "--judge", "my_judge:judge"]).exit_code in (0, 1)
    labels = {f"q{i}": "pass" for i in range(5)} | {"q5": "fail"}
    (d / "labels.yaml").write_text(yaml.safe_dump({"labels": labels}))
    r = cli.invoke(app, ["calibrate", "labels.yaml"])
    assert r.exit_code == 0, r.output
    assert "kappa 1.00" in r.output and (d / ".evalplane" / "calibration.json").exists()
