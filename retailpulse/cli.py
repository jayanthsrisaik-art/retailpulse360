"""Command line: generate | build | report | serve."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import etl, generate, inventory, kpis


def _generate(a):
    print(json.dumps(generate.generate(Path(a.out), n_customers=a.customers, seed=a.seed)))


def _build(a):
    res = etl.build_warehouse(a.raw, a.db)
    print(json.dumps(res, indent=2))


def report(db: str) -> dict:
    conn = etl.connect(db)
    _, hi = kpis.data_range(conn)
    cmp = kpis.period_comparison(conn, hi, 28)
    inv = inventory.status_counts(inventory.assess(conn))
    row = conn.execute("SELECT report FROM model_eval").fetchone()
    ev = json.loads(row[0]) if row else None
    segs = dict(conn.execute("SELECT rfm_segment, COUNT(*) FROM customer_scores GROUP BY 1 ORDER BY 1").fetchall())
    return {"as_of": hi, "last_28_days": cmp["current"], "vs_last_year": cmp["vs_last_year"],
            "inventory_status": inv, "segments": segs, "churn_model": ev}


def _report(a):
    r = report(a.db)
    if a.json:
        print(json.dumps(r, indent=2))
        return
    c = r["last_28_days"]
    print(f"RetailPulse 360 — 28 days to {r['as_of']}")
    print(f"  net sales {c['net_sales']:,.2f}  orders {c['orders']:,}  AOV {c['avg_order_value']:.2f}  "
          f"margin {c['gross_margin_pct']:.1%}  return rate {c['return_rate_units']:.1%}")
    print("  vs last year: " + ", ".join(f"{k} {v:+.1%}" for k, v in r["vs_last_year"].items() if v is not None))
    print(f"  inventory: {r['inventory_status']}")
    print(f"  segments: {r['segments']}")
    ev = r["churn_model"]
    if ev is None:
        print("\nChurn model: skipped (not enough history)")
        return
    print(f"\nChurn backtest (train cutoff {ev['train_cutoff']}, test cutoff {ev['test_cutoff']}, "
          f"{ev['horizon_days']}-day horizon; test n={ev['test_customers']}, churn rate {ev['test_churn_rate']:.3f})")
    print(f"  {'model':<22}{'ROC-AUC':>9}{'PR-AUC':>9}{'lift@10%':>10}{'Brier':>8}")
    for name, m in ev["results"].items():
        t = m["test"]
        brier = f"{t['brier']:.3f}" if "brier" in t else "–"
        print(f"  {name:<22}{t['roc_auc']:>9.3f}{t['pr_auc']:>9.3f}{t['lift@10%']:>10.2f}{brier:>8}")
    print(f"  selected: {ev['selected_model']} (by {ev['selected_on']})")


def _serve(a):
    import uvicorn

    from .api import create_app
    uvicorn.run(create_app(a.db), host=a.host, port=a.port)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="retailpulse")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="write a synthetic raw export (CSV)")
    g.add_argument("--out", default="data/raw")
    g.add_argument("--customers", type=int, default=3000)
    g.add_argument("--seed", type=int, default=42)
    g.set_defaults(fn=_generate)
    b = sub.add_parser("build", help="validate raw files, rebuild the warehouse, score customers")
    b.add_argument("--raw", default="data/raw")
    b.add_argument("--db", default="data/warehouse.db")
    b.set_defaults(fn=_build)
    r = sub.add_parser("report", help="print KPIs, inventory status and churn backtest")
    r.add_argument("--db", default="data/warehouse.db")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=_report)
    s = sub.add_parser("serve", help="run the API and dashboard")
    s.add_argument("--db", default="data/warehouse.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=_serve)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
