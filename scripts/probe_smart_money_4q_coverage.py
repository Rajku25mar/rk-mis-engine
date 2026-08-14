from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import probe_smart_money_xbrl_mapping as mapping_probe
import probe_smart_money_xbrl_schema as schema_probe
import run_promoter_accumulation_replay as promoter
import run_technical_replay as market
from multibagger_pipeline.empirical_sampling import deterministic_market_sample

WORKERS = 4
_TLS = threading.local()


def worker_session() -> market.OfficialSession:
    session = getattr(_TLS, "session", None)
    if session is None:
        session = market.OfficialSession(request_budget=500, timeout=30, sleep_seconds=0.04)
        _TLS.session = session
    return session


def latest_distinct_safe_xbrls(raw_rows: list[dict[str, Any]], anchor: str, count: int) -> list[dict[str, Any]]:
    by_period: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        norm = promoter.normalize_shareholding_row(raw)
        url = schema_probe.official_xbrl_url(raw.get("xbrl"))
        if not url or not promoter.safe_at_anchor(norm, anchor):
            continue
        period = str(norm.get("period_end") or "")
        known = str(norm.get("version_known_at") or "")
        if not period or not known:
            continue
        candidate = {
            "period_end": period,
            "known_at": known,
            "known_at_basis": norm.get("known_at_basis"),
            "source_url": url,
        }
        prior = by_period.get(period)
        if prior is None or (known, url) > (str(prior.get("known_at") or ""), str(prior.get("source_url") or "")):
            by_period[period] = candidate
    ordered = sorted(by_period.values(), key=lambda row: (row["period_end"], row["known_at"]))
    return ordered[-count:]


def mapping_complete_for_period(content: bytes, lock: dict[str, Any]) -> tuple[dict[str, bool], str]:
    contexts, facts = mapping_probe.parse_contexts_and_fact_meta(content)
    components = lock["approved_component_mappings"]
    result: dict[str, bool] = {}
    for field in (
        "mf_holding_change_pp_4q",
        "fii_holding_change_pp_4q",
        "institutional_breadth_change_4q",
    ):
        spec = components[field]
        meta = mapping_probe.mapping_meta(
            contexts,
            facts,
            member=spec["category_member"],
            fact=spec["fact"],
        )
        result[field] = bool(meta["unambiguous"])
    return result, hashlib.sha256(content).hexdigest()


def acquire_company(member: dict[str, Any], protocol: dict[str, Any], lock: dict[str, Any]) -> dict[str, Any]:
    session = worker_session()
    before = session.requests_made
    required = int(protocol["required_distinct_safe_periods"])
    window = protocol["shareholding_window"]
    raw_rows, attempts, metadata_hash = promoter.fetch_symbol_shareholding(
        session,
        member["symbol"],
        window["from_date"],
        protocol["anchor_date"],
    )
    selected = latest_distinct_safe_xbrls(raw_rows, protocol["anchor_date"], required)
    safe_period_count = len({
        str(promoter.normalize_shareholding_row(raw).get("period_end") or "")
        for raw in raw_rows
        if promoter.safe_at_anchor(promoter.normalize_shareholding_row(raw), protocol["anchor_date"])
        and schema_probe.official_xbrl_url(raw.get("xbrl"))
    } - {""})

    span_days = None
    if len(selected) == required:
        span_days = (
            date.fromisoformat(selected[-1]["period_end"])
            - date.fromisoformat(selected[0]["period_end"])
        ).days

    period_results: list[dict[str, Any]] = []
    xbrl_hashes: list[str] = []
    xbrl_errors = 0
    if len(selected) == required:
        for row in selected:
            try:
                content = session.request(
                    row["source_url"],
                    referer=promoter.REFERER,
                    accept="application/xml,text/xml,*/*",
                )
                mapped, source_hash = mapping_complete_for_period(content, lock)
                xbrl_hashes.append(source_hash)
                period_results.append({"period_end": row["period_end"], "mapped": mapped})
            except Exception:
                xbrl_errors += 1
                period_results.append({
                    "period_end": row["period_end"],
                    "mapped": {
                        "mf_holding_change_pp_4q": False,
                        "fii_holding_change_pp_4q": False,
                        "institutional_breadth_change_4q": False,
                    },
                })

    component_complete: dict[str, bool] = {}
    for field in (
        "mf_holding_change_pp_4q",
        "fii_holding_change_pp_4q",
        "institutional_breadth_change_4q",
    ):
        component_complete[field] = bool(
            len(period_results) == required
            and all(bool(row["mapped"].get(field)) for row in period_results)
        )

    return {
        "safe_period_count": safe_period_count,
        "selected_five": len(selected) == required,
        "span_days": span_days,
        "xbrl_five_parsed": len(period_results) == required and xbrl_errors == 0,
        "xbrl_errors": xbrl_errors,
        "component_complete": component_complete,
        "components_complete_count": sum(component_complete.values()),
        "metadata_hash": metadata_hash,
        "xbrl_hashes": xbrl_hashes,
        "api_attempts_ok": sum(a.get("status") == "OK" for a in attempts),
        "api_attempts_error": sum(a.get("status") == "ERROR" for a in attempts),
        "requests": session.requests_made - before,
    }


