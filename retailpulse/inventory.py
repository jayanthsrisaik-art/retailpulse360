"""Inventory health and reorder suggestions from the latest stock snapshot.

For every store x product in the snapshot:

    daily demand    mean and std of daily units over the trailing ``window`` days
    safety stock    z * std * sqrt(lead_time)            (z = 1.65 ~ 95% cycle service)
    reorder point   mean * lead_time + safety stock
    order-up-to     reorder point + mean * review_days
    suggested qty   max(0, ceil(order-up-to - on_hand - on_order)) when position <= reorder point

Status, in priority order: ``stockout`` (nothing on hand but recent demand),
``reorder`` (inventory position at or below the reorder point), ``overstock``
(more than ``overstock_days`` of cover), ``no_recent_sales`` (stock but no
demand in the window), otherwise ``ok``.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

Z = 1.65


def assess(conn: sqlite3.Connection, window: int = 28, review_days: int = 7, overstock_days: int = 90,
           store: str | None = None) -> list[dict]:
    snap = conn.execute("SELECT MAX(snapshot_date) FROM fact_inventory").fetchone()[0]
    if snap is None:
        return []
    end = date.fromisoformat(snap)
    start = end - timedelta(days=window - 1)
    daily: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for st, pid, dk, q in conn.execute(
            "SELECT store_id, product_id, date_key, SUM(quantity) FROM fact_sales WHERE date_key BETWEEN ? AND ? "
            "GROUP BY 1, 2, 3", (start.isoformat(), end.isoformat())):
        daily[(st, pid)][dk] = q
    sql = """SELECT i.store_id, i.product_id, p.product_name, p.category, p.lead_time_days, p.unit_cost,
                    i.on_hand, i.on_order
             FROM fact_inventory i JOIN dim_product p USING(product_id) WHERE i.snapshot_date = ?"""
    args: list = [snap]
    if store:
        sql += " AND i.store_id = ?"
        args.append(store)
    out = []
    for st, pid, name, cat, lead, cost, on_hand, on_order in conn.execute(sql, args):
        series = [daily[(st, pid)].get((start + timedelta(days=k)).isoformat(), 0) for k in range(window)]
        mean = sum(series) / window
        std = math.sqrt(sum((x - mean) ** 2 for x in series) / (window - 1)) if window > 1 else 0.0
        safety = Z * std * math.sqrt(lead)
        rop = mean * lead + safety
        up_to = rop + mean * review_days
        position = on_hand + on_order
        cover = on_hand / mean if mean > 0 else None
        if mean > 0 and on_hand == 0:
            status = "stockout"
        elif mean > 0 and position <= rop:
            status = "reorder"
        elif cover is not None and cover > overstock_days:
            status = "overstock"
        elif mean == 0 and on_hand > 0:
            status = "no_recent_sales"
        else:
            status = "ok"
        qty = max(0, math.ceil(up_to - position)) if status in ("stockout", "reorder") else 0
        out.append({
            "store_id": st, "product_id": pid, "product_name": name, "category": cat, "status": status,
            "on_hand": on_hand, "on_order": on_order, "avg_daily_units": round(mean, 3),
            "days_of_cover": round(cover, 1) if cover is not None else None, "lead_time_days": lead,
            "safety_stock": round(safety, 1), "reorder_point": round(rop, 1), "suggested_order_qty": qty,
            "suggested_order_cost": round(qty * cost, 2), "inventory_value": round(on_hand * cost, 2),
        })
    priority = {"stockout": 0, "reorder": 1, "overstock": 2, "no_recent_sales": 3, "ok": 4}
    out.sort(key=lambda r: (priority[r["status"]], -r["avg_daily_units"], r["store_id"], r["product_id"]))
    return out


def status_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    return dict(counts)
