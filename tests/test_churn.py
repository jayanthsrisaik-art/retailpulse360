import json
import sqlite3
from datetime import date, timedelta

import numpy as np

from retailpulse import churn, etl


def test_lift_at():
    y = np.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0])
    s = np.arange(10, 0, -1)  # best scores on the positives
    assert churn.lift_at(y, s, 0.2) == 5.0


def test_features_ignore_everything_after_cutoff(synth, tmp_path):
    """Leakage check: deleting all post-cutoff data must not change any feature at the cutoff."""
    h = churn.load_history(sqlite3.connect(synth["db"]))
    cutoff = h.end - timedelta(days=90)
    ids_a, X_a = churn.features_at(h, cutoff)
    trunc = churn.History(
        orders={c: [o for o in os if o[0] <= cutoff] for c, os in h.orders.items()},
        lines={c: [l for l in ls if l[0] <= cutoff] for c, ls in h.lines.items()},
        returns={c: [r for r in rs if r[0] <= cutoff] for c, rs in h.returns.items()},
        tiers=h.tiers, end=cutoff)
    ids_b, X_b = churn.features_at(trunc, cutoff)
    assert ids_a == ids_b
    assert np.array_equal(X_a, X_b)


def test_labels_and_population_on_toy_history():
    d = date(2026, 1, 1)
    h = churn.History(
        orders={"a": [(d, 10.0, 0, "store"), (d + timedelta(days=30), 5.0, 0, "store")],
                "b": [(d, 10.0, 0, "online")],
                "old": [(d - timedelta(days=400), 10.0, 0, "store")]},
        lines={}, returns={}, tiers={"a": "gold"}, end=d + timedelta(days=200))
    ids, X = churn.features_at(h, d)
    assert ids == ["a", "b"]  # 'old' lapsed more than ACTIVE_WINDOW days before the cutoff
    assert list(churn.labels_at(h, ids, d)) == [0, 1]  # a buys again on day 30, b never does
    f = dict(zip(churn.FEATURES, X[0]))
    assert f["recency_days"] == 0 and f["orders_total"] == 1 and f["tier_gold"] == 1


def test_backtest_report_shape_and_baseline(synth):
    rep = json.loads(etl.connect(synth["db"]).execute("SELECT report FROM model_eval").fetchone()[0])
    assert rep["train_cutoff"] < rep["test_cutoff"]
    assert set(rep["results"]) == {"baseline_recency", "logistic_regression", "gradient_boosting"}
    assert rep["selected_model"] in ("logistic_regression", "gradient_boosting")
    for name, m in rep["results"].items():
        assert 0.5 < m["test"]["roc_auc"] <= 1.0, name  # every method beats random on this data
    assert 0 < rep["test_churn_rate"] < 1


def test_scores_written_for_every_customer_with_history(synth):
    c = etl.connect(synth["db"])
    n_buyers = c.execute("SELECT COUNT(DISTINCT customer_id) FROM fact_sales WHERE customer_id != 'GUEST'").fetchone()[0]
    rows = c.execute("SELECT churn_probability, risk_band FROM customer_scores").fetchall()
    assert len(rows) == n_buyers
    for p, band in rows:
        assert (p is None) == (band == "lapsed")
        if p is not None:
            assert 0 <= p <= 1


def test_tiny_data_skips_churn(sample_db):
    c = etl.connect(sample_db)
    assert c.execute("SELECT COUNT(*) FROM customer_scores").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM model_eval").fetchone()[0] == 0
