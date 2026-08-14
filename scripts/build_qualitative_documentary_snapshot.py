from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

RUNWAY_CATEGORIES = {
    "committed_capacity_expansion",
    "multi_phase_growth_roadmap",
    "funding_visibility",
    "physical_or_operational_headroom",
    "ramp_or_utilisation_headroom",
}
MOAT_CATEGORIES = {
    "proprietary_or_ip",
    "qualification_or_regulatory_barrier",
    "customer_stickiness",
    "cost_process_or_scale_advantage",
    "market_position_or_limited_competition",
}
OPTIONALITY_CATEGORIES = {
    "new_product_or_platform",
    "new_customer_or_vendor_approval",
    "new_geography",
    "export_expansion",
    "adjacent_vertical_or_use_case",
}


class SnapshotError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def band_fraction(value: float | None, spec: dict[str, Any]) -> float | None:
    if value is None:
        return None
    if spec["direction"] == "higher":
        for threshold, fraction in sorted(spec["bands"], key=lambda x: float(x[0]), reverse=True):
            if value >= float(threshold):
                return float(fraction)
    else:
        for threshold, fraction in sorted(spec["bands"], key=lambda x: float(x[0])):
            if value <= float(threshold):
                return float(fraction)
    return 0.0


def approved_pre_anchor(records: list[dict[str, Any]], anchor: str) -> list[dict[str, Any]]:
    return [
        r for r in records
        if str(r.get("review_state") or "").upper() == "APPROVED"
        and str(r.get("known_at") or "")[:10] <= anchor
    ]


def evidence_score(records: list[dict[str, Any]], family: str, allowed_categories: set[str]) -> float | None:
    categories = {
        str(r.get("evidence_category"))
        for r in records
        if r.get("evidence_family") == family and r.get("evidence_category") in allowed_categories
    }
    return float(min(100, len(categories) * 20)) if categories else None


def build_features(records: list[dict[str, Any]], anchor: str) -> dict[str, Any]:
    valid = approved_pre_anchor(records, anchor)
    return {
        "reinvestment_runway_score": evidence_score(valid, "runway", RUNWAY_CATEGORIES),
        "moat_evidence_score": evidence_score(valid, "moat", MOAT_CATEGORIES),
        "new_product_export_optionalities_score": evidence_score(valid, "optionality", OPTIONALITY_CATEGORIES),
        "approved_review_records": len(valid),
    }


def build_snapshot(protocol: dict[str, Any], reviews: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    anchor = protocol["anchor_date"]
    specs = {x["field"]: x for x in protocol["features"]}
    by_isin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    symbols: dict[str, str] = {}
    for row in reviews:
        isin = str(row.get("sample_isin") or "").strip().upper()
        symbol = str(row.get("sample_symbol") or "").strip().upper()
        if not isin or not symbol:
            continue
        by_isin[isin].append(row)
        symbols[isin] = symbol

    rows = []
    coverage_counts = defaultdict(int)
    for isin in sorted(by_isin):
        features = build_features(by_isin[isin], anchor)
        details = {}
        covered = earned = 0.0
        for field, spec in specs.items():
            value = features.get(field)
            fraction = band_fraction(value, spec)
            weight = float(spec["effective_100_point_weight"])
            if fraction is not None:
                covered += weight
                earned += weight * fraction
                coverage_counts[field] += 1
            details[field] = {"value": value, "fraction": fraction, "effective_weight": weight}
        covered_features = [field for field, d in details.items() if d["fraction"] is not None]
        runway_or_moat = any(x in covered_features for x in ("reinvestment_runway_score", "moat_evidence_score"))
        eligible = (
            len(covered_features) >= int(protocol["ranking_eligibility"]["minimum_covered_features"])
            and (runway_or_moat if protocol["ranking_eligibility"]["must_cover_runway_or_moat"] else True)
        )
        score = round(earned / covered * 100.0, 4) if eligible and covered > 0 else None
        rows.append({
            "sample_isin": isin,
            "sample_symbol": symbols[isin],
            **features,
            "covered_features": covered_features,
            "covered_effective_weight": round(covered, 4),
            "qualitative_documentary_partial_score": score,
            "ranking_eligible": eligible,
            "feature_details": details,
        })

    manifest = {
        "anchor_date": anchor,
        "companies_with_review_records": len(rows),
        "ranking_eligible_rows": sum(bool(r["ranking_eligible"]) for r in rows),
        "feature_coverage_counts": dict(sorted(coverage_counts.items())),
        "outcomes_seen_when_predictor_snapshot_frozen": False,
        "missing_data_imputed": False,
    }
    return rows, manifest


def main() -> None:
    p = argparse.ArgumentParser(description="Compile frozen RK-MIS qualitative documentary predictor snapshot")
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--rubric", type=Path, required=True)
    p.add_argument("--review-policy", type=Path, required=True)
    p.add_argument("--reviews", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    rubric = json.loads(args.rubric.read_text(encoding="utf-8"))
    review_policy = json.loads(args.review_policy.read_text(encoding="utf-8"))
    ledger = json.loads(args.reviews.read_text(encoding="utf-8"))
    if protocol.get("status") != "PREREGISTERED_AFTER_PRE_OUTCOME_DATA_AVAILABILITY_DIAGNOSIS_BEFORE_REVIEW_AND_OUTCOME_LOAD":
        raise SnapshotError("qualitative protocol not frozen")
    if rubric.get("status") != "LOCKED_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD":
        raise SnapshotError("documentary rubric not frozen")
    if review_policy.get("status") != "LOCKED_BEFORE_RELATION_RESULT_REVIEW_AND_OUTCOME_LOAD":
        raise SnapshotError("review policy not frozen")
    if ledger.get("anchor_date") != protocol["anchor_date"]:
        raise SnapshotError("review ledger anchor mismatch")
    if ledger.get("future_outcomes_seen") not in (False, None):
        raise SnapshotError("review ledger indicates future outcomes were seen")

    rows, manifest = build_snapshot(protocol, ledger.get("reviews") or [])
    freeze_payload = [
        {
            "sample_isin": r["sample_isin"],
            "sample_symbol": r["sample_symbol"],
            "reinvestment_runway_score": r["reinvestment_runway_score"],
            "moat_evidence_score": r["moat_evidence_score"],
            "new_product_export_optionalities_score": r["new_product_export_optionalities_score"],
            "qualitative_documentary_partial_score": r["qualitative_documentary_partial_score"],
            "ranking_eligible": r["ranking_eligible"],
        }
        for r in rows
    ]
    manifest.update({
        "protocol_sha256": sha256(args.protocol.read_bytes()),
        "rubric_sha256": sha256(args.rubric.read_bytes()),
        "review_policy_sha256": sha256(args.review_policy.read_bytes()),
        "approved_review_ledger_sha256": sha256(args.reviews.read_bytes()),
        "predictor_snapshot_sha256": sha256(json.dumps(freeze_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")),
    })
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "predictor_snapshot.json").write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
