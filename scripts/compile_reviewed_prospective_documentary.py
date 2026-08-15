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
    p = argparse.ArgumentParser(description="Compile one or more reviewed RK-MIS documentary ledgers into point-in-time features")
    p.add_argument("--ledger", type=Path, required=True, nargs="+")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--db", type=Path)
    args = p.parse_args()

    ledgers: list[tuple[Path, dict[str, Any]]] = []
    as_of_dates = set()
    for path in args.ledger:
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("status") != "REVIEWED_PRIMARY_SOURCE_EVIDENCE":
            raise ValueError(f"ledger is not in reviewed-primary-evidence state: {path}")
        as_of_dates.add(str(ledger["as_of_date"]))
        ledgers.append((path, ledger))
    if len(as_of_dates) != 1:
        raise ValueError(f"all layered ledgers must use one as_of_date, got {sorted(as_of_dates)}")
    as_of = next(iter(as_of_dates))

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
    total_decisions = 0

    for path, ledger in ledgers:
        decisions = ledger.get("decisions") or []
        if not isinstance(decisions, list) or not decisions:
            raise ValueError(f"review ledger contains no decisions: {path}")
        total_decisions += len(decisions)
        reviewed_at_default = str(ledger.get("review_policy", {}).get("reviewed_at") or as_of)
        reviewer_default = str(ledger.get("review_policy", {}).get("reviewer") or "RK_MIS_REVIEW")
        for item in decisions:
            claim = item.get("claim")
            review = item.get("review") or {}
            if not isinstance(claim, dict):
                raise ValueError(f"decision in {path} requires a claim object")
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

    ledger_meta = [
        {"path": str(path), "sha256": sha256(path.read_bytes()), "decision_rows": len(ledger.get("decisions") or [])}
        for path, ledger in ledgers
    ]
    report = {
        "version": "rk-mis-reviewed-prospective-documentary-compile-v1.1",
        "ledgers": ledger_meta,
        "ledger_chain_sha256": sha256("\n".join(f"{x['path']}|{x['sha256']}" for x in ledger_meta).encode("utf-8")),
        "as_of_date": as_of,
        "decision_rows": total_decisions,
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
        "version": "rk-mis-prospective-documentary-features-v1.1",
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
