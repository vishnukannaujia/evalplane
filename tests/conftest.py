from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
REPO = TESTS_DIR.parent
EXAMPLE = REPO / "examples" / "support-refund"
FIXTURES = TESTS_DIR / "fixtures"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def example_copy(tmp_path: Path) -> Path:
    """A private copy of examples/support-refund (without previous results), using its real pack."""
    dst = tmp_path / "support-refund"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".evalplane", "__pycache__"))
    return dst
