from __future__ import annotations

import argparse
import importlib.util
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

TECH_SPEC = importlib.util.spec_from_file_location(
    "rk_mis_technical_replay_common",
    ROOT / "scripts/run_technical_replay.py",
)
TECH = importlib.util.module_from_spec(TECH_SPEC)
assert TECH_SPEC and TECH_SPEC.loader
TECH_SPEC.loader.exec_module(TECH)

from multibagger_pipeline.corporate_action_normalizer import cumulative_backward_price_factor


class DocumentaryHoldoutError(RuntimeError):
    pass


def _rank(values: list[float]) -> list[float]:
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


def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    numerator = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra)
    db = sum((y - mb) ** 2 for y in rb)
    return None if da <= 0 or db <= 0 else round(numerator / math.sqrt(da * db), 4)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(r["forward_multiple_3y"]) for r in rows]
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean_multiple": round(sum(values) / len(values), 4),
        "two_x_rate_pct": round(sum(x >= 2 for x in values) / len(values) * 100, 2),
        "five_x_rate_pct": round(sum(x >= 5 for x in values) / len(values) * 100, 2),
        "loss_rate_pct": round(sum(x < 1 for x in values) / len(values) * 100, 2),
    }


def _evaluate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(
        rows,
        key=lambda r: (float(r["qualitative_documentary_partial_score"]), str(r["sample_symbol"])),
        reverse=True,
    )
    top_n = max(1, math.ceil(len(ranked) * 0.25)) if ranked else 0
    top = ranked[:top_n]
    cohort = _summary(ranked)
    topq = _summary(top)
    return {
        "ranked_n": len(ranked),
        "cohort": cohort,
        "top_quartile": topq,
        "top_quartile_mean_lift_x": None if not cohort.get("mean_multiple") else round(topq.get("mean_multiple", 0) / cohort["mean_multiple"], 4),
        "spearman": _spearman(
            [float(r["qualitative_documentary_partial_score"]) for r in ranked],
            [float(r["forward_multiple_3y"]) for r in ranked],
        ),
        "top_quartile_minus_cohort": {
            "two_x_pp": None if not topq else round(float(topq.get("two_x_rate_pct", 0)) - float(cohort.get("two_x_rate_pct", 0)), 2),
            "five_x_pp": None if not topq else round(float(topq.get("five_x_rate_pct", 0)) - float(cohort.get("five_x_rate_pct", 0)), 2),
            "loss_pp": None if not topq else round(float(topq.get("loss_rate_pct", 0)) - float(cohort.get("loss_rate_pct", 0)), 2),
            "mean_multiple": None if not topq else round(float(topq.get("mean_multiple", 0)) - float(cohort.get("mean_multiple", 0)), 4),
        },
    }


def _decision(metrics: dict[str, Any]) -> str:
    delta = metrics["top_quartile_minus_cohort"]
    mean_positive = float(delta.get("mean_multiple") or 0) > 0
    five_x_positive = float(delta.get("five_x_pp") or 0) > 0
    loss_ok = float(delta.get("loss_pp") or 0) <= 5.0
    rank_ok = metrics.get("spearman") is not None and float(metrics["spearman"]) >= 0
    return "POSITIVE_DIAGNOSTIC_FURTHER_HOLDOUTS_JUSTIFIED" if (mean_positive or five_x_positive) and loss_ok and rank_ok else "NO_POSITIVE_DIAGNOSTIC_NO_WEIGHT_CHANGE"


