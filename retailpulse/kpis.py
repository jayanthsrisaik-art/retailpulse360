"""KPI queries over the star schema.

All money figures are net of line discounts; "net of returns" figures subtract
refunds *dated within the same period* (the usual retail convention, so a
period's numbers do not change after it closes).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

# week periods are labelled by their Monday so weeks never split at a year boundary
GRAINS = {"day": "s.date_key", "week": "date(s.date_key, '-6 days', 'weekday 1')", "month": "substr(s.date_key, 1, 7)"}
DIMENSIONS = {"store": "s.store_id", "region": "st.region", "category": "p.category", "channel": "s.channel"}


def data_range(conn: sqlite3.Connection) -> tuple[str, str]:
    lo, hi = conn.execute("SELECT MIN(date_key), MAX(date_key) FROM fact_sales").fetchone()
    return lo, hi


def _filters(start: str | None, end: str | None, store: str | None, category: str | None) -> tuple[str, list]:
    where, args = [], []
    if start:
        where.append("s.date_key >= ?")
        args.append(start)
    if end:
        where.append("s.date_key <= ?")
        args.append(end)
    if store:
        where.append("s.store_id = ?")
        args.append(store)
    if category:
        where.append("p.category = ?")
        args.append(category)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def summary(conn: sqlite3.Connection, start: str, end: str, store: str | None = None,
            category: str | None = None) -> dict:
    where, args = _filters(start, end, store, category)
    row = conn.execute(f"""
        SELECT COUNT(DISTINCT s.order_id) orders, COALESCE(SUM(s.net_amount), 0) net_sales,
               COALESCE(SUM(s.gross_amount), 0) gross_sales, COALESCE(SUM(s.cost_amount), 0) cost,
               COALESCE(SUM(s.quantity), 0) units,
               COUNT(DISTINCT CASE WHEN s.customer_id != 'GUEST' THEN s.customer_id END) customers
        FROM fact_sales s JOIN dim_product p USING(product_id) {where}""", args).fetchone()

    rwhere, rargs = _filters(start, end, store, category)
    rwhere = rwhere.replace("s.date_key", "r.return_date")
    refunds = conn.execute(f"""
        SELECT COALESCE(SUM(r.refund_amount), 0), COALESCE(SUM(r.quantity), 0)
        FROM fact_returns r JOIN fact_sales s USING(line_id) JOIN dim_product p USING(product_id) {rwhere}""",
                           rargs).fetchone()
    orders, net, gross, cost, units, customers = row
    return {
        "start": start, "end": end, "store": store, "category": category,
        "orders": orders, "customers": customers, "units": units,
        "gross_sales": round(gross, 2), "net_sales": round(net, 2),
        "discounts": round(gross - net, 2),
        "gross_margin": round(net - cost, 2),
        "gross_margin_pct": round((net - cost) / net, 4) if net else None,
        "avg_order_value": round(net / orders, 2) if orders else None,
        "units_per_order": round(units / orders, 3) if orders else None,
        "refunds": round(refunds[0], 2),
        "net_sales_after_returns": round(net - refunds[0], 2),
        "return_rate_units": round(refunds[1] / units, 4) if units else None,
    }


def period_comparison(conn: sqlite3.Connection, end: str, days: int = 28, **filters) -> dict:
    """Current window vs the previous window and the same window one year (364 days) earlier."""
    e = date.fromisoformat(end)
    cur = summary(conn, (e - timedelta(days=days - 1)).isoformat(), end, **filters)
    prev_end = e - timedelta(days=days)
    prev = summary(conn, (prev_end - timedelta(days=days - 1)).isoformat(), prev_end.isoformat(), **filters)
    ly_end = e - timedelta(days=364)  # 52 weeks: keeps weekday mix identical
    ly = summary(conn, (ly_end - timedelta(days=days - 1)).isoformat(), ly_end.isoformat(), **filters)

    def growth(a, b):
        return round(a / b - 1, 4) if a is not None and b else None

    keys = ("net_sales", "orders", "avg_order_value", "gross_margin", "customers")
    return {"current": cur, "previous": prev, "last_year": ly,
            "vs_previous": {k: growth(cur[k], prev[k]) for k in keys},
            "vs_last_year": {k: growth(cur[k], ly[k]) for k in keys}}


def timeseries(conn: sqlite3.Connection, start: str, end: str, grain: str = "week",
               by: str | None = None, store: str | None = None, category: str | None = None) -> list[dict]:
    if grain not in GRAINS:
        raise ValueError(f"grain must be one of {sorted(GRAINS)}")
    if by is not None and by not in DIMENSIONS:
        raise ValueError(f"by must be one of {sorted(DIMENSIONS)}")
    where, args = _filters(start, end, store, category)
    g = GRAINS[grain]
    dim = DIMENSIONS[by] if by else "'all'"
    rows = conn.execute(f"""
        SELECT {g} period, {dim} dim, COUNT(DISTINCT s.order_id), SUM(s.net_amount), SUM(s.net_amount - s.cost_amount),
               SUM(s.quantity)
        FROM fact_sales s JOIN dim_product p USING(product_id) JOIN dim_store st USING(store_id) {where}
        GROUP BY 1, 2 ORDER BY 1, 2""", args).fetchall()
    return [{"period": r[0], "group": r[1], "orders": r[2], "net_sales": round(r[3], 2),
             "gross_margin": round(r[4], 2), "units": r[5]} for r in rows]


def breakdown(conn: sqlite3.Connection, start: str, end: str, by: str = "category") -> list[dict]:
    if by not in DIMENSIONS:
        raise ValueError(f"by must be one of {sorted(DIMENSIONS)}")
    dim = DIMENSIONS[by]
    joins = "FROM fact_sales s JOIN dim_product p USING(product_id) JOIN dim_store st USING(store_id)"
    rows = conn.execute(f"""
        SELECT {dim}, SUM(s.net_amount), SUM(s.net_amount - s.cost_amount), COUNT(DISTINCT s.order_id), SUM(s.quantity)
        {joins} WHERE s.date_key BETWEEN ? AND ? GROUP BY 1 ORDER BY 2 DESC""", (start, end)).fetchall()
    returned = dict(conn.execute(f"""
        SELECT {dim}, SUM(r.quantity) FROM fact_returns r JOIN fact_sales s USING(line_id)
        JOIN dim_product p ON p.product_id = s.product_id JOIN dim_store st ON st.store_id = s.store_id
        WHERE r.return_date BETWEEN ? AND ? GROUP BY 1""", (start, end)).fetchall())
    total = sum(r[1] for r in rows) or 1.0
    return [{"key": k, "net_sales": round(net, 2), "share": round(net / total, 4),
             "gross_margin_pct": round(gm / net, 4) if net else None, "orders": orders, "units": units,
             "return_rate_units": round(returned.get(k, 0) / units, 4) if units else None}
            for k, net, gm, orders, units in rows]


def top_products(conn: sqlite3.Connection, start: str, end: str, limit: int = 10, store: str | None = None) -> list[dict]:
    where, args = _filters(start, end, store, None)
    rows = conn.execute(f"""
        SELECT p.product_id, p.product_name, p.category, SUM(s.quantity), SUM(s.net_amount),
               SUM(s.net_amount - s.cost_amount)
        FROM fact_sales s JOIN dim_product p USING(product_id) {where}
        GROUP BY 1 ORDER BY 5 DESC LIMIT ?""", [*args, limit]).fetchall()
    return [{"product_id": r[0], "product_name": r[1], "category": r[2], "units": r[3],
             "net_sales": round(r[4], 2), "gross_margin": round(r[5], 2)} for r in rows]
