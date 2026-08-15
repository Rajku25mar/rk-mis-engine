from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from multibagger_pipeline.prospective_documentary import ProspectiveDocumentEvidenceStore


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser(description="Compile a reviewed RK-MIS documentary ledger into point-in-time features")
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--db", type=Path)
    args = p.parse_args()

    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    if ledger.get("status") != "REVIEWED_PRIMARY_SOURCE_EVIDENCE":
        raise ValueError("ledger is not in reviewed-primary-evidence state")
    as_of = str(ledger["as_of_date"])
    decisions = ledger.get("decisions") or []
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("review ledger contains no decisions")

    if args.db:
        db_path = args.db
        db_path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
    else:
        temp = tempfile.TemporaryDirectory()
        db_path = Path(temp.name) / "prospective_documentary.sqlite"

    store = ProspectiveDocumentEvidenceStore(db_path)
    review_counts: Counter[str] = Counter()
    claim_counts: Counter[str] = Counter()

    reviewed_at_default = str(ledger.get("review_policy", {}).get("reviewed_at") or as_of)
    reviewer_default = str(ledger.get("review_policy", {}).get("reviewer") or "RK_MIS_REVIEW")

    for item in decisions:
        claim = item.get("claim")
        review = item.get("review") or {}
        if not isinstance(claim, dict):
            raise ValueError("each decision requires a claim object")
        state = str(review.get("state") or "").upper()
        if state not in {"APPROVED", "REJECTED", "PENDING"}:
            raise ValueError(f"invalid decision review state {state!r}")
        claim_id = store.add_claim(claim)
        claim_counts[f"{claim['evidence_family']}.{claim['evidence_category']}"] += 1
        store.review(
            claim_id,
            state,
            reviewed_at=str(review.get("reviewed_at") or reviewed_at_default),
            reviewer=str(review.get("reviewer") or reviewer_default),
            note=str(review.get("note") or ""),
        )
        review_counts[state] += 1

    rows = store.derive_all(as_of_date=as_of)
    rows.sort(key=lambda row: (str(row.get("symbol") or ""), str(row.get("isin") or "")))
    feature_rows: list[dict[str, Any]] = []
    for row in rows:
        feature_rows.append({
            "isin": row.get("isin"),
            "symbol": row.get("symbol"),
            "as_of_date": row["as_of_date"],
            **row["features"],
            "approved_categories": row["approved_categories"],
            "eligible_claim_count": row["eligible_claim_count"],
            "ineligible_claim_count": row["ineligible_claim_count"],
            "approved_order_claims": row["nonqualitative_evidence_counts"]["approved_order_claims"],
            "approved_capacity_capex_claims": row["nonqualitative_evidence_counts"]["approved_capacity_capex_claims"],
            "approved_governance_claims": row["nonqualitative_evidence_counts"]["approved_governance_claims"],
            "management_execution": row["management_execution"],
            "warnings": row["warnings"],
            "point_in_time_safe": row["point_in_time_safe"],
        })

    report = {
        "version": "rk-mis-reviewed-prospective-documentary-compile-v1",
        "ledger_path": str(args.ledger),
        "ledger_sha256": sha256(args.ledger.read_bytes()),
        "as_of_date": as_of,
        "decision_rows": len(decisions),
        "review_state_counts": dict(sorted(review_counts.items())),
        "claim_category_counts": dict(sorted(claim_counts.items())),
        "identities": len(feature_rows),
        "rows_with_runway": sum(row.get("reinvestment_runway_score") is not None for row in feature_rows),
        "rows_with_moat": sum(row.get("moat_evidence_score") is not None for row in feature_rows),
        "rows_with_optionality": sum(row.get("new_product_export_optionalities_score") is not None for row in feature_rows),
        "rows_with_management_execution_score": sum(bool(row["management_execution"].get("score_available")) for row in feature_rows),
        "management_minimum_closed_promises": 3,
        "automatic_keyword_candidates_scored": False,
        "official_100_point_weights_changed": False,
        "missing_data_imputed": False,
    }

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "features.json").write_text(json.dumps({
        "version": "rk-mis-prospective-documentary-features-v1",
        "as_of_date": as_of,
        "rows": feature_rows,
        "missing_data_imputed": False,
        "score_weights_changed": False,
    }, indent=2), encoding="utf-8")
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    if temp is not None:
        temp.cleanup()


if __name__ == "__main__":
    main()