def main() -> None:
    p = argparse.ArgumentParser(description="Run fail-closed RK-MIS qualitative documentary historical holdout")
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--snapshot-manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    snapshot_payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    snapshot_manifest = json.loads(args.snapshot_manifest.read_text(encoding="utf-8"))

    if protocol.get("status") != "PREREGISTERED_AFTER_PRE_OUTCOME_DATA_AVAILABILITY_DIAGNOSIS_BEFORE_REVIEW_AND_OUTCOME_LOAD":
        raise DocumentaryHoldoutError("qualitative protocol is not frozen")
    if snapshot_manifest.get("outcomes_seen_when_predictor_snapshot_frozen") is not False:
        raise DocumentaryHoldoutError("predictor snapshot chronology guard failed")
    if snapshot_manifest.get("missing_data_imputed") is not False:
        raise DocumentaryHoldoutError("snapshot indicates missing-data imputation")
    if snapshot_manifest.get("anchor_date") != protocol.get("anchor_date"):
        raise DocumentaryHoldoutError("snapshot/protocol anchor mismatch")

    eligible = [r for r in (snapshot_payload.get("rows") or []) if r.get("ranking_eligible") and r.get("qualitative_documentary_partial_score") is not None]
    minimum = int(protocol["evaluation"]["minimum_ranking_eligible_rows"])
    if len(eligible) < minimum:
        # Critical chronology guarantee: fail before creating an OfficialSession or
        # requesting entry/outcome bhavcopies. No future-price source call occurs.
        raise DocumentaryHoldoutError(
            f"PRE_OUTCOME_FEASIBILITY_STOP: ranking eligible {len(eligible)} < frozen minimum {minimum}; no future prices requested"
        )

    session = TECH.OfficialSession(request_budget=120, timeout=30, sleep_seconds=0.04)
    anchor = protocol["anchor_date"]
    outcome_target = protocol["outcome_target_date"]
    entry_date, entry_raw, entry_meta = TECH.resolve_after(anchor, session)
    outcome_date, outcome_raw, outcome_meta = TECH.resolve_on_or_before(outcome_target, session)
    entry_rows = TECH.parse_bhavcopy(entry_raw)
    outcome_rows = TECH.parse_bhavcopy(outcome_raw)
    entry_by_isin = {str(r.get("isin") or "").upper(): r for r in entry_rows if r.get("series") == "EQ" and r.get("isin")}
    outcome_by_isin = {str(r.get("isin") or "").upper(): r for r in outcome_rows if r.get("series") == "EQ" and r.get("isin")}
    action_rows, action_meta = TECH.fetch_actions(session, entry_date, outcome_date)
    actions_by_symbol = TECH.normalize_actions(action_rows)

    observed = []
    statuses: dict[str, int] = defaultdict(int)
    for pred in eligible:
        isin = str(pred["sample_isin"]).upper()
        erow = entry_by_isin.get(isin)
        xrow = outcome_by_isin.get(isin)
        status = "OBSERVED_CALIBRATION_SAFE"
        multiple = None
        if not erow or not erow.get("close"):
            status = "MISSING_ENTRY_PRICE"
        elif not xrow or not xrow.get("close"):
            status = "MISSING_EXIT_PRICE_OR_DELISTED"
        else:
            aliases = {str(pred.get("sample_symbol") or "").upper(), erow["symbol"], xrow["symbol"]}
            actions = TECH.dedupe_actions(a for alias in aliases for a in actions_by_symbol.get(alias, []))
            factor = cumulative_backward_price_factor(actions, price_date=entry_date, target_date=outcome_date)
            if not factor["calibration_safe"]:
                status = "UNRESOLVED_CORPORATE_ACTION"
            else:
                adjusted_entry = float(erow["close"]) * float(factor["price_factor"])
                if adjusted_entry > 0:
                    multiple = round(float(xrow["close"]) / adjusted_entry, 6)
                else:
                    status = "INVALID_ADJUSTED_ENTRY"
        statuses[status] += 1
        if status == "OBSERVED_CALIBRATION_SAFE" and multiple is not None:
            observed.append({
                "sample_isin": isin,
                "sample_symbol": pred.get("sample_symbol"),
                "qualitative_documentary_partial_score": pred["qualitative_documentary_partial_score"],
                "forward_multiple_3y": multiple,
            })

    scored_n = len(eligible)
    safe_pct = round(len(observed) / scored_n * 100, 2) if scored_n else 0.0
    minimum_safe = float(protocol["evaluation"]["calibration_safe_outcome_minimum_pct_of_scored"])
    metrics = _evaluate(observed) if safe_pct >= minimum_safe else {
        "ranked_n": len(observed),
        "status": "INSUFFICIENT_CALIBRATION_SAFE_OUTCOME_COVERAGE",
    }
    decision = _decision(metrics) if "cohort" in metrics else "NO_DECISION_INSUFFICIENT_OUTCOME_COVERAGE"

    report = {
        "version": "rk-mis-qualitative-documentary-holdout-result-v1",
        "protocol_sha256": TECH.sha256(args.protocol.read_bytes()),
        "predictor_snapshot_sha256": snapshot_manifest.get("predictor_snapshot_sha256"),
        "anchor_date": anchor,
        "entry_date": entry_date,
        "outcome_date": outcome_date,
        "ranking_eligible_rows": scored_n,
        "minimum_ranking_eligible_rows": minimum,
        "calibration_safe_outcome_rows": len(observed),
        "calibration_safe_outcome_coverage_pct": safe_pct,
        "outcome_status_counts": dict(sorted(statuses.items())),
        "metrics": metrics,
        "decision": decision,
        "official_100_point_score_mutated": False,
        "alpha_claim": False,
        "no_post_result_tuning": True,
    }
    manifest = {
        "entry_source": entry_meta,
        "outcome_source": outcome_meta,
        "corporate_action_sources": action_meta,
        "official_requests_made": session.requests_made,
        "raw_exchange_files_published": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
