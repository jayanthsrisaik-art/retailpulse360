"""Raw CSV export -> validated SQLite star schema.

The warehouse is rebuilt from the raw files into a temporary database and then
swapped in with an atomic rename, so readers (the API) never see a half-built
warehouse and re-running the pipeline on the same input gives the same result.

Every rejected record goes to ``etl_quarantine`` with a reason; exact duplicate
exports are dropped and counted; data-quality checks run after the load and are
stored in ``dq_results``.  A failing check fails the run and the old warehouse
is kept.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE dim_date (
    date_key TEXT PRIMARY KEY, year INT, quarter INT, month INT, iso_week INT,
    day_of_week INT, is_weekend INT
);
CREATE TABLE dim_store (
    store_id TEXT PRIMARY KEY, store_name TEXT, region TEXT, opened_on TEXT, size_sqm INT
);
CREATE TABLE dim_product (
    product_id TEXT PRIMARY KEY, product_name TEXT, category TEXT, unit_price REAL, unit_cost REAL,
    lead_time_days INT
);
CREATE TABLE dim_customer (
    customer_id TEXT PRIMARY KEY, signup_date TEXT, home_store_id TEXT, region TEXT, loyalty_tier TEXT,
    birth_year INT
);
CREATE TABLE fact_sales (
    line_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, date_key TEXT NOT NULL REFERENCES dim_date,
    order_ts TEXT NOT NULL, store_id TEXT NOT NULL REFERENCES dim_store,
    product_id TEXT NOT NULL REFERENCES dim_product, customer_id TEXT NOT NULL REFERENCES dim_customer,
    channel TEXT NOT NULL, quantity INT NOT NULL, unit_price REAL NOT NULL, discount_pct REAL NOT NULL,
    gross_amount REAL NOT NULL, net_amount REAL NOT NULL, cost_amount REAL NOT NULL
);
CREATE INDEX fs_date ON fact_sales(date_key);
CREATE INDEX fs_customer ON fact_sales(customer_id, date_key);
CREATE INDEX fs_store_product ON fact_sales(store_id, product_id, date_key);
CREATE TABLE fact_returns (
    return_id TEXT PRIMARY KEY, line_id TEXT NOT NULL REFERENCES fact_sales, return_date TEXT NOT NULL,
    quantity INT NOT NULL, refund_amount REAL NOT NULL, reason TEXT
);
CREATE INDEX fr_line ON fact_returns(line_id);
CREATE TABLE fact_inventory (
    snapshot_date TEXT NOT NULL, store_id TEXT NOT NULL REFERENCES dim_store,
    product_id TEXT NOT NULL REFERENCES dim_product, on_hand INT NOT NULL, on_order INT NOT NULL,
    PRIMARY KEY (snapshot_date, store_id, product_id)
);
CREATE TABLE etl_quarantine (source TEXT, record_key TEXT, reason TEXT, raw TEXT);
CREATE TABLE etl_runs (
    run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, stats TEXT
);
CREATE TABLE dq_results (run_id TEXT, check_name TEXT, passed INT, detail TEXT);
"""

GUEST = "GUEST"


@dataclass
class RunStats:
    loaded: Counter = field(default_factory=Counter)
    quarantined: Counter = field(default_factory=Counter)
    duplicates_dropped: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {"loaded": dict(self.loaded), "quarantined": dict(self.quarantined),
                "duplicates_dropped": dict(self.duplicates_dropped)}


class DataQualityError(RuntimeError):
    pass


def _read(raw_dir: Path, name: str) -> list[dict]:
    with (raw_dir / name).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _num(value: str, kind=float):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


