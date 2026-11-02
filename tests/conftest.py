import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from retailpulse import etl, generate  # noqa: E402

SAMPLES = ROOT / "samples"


@pytest.fixture(scope="session")
def sample_db(tmp_path_factory):
    """Warehouse built from the hand-written sample export (exact expected numbers)."""
    db = tmp_path_factory.mktemp("sample") / "w.db"
    etl.build_warehouse(SAMPLES / "raw", db)
    return db


@pytest.fixture(scope="session")
def synth(tmp_path_factory):
    """Warehouse built from a small synthetic export (big enough for the churn backtest)."""
    base = tmp_path_factory.mktemp("synth")
    counts = generate.generate(base / "raw", n_customers=900, seed=3)
    result = etl.build_warehouse(base / "raw", base / "w.db")
    return {"raw": base / "raw", "db": base / "w.db", "counts": counts, "result": result}
