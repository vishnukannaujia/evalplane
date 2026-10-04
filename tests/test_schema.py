import json
from pathlib import Path

from typer.testing import CliRunner

from evalplane.cli import app

ROOT = Path(__file__).parent.parent


def test_committed_schemas_are_current():
    for kind, name in (("profile", "agent.eval.schema.json"), ("cases", "cases.schema.json")):
        r = CliRunner().invoke(app, ["schema", "--kind", kind])
        assert r.exit_code == 0
        assert json.loads(r.stdout) == json.loads((ROOT / "schema" / name).read_text()), \
            f"schema/{name} is stale: run `evalplane schema --kind {kind} -o schema/{name}`"


def test_tau2_example_profiles_are_valid():
    """The retail and telecom profiles back published numbers, so they must stay loadable and T4."""
    from pathlib import Path

    from evalplane.models import load_profile
    from evalplane.tiering import derive_tier

    root = Path(__file__).parent.parent / "examples"
    for name in ("tau2-airline", "tau2-retail", "tau2-telecom"):
        p = load_profile(root / name / "agent.eval.yaml")
        assert derive_tier(p).tier.value == "T4", name
        assert p.policies and all(pol.check or pol.rule for pol in p.policies), name

    telecom = load_profile(root / "tau2-telecom" / "agent.eval.yaml")
    bill = next(pol for pol in telecom.policies if pol.id == "READ-BILL-BEFORE-REQUEST")
    # the correction the benchmark forced: either getter reads the bill (88% false positives without it)
    assert bill.check.prior == ["get_bills_for_customer", "get_details_by_id"]


def test_published_benchmark_figures_are_loadable_and_complete():
    """docs/benchmark-tau2.md cites this file; scripts/verify-benchmark.py recomputes it.

    The figures themselves need the τ² corpus, which is not in the repo — this only pins the shape, so a
    renamed key fails here rather than silently skipping a check in the verifier.
    """
    import json
    from pathlib import Path

    figures = json.loads((Path(__file__).parent.parent / "docs" / "benchmark-figures.json").read_text())
    assert set(figures) >= {"corpus", "results_table", "effect_split", "like_for_like_three_tools",
                            "gradient_per_100_action_calls", "scalars", "not_machine_checked"}
    for domain in ("airline", "retail", "telecom"):
        assert set(figures["results_table"][domain]) == {
            "trajectories", "solved", "breaking", "solved_and_breaking", "lenient_rate_pct"}
        assert set(figures["effect_split"][domain]) == {"effective", "blocked"}
        assert figures["corpus"][domain], f"{domain} needs file patterns"
    assert figures["not_machine_checked"], "say what is not covered, or the absence of a failure misleads"
