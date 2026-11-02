import json
import sqlite3

import pytest

from retailpulse import etl

from .conftest import SAMPLES


def test_sample_quarantine_reasons(sample_db):
    conn = etl.connect(sample_db)
    got = [dict(r) for r in conn.execute("SELECT source, record_key, reason FROM etl_quarantine ORDER BY source, record_key")]
    assert got == json.loads((SAMPLES / "expected_outputs.json").read_text())["quarantine"]
    reasons = {r["record_key"]: r["reason"] for r in got}
    assert reasons["OBAD"] == "unparseable order_ts"
    assert reasons["L5"] == "unknown product"
    assert reasons["L6"] == "non-positive quantity"
    assert reasons["L7"] == "order missing or quarantined"
    assert reasons["R3"] == "returns exceed quantity sold"


def test_sample_fact_amounts_and_guest_member(sample_db):
    conn = etl.connect(sample_db)
    rows = {r["line_id"]: dict(r) for r in conn.execute("SELECT * FROM fact_sales")}
    assert set(rows) == {"L1", "L2", "L3", "L4"}  # exact duplicate of L1 dropped
    assert (rows["L2"]["gross_amount"], rows["L2"]["net_amount"], rows["L2"]["cost_amount"]) == (50.0, 40.0, 20.0)
    assert rows["L4"]["customer_id"] == "GUEST"
    assert rows["L3"]["date_key"] == "2026-06-02" and rows["L3"]["channel"] == "online"
    refund = conn.execute("SELECT refund_amount FROM fact_returns WHERE return_id = 'R1'").fetchone()[0]
    assert refund == 50.0
    run = conn.execute("SELECT status, stats FROM etl_runs").fetchone()
    assert run["status"] == "succeeded"
    assert json.loads(run["stats"])["duplicates_dropped"] == {"order_lines": 1}


def test_all_dq_checks_pass(sample_db, synth):
    for db in (sample_db, synth["db"]):
        conn = etl.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM dq_results WHERE passed = 0").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM dq_results").fetchone()[0] == len(etl.DQ_CHECKS) + 1


def test_synthetic_problems_are_caught(synth):
    r = synth["result"]
    assert r["duplicates_dropped"]["order_lines"] > 0
    assert r["quarantined"]["orders"] == 5
    assert r["quarantined"]["order_lines"] >= 20  # 12 unknown products + 8 bad quantities (+ orphans)
    assert r["quarantined"]["returns"] >= 6
    loaded = r["loaded"]["fact_sales"] + r["quarantined"]["order_lines"] + r["duplicates_dropped"]["order_lines"]
    assert loaded == synth["counts"]["order_lines"]  # every raw line is accounted for


def test_rebuild_is_deterministic(synth, tmp_path):
    db2 = tmp_path / "again.db"
    etl.build_warehouse(synth["raw"], db2)
    q = "SELECT COUNT(*), ROUND(SUM(net_amount), 2), ROUND(SUM(cost_amount), 2) FROM fact_sales"
    a, b = sqlite3.connect(synth["db"]), sqlite3.connect(db2)
    assert a.execute(q).fetchone() == b.execute(q).fetchone()
    s = "SELECT customer_id, churn_probability, rfm_segment FROM customer_scores ORDER BY 1"
    assert a.execute(s).fetchall() == b.execute(s).fetchall()


def test_failed_dq_keeps_previous_warehouse(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    etl.build_warehouse(SAMPLES / "raw", db)
    before = db.read_bytes()
    monkeypatch.setitem(etl.DQ_CHECKS, "always_fails", "SELECT 1")
    with pytest.raises(etl.DataQualityError, match="always_fails"):
        etl.build_warehouse(SAMPLES / "raw", db)
    assert db.read_bytes() == before
    assert not (tmp_path / "w.db.building").exists()


def test_connect_missing_warehouse(tmp_path):
    with pytest.raises(FileNotFoundError):
        etl.connect(tmp_path / "nope.db")


def test_warehouse_is_opened_read_only(sample_db):
    conn = etl.connect(sample_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM fact_sales")
