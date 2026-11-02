"""Seeded synthetic retailer: stores, products, customers, orders, returns, inventory.

The raw files deliberately look like an operational export, warts included:

* ~0.4% of order lines are exported twice (same line_id);
* a few lines reference product ids that do not exist in the catalogue;
* a few lines have zero/negative quantity or an unparseable order date;
* returns can reference lines that were never exported.

Customer behaviour has a latent lifetime: each customer becomes active at some
date, buys at a personal rate with weekly and yearly seasonality and promotion
uplift, and silently stops buying at a latent churn date.  Customers who
return a lot and those with low engagement churn sooner.  That is the signal
the churn model has to recover from purchase history alone.
"""

from __future__ import annotations

import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

START = date(2025, 1, 1)
END = date(2026, 6, 30)

REGIONS = ["North", "South", "East", "West"]
CATEGORIES = {
    # category: (price range, margin range, lines per basket weight)
    "Grocery": ((1.5, 12.0), (0.18, 0.30), 5),
    "Household": ((3.0, 25.0), (0.25, 0.40), 3),
    "Beauty": ((5.0, 45.0), (0.40, 0.60), 2),
    "Electronics": ((20.0, 400.0), (0.10, 0.22), 1),
    "Apparel": ((10.0, 90.0), (0.45, 0.65), 2),
    "Home & Garden": ((8.0, 150.0), (0.30, 0.50), 1),
    "Toys": ((6.0, 70.0), (0.30, 0.45), 1),
}
CHANNELS = ["store", "online"]


def _season(d: date) -> float:
    """Weekly + yearly seasonality multiplier (weekends and Nov/Dec are busier)."""
    weekly = 1.25 if d.weekday() >= 5 else 0.92
    doy = d.timetuple().tm_yday
    yearly = 1 + 0.35 * math.exp(-((doy - 340) / 25) ** 2) + 0.1 * math.sin(2 * math.pi * (doy - 80) / 365)
    return weekly * yearly


