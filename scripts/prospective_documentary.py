from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_documentary import ProspectiveDocumentEvidenceStore

QUAL_FEATURES = (
    "reinvestment_runway_score",
    "moat_evidence_score",
    "new_product_export_optionalities_score",
)


def load_rows(path: Path, key: str) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get(key), list):
        rows = payload[key]
    else:
        raise ValueError(f"input JSON must be a list or object containing {key!r}")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("all input rows must be JSON objects")
    return rows


def cmd_append(args: argparse.Namespace) -> int:
    store = ProspectiveDocumentEvidenceStore(args.db)
    rows = load_rows(args.input, "claims")
    ids = store.add_claims(rows)
    print(json.dumps({"status": "ok", "claims_received": len(rows), "claim_ids": ids}, indent=2))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    store = ProspectiveDocumentEvidenceStore(args.db)
    review_id = store.review(
        args.claim_id,
        args.state,
        reviewed_at=args.reviewed_at,
        reviewer=args.reviewer,
        note=args.note,
    )
    print(json.dumps({"status": "ok", "claim_id": args.claim_id, "review_id": review_id, "state": args.state.upper()}, indent=2))
    return 0


def snapshot_rows(store: ProspectiveDocumentEvidenceStore, as_of: str) -> list[dict[str, Any]]:
    out = []
    for row in store.derive_all(as_of_date=as_of):
        flat = {
            "isin": row.get("isin"),
            "symbol": row.get("symbol"),
            "as_of_date": row["as_of_date"],
            **row["features"],
            "eligible_claim_count": row["eligible_claim_count"],
            "ineligible_claim_count": row["ineligible_claim_count"],
            "approved_runway_categories": row["approved_categories"]["RUNWAY"],
            "approved_moat_categories": row["approved_categories"]["MOAT"],
            "approved_optionality_categories": row["approved_categories"]["OPTIONALITY"],
            "linked_closed_promise_ids": row["management_execution"]["linked_closed_promise_ids"],
            "promise_delivery_pct": row["management_execution"]["promise_delivery_pct"],
            "management_score_available": row["management_execution"]["score_available"],
            "warnings": row["warnings"],
        }
        out.append(flat)
    return out


def cmd_export(args: argparse.Namespace) -> int:
    store = ProspectiveDocumentEvidenceStore(args.db)
    rows = snapshot_rows(store, args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "rk-mis-prospective-documentary-features-v1",
        "as_of_date": args.as_of,
        "rows": rows,
        "guardrails": {
            "approved_review_only": True,
            "minimum_extraction_confidence": 0.78,
            "missing_data_imputed": False,
            "management_minimum_closed_promises": 3,
            "management_score_derived_here": False,
            "score_weights_changed": False,
        },
    }
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "isin","symbol","as_of_date",*QUAL_FEATURES,"eligible_claim_count","ineligible_claim_count",
            "approved_runway_categories","approved_moat_categories","approved_optionality_categories",
            "linked_closed_promise_ids","promise_delivery_pct","management_score_available","warnings"
        ]
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                item = dict(row)
                for name in ("approved_runway_categories","approved_moat_categories","approved_optionality_categories","warnings"):
                    item[name] = "|".join(item.get(name) or [])
                writer.writerow({field: item.get(field) for field in fields})
    print(json.dumps({
        "status": "ok",
        "as_of_date": args.as_of,
        "identities": len(rows),
        "rows_with_any_qualitative_feature": sum(any(row.get(field) is not None for field in QUAL_FEATURES) for row in rows),
        "output": str(args.output),
        "csv": None if not args.csv else str(args.csv),
    }, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = ProspectiveDocumentEvidenceStore(args.db)
    rows = snapshot_rows(store, args.as_of)
    print(json.dumps({
        "as_of_date": args.as_of,
        "identities": len(rows),
        "qualitative_feature_coverage": {field: sum(row.get(field) is not None for row in rows) for field in QUAL_FEATURES},
        "rows_with_any_qualitative_feature": sum(any(row.get(field) is not None for field in QUAL_FEATURES) for row in rows),
        "management_score_available_rows": sum(bool(row.get("management_score_available")) for row in rows),
        "missing_data_imputed": False,
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RK-MIS prospective documentary evidence journal")
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append-json")
    append.add_argument("--db", type=Path, required=True)
    append.add_argument("--input", type=Path, required=True)
    append.set_defaults(func=cmd_append)

    review = sub.add_parser("review")
    review.add_argument("--db", type=Path, required=True)
    review.add_argument("--claim-id", required=True)
    review.add_argument("--state", required=True, choices=["approved","rejected","pending"])
    review.add_argument("--reviewed-at", required=True)
    review.add_argument("--reviewer")
    review.add_argument("--note")
    review.set_defaults(func=cmd_review)

    export = sub.add_parser("export-features")
    export.add_argument("--db", type=Path, required=True)
    export.add_argument("--as-of", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--csv", type=Path)
    export.set_defaults(func=cmd_export)

    status = sub.add_parser("status")
    status.add_argument("--db", type=Path, required=True)
    status.add_argument("--as-of", required=True)
    status.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