class _Loader:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.stats = RunStats()

    def reject(self, source: str, key: str, reason: str, raw: dict) -> None:
        self.conn.execute("INSERT INTO etl_quarantine VALUES (?,?,?,?)", (source, key, reason, json.dumps(raw)))
        self.stats.quarantined[source] += 1

    def load_dims(self, raw: Path) -> None:
        c = self.conn
        stores = _read(raw, "stores.csv")
        c.executemany("INSERT INTO dim_store VALUES (?,?,?,?,?)",
                      [(r["store_id"], r["store_name"], r["region"], r["opened_on"], int(r["size_sqm"])) for r in stores])
        self.stats.loaded["dim_store"] = len(stores)

        products = []
        for r in _read(raw, "products.csv"):
            price, cost = _num(r["unit_price"]), _num(r["unit_cost"])
            if price is None or cost is None or price <= 0 or cost < 0:
                self.reject("products", r.get("product_id", ""), "invalid price/cost", r)
                continue
            products.append((r["product_id"], r["product_name"], r["category"], price, cost, int(r["lead_time_days"])))
        c.executemany("INSERT INTO dim_product VALUES (?,?,?,?,?,?)", products)
        self.stats.loaded["dim_product"] = len(products)

        customers = [(r["customer_id"], r["signup_date"], r["home_store_id"], r["region"], r["loyalty_tier"],
                      _num(r["birth_year"], int)) for r in _read(raw, "customers.csv")]
        customers.append((GUEST, None, None, None, "none", None))  # unknown-member row for guest checkouts
        c.executemany("INSERT INTO dim_customer VALUES (?,?,?,?,?,?)", customers)
        self.stats.loaded["dim_customer"] = len(customers)

    def load_sales(self, raw: Path) -> None:
        c = self.conn
        stores = {r[0] for r in c.execute("SELECT store_id FROM dim_store")}
        customers = {r[0] for r in c.execute("SELECT customer_id FROM dim_customer")}
        products = {r[0]: (r[1],) for r in c.execute("SELECT product_id, unit_cost FROM dim_product")}

        orders: dict[str, dict] = {}
        for r in _read(raw, "orders.csv"):
            ts = _parse_ts(r["order_ts"])
            reason = None
            if ts is None:
                reason = "unparseable order_ts"
            elif r["store_id"] not in stores:
                reason = "unknown store"
            elif r["customer_id"] and r["customer_id"] not in customers:
                reason = "unknown customer"
            elif r["order_id"] in orders:
                reason = "duplicate order_id"
            if reason:
                self.reject("orders", r["order_id"], reason, r)
                continue
            orders[r["order_id"]] = {**r, "ts": ts, "customer_id": r["customer_id"] or GUEST}

        seen: dict[str, dict] = {}
        rows = []
        for r in _read(raw, "order_lines.csv"):
            lid = r["line_id"]
            if lid in seen:
                if seen[lid] == r:
                    self.stats.duplicates_dropped["order_lines"] += 1
                else:
                    self.reject("order_lines", lid, "conflicting duplicate line_id", r)
                continue
            seen[lid] = r
            o = orders.get(r["order_id"])
            qty, price, disc = _num(r["quantity"], int), _num(r["unit_price"]), _num(r["discount_pct"])
            reason = None
            if o is None:
                reason = "order missing or quarantined"
            elif r["product_id"] not in products:
                reason = "unknown product"
            elif qty is None or qty <= 0:
                reason = "non-positive quantity"
            elif price is None or price < 0 or disc is None or not 0 <= disc < 1:
                reason = "invalid price/discount"
            if reason:
                self.reject("order_lines", lid, reason, r)
                continue
            gross = round(qty * price, 2)
            net = round(gross * (1 - disc), 2)
            cost = round(qty * products[r["product_id"]][0], 2)
            rows.append((lid, r["order_id"], o["ts"].date().isoformat(), o["ts"].isoformat(), o["store_id"],
                         r["product_id"], o["customer_id"], o["channel"], qty, price, disc, gross, net, cost))
        c.executemany("INSERT INTO fact_sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.stats.loaded["fact_sales"] = len(rows)

        lines = {r[0]: r for r in c.execute("SELECT line_id, quantity, net_amount, date_key FROM fact_sales")}
        returned: Counter = Counter()
        rrows = []
        for r in _read(raw, "returns.csv"):
            ln = lines.get(r["line_id"])
            qty = _num(r["quantity"], int)
            reason = None
            if ln is None:
                reason = "line missing or quarantined"
            elif qty is None or qty <= 0:
                reason = "non-positive quantity"
            elif returned[r["line_id"]] + qty > ln[1]:
                reason = "returns exceed quantity sold"
            elif r["return_date"] < ln[3]:
                reason = "return before sale"
            if reason:
                self.reject("returns", r["return_id"], reason, r)
                continue
            returned[r["line_id"]] += qty
            rrows.append((r["return_id"], r["line_id"], r["return_date"], qty, round(ln[2] * qty / ln[1], 2), r["reason"]))
        c.executemany("INSERT INTO fact_returns VALUES (?,?,?,?,?,?)", rrows)
        self.stats.loaded["fact_returns"] = len(rrows)

        irows = []
        for r in _read(raw, "inventory_snapshot.csv"):
            on_hand, on_order = _num(r["on_hand"], int), _num(r["on_order"], int)
            if r["store_id"] not in stores or r["product_id"] not in products or on_hand is None or on_hand < 0 \
                    or on_order is None or on_order < 0:
                self.reject("inventory", f"{r['store_id']}/{r['product_id']}", "invalid inventory row", r)
                continue
            irows.append((r["snapshot_date"], r["store_id"], r["product_id"], on_hand, on_order))
        c.executemany("INSERT INTO fact_inventory VALUES (?,?,?,?,?)", irows)
        self.stats.loaded["fact_inventory"] = len(irows)

    def load_dates(self) -> None:
        lo, hi = self.conn.execute(
            "SELECT MIN(d), MAX(d) FROM (SELECT date_key d FROM fact_sales UNION ALL SELECT return_date FROM fact_returns "
            "UNION ALL SELECT snapshot_date FROM fact_inventory)").fetchone()
        if lo is None:
            return
        d, end, rows = date.fromisoformat(lo), date.fromisoformat(hi), []
        while d <= end:
            iso = d.isocalendar()
            rows.append((d.isoformat(), d.year, (d.month - 1) // 3 + 1, d.month, iso[1], iso[2], int(iso[2] >= 6)))
            d += timedelta(days=1)
        self.conn.executemany("INSERT INTO dim_date VALUES (?,?,?,?,?,?,?)", rows)
        self.stats.loaded["dim_date"] = len(rows)


DQ_CHECKS = {
    "sales_dates_in_dim_date": "SELECT COUNT(*) FROM fact_sales s LEFT JOIN dim_date d USING(date_key) WHERE d.date_key IS NULL",
    "sales_fk_product": "SELECT COUNT(*) FROM fact_sales s LEFT JOIN dim_product p USING(product_id) WHERE p.product_id IS NULL",
    "sales_fk_customer": "SELECT COUNT(*) FROM fact_sales s LEFT JOIN dim_customer c USING(customer_id) WHERE c.customer_id IS NULL",
    "sales_fk_store": "SELECT COUNT(*) FROM fact_sales s LEFT JOIN dim_store t USING(store_id) WHERE t.store_id IS NULL",
    "net_not_above_gross": "SELECT COUNT(*) FROM fact_sales WHERE net_amount > gross_amount + 0.005",
    "positive_quantities": "SELECT COUNT(*) FROM fact_sales WHERE quantity <= 0",
    "returns_within_sold_qty": """SELECT COUNT(*) FROM (SELECT r.line_id, SUM(r.quantity) q, MAX(s.quantity) sold
                                  FROM fact_returns r JOIN fact_sales s USING(line_id) GROUP BY r.line_id) WHERE q > sold""",
    "refund_not_above_net": """SELECT COUNT(*) FROM (SELECT r.line_id, SUM(r.refund_amount) amt, MAX(s.net_amount) net
                               FROM fact_returns r JOIN fact_sales s USING(line_id) GROUP BY r.line_id) WHERE amt > net + 0.01""",
    "orders_single_store_and_customer": """SELECT COUNT(*) FROM (SELECT order_id FROM fact_sales GROUP BY order_id
                                           HAVING COUNT(DISTINCT store_id) > 1 OR COUNT(DISTINCT customer_id) > 1)""",
}


def run_checks(conn: sqlite3.Connection, run_id: str) -> list[tuple[str, bool, str]]:
    results = []
    for name, sql in DQ_CHECKS.items():
        bad = conn.execute(sql).fetchone()[0]
        results.append((name, bad == 0, f"{bad} violating rows"))
    if conn.execute("SELECT COUNT(*) FROM fact_sales").fetchone()[0] == 0:
        results.append(("fact_sales_not_empty", False, "0 rows"))
    else:
        results.append(("fact_sales_not_empty", True, "ok"))
    conn.executemany("INSERT INTO dq_results VALUES (?,?,?,?)", [(run_id, n, int(p), d) for n, p, d in results])
    return results


def build_warehouse(raw_dir: str | Path, db_path: str | Path) -> dict:
    """Rebuild the warehouse from ``raw_dir``; atomically replace ``db_path`` on success."""
    raw_dir, db_path = Path(raw_dir), Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(db_path.suffix + ".building")
    if tmp.exists():
        tmp.unlink()
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%S%fZ")
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA)
        loader = _Loader(conn)
        with conn:
            loader.load_dims(raw_dir)
            loader.load_sales(raw_dir)
            loader.load_dates()
        from .churn import score_customers  # local import: churn depends on the warehouse tables
        with conn:
            checks = run_checks(conn, run_id)
            failed = [c for c in checks if not c[1]]
            status = "failed" if failed else "succeeded"
            conn.execute("INSERT INTO etl_runs VALUES (?,?,?,?,?)",
                         (run_id, started.isoformat(), datetime.now(timezone.utc).isoformat(), status,
                          json.dumps(loader.stats.as_dict())))
        if failed:
            raise DataQualityError("data-quality checks failed: " + ", ".join(f"{n} ({d})" for n, _, d in failed))
        score_info = score_customers(conn)
        conn.commit()
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp, db_path)
    return {"run_id": run_id, **loader.stats.as_dict(), "dq_checks": {n: p for n, p, _ in checks}, "churn": score_info}


def connect(db_path: str | Path) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"warehouse not found: {db_path} (run `python -m retailpulse build` first)")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
