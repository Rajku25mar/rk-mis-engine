from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_smart_money import ProspectiveSmartMoneyStore

FEATURE_FIELDS = (
    "mf_holding_change_pp_4q",
    "fii_holding_change_pp_4q",
    "institutional_breadth_change_4q",
    "promoter_holding_change_pp_4q",
)


def load_observations(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("observations"), list):
        rows = payload["observations"]
    else:
        raise ValueError("input JSON must be a list or an object with an observations list")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("every observation must be a JSON object")
    return rows


def cmd_append(args: argparse.Namespace) -> int:
    store = ProspectiveSmartMoneyStore(args.db)
    rows = load_observations(args.input)
    ids = store.add_many(rows)
    print(json.dumps({"status": "ok", "observations_received": len(rows), "observation_ids": ids}, indent=2))
    return 0


def feature_rows(store: ProspectiveSmartMoneyStore, as_of: str) -> list[dict[str, Any]]:
    out = []
    for result in store.derive_all(as_of_date=as_of):
        row = {
            "isin": result.get("isin"),
            "symbol": result.get("symbol"),
            "as_of_date": result["as_of_date"],
            "span_days": result["span_days"],
            "available_feature_count": len(result["available_features"]),
            "warnings": result["warnings"],
        }
        row.update(result["features"])
        out.append(row)
    return out


def cmd_export(args: argparse.Namespace) -> int:
    store = ProspectiveSmartMoneyStore(args.db)
    rows = feature_rows(store, args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "rk-mis-prospective-smart-money-features-v1",
        "as_of_date": args.as_of,
        "rows": rows,
        "guardrails": {
            "required_periods": 5,
            "required_span_days": "330-400",
            "missing_data_imputed": False,
            "score_weights_changed": False,
        },
    }
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        fields = ["isin", "symbol", "as_of_date", *FEATURE_FIELDS, "span_days", "available_feature_count", "warnings"]
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                item = dict(row)
                item["warnings"] = "|".join(item.get("warnings") or [])
                writer.writerow({field: item.get(field) for field in fields})
    print(json.dumps({
        "status": "ok",
        "as_of_date": args.as_of,
        "identities": len(rows),
        "rows_with_any_approved_feature": sum(any(row.get(field) is not None for field in FEATURE_FIELDS) for row in rows),
        "output": str(args.output),
        "csv": None if not args.csv else str(args.csv),
    }, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = ProspectiveSmartMoneyStore(args.db)
    rows = feature_rows(store, args.as_of)
    coverage = {field: sum(row.get(field) is not None for row in rows) for field in FEATURE_FIELDS}
    print(json.dumps({
        "as_of_date": args.as_of,
        "identities": len(rows),
        "coverage_counts": coverage,
        "rows_with_any_approved_feature": sum(any(row.get(field) is not None for field in FEATURE_FIELDS) for row in rows),
        "rows_with_mf_fii_and_breadth": sum(all(row.get(field) is not None for field in FEATURE_FIELDS[:3]) for row in rows),
        "missing_data_imputed": False,
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RK-MIS prospective Smart Money point-in-time journal")
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append-json", help="Append authorized point-in-time observations from JSON")
    append.add_argument("--db", type=Path, required=True)
    append.add_argument("--input", type=Path, required=True)
    append.set_defaults(func=cmd_append)

    export = sub.add_parser("export-features", help="Export only qualified existing RK-MIS 4Q feature fields")
    export.add_argument("--db", type=Path, required=True)
    export.add_argument("--as-of", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--csv", type=Path)
    export.set_defaults(func=cmd_export)

    status = sub.add_parser("status", help="Show aggregate prospective Smart Money coverage")
    status.add_argument("--db", type=Path, required=True)
    status.add_argument("--as-of", required=True)
    status.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
