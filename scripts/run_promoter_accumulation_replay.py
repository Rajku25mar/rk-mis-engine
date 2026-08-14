from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Reuse the already-tested public NSE archive, UDiFF and corporate-action adapter.
TECH_SPEC = importlib.util.spec_from_file_location(
    "rk_mis_technical_replay_common", ROOT / "scripts/run_technical_replay.py"
)
TECH = importlib.util.module_from_spec(TECH_SPEC)
assert TECH_SPEC and TECH_SPEC.loader
TECH_SPEC.loader.exec_module(TECH)

REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"


class ReplayError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def robust_date(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
    for fmt, width in (
        ("%d-%b-%Y", 11),
        ("%d-%m-%Y", 10),
        ("%d/%m/%Y", 10),
        ("%d-%b-%y", 9),
    ):
        try:
            return datetime.strptime(text[:width], fmt).date().isoformat()
        except ValueError:
            pass
    return None


def num(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def payload_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "rows", "results"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    return []


def first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def normalize_shareholding_row(row: dict[str, Any]) -> dict[str, Any]:
    period = robust_date(first(row, "date", "asOnDate", "as_on_date", "shareholdingDate", "toDate"))
    submission = robust_date(first(row, "submissionDate", "submission_date", "filingDate", "dateOfSubmission"))
    broadcast = robust_date(first(row, "broadcastDate", "broadcastDateTime", "broadCastDate", "broadCastDateTime", "broadcast_date_time", "systemDate"))
    revision = robust_date(first(row, "revisionDate", "revisedDate", "revision_date", "revised_date"))
    if revision:
        known_candidates = [x for x in (revision, broadcast) if x]
        known = max(known_candidates) if known_candidates else revision
        basis = "REVISION_OR_BROADCAST"
    elif broadcast:
        known = broadcast
        basis = "BROADCAST"
    else:
        known = submission
        basis = "SUBMISSION_FALLBACK_NO_BROADCAST"
    promoter = num(first(row, "pr_and_prgrp", "promoterAndPromoterGroup", "promoter_pct", "promoterHolding", "promoter"))
    return {
        "period_end": period,
        "submission_date": submission,
        "broadcast_date": broadcast,
        "revision_date": revision,
        "version_known_at": known,
        "known_at_basis": basis,
        "promoter_pct": promoter,
    }


def safe_at_anchor(row: dict[str, Any], anchor: str) -> bool:
    period = row.get("period_end")
    known = row.get("version_known_at")
    return bool(period and known and period <= anchor and known <= anchor)


def promoter_change_feature(rows: list[dict[str, Any]], anchor: str, span_min: int, span_max: int) -> dict[str, Any]:
    safe = [r for r in rows if safe_at_anchor(r, anchor) and r.get("promoter_pct") is not None]
    by_period: dict[str, dict[str, Any]] = {}
    for row in sorted(safe, key=lambda x: (x["period_end"], x["version_known_at"])):
        by_period[row["period_end"]] = row
    snapshots = sorted(by_period.values(), key=lambda x: x["period_end"])
    latest5 = snapshots[-5:]
    change = None
    span = None
    if len(latest5) == 5:
        span = (date.fromisoformat(latest5[-1]["period_end"]) - date.fromisoformat(latest5[0]["period_end"])).days
        if span_min <= span <= span_max:
            change = round(float(latest5[-1]["promoter_pct"]) - float(latest5[0]["promoter_pct"]), 4)
    return {
        "safe_snapshot_count": len(snapshots),
        "latest_period": snapshots[-1]["period_end"] if snapshots else None,
        "latest_promoter_pct": snapshots[-1]["promoter_pct"] if snapshots else None,
        "promoter_holding_change_pp_4q": change,
        "five_snapshot_span_days": span,
        "periods": [r["period_end"] for r in latest5],
    }


def fraction(value: float | None, spec: dict[str, Any]) -> float | None:
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


def fetch_symbol_shareholding(
    session: Any,
    symbol: str,
    start: str,
    anchor: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    d0 = date.fromisoformat(start).strftime("%d-%m-%Y")
    d1 = date.fromisoformat(anchor).strftime("%d-%m-%Y")
    attempts: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []
    hash_parts: list[str] = []
    queries = [
        {"index": "equities", "from_date": d0, "to_date": d1, "symbol": symbol},
        {"index": "equities", "symbol": symbol},
    ]
    for idx, query in enumerate(queries):
        url = "https://www.nseindia.com/api/corporate-share-holdings-master?" + urllib.parse.urlencode(query)
        try:
            payload = session.json(url, referer=REFERER)
            rows = payload_rows(payload)
            canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            payload_hash = sha256(canonical)
            attempts.append({"url": url, "status": "OK", "rows": len(rows), "sha256": payload_hash})
            hash_parts.append(payload_hash)
            combined.extend(rows)
            if rows and idx == 0:
                break
        except Exception as exc:
            attempts.append({"url": url, "status": "ERROR", "rows": 0, "error": type(exc).__name__})
        time.sleep(0.04)
    seen: set[str] = set(); deduped: list[dict[str, Any]] = []
    for row in combined:
        key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key); deduped.append(row)
    return deduped, attempts, sha256("|".join(hash_parts).encode("utf-8")) if hash_parts else sha256(b"")


def build_predictor_snapshot(
    sample: list[dict[str, Any]],
    session: Any,
    protocol: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    anchor = protocol["anchor_date"]
    window = protocol["shareholding_window"]
    smart_specs = {x["field"]: x for x in config["pillar_features"]["smart_money"]}
    score_spec = smart_specs["promoter_holding_change_pp_4q"]
    expected = protocol["predictor"]
    if score_spec["direction"] != expected["direction"] or score_spec["bands"] != expected["bands"]:
        raise ReplayError("frozen protocol predictor bands no longer match rk_mie_scoring.json")

    snapshot: list[dict[str, Any]] = []
    source_hashes: list[str] = []
    attempts_ok = attempts_error = 0
    post_anchor_revision_rows_rejected = 0
    symbols_with_any_safe = 0

    for i, member in enumerate(sample):
        raw_rows, attempts, source_hash = fetch_symbol_shareholding(
            session, member["symbol"], window["from_date"], anchor
        )
        source_hashes.append(f"{member['isin']}|{source_hash}")
        attempts_ok += sum(a["status"] == "OK" for a in attempts)
        attempts_error += sum(a["status"] == "ERROR" for a in attempts)
        normalized = [normalize_shareholding_row(r) for r in raw_rows]
        safe = [r for r in normalized if safe_at_anchor(r, anchor)]
        if safe:
            symbols_with_any_safe += 1
        post_anchor_revision_rows_rejected += sum(
            bool(
                r.get("revision_date")
                and r.get("period_end")
                and r["period_end"] <= anchor
                and r["revision_date"] > anchor
            )
            for r in normalized
        )
        feat = promoter_change_feature(
            normalized,
            anchor,
            int(window["required_first_to_last_span_days_min"]),
            int(window["required_first_to_last_span_days_max"]),
        )
        raw_change = feat["promoter_holding_change_pp_4q"]
        frac = fraction(raw_change, score_spec)
        snapshot.append({
            "isin": member["isin"],
            "symbol": member["symbol"],
            **feat,
            "component_fraction": frac,
            "shareholding_replay_grade": raw_change is not None and frac is not None,
        })
        if i + 1 < len(sample):
            time.sleep(0.055)

    freeze_payload = [
        {
            "isin": r["isin"],
            "symbol": r["symbol"],
            "promoter_holding_change_pp_4q": r["promoter_holding_change_pp_4q"],
            "component_fraction": r["component_fraction"],
            "shareholding_replay_grade": r["shareholding_replay_grade"],
        }
        for r in sorted(snapshot, key=lambda x: (x["isin"], x["symbol"]))
    ]
    manifest = {
        "anchor_date": anchor,
        "shareholding_from_date": window["from_date"],
        "sample_rows": len(sample),
        "symbols_with_any_safe_shareholding_row": symbols_with_any_safe,
        "shareholding_replay_grade_rows": sum(bool(r["shareholding_replay_grade"]) for r in snapshot),
        "post_anchor_revision_rows_rejected": post_anchor_revision_rows_rejected,
        "api_attempts_ok": attempts_ok,
        "api_attempts_error": attempts_error,
        "shareholding_source_hash_chain_sha256": sha256("\n".join(sorted(source_hashes)).encode("utf-8")),
        "predictor_snapshot_sha256": sha256(json.dumps(freeze_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")),
        "outcomes_seen_when_snapshot_frozen": False,
        "smart_money_scope": "PROMOTER_HOLDING_CHANGE_ONLY_NO_FII_DII_MF_INFERENCE",
    }
    return snapshot, manifest


def acquire_outcomes(
    snapshot: list[dict[str, Any]],
    anchor_target: str,
    outcome_target: str,
    session: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entry_date, entry_raw, entry_meta = TECH.resolve_after(anchor_target, session)
    exit_date, exit_raw, exit_meta = TECH.resolve_on_or_before(outcome_target, session)
    entry_rows = TECH.parse_bhavcopy(entry_raw)
    exit_rows = TECH.parse_bhavcopy(exit_raw)
    entry_by_isin = {
        str(r.get("isin") or "").upper(): r
        for r in entry_rows if r.get("series") == "EQ" and r.get("isin")
    }
    exit_by_isin = {
        str(r.get("isin") or "").upper(): r
        for r in exit_rows if r.get("series") == "EQ" and r.get("isin")
    }
    action_rows, action_meta = TECH.fetch_actions(session, entry_date, exit_date)
    actions_by_symbol = TECH.normalize_actions(action_rows)

    joined: list[dict[str, Any]] = []
    statuses: dict[str, int] = defaultdict(int)
    for pred in snapshot:
        if not pred.get("shareholding_replay_grade"):
            continue
        isin = pred["isin"]
        erow, xrow = entry_by_isin.get(isin), exit_by_isin.get(isin)
        status = "OBSERVED_CALIBRATION_SAFE"
        multiple = None
        if not erow or not erow.get("close"):
            status = "MISSING_ENTRY_PRICE"
        elif not xrow or not xrow.get("close"):
            status = "MISSING_EXIT_PRICE_OR_DELISTED"
        else:
            aliases = {pred["symbol"], erow["symbol"], xrow["symbol"]}
            actions = TECH.dedupe_actions(
                a for alias in aliases for a in actions_by_symbol.get(alias, [])
            )
            factor_result = TECH.cumulative_backward_price_factor(
                actions, price_date=entry_date, target_date=exit_date
            )
            if not factor_result["calibration_safe"]:
                status = "UNRESOLVED_CORPORATE_ACTION"
            else:
                adjusted_entry = float(erow["close"]) * float(factor_result["price_factor"])
                if adjusted_entry > 0:
                    multiple = round(float(xrow["close"]) / adjusted_entry, 6)
                else:
                    status = "INVALID_ADJUSTED_ENTRY"
        statuses[status] += 1
        joined.append({
            "isin": isin,
            "symbol": pred["symbol"],
            "promoter_holding_change_pp_4q": pred["promoter_holding_change_pp_4q"],
            "component_fraction": pred["component_fraction"],
            "forward_multiple_3y": multiple,
            "outcome_status": status,
        })
    return joined, {
        "entry_date": entry_date,
        "outcome_date": exit_date,
        "entry_source": entry_meta,
        "outcome_source": exit_meta,
        "corporate_action_sources": action_meta,
        "outcome_status_counts": dict(sorted(statuses.items())),
    }


def rank(values: list[float]) -> list[float]:
    pairs = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(pairs):
        j = i + 1
        while j < len(pairs) and pairs[j][1] == pairs[i][1]:
            j += 1
        avg = (i + j - 1) / 2 + 1
        for k in range(i, j):
            ranks[pairs[k][0]] = avg
        i = j
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    numerator = sum((x-ma)*(y-mb) for x, y in zip(ra, rb))
    da = sum((x-ma)**2 for x in ra)
    db = sum((y-mb)**2 for y in rb)
    return None if da <= 0 or db <= 0 else round(numerator / math.sqrt(da*db), 4)


def outcome_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [float(x["forward_multiple_3y"]) for x in rows]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean_multiple": round(sum(vals)/len(vals), 4),
        "two_x_rate_pct": round(sum(x >= 2 for x in vals)/len(vals)*100, 2),
        "five_x_rate_pct": round(sum(x >= 5 for x in vals)/len(vals)*100, 2),
        "loss_rate_pct": round(sum(x < 1 for x in vals)/len(vals)*100, 2),
    }


def evaluate(joined: list[dict[str, Any]]) -> dict[str, Any]:
    safe = [
        r for r in joined
        if r["outcome_status"] == "OBSERVED_CALIBRATION_SAFE"
        and r["forward_multiple_3y"] is not None
    ]
    safe.sort(
        key=lambda r: (
            -float(r["component_fraction"]),
            -float(r["promoter_holding_change_pp_4q"]),
            r["symbol"],
        )
    )
    top_n = max(1, math.ceil(len(safe) * 0.25)) if safe else 0
    top = safe[:top_n]
    cohort = outcome_summary(safe)
    topq = outcome_summary(top)
    raw_corr = spearman(
        [float(r["promoter_holding_change_pp_4q"]) for r in safe],
        [float(r["forward_multiple_3y"]) for r in safe],
    )
    return {
        "ranked_calibration_safe_n": len(safe),
        "cohort": cohort,
        "top_quartile": topq,
        "top_quartile_mean_lift_x": None if not cohort.get("mean_multiple") else round(topq.get("mean_multiple", 0) / cohort["mean_multiple"], 4),
        "spearman_raw_promoter_change_vs_forward_multiple": raw_corr,
        "top_quartile_minus_cohort": {
            "two_x_pp": round(topq.get("two_x_rate_pct", 0) - cohort.get("two_x_rate_pct", 0), 2),
            "five_x_pp": round(topq.get("five_x_rate_pct", 0) - cohort.get("five_x_rate_pct", 0), 2),
            "loss_pp": round(topq.get("loss_rate_pct", 0) - cohort.get("loss_rate_pct", 0), 2),
            "mean_multiple": round(topq.get("mean_multiple", 0) - cohort.get("mean_multiple", 0), 4),
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Run frozen 2023 RK-MIS promoter accumulation holdout")
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol.get("status") != "PREREGISTERED_BEFORE_2023_OUTCOME_LOAD":
        raise ReplayError("protocol is not in preregistered state")
    config = json.loads((ROOT / "config/rk_mie_scoring.json").read_text(encoding="utf-8"))
    anchor = protocol["anchor_date"]
    outcome = protocol["outcome_target_date"]
    sample_size = int(protocol["sample_size"])

    args.output.mkdir(parents=True, exist_ok=True)
    session = TECH.OfficialSession(request_budget=850, timeout=30, sleep_seconds=0.025)
    resolved_anchor, anchor_raw, anchor_meta = TECH.resolve_on_or_before(anchor, session)
    universe = TECH.company_universe(TECH.parse_bhavcopy(anchor_raw), resolved_anchor)
    sample = TECH.deterministic_market_sample(
        universe,
        sample_size=sample_size,
        salt=protocol["sample_salt"],
        strata_fields=("market_type",),
    )
    if len(sample) != sample_size:
        raise ReplayError(f"sample underflow: expected {sample_size}, got {len(sample)}")

    snapshot, predictor_manifest = build_predictor_snapshot(sample, session, protocol, config)
    joined, outcome_manifest = acquire_outcomes(snapshot, anchor, outcome, session)
    metrics = evaluate(joined)

    scored = predictor_manifest["shareholding_replay_grade_rows"]
    safe_n = metrics["ranked_calibration_safe_n"]
    score_coverage = round(scored / sample_size * 100, 2) if sample_size else 0.0
    outcome_coverage = round(safe_n / scored * 100, 2) if scored else 0.0
    minimum_score_coverage = float(protocol["evaluation"]["coverage_minimum_for_interpretation_pct"])
    minimum_outcome_coverage = float(protocol["evaluation"]["calibration_safe_outcome_minimum_pct_of_scored"])
    interpretable = score_coverage >= minimum_score_coverage and outcome_coverage >= minimum_outcome_coverage

    report = {
        "version": "rk-mis-promoter-accumulation-holdout-result-v1",
        "protocol": str(args.protocol),
        "protocol_sha256": sha256(args.protocol.read_bytes()),
        "anchor_target": anchor,
        "anchor_resolved_trading_date": resolved_anchor,
        "outcome_target": outcome,
        "eligible_company_universe_rows": len(universe),
        "sample_rows": sample_size,
        "shareholding_replay_grade_rows": scored,
        "smart_money_component_coverage_pct": score_coverage,
        "calibration_safe_outcome_coverage_of_scored_pct": outcome_coverage,
        "minimum_coverage_tests_pass": interpretable,
        "metrics": metrics,
        "smart_money_scope": "PROMOTER_HOLDING_CHANGE_ONLY",
        "full_smart_money_pillar_claim": False,
        "official_100_point_score_mutated": False,
        "alpha_claim": False,
        "no_post_result_tuning": True,
    }
    manifest = {
        "anchor_source": anchor_meta,
        "predictor": predictor_manifest,
        "outcomes": outcome_manifest,
        "official_requests_made": session.requests_made,
        "raw_exchange_files_published": False,
        "public_artifact_contains_only_derived_aggregate_results_and_source_metadata": True,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