def generate(out_dir: Path, n_customers: int = 3000, n_products: int = 140, seed: int = 42) -> dict[str, int]:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- stores ---------------------------------------------------------------
    stores = []
    for i in range(1, 9):
        stores.append({"store_id": f"S{i:02d}", "store_name": f"RetailPulse {rng.choice(['Central', 'Park', 'Harbor', 'Mill', 'Station', 'Market', 'Bridge', 'Square'])} {i}",
                       "region": REGIONS[(i - 1) % 4], "opened_on": (START - timedelta(days=rng.randint(200, 3000))).isoformat(),
                       "size_sqm": rng.choice([800, 1200, 1500, 2200, 3000])})
    online = {"store_id": "WEB", "store_name": "RetailPulse Online", "region": "Online",
              "opened_on": (START - timedelta(days=900)).isoformat(), "size_sqm": 0}
    stores.append(online)

    # --- products -------------------------------------------------------------
    products = []
    cats = list(CATEGORIES)
    for i in range(1, n_products + 1):
        cat = cats[(i - 1) % len(cats)] if i <= len(cats) * 2 else rng.choice(cats)
        (plo, phi), (mlo, mhi), _ = CATEGORIES[cat]
        price = round(math.exp(rng.uniform(math.log(plo), math.log(phi))), 2)
        cost = round(price * (1 - rng.uniform(mlo, mhi)), 2)
        products.append({"product_id": f"P{i:04d}", "product_name": f"{cat} item {i}", "category": cat,
                         "unit_price": price, "unit_cost": cost, "lead_time_days": rng.choice([3, 5, 7, 10, 14]),
                         "popularity": rng.paretovariate(1.3)})
    by_cat: dict[str, list[dict]] = {c: [p for p in products if p["category"] == c] for c in cats}
    cat_weights = [CATEGORIES[c][2] for c in cats]

    # promotions: random 7-day windows, one category at 20% off
    promos: dict[date, str] = {}
    d = START
    while d <= END:
        if rng.random() < 0.25:
            cat = rng.choice(cats)
            for k in range(7):
                promos[d + timedelta(days=k)] = cat
        d += timedelta(days=14)

    # --- customers ------------------------------------------------------------
    customers = []
    days_total = (END - START).days
    for i in range(1, n_customers + 1):
        signup = START + timedelta(days=int(rng.random() ** 1.6 * days_total * 0.85))
        engagement = rng.gammavariate(2.0, 1.0)  # personal purchase rate scale
        returner = rng.random() < 0.15
        bargain = rng.random() < 0.3
        # latent lifetime: low engagement and frequent returns shorten it
        mean_life = 260 * (0.6 + 0.4 * min(engagement, 3)) * (0.55 if returner else 1.0)
        churn_on = signup + timedelta(days=int(rng.expovariate(1 / mean_life)) + 20)
        home = rng.choice(stores[:-1])
        customers.append({"customer_id": f"C{i:05d}", "signup_date": signup.isoformat(),
                          "home_store_id": home["store_id"], "region": home["region"],
                          "loyalty_tier": rng.choices(["none", "silver", "gold"], [0.6, 0.3, 0.1])[0],
                          "birth_year": rng.randint(1950, 2006),
                          "_eng": engagement, "_ret": returner, "_bargain": bargain, "_churn": churn_on,
                          "_online": rng.random() < 0.35})

    # --- orders ---------------------------------------------------------------
    orders, lines, returns = [], [], []
    line_seq = 0
    for c in customers:
        d = date.fromisoformat(c["signup_date"])
        last = min(c["_churn"], END)
        base_rate = 0.022 * c["_eng"]  # orders per day
        while d <= last:
            p = base_rate * _season(d)
            if d in promos:
                p *= 1.6 if c["_bargain"] else 1.15
            if rng.random() < min(p, 0.9):
                oid = f"O{len(orders) + 1:07d}"
                channel = "online" if (c["_online"] and rng.random() < 0.7) or rng.random() < 0.08 else "store"
                store_id = "WEB" if channel == "online" else (c["home_store_id"] if rng.random() < 0.85 else rng.choice(stores[:-1])["store_id"])
                guest = rng.random() < 0.03
                orders.append({"order_id": oid, "order_ts": f"{d.isoformat()}T{rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}:00",
                               "customer_id": "" if guest else c["customer_id"], "store_id": store_id, "channel": channel})
                n_lines = max(1, int(rng.expovariate(1 / 2.6)))
                for _ in range(n_lines):
                    cat = rng.choices(cats, cat_weights)[0]
                    if d in promos and rng.random() < 0.4:
                        cat = promos[d]
                    prod = rng.choices(by_cat[cat], [x["popularity"] for x in by_cat[cat]])[0]
                    qty = 1 if prod["unit_price"] > 50 else max(1, int(rng.expovariate(1 / 1.8)))
                    disc = 0.20 if promos.get(d) == cat else (0.10 if c["loyalty_tier"] == "gold" and rng.random() < 0.3 else 0.0)
                    line_seq += 1
                    line = {"line_id": f"L{line_seq:08d}", "order_id": oid, "product_id": prod["product_id"],
                            "quantity": qty, "unit_price": prod["unit_price"], "discount_pct": disc}
                    lines.append(line)
                    ret_p = 0.25 if c["_ret"] else 0.03
                    if prod["category"] == "Apparel":
                        ret_p *= 2
                    if rng.random() < ret_p:
                        rd = d + timedelta(days=rng.randint(1, 30))
                        if rd <= END:
                            returns.append({"return_id": f"RT{len(returns) + 1:07d}", "line_id": line["line_id"],
                                            "return_date": rd.isoformat(), "quantity": rng.randint(1, qty),
                                            "reason": rng.choice(["damaged", "wrong size", "changed mind", "not as described"])})
            d += timedelta(days=1)

    # --- inject export problems -------------------------------------------------
    n_lines_clean = len(lines)
    for _ in range(int(n_lines_clean * 0.004)):
        lines.append(dict(rng.choice(lines[:n_lines_clean])))  # exact duplicate export
    for _ in range(12):
        bad = dict(rng.choice(lines[:n_lines_clean]))
        bad["line_id"] = f"LX{rng.getrandbits(32):08x}"
        bad["product_id"] = f"P9{rng.randint(100, 999)}"  # unknown product
        lines.append(bad)
    for _ in range(8):
        bad = dict(rng.choice(lines[:n_lines_clean]))
        bad["line_id"] = f"LQ{rng.getrandbits(32):08x}"
        bad["quantity"] = rng.choice([0, -1, -3])
        lines.append(bad)
    for _ in range(5):
        o = dict(rng.choice(orders))
        o["order_id"] = f"OX{rng.getrandbits(24):06x}"
        o["order_ts"] = rng.choice(["2025-13-01T10:00:00", "not a date", ""])
        orders.append(o)
    for _ in range(6):
        returns.append({"return_id": f"RTX{rng.getrandbits(24):06x}", "line_id": f"LMISSING{rng.randint(0, 99999)}",
                        "return_date": END.isoformat(), "quantity": 1, "reason": "damaged"})
    rng.shuffle(lines)

    # --- inventory snapshot (on-hand at END) ------------------------------------
    sold_recent: dict[tuple[str, str], int] = {}
    order_idx = {o["order_id"]: o for o in orders}
    cutoff = (END - timedelta(days=28)).isoformat()
    for ln in lines[:]:
        o = order_idx.get(ln["order_id"])
        if o and o["order_ts"][:10] > cutoff and isinstance(ln["quantity"], int) and ln["quantity"] > 0:
            k = (o["store_id"], ln["product_id"])
            sold_recent[k] = sold_recent.get(k, 0) + ln["quantity"]
    inventory = []
    for s in stores:
        for prod in products:
            rate = sold_recent.get((s["store_id"], prod["product_id"]), 0) / 28
            cover_days = rng.choice([0, 2, 5, 9, 14, 21, 30, 45, 60, 120, 180]) if rate > 0 else rng.choice([0, 30, 90])
            on_hand = int(round(rate * cover_days)) if rate > 0 else rng.randint(0, 12)
            inventory.append({"snapshot_date": END.isoformat(), "store_id": s["store_id"], "product_id": prod["product_id"],
                              "on_hand": on_hand, "on_order": rng.choice([0, 0, 0, 12, 24])})

    def dump(name: str, rows: list[dict], cols: list[str]) -> None:
        with (out_dir / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    dump("stores.csv", stores, ["store_id", "store_name", "region", "opened_on", "size_sqm"])
    dump("products.csv", products, ["product_id", "product_name", "category", "unit_price", "unit_cost", "lead_time_days"])
    dump("customers.csv", customers, ["customer_id", "signup_date", "home_store_id", "region", "loyalty_tier", "birth_year"])
    dump("orders.csv", orders, ["order_id", "order_ts", "customer_id", "store_id", "channel"])
    dump("order_lines.csv", lines, ["line_id", "order_id", "product_id", "quantity", "unit_price", "discount_pct"])
    dump("returns.csv", returns, ["return_id", "line_id", "return_date", "quantity", "reason"])
    dump("inventory_snapshot.csv", inventory, ["snapshot_date", "store_id", "product_id", "on_hand", "on_order"])
    return {"stores": len(stores), "products": len(products), "customers": len(customers), "orders": len(orders),
            "order_lines": len(lines), "returns": len(returns), "inventory_rows": len(inventory)}
