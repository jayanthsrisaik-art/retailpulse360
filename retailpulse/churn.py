"""Customer churn risk (no purchase in the next 90 days) and RFM segments.

Leakage control
---------------
Features for a cutoff date T use only sales with ``date_key <= T`` and returns
with ``return_date <= T``.  The label is "no order in (T, T + 90 days]".

Out-of-time backtest
--------------------
With E = last date in the warehouse:

* train:      features at T1 = E - 180, labels from (T1, T1 + 90]
* validation: 20% of train customers held out (seeded) to pick the model
* test:       features at T2 = E - 90,  labels from (T2, E]

The test period is later than anything the model was fit or selected on.
After evaluation, the chosen model is refit on the T2 snapshot and used to
score every customer as of E (the "live" score served by the API).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

HORIZON = 90
MIN_CUSTOMERS = 100
ACTIVE_WINDOW = 120  # population: customers with an order in the 120 days up to the cutoff
SEED = 7

FEATURES = [
    "recency_days", "orders_total", "orders_last_90", "orders_prev_90", "net_total", "net_last_90",
    "avg_order_value", "tenure_days", "mean_gap_days", "recency_over_gap", "distinct_categories",
    "return_rate", "promo_line_share", "online_order_share", "tier_silver", "tier_gold",
]


@dataclass
class History:
    """Per-customer purchase history pulled once from the warehouse."""
    orders: dict[str, list[tuple[date, float, int, str]]]  # cust -> [(date, net, n_promo_lines/total encoded, channel)]
    lines: dict[str, list[tuple[date, str, int, float]]]    # cust -> [(date, category, qty, discount)]
    returns: dict[str, list[tuple[date, int]]]              # cust -> [(return_date, qty)]
    tiers: dict[str, str]
    end: date


def load_history(conn: sqlite3.Connection) -> History:
    orders: dict[str, dict[str, list]] = defaultdict(dict)
    lines: dict[str, list] = defaultdict(list)
    for cust, oid, dk, net, cat, qty, disc, ch in conn.execute(
            """SELECT s.customer_id, s.order_id, s.date_key, s.net_amount, p.category, s.quantity, s.discount_pct, s.channel
               FROM fact_sales s JOIN dim_product p USING(product_id) WHERE s.customer_id != 'GUEST'"""):
        d = date.fromisoformat(dk)
        o = orders[cust].setdefault(oid, [d, 0.0, 0, ch])
        o[1] += net
        lines[cust].append((d, cat, qty, disc))
    returns: dict[str, list] = defaultdict(list)
    for cust, rd, qty in conn.execute(
            "SELECT s.customer_id, r.return_date, r.quantity FROM fact_returns r JOIN fact_sales s USING(line_id) "
            "WHERE s.customer_id != 'GUEST'"):
        returns[cust].append((date.fromisoformat(rd), qty))
    tiers = {c: t for c, t in conn.execute("SELECT customer_id, loyalty_tier FROM dim_customer")}
    end = date.fromisoformat(conn.execute("SELECT MAX(date_key) FROM fact_sales").fetchone()[0])
    return History({c: sorted(tuple(v) for v in os.values()) for c, os in orders.items()}, lines, returns, tiers, end)


def features_at(h: History, cutoff: date, active_window: int | None = ACTIVE_WINDOW) -> tuple[list[str], np.ndarray]:
    """Feature matrix for customers whose latest order on or before ``cutoff`` is within ``active_window`` days.

    Customers who already lapsed long ago are not "at risk" any more; including them
    would make the task trivially solvable by recency alone.  ``active_window=None``
    keeps everyone with any past order (used for RFM segmentation).
    """
    ids, rows = [], []
    for cust, os in h.orders.items():
        past = [o for o in os if o[0] <= cutoff]
        if not past or (active_window is not None and (cutoff - past[-1][0]).days >= active_window):
            continue
        dates = [o[0] for o in past]
        last, first = dates[-1], dates[0]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        mean_gap = float(np.mean(gaps)) if gaps else float((cutoff - first).days + 1)
        recency = (cutoff - last).days
        l90 = [o for o in past if (cutoff - o[0]).days < 90]
        p90 = [o for o in past if 90 <= (cutoff - o[0]).days < 180]
        ls = [l for l in h.lines.get(cust, []) if l[0] <= cutoff]
        sold = sum(l[2] for l in ls)
        ret = sum(q for d, q in h.returns.get(cust, []) if d <= cutoff)
        net_total = sum(o[1] for o in past)
        tier = h.tiers.get(cust, "none")
        ids.append(cust)
        rows.append([
            recency, len(past), len(l90), len(p90), net_total, sum(o[1] for o in l90),
            net_total / len(past), (cutoff - first).days, mean_gap, recency / max(mean_gap, 1.0),
            len({l[1] for l in ls}), ret / sold if sold else 0.0,
            sum(1 for l in ls if l[3] > 0) / len(ls) if ls else 0.0,
            sum(1 for o in past if o[3] == "online") / len(past),
            float(tier == "silver"), float(tier == "gold"),
        ])
    return ids, np.array(rows, dtype=float).reshape(-1, len(FEATURES))


def labels_at(h: History, ids: list[str], cutoff: date, horizon: int = HORIZON) -> np.ndarray:
    """1 = churned: no order in (cutoff, cutoff + horizon]."""
    stop = cutoff + timedelta(days=horizon)
    return np.array([int(not any(cutoff < o[0] <= stop for o in h.orders[c])) for c in ids])


def lift_at(y: np.ndarray, score: np.ndarray, frac: float = 0.1) -> float:
    k = max(1, int(round(len(y) * frac)))
    top = np.argsort(-score, kind="stable")[:k]
    return float(y[top].mean() / y.mean()) if y.mean() > 0 else 0.0


def _models() -> dict:
    return {
        "logistic_regression": make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000)),
        "gradient_boosting": HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                                            l2_regularization=1.0, random_state=SEED),
    }


def _metrics(y: np.ndarray, s: np.ndarray, proba: bool) -> dict:
    out = {"roc_auc": float(roc_auc_score(y, s)), "pr_auc": float(average_precision_score(y, s)),
           "lift@10%": lift_at(y, s)}
    if proba:
        out["brier"] = float(brier_score_loss(y, s))
    return out


def backtest(h: History) -> dict:
    t1, t2 = h.end - timedelta(days=2 * HORIZON), h.end - timedelta(days=HORIZON)
    ids1, X1 = features_at(h, t1)
    y1 = labels_at(h, ids1, t1)
    ids2, X2 = features_at(h, t2)
    y2 = labels_at(h, ids2, t2)
    Xtr, Xva, ytr, yva = train_test_split(X1, y1, test_size=0.2, random_state=SEED, stratify=y1)

    recency = FEATURES.index("recency_days")
    results = {"baseline_recency": {"validation": _metrics(yva, Xva[:, recency], False),
                                    "test": _metrics(y2, X2[:, recency], False)}}
    for name, m in _models().items():
        m.fit(Xtr, ytr)
        results[name] = {"validation": _metrics(yva, m.predict_proba(Xva)[:, 1], True)}
        m.fit(X1, y1)  # refit on all train-period customers before touching the test period
        results[name]["test"] = _metrics(y2, m.predict_proba(X2)[:, 1], True)
    chosen = max(_models(), key=lambda n: results[n]["validation"]["roc_auc"])
    return {
        "train_cutoff": t1.isoformat(), "test_cutoff": t2.isoformat(), "horizon_days": HORIZON,
        "train_customers": len(ids1), "train_churn_rate": float(y1.mean()),
        "test_customers": len(ids2), "test_churn_rate": float(y2.mean()),
        "selected_model": chosen, "selected_on": "validation roc_auc", "results": results,
    }


def rfm_segments(h: History, ids: list[str], X: np.ndarray) -> list[str]:
    """Classic RFM quintile segmentation as of the scoring date."""
    def quint(v: np.ndarray, reverse: bool = False) -> np.ndarray:
        ranks = np.argsort(np.argsort(-v if reverse else v, kind="stable"), kind="stable")
        return 1 + (ranks * 5 // max(len(v), 1))
    r = quint(X[:, FEATURES.index("recency_days")], reverse=True)  # recent -> 5
    f = quint(X[:, FEATURES.index("orders_total")])
    m = quint(X[:, FEATURES.index("net_total")])
    tenure = X[:, FEATURES.index("tenure_days")]
    out = []
    for ri, fi, mi, t in zip(r, f, m, tenure):
        if t < 60 and ri >= 4:
            out.append("New")
        elif ri >= 4 and fi >= 4:
            out.append("Champions")
        elif ri >= 3 and fi >= 3:
            out.append("Loyal")
        elif ri <= 2 and fi >= 3:
            out.append("At risk")
        elif ri <= 2 and mi >= 4:
            out.append("Can't lose")
        elif ri <= 1:
            out.append("Hibernating")
        else:
            out.append("Needs attention")
    return out


def score_customers(conn: sqlite3.Connection) -> dict:
    """Backtest, refit the selected model on the latest labelled snapshot, score everyone as of E."""
    h = load_history(conn)
    conn.executescript("""
        DROP TABLE IF EXISTS customer_scores;
        CREATE TABLE customer_scores (customer_id TEXT PRIMARY KEY REFERENCES dim_customer, scored_as_of TEXT,
            churn_probability REAL, risk_band TEXT, rfm_segment TEXT, features TEXT);
        DROP TABLE IF EXISTS model_eval;
        CREATE TABLE model_eval (created_as_of TEXT, report TEXT);
    """)
    t1, t2 = h.end - timedelta(days=2 * HORIZON), h.end - timedelta(days=HORIZON)
    ids1, _ = features_at(h, t1)
    ids2, _ = features_at(h, t2)
    y1, y2 = labels_at(h, ids1, t1), labels_at(h, ids2, t2)
    if min(len(ids1), len(ids2)) < MIN_CUSTOMERS or len(set(y1)) < 2 or len(set(y2)) < 2 or min(np.bincount(y1)) < 10:
        return {"skipped": f"not enough history for a backtest (need >= {MIN_CUSTOMERS} active customers "
                           "with both outcomes at each cutoff)"}
    report = backtest(h)
    t2 = h.end - timedelta(days=HORIZON)
    ids2, X2 = features_at(h, t2)
    model = _models()[report["selected_model"]].fit(X2, labels_at(h, ids2, t2))
    ids, X = features_at(h, h.end, active_window=None)
    active = X[:, FEATURES.index("recency_days")] < ACTIVE_WINDOW
    p = np.where(active, model.predict_proba(X)[:, 1], np.nan)  # lapsed customers are not scored
    segs = rfm_segments(h, ids, X)
    def band(x: float) -> str:
        if np.isnan(x):
            return "lapsed"
        return "high" if x >= 0.6 else "medium" if x >= 0.3 else "low"

    conn.executemany("INSERT INTO customer_scores VALUES (?,?,?,?,?,?)", [
        (c, h.end.isoformat(), None if np.isnan(pi) else round(float(pi), 4), band(pi), s,
         json.dumps(dict(zip(FEATURES, map(float, x))))) for c, pi, s, x in zip(ids, p, segs, X)])
    conn.execute("INSERT INTO model_eval VALUES (?,?)", (h.end.isoformat(), json.dumps(report)))
    return {"scored_customers": int(active.sum()), "lapsed_customers": int((~active).sum()), "selected_model": report["selected_model"],
            "test_roc_auc": report["results"][report["selected_model"]]["test"]["roc_auc"]}
