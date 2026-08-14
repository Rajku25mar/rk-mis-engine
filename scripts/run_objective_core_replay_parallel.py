from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import run_objective_core_replay as core
import run_technical_replay as market

WORKERS = 4
WORKER_REQUEST_BUDGET = 600
WORKER_TIMEOUT_SECONDS = 25
OUTCOME_REQUEST_RESERVE = 120
_TLS = threading.local()


def _worker_session() -> market.OfficialSession:
    session = getattr(_TLS, "session", None)
    if session is None:
        session = market.OfficialSession(
            request_budget=WORKER_REQUEST_BUDGET,
            timeout=WORKER_TIMEOUT_SECONDS,
            sleep_seconds=0.04,
        )
        _TLS.session = session
    return session


def _acquire_member(member: dict[str, Any], config: dict[str, Any], protocol: dict[str, Any]) -> dict[str, Any]:
    session = _worker_session()
    before = session.requests_made
    symbol = member["symbol"]
    start = protocol["financial_filing_window_start"]
    anchor = protocol["anchor_date"]
    financial, fmeta = core._financial_features(session, symbol, start, anchor)
    shareholding, smeta = core._shareholding_features(session, symbol, start, anchor)
    raw = {"isin": member["isin"], "symbol": symbol, **financial, **shareholding}
    scored = core._score_snapshot_row(raw, config, protocol)
    return {
        "scored": scored,
        "financial_meta": fmeta,
        "share_meta": smeta,
        "worker_requests": session.requests_made - before,
    }


def build_predictor_snapshot_parallel(
    sample: list[dict[str, Any]],
    session: market.OfficialSession,
    config: dict[str, Any],
    protocol: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # executor.map preserves sample order, so the score snapshot and metadata hash
    # chains remain deterministic even though independent company acquisition is parallel.
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="rk-mis-objective") as pool:
        results = list(pool.map(lambda member: _acquire_member(member, config, protocol), sample))

    rows: list[dict[str, Any]] = []
    financial_meta_chain = []
    share_meta_chain = []
    xbrl_hashes = []
    guarded = 0
    worker_requests = 0

    for member, result in zip(sample, results):
        symbol = member["symbol"]
        scored = result["scored"]
        fmeta = result["financial_meta"]
        smeta = result["share_meta"]
        worker_requests += int(result["worker_requests"])
        rows.append(scored)
        if scored.get("guard_industrial_quality"):
            guarded += 1
        financial_meta_chain.append((symbol, fmeta.get("metadata", {}).get("sha256"), fmeta.get("metadata", {}).get("rows")))
        for key in ("latest_xbrl", "prior_xbrl"):
            meta = fmeta.get(key) or {}
            if meta.get("sha256"):
                xbrl_hashes.append((symbol, meta.get("period_end"), meta["sha256"]))
        attempt_hashes = [x.get("sha256") for x in smeta.get("attempts", []) if x.get("sha256")]
        share_meta_chain.append((symbol, attempt_hashes, smeta.get("safe_periods")))

    # Preserve the original aggregate request-budget accounting even though the
    # predictor calls were made by worker sessions. Leave deterministic headroom
    # for entry/exit bhavcopies and corporate-action acquisition.
    if session.requests_made + worker_requests > session.request_budget - OUTCOME_REQUEST_RESERVE:
        raise core.ObjectiveReplayError(
            f"parallel predictor acquisition used {worker_requests} worker requests; "
            f"aggregate budget would leave less than {OUTCOME_REQUEST_RESERVE} requests for outcomes"
        )
    session.requests_made += worker_requests

    freeze_payload = []
    for row in sorted(rows, key=lambda x: (x["isin"], x["symbol"])):
        freeze_payload.append({
            "isin": row["isin"],
            "symbol": row["symbol"],
            "latest_sales_growth_yoy_pct": row.get("latest_sales_growth_yoy_pct"),
            "latest_pat_growth_yoy_pct": row.get("latest_pat_growth_yoy_pct"),
            "debt_equity": row.get("debt_equity"),
            "interest_coverage": row.get("interest_coverage"),
            "ebitda_margin_pct": row.get("ebitda_margin_pct"),
            "promoter_holding_change_pp_4q": row.get("promoter_holding_change_pp_4q"),
            "selected_slice_coverage_pct": row.get("selected_slice_coverage_pct"),
            "objective_core_replay_grade": row.get("objective_core_replay_grade"),
            "base_objective_core_score": row.get("base_objective_core_score"),
            "base_plus_promoter_score": row.get("base_plus_promoter_score"),
        })
    freeze_bytes = json.dumps(freeze_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return rows, {
        "sample_rows": len(sample),
        "objective_core_replay_grade_rows": sum(bool(r.get("objective_core_replay_grade")) for r in rows),
        "promoter_extension_rows": sum(r.get("base_plus_promoter_score") is not None for r in rows),
        "financial_businesses_guarded": guarded,
        "symbols_with_both_growth_metrics": sum(r.get("latest_sales_growth_yoy_pct") is not None and r.get("latest_pat_growth_yoy_pct") is not None for r in rows),
        "symbols_with_promoter_4q_change": sum(r.get("promoter_holding_change_pp_4q") is not None for r in rows),
        "financial_metadata_hash_chain_sha256": market.sha256(json.dumps(financial_meta_chain, sort_keys=True).encode("utf-8")),
        "xbrl_hash_chain_sha256": market.sha256(json.dumps(sorted(xbrl_hashes), sort_keys=True).encode("utf-8")),
        "shareholding_metadata_hash_chain_sha256": market.sha256(json.dumps(share_meta_chain, sort_keys=True).encode("utf-8")),
        "predictor_snapshot_sha256": market.sha256(freeze_bytes),
        "outcomes_seen_when_snapshot_frozen": False,
        "acquisition_mode": "BOUNDED_PARALLEL_COMPANY_ACQUISITION",
        "parallel_workers": WORKERS,
        "worker_official_requests_made": worker_requests,
    }


def main() -> None:
    core.build_predictor_snapshot = build_predictor_snapshot_parallel
    core.main()


if __name__ == "__main__":
    main()
