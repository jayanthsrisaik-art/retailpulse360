import json

import pytest

from retailpulse import etl, inventory, kpis

from .conftest import SAMPLES

EXPECTED = json.loads((SAMPLES / "expected_outputs.json").read_text())


def test_summary_matches_hand_computed_sample(sample_db):
    s = kpis.summary(etl.connect(sample_db), "2026-06-01", "2026-06-08")
    assert s == EXPECTED["summary_2026-06-01_2026-06-08"]
    # spelled out: lines net 20 + 40 + 100 + 30, cost 12 + 20 + 40 + 18, one of 8 units returned for 50
    assert (s["net_sales"], s["gross_margin"], s["orders"], s["units"]) == (190.0, 100.0, 3, 8)
    assert (s["refunds"], s["return_rate_units"], s["customers"]) == (50.0, 0.125, 2)


def test_summary_filters(sample_db):
    c = etl.connect(sample_db)
    web = kpis.summary(c, "2026-06-01", "2026-06-08", store="WEB")
    assert (web["net_sales"], web["orders"], web["refunds"]) == (100.0, 1, 50.0)
    groc = kpis.summary(c, "2026-06-01", "2026-06-08", category="Grocery")
    assert (groc["net_sales"], groc["units"], groc["refunds"]) == (50.0, 5, 0.0)
    empty = kpis.summary(c, "2030-01-01", "2030-01-31")
    assert empty["orders"] == 0 and empty["avg_order_value"] is None


def test_breakdown_and_timeseries(sample_db):
    c = etl.connect(sample_db)
    assert kpis.breakdown(c, "2026-06-01", "2026-06-08", "category") == EXPECTED["breakdown_category"]
    weeks = kpis.timeseries(c, "2026-06-01", "2026-06-08", "week")
    assert [(w["period"], w["net_sales"]) for w in weeks] == [("2026-06-01", 160.0), ("2026-06-08", 30.0)]
    by_ch = kpis.timeseries(c, "2026-06-01", "2026-06-08", "month", by="channel")
    assert {(r["group"], r["net_sales"]) for r in by_ch} == {("online", 100.0), ("store", 90.0)}
    with pytest.raises(ValueError):
        kpis.timeseries(c, "2026-06-01", "2026-06-08", "hour")


def test_week_does_not_split_at_year_boundary(synth):
    c = etl.connect(synth["db"])
    weeks = kpis.timeseries(c, "2025-12-29", "2026-01-04", "week")
    assert [w["period"] for w in weeks] == ["2025-12-29"]


def test_period_comparison_growth(synth):
    c = etl.connect(synth["db"])
    _, hi = kpis.data_range(c)
    cmp = kpis.period_comparison(c, hi, 28)
    cur, ly = cmp["current"], cmp["last_year"]
    assert cmp["vs_last_year"]["net_sales"] == pytest.approx(cur["net_sales"] / ly["net_sales"] - 1, abs=1e-4)
    assert cur["end"] == hi


def test_inventory_rules_on_sample(sample_db):
    rows = inventory.assess(etl.connect(sample_db))
    assert rows == EXPECTED["inventory"]
    by = {(r["store_id"], r["product_id"]): r for r in rows}
    assert by[("S01", "P001")]["status"] == "stockout"
    assert by[("WEB", "P002")]["status"] == "reorder" and by[("WEB", "P002")]["suggested_order_qty"] == 2
    assert by[("S01", "P002")]["status"] == "overstock"
    assert by[("WEB", "P001")]["status"] == "no_recent_sales"
    assert [r["status"] for r in rows] == ["stockout", "reorder", "overstock", "no_recent_sales"]


def test_inventory_counts_cover_every_snapshot_row(synth):
    c = etl.connect(synth["db"])
    rows = inventory.assess(c)
    assert len(rows) == c.execute("SELECT COUNT(*) FROM fact_inventory").fetchone()[0]
    assert all(r["suggested_order_qty"] == 0 for r in rows if r["status"] not in ("stockout", "reorder"))
