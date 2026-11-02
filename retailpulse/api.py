"""FastAPI service: KPIs, customer 360, churn risk, inventory alerts and a dashboard."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from . import etl, inventory, kpis

STATIC = Path(__file__).parent / "static"


def create_app(db_path: str | Path) -> FastAPI:
    conn = etl.connect(db_path)
    app = FastAPI(title="RetailPulse 360", version="1.0.0",
                  description="Retail KPIs, customer 360 with churn risk, and inventory alerts.")
    lo, hi = kpis.data_range(conn)

    def _dates(start: Optional[str], end: Optional[str], default_days: int = 28) -> tuple[str, str]:
        e = end or hi
        s = start or (date.fromisoformat(e) - timedelta(days=default_days - 1)).isoformat()
        try:
            if date.fromisoformat(s) > date.fromisoformat(e):
                raise HTTPException(422, "start must be on or before end")
        except ValueError as err:
            raise HTTPException(422, f"dates must be YYYY-MM-DD: {err}") from err
        return s, e

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        run = conn.execute("SELECT run_id, status, finished_at FROM etl_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return {"status": "ok", "data_from": lo, "data_to": hi, "last_etl_run": dict(run) if run else None}

    @app.get("/api/meta")
    def meta():
        run = conn.execute("SELECT * FROM etl_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return {
            "data_from": lo, "data_to": hi,
            "stores": [dict(r) for r in conn.execute("SELECT store_id, store_name, region FROM dim_store ORDER BY 1")],
            "categories": [r[0] for r in conn.execute("SELECT DISTINCT category FROM dim_product ORDER BY 1")],
            "etl_run": {**dict(run), "stats": json.loads(run["stats"])} if run else None,
            "dq_checks": [dict(r) for r in conn.execute(
                "SELECT check_name, passed, detail FROM dq_results WHERE run_id = ?", (run["run_id"],))] if run else [],
        }

    @app.get("/api/kpis/summary")
    def kpi_summary(start: Optional[str] = None, end: Optional[str] = None, store: Optional[str] = None,
                    category: Optional[str] = None):
        s, e = _dates(start, end)
        return kpis.summary(conn, s, e, store=store, category=category)

    @app.get("/api/kpis/compare")
    def kpi_compare(end: Optional[str] = None, days: int = Query(28, ge=1, le=366), store: Optional[str] = None,
                    category: Optional[str] = None):
        _, e = _dates(None, end)
        return kpis.period_comparison(conn, e, days, store=store, category=category)

    @app.get("/api/kpis/timeseries")
    def kpi_timeseries(start: Optional[str] = None, end: Optional[str] = None,
                       grain: Literal["day", "week", "month"] = "week",
                       by: Optional[Literal["store", "region", "category", "channel"]] = None,
                       store: Optional[str] = None, category: Optional[str] = None):
        s, e = _dates(start, end, default_days=365)
        return kpis.timeseries(conn, s, e, grain, by, store, category)

    @app.get("/api/kpis/breakdown")
    def kpi_breakdown(by: Literal["store", "region", "category", "channel"] = "category",
                      start: Optional[str] = None, end: Optional[str] = None):
        s, e = _dates(start, end)
        return kpis.breakdown(conn, s, e, by)

    @app.get("/api/products/top")
    def products_top(start: Optional[str] = None, end: Optional[str] = None, store: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=100)):
        s, e = _dates(start, end)
        return kpis.top_products(conn, s, e, limit, store)

    @app.get("/api/customers/at-risk")
    def at_risk(limit: int = Query(20, ge=1, le=500), min_probability: float = Query(0.6, ge=0, le=1)):
        """Active customers most likely to lapse, ranked by expected lost spend (probability x 90-day spend)."""
        rows = conn.execute("""
            SELECT cs.customer_id, cs.churn_probability, cs.rfm_segment, cs.features, c.loyalty_tier, c.home_store_id
            FROM customer_scores cs JOIN dim_customer c USING(customer_id)
            WHERE cs.churn_probability >= ?""", (min_probability,)).fetchall()
        out = []
        for r in rows:
            f = json.loads(r["features"])
            out.append({"customer_id": r["customer_id"], "churn_probability": r["churn_probability"],
                        "rfm_segment": r["rfm_segment"], "loyalty_tier": r["loyalty_tier"],
                        "home_store_id": r["home_store_id"], "recency_days": int(f["recency_days"]),
                        "net_last_90": round(f["net_last_90"], 2),
                        "value_at_risk": round(r["churn_probability"] * f["net_last_90"], 2)})
        out.sort(key=lambda x: (-x["value_at_risk"], x["customer_id"]))
        return out[:limit]

    @app.get("/api/customers/{customer_id}")
    def customer_360(customer_id: str):
        c = conn.execute("SELECT * FROM dim_customer WHERE customer_id = ? AND customer_id != 'GUEST'",
                         (customer_id,)).fetchone()
        if c is None:
            raise HTTPException(404, "customer not found")
        life = conn.execute("""
            SELECT COUNT(DISTINCT order_id) orders, COALESCE(SUM(net_amount), 0) net_sales, MIN(date_key) first_order,
                   MAX(date_key) last_order, COALESCE(SUM(quantity), 0) units
            FROM fact_sales WHERE customer_id = ?""", (customer_id,)).fetchone()
        refunds = conn.execute("""SELECT COALESCE(SUM(r.refund_amount), 0), COUNT(*) FROM fact_returns r
                                  JOIN fact_sales s USING(line_id) WHERE s.customer_id = ?""", (customer_id,)).fetchone()
        cats = conn.execute("""SELECT p.category, ROUND(SUM(s.net_amount), 2) net FROM fact_sales s
                               JOIN dim_product p USING(product_id) WHERE s.customer_id = ?
                               GROUP BY 1 ORDER BY 2 DESC""", (customer_id,)).fetchall()
        recent = conn.execute("""SELECT order_id, date_key, store_id, channel, ROUND(SUM(net_amount), 2) net,
                                        SUM(quantity) units
                                 FROM fact_sales WHERE customer_id = ? GROUP BY 1 ORDER BY 2 DESC, 1 DESC LIMIT 10""",
                              (customer_id,)).fetchall()
        score = conn.execute("SELECT * FROM customer_scores WHERE customer_id = ?", (customer_id,)).fetchone()
        return {
            "profile": dict(c),
            "lifetime": {**dict(life), "net_sales": round(life["net_sales"], 2), "refunds": round(refunds[0], 2),
                         "returns": refunds[1]},
            "categories": [dict(r) for r in cats],
            "recent_orders": [dict(r) for r in recent],
            "score": None if score is None else {
                "scored_as_of": score["scored_as_of"], "churn_probability_90d": score["churn_probability"],
                "risk_band": score["risk_band"], "rfm_segment": score["rfm_segment"],
                "features": json.loads(score["features"])},
        }

    @app.get("/api/segments")
    def segments():
        rows = conn.execute("""
            SELECT rfm_segment, COUNT(*) customers, ROUND(AVG(churn_probability), 4) avg_churn_probability,
                   SUM(risk_band = 'high') high_risk, SUM(risk_band = 'lapsed') lapsed,
                   ROUND(AVG(json_extract(features, '$.net_total')), 2) avg_lifetime_net
            FROM customer_scores GROUP BY 1 ORDER BY customers DESC""").fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/inventory/alerts")
    def inventory_alerts(status: Optional[Literal["stockout", "reorder", "overstock", "no_recent_sales", "ok"]] = None,
                         store: Optional[str] = None, limit: int = Query(50, ge=1, le=5000)):
        rows = inventory.assess(conn, store=store)
        counts = inventory.status_counts(rows)
        if status:
            rows = [r for r in rows if r["status"] == status]
        return {"counts": counts, "items": rows[:limit],
                "suggested_order_cost_total": round(sum(r["suggested_order_cost"] for r in rows), 2)}

    @app.get("/api/model/churn")
    def model_eval():
        row = conn.execute("SELECT report FROM model_eval").fetchone()
        if row is None:
            raise HTTPException(404, "no model evaluation stored")
        return json.loads(row[0])

    return app
