from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import probe_smart_money_4q_coverage as base_probe
import run_technical_replay as market
from multibagger_pipeline.empirical_sampling import deterministic_market_sample


def identity(row: dict[str, Any]) -> str:
    return str(row.get("isin") or row.get("canonical_id") or row.get("symbol") or "").upper()


def sample_hash(rows: list[dict[str, Any]]) -> str:
    payload = [{"isin": row.get("isin"), "symbol": row.get("symbol")} for row in rows]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def pct(n: int, d: int) -> float:
    return round(n / d * 100.0, 2) if d else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Final pre-outcome Smart Money 4Q coverage expansion")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_PRE_OUTCOME_4Q_COVERAGE_EXPANSION":
        raise RuntimeError("coverage expansion protocol is not frozen")
    if protocol.get("future_outcomes_seen") is not False:
        raise RuntimeError("coverage expansion must remain pre-outcome")

    lock_path = ROOT / protocol["semantic_mapping_lock"]
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("status") != "LOCKED_BEFORE_NUMERIC_4Q_FEATURE_RECONSTRUCTION_AND_OUTCOME_LOAD":
        raise RuntimeError("semantic mapping lock is not frozen")
    if lock.get("anchor_date") != protocol["anchor_date"]:
        raise RuntimeError("semantic mapping anchor mismatch")

    coordinator = market.OfficialSession(request_budget=40, timeout=30, sleep_seconds=0.04)
    resolved_anchor, anchor_raw, anchor_meta = market.resolve_on_or_before(protocol["anchor_date"], coordinator)
    universe = market.company_universe(market.parse_bhavcopy(anchor_raw), resolved_anchor)

    base_cfg = protocol["base_sample"]
    extension_cfg = protocol["extension_sample"]
    base_sample = deterministic_market_sample(
        universe,
        sample_size=int(base_cfg["size"]),
        salt=base_cfg["salt"],
        strata_fields=("market_type",),
    )
    actual_base_hash = sample_hash(base_sample)
    if actual_base_hash != base_cfg["expected_identity_sha256"]:
        raise RuntimeError(
            f"base sample identity drift: expected {base_cfg['expected_identity_sha256']}, got {actual_base_hash}"
        )

    base_ids = {identity(row) for row in base_sample}
    if "" in base_ids:
        raise RuntimeError("base sample contains missing identity")
    remaining = [row for row in universe if identity(row) not in base_ids]
    extension_sample = deterministic_market_sample(
        remaining,
        sample_size=int(extension_cfg["size"]),
        salt=extension_cfg["salt"],
        strata_fields=("market_type",),
    )
    extension_ids = {identity(row) for row in extension_sample}
    if base_ids & extension_ids:
        raise RuntimeError("base and extension samples overlap")
    sample = base_sample + extension_sample
    if len(sample) != int(protocol["total_sample_size"]):
        raise RuntimeError(f"expanded sample underflow: expected {protocol['total_sample_size']}, got {len(sample)}")

    acquisition_protocol = {
        "anchor_date": protocol["anchor_date"],
        "shareholding_window": protocol["shareholding_window"],
        "required_distinct_safe_periods": protocol["required_distinct_safe_periods"],
    }
    with ThreadPoolExecutor(max_workers=base_probe.WORKERS, thread_name_prefix="rk-mis-smart-money-expand") as pool:
        rows = list(pool.map(lambda member: base_probe.acquire_company(member, acquisition_protocol, lock), sample))

    span_min = int(protocol["required_first_to_last_span_days_min"])
    span_max = int(protocol["required_first_to_last_span_days_max"])
    required = int(protocol["required_distinct_safe_periods"])
    for row in rows:
        span = row.get("span_days")
        row["valid_4q_span"] = bool(span is not None and span_min <= int(span) <= span_max)

    metadata_five = sum(row["safe_period_count"] >= required for row in rows)
    selected_five = sum(bool(row["selected_five"]) for row in rows)
    span_valid = sum(bool(row["selected_five"] and row["valid_4q_span"]) for row in rows)
    parsed_span_valid = sum(bool(row["xbrl_five_parsed"] and row["valid_4q_span"]) for row in rows)

    fields = (
        "mf_holding_change_pp_4q",
        "fii_holding_change_pp_4q",
        "institutional_breadth_change_4q",
    )
    component_counts = {
        field: sum(bool(row["valid_4q_span"] and row["component_complete"][field]) for row in rows)
        for field in fields
    }
    all_three = sum(bool(row["valid_4q_span"] and row["components_complete_count"] == 3) for row in rows)
    at_least_two = sum(bool(row["valid_4q_span"] and row["components_complete_count"] >= 2) for row in rows)
    spans = [int(row["span_days"]) for row in rows if row.get("span_days") is not None]
    valid_spans = [int(row["span_days"]) for row in rows if row.get("valid_4q_span")]

    source_hash_payload = [
        {"metadata_hash": row["metadata_hash"], "xbrl_hashes": sorted(row["xbrl_hashes"])}
        for row in rows
    ]
    source_hash_chain = hashlib.sha256(
        json.dumps(source_hash_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    extension_hash = sample_hash(extension_sample)
    expanded_hash = sample_hash(sample)
    minimum = int(protocol["minimum_future_holdout_cohort"])

    report = {
        "version": "rk-mis-smart-money-4q-coverage-expansion-result-v2",
        "status": "PRE_OUTCOME_4Q_COVERAGE_EXPANSION_COMPLETE",
        "protocol": str(args.protocol),
        "protocol_sha256": hashlib.sha256(args.protocol.read_bytes()).hexdigest(),
        "semantic_mapping_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "anchor_target": protocol["anchor_date"],
        "anchor_resolved_trading_date": resolved_anchor,
        "eligible_company_universe_rows": len(universe),
        "sampling": {
            "base_rows": len(base_sample),
            "base_identity_sha256": actual_base_hash,
            "base_identity_preserved": True,
            "extension_rows": len(extension_sample),
            "extension_identity_sha256": extension_hash,
            "base_extension_overlap": 0,
            "total_rows": len(sample),
            "expanded_identity_sha256": expanded_hash,
        },
        "coverage": {
            "symbols_with_at_least_five_safe_resolvable_periods": metadata_five,
            "symbols_with_five_selected_periods": selected_five,
            "symbols_with_valid_330_to_400_day_4q_span": span_valid,
            "symbols_with_five_xbrls_parsed_and_valid_4q_span": parsed_span_valid,
            "mf_valid_4q_complete": component_counts["mf_holding_change_pp_4q"],
            "fii_valid_4q_complete": component_counts["fii_holding_change_pp_4q"],
            "institutional_breadth_valid_4q_complete": component_counts["institutional_breadth_change_4q"],
            "all_three_components_valid_4q_complete": all_three,
            "all_three_coverage_pct": pct(all_three, len(sample)),
            "at_least_two_components_valid_4q_complete": at_least_two,
            "at_least_two_coverage_pct": pct(at_least_two, len(sample)),
            "minimum_future_holdout_cohort": minimum,
        },
        "period_span_days": {
            "all_five_period_sequences_n": len(spans),
            "all_min": min(spans) if spans else None,
            "all_median": round(float(statistics.median(spans)), 2) if spans else None,
            "all_max": max(spans) if spans else None,
            "valid_4q_sequences_n": len(valid_spans),
            "valid_min": min(valid_spans) if valid_spans else None,
            "valid_median": round(float(statistics.median(valid_spans)), 2) if valid_spans else None,
            "valid_max": max(valid_spans) if valid_spans else None,
        },
        "acquisition": {
            "parallel_workers": base_probe.WORKERS,
            "worker_requests": sum(int(row["requests"]) for row in rows),
            "coordinator_requests": coordinator.requests_made,
            "api_attempts_ok": sum(int(row["api_attempts_ok"]) for row in rows),
            "api_attempts_error": sum(int(row["api_attempts_error"]) for row in rows),
            "xbrl_parse_errors": sum(int(row["xbrl_errors"]) for row in rows),
            "source_hash_chain_sha256": source_hash_chain,
            "anchor_source": anchor_meta,
        },
        "decision": (
            "PROCEED_TO_SEPARATELY_PREREGISTERED_SMART_MONEY_PARTIAL_HOLDOUT"
            if all_three >= minimum
            else "STOP_HISTORICAL_SMART_MONEY_SOURCE_PATH_BEFORE_OUTCOME_LOAD"
        ),
        "company_level_numeric_values_published": False,
        "company_level_coverage_rows_published": False,
        "raw_xbrl_published": False,
        "future_outcomes_seen": False,
        "official_score_mutated": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
