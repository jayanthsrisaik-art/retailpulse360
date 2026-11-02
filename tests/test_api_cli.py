import pytest
from fastapi.testclient import TestClient

from retailpulse.api import create_app
from retailpulse.cli import main, report


@pytest.fixture(scope="module")
def client(synth):
    return TestClient(create_app(synth["db"]))


@pytest.fixture(scope="module")
def sample_client(sample_db):
    return TestClient(create_app(sample_db))


def test_health_and_meta(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["last_etl_run"]["status"] == "succeeded"
    m = client.get("/api/meta").json()
    assert len(m["stores"]) == 9 and "Grocery" in m["categories"]
    assert all(c["passed"] for c in m["dq_checks"])


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "RetailPulse 360" in r.text


def test_kpi_endpoints_on_sample(sample_client):
    s = sample_client.get("/api/kpis/summary", params={"start": "2026-06-01", "end": "2026-06-08"}).json()
    assert s["net_sales"] == 190.0
    b = sample_client.get("/api/kpis/breakdown", params={"by": "channel", "start": "2026-06-01"}).json()
    assert {r["key"] for r in b} == {"online", "store"}
    assert sample_client.get("/api/products/top", params={"start": "2026-06-01"}).json()[0]["product_id"] == "P002"
    assert sample_client.get("/api/kpis/summary", params={"start": "2026-06-09", "end": "2026-06-01"}).status_code == 422
    assert sample_client.get("/api/kpis/summary", params={"start": "June"}).status_code == 422
    assert sample_client.get("/api/kpis/timeseries", params={"grain": "hour"}).status_code == 422


def test_inventory_endpoint(sample_client):
    r = sample_client.get("/api/inventory/alerts", params={"status": "reorder"}).json()
    assert r["counts"] == {"stockout": 1, "reorder": 1, "overstock": 1, "no_recent_sales": 1}
    assert [i["product_id"] for i in r["items"]] == ["P002"]
    assert r["suggested_order_cost_total"] == 40.0


def test_customer_360_and_risk(client):
    risk = client.get("/api/customers/at-risk", params={"limit": 5}).json()
    assert risk and all(r["churn_probability"] >= 0.6 for r in risk)
    assert [r["value_at_risk"] for r in risk] == sorted((r["value_at_risk"] for r in risk), reverse=True)
    cid = risk[0]["customer_id"]
    c = client.get(f"/api/customers/{cid}").json()
    assert c["profile"]["customer_id"] == cid
    assert c["score"]["churn_probability_90d"] == risk[0]["churn_probability"]
    assert len(c["recent_orders"]) <= 10 and c["lifetime"]["orders"] >= len(c["recent_orders"])
    assert client.get("/api/customers/GUEST").status_code == 404
    assert client.get("/api/customers/NOPE").status_code == 404


def test_segments_and_model(client, sample_client):
    segs = client.get("/api/segments").json()
    assert sum(s["customers"] for s in segs) > 0
    assert client.get("/api/model/churn").json()["horizon_days"] == 90
    assert sample_client.get("/api/model/churn").status_code == 404


def test_cli_report(synth, sample_db, capsys):
    r = report(str(synth["db"]))
    assert r["churn_model"]["selected_model"]
    main(["report", "--db", str(sample_db)])
    out = capsys.readouterr().out
    assert "net sales 190.00" in out and "skipped" in out
