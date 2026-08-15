from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from multibagger_pipeline.prospective_catalyst_loader import load_reviewed_catalyst_ledgers
from multibagger_pipeline.prospective_catalysts import ProspectiveCatalystStore


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser(description="Compile reviewed RK-MIS catalyst ledgers into point-in-time features")
    p.add_argument("--ledger", type=Path, nargs="+", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    with tempfile.TemporaryDirectory(prefix="rk-mis-catalyst-compile-") as tmp:
        store = ProspectiveCatalystStore(Path(tmp) / "catalyst.sqlite")
        load_report = load_reviewed_catalyst_ledgers(store, args.ledger, scoring_as_of=args.as_of)
        rows = []
        for item in store.identities():
            result = store.derive_features(
                isin=item.get("isin"), symbol=item.get("symbol"), as_of_date=args.as_of,
                ttm_revenue_cr=None,
            )
            rows.append({
                "isin": item.get("isin"),
                "symbol": item.get("symbol"),
                "as_of_date": args.as_of,
                **result["features"],
                "warnings": result["warnings"],
                "provenance": result["provenance"],
                "point_in_time_safe": result["point_in_time_safe"],
            })
        rows.sort(key=lambda r: (str(r.get("symbol") or ""), str(r.get("isin") or "")))

    report = {
        "version": "rk-mis-reviewed-prospective-catalyst-compile-v1",
        "as_of_date": args.as_of,
        "ledger_load": load_report,
        "identities": len(rows),
        "rows_with_order_quality": sum(r["order_quality_score"] is not None for r in rows),
        "rows_with_orderbook_to_sales": sum(r["orderbook_to_sales"] is not None for r in rows),
        "rows_with_capex_execution": sum(r["capex_execution_score"] is not None for r in rows),
        "rows_with_planned_capacity_increase": sum(r["planned_capacity_increase_pct"] is not None for r in rows),
        "orderbook_to_sales_release_rule": "WITHHELD_HERE; RELEASED_ONLY_DURING_RK_MIS_RUN_IF_BASE_INPUT_HAS_EXPLICIT_TTM_REVENUE",
        "official_100_point_weights_changed": False,
        "missing_data_imputed": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "features.json").write_text(json.dumps({
        "version": "rk-mis-prospective-catalyst-features-v1",
        "as_of_date": args.as_of,
        "rows": rows,
        "missing_data_imputed": False,
        "score_weights_changed": False,
    }, indent=2), encoding="utf-8")
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
