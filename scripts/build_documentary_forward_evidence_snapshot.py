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


def num(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def band_fraction(value: float | None, spec: dict[str, Any]) -> float | None:
    if value is None:
        return None
    if spec["direction"] == "higher":
        for threshold, frac in sorted(spec["bands"], key=lambda x: float(x[0]), reverse=True):
            if value >= float(threshold):
                return float(frac)
    else:
        for threshold, frac in sorted(spec["bands"], key=lambda x: float(x[0])):
            if value <= float(threshold):
                return float(frac)
    return 0.0


def approved(records: list[dict[str, Any]], anchor: str) -> list[dict[str, Any]]:
    return [
        r for r in records
        if str(r.get("review_state") or "").upper() == "APPROVED"
        and str(r.get("known_at") or "")[:10] <= anchor
    ]


def qualitative_score(records: list[dict[str, Any]], family: str, allowed: set[str]) -> float | None:
    categories = {
        str(r.get("evidence_category"))
        for r in records
        if r.get("evidence_family") == family and r.get("evidence_category") in allowed
    }
    return float(min(100, 20 * len(categories))) if categories else None


def _latest_direct_metric(records: list[dict[str, Any]], metric_name: str) -> float | None:
    rows = [r for r in records if r.get("metric_name") == metric_name and num(r.get("metric_value")) is not None]
    if not rows:
        return None
    rows.sort(key=lambda r: (str(r.get("known_at") or ""), str(r.get("source_sha256") or "")))
    latest_date = str(rows[-1].get("known_at") or "")[:10]
    latest = [r for r in rows if str(r.get("known_at") or "")[:10] == latest_date]
    values = {round(float(r["metric_value"]), 10) for r in latest}
    return next(iter(values)) if len(values) == 1 else None


def numeric_metric(records: list[dict[str, Any]], metric: str) -> float | None:
    direct = _latest_direct_metric(records, metric)
    if direct is not None:
        return direct
    if metric == "orderbook_to_sales":
        numerator = _latest_direct_metric(records, "orderbook_cr")
        denominator = _latest_direct_metric(records, "revenue_cr")
        if numerator is None or denominator in (None, 0):
            return None
        return round(numerator / denominator, 6)
    if metric == "planned_capacity_increase_pct":
        inc = _latest_direct_metric(records, "incremental_capacity")
        base = _latest_direct_metric(records, "pre_expansion_capacity")
        if inc is None or base in (None, 0):
            return None
        units = {
            str(r.get("metric_unit") or "").strip().upper()
            for r in records
            if r.get("metric_name") in {"incremental_capacity", "pre_expansion_capacity"}
            and num(r.get("metric_value")) is not None
        }
        if len(units) != 1:
            return None
        return round(inc / base * 100.0, 6)
    raise SnapshotError(f"unknown numeric metric {metric}")


def build_company_features(records: list[dict[str, Any]], anchor: str) -> dict[str, Any]:
    valid = approved(records, anchor)
    return {
        "orderbook_to_sales": numeric_metric(valid, "orderbook_to_sales"),
        "planned_capacity_increase_pct": numeric_metric(valid, "planned_capacity_increase_pct"),
        "reinvestment_runway_score": qualitative_score(valid, "runway", RUNWAY_CATEGORIES),
        "moat_evidence_score": qualitative_score(valid, "moat", MOAT_CATEGORIES),
        "new_product_export_optionalities_score": qualitative_score(valid, "optionality", OPTIONALITY_CATEGORIES),
        "approved_review_records": len(valid),
    }


def build_snapshot(protocol: dict[str, Any], reviews: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    anchor = protocol["anchor_date"]
    specs = {x["field"]: x for x in protocol["features"]}
    by_isin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    symbols = {}
    for row in reviews:
        isin = str(row.get("sample_isin") or "").strip().upper()
        if not isin:
            continue
        by_isin[isin].append(row)
        symbols[isin] = str(row.get("sample_symbol") or "").strip().upper()

    rows = []
    coverage_counts = defaultdict(int)
    for isin in sorted(by_isin):
        features = build_company_features(by_isin[isin], anchor)
        details = {}
        earned = covered = 0.0
        numeric_covered = False
        documentary_covered = False
        for field, spec in specs.items():
            value = features.get(field)
            frac = band_fraction(value, spec)
            weight = float(spec["experimental_slice_weight"])
            if frac is not None:
                covered += weight
                earned += weight * frac
                coverage_counts[field] += 1
                if field in {"orderbook_to_sales", "planned_capacity_increase_pct"}:
                    numeric_covered = True
                else:
                    documentary_covered = True
            details[field] = {"value": value, "fraction": frac, "weight": weight}
        covered_count = sum(x["fraction"] is not None for x in details.values())
        eligible = (
            covered_count >= int(protocol["ranking_eligibility"]["minimum_feature_coverage_count"])
            and (numeric_covered if protocol["ranking_eligibility"]["must_include_at_least_one_numeric_catalyst"] else True)
            and (documentary_covered if protocol["ranking_eligibility"]["must_include_at_least_one_approved_documentary_score"] else True)
        )
        score = round(earned / covered * 100.0, 4) if eligible and covered > 0 else None
        rows.append({
            "sample_isin": isin,
            "sample_symbol": symbols.get(isin),
            **features,
            "covered_feature_count": covered_count,
            "covered_weight": covered,
            "documentary_partial_score": score,
            "ranking_eligible": eligible,
            "feature_details": details,
        })
    manifest = {
        "anchor_date": anchor,
        "companies_with_any_review_record": len(rows),
        "ranking_eligible_rows": sum(bool(r["ranking_eligible"]) for r in rows),
        "feature_coverage_counts": dict(sorted(coverage_counts.items())),
        "outcomes_seen_when_predictor_snapshot_frozen": False,
    }
    return rows, manifest


def main() -> None:
    p = argparse.ArgumentParser(description="Build frozen RK-MIS documentary predictor snapshot from approved reviews")
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--rubric", type=Path, required=True)
    p.add_argument("--ledger-schema", type=Path, required=True)
    p.add_argument("--reviews", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    rubric = json.loads(args.rubric.read_text(encoding="utf-8"))
    schema = json.loads(args.ledger_schema.read_text(encoding="utf-8"))
    ledger = json.loads(args.reviews.read_text(encoding="utf-8"))
    if protocol.get("status") != "PREREGISTERED_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD":
        raise SnapshotError("protocol not frozen")
    if rubric.get("status") != "LOCKED_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD":
        raise SnapshotError("rubric not frozen")
    if schema.get("status") != "LOCKED_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD":
        raise SnapshotError("ledger schema not frozen")
    if ledger.get("anchor_date") != protocol["anchor_date"]:
        raise SnapshotError("review ledger anchor mismatch")

    rows, manifest = build_snapshot(protocol, ledger.get("reviews") or [])
    freeze_payload = [
        {
            "sample_isin": r["sample_isin"],
            "sample_symbol": r["sample_symbol"],
            "documentary_partial_score": r["documentary_partial_score"],
            "ranking_eligible": r["ranking_eligible"],
            **{x["field"]: r.get(x["field"]) for x in protocol["features"]},
        }
        for r in rows
    ]
    manifest.update({
        "protocol_sha256": sha256(args.protocol.read_bytes()),
        "rubric_sha256": sha256(args.rubric.read_bytes()),
        "ledger_schema_sha256": sha256(args.ledger_schema.read_bytes()),
        "approved_review_ledger_sha256": sha256(args.reviews.read_bytes()),
        "predictor_snapshot_sha256": sha256(json.dumps(freeze_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")),
    })
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "predictor_snapshot.json").write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