def pct(n: int, d: int) -> float:
    return round(n / d * 100.0, 2) if d else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-outcome 5-quarter Smart Money XBRL coverage diagnosis")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_PRE_OUTCOME_4Q_COVERAGE_DIAGNOSIS":
        raise RuntimeError("coverage protocol is not frozen")
    if protocol.get("future_outcomes_seen") is not False:
        raise RuntimeError("coverage diagnosis must remain pre-outcome")

    lock_path = ROOT / protocol["semantic_mapping_lock"]
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("status") != "LOCKED_BEFORE_NUMERIC_4Q_FEATURE_RECONSTRUCTION_AND_OUTCOME_LOAD":
        raise RuntimeError("semantic mapping is not frozen")
    if lock.get("anchor_date") != protocol["anchor_date"]:
        raise RuntimeError("semantic lock anchor mismatch")
    if lock.get("future_outcomes_seen") is not False:
        raise RuntimeError("semantic lock indicates future outcome exposure")

    coordinator = market.OfficialSession(request_budget=40, timeout=30, sleep_seconds=0.04)
    resolved_anchor, anchor_raw, anchor_meta = market.resolve_on_or_before(protocol["anchor_date"], coordinator)
    universe = market.company_universe(market.parse_bhavcopy(anchor_raw), resolved_anchor)
    sample = deterministic_market_sample(
        universe,
        sample_size=int(protocol["sample_size"]),
        salt=protocol["sample_salt"],
        strata_fields=("market_type",),
    )
    if len(sample) != int(protocol["sample_size"]):
        raise RuntimeError(f"sample underflow: expected {protocol['sample_size']}, got {len(sample)}")

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="rk-mis-smart-money") as pool:
        rows = list(pool.map(lambda member: acquire_company(member, protocol, lock), sample))

    required = int(protocol["required_distinct_safe_periods"])
    metadata_five = sum(row["safe_period_count"] >= required for row in rows)
    selected_five = sum(row["selected_five"] for row in rows)
    parsed_five = sum(row["xbrl_five_parsed"] for row in rows)
    mf_complete = sum(row["component_complete"]["mf_holding_change_pp_4q"] for row in rows)
    fii_complete = sum(row["component_complete"]["fii_holding_change_pp_4q"] for row in rows)
    breadth_complete = sum(row["component_complete"]["institutional_breadth_change_4q"] for row in rows)
    all_three = sum(row["components_complete_count"] == 3 for row in rows)
    at_least_two = sum(row["components_complete_count"] >= 2 for row in rows)
    spans = [int(row["span_days"]) for row in rows if row["span_days"] is not None]

    source_hash_payload = []
    for row in rows:
        source_hash_payload.append({
            "metadata_hash": row["metadata_hash"],
            "xbrl_hashes": sorted(row["xbrl_hashes"]),
        })
    source_hash_chain = hashlib.sha256(
        json.dumps(source_hash_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    sample_identity_hash = hashlib.sha256(
        json.dumps(
            [{"isin": row["isin"], "symbol": row["symbol"]} for row in sample],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    report = {
        "version": "rk-mis-smart-money-4q-coverage-diagnosis-v1",
        "status": "PRE_OUTCOME_4Q_COVERAGE_DIAGNOSIS_COMPLETE",
        "protocol": str(args.protocol),
        "protocol_sha256": hashlib.sha256(args.protocol.read_bytes()).hexdigest(),
        "semantic_mapping_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "anchor_target": protocol["anchor_date"],
        "anchor_resolved_trading_date": resolved_anchor,
        "eligible_company_universe_rows": len(universe),
        "sample_rows": len(sample),
        "sample_identity_sha256": sample_identity_hash,
        "coverage": {
            "symbols_with_at_least_five_safe_resolvable_periods": metadata_five,
            "metadata_five_period_coverage_pct": pct(metadata_five, len(sample)),
            "symbols_with_five_selected_periods": selected_five,
            "symbols_with_all_five_xbrls_parsed_without_error": parsed_five,
            "mf_five_period_complete": mf_complete,
            "fii_five_period_complete": fii_complete,
            "institutional_breadth_five_period_complete": breadth_complete,
            "all_three_components_five_period_complete": all_three,
            "all_three_coverage_pct": pct(all_three, len(sample)),
            "at_least_two_components_five_period_complete": at_least_two,
            "at_least_two_coverage_pct": pct(at_least_two, len(sample)),
            "minimum_future_holdout_cohort": int(protocol["minimum_future_holdout_cohort"]),
        },
        "period_span_days": {
            "n": len(spans),
            "min": min(spans) if spans else None,
            "median": round(float(statistics.median(spans)), 2) if spans else None,
            "max": max(spans) if spans else None,
        },
        "acquisition": {
            "parallel_workers": WORKERS,
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
            if all_three >= int(protocol["minimum_future_holdout_cohort"])
            else "STOP_BEFORE_OUTCOME_LOAD_MINIMUM_ALL_THREE_COHORT_NOT_MET"
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
