from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_technical_replay as market
from multibagger_pipeline.corporate_action_normalizer import cumulative_backward_price_factor
from multibagger_pipeline.empirical_sampling import deterministic_market_sample

FINANCIAL_RESULTS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
SHAREHOLDING_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"
FINANCIAL_NAME_RE = re.compile(r"\b(finance|financial|finserv|credit|leasing|securities|housing finance)\b", re.I)

REVENUE_TAGS = (
    "RevenueFromOperations",
    "RevenueFromSaleOfProductsAndServices",
    "RevenueFromSaleOfProducts",
)
PAT_TAGS = (
    "ProfitLossForPeriod",
    "ProfitLossForPeriodFromContinuingOperations",
)
FINANCE_COST_TAGS = ("FinanceCosts", "FinanceCost")
PBT_TAGS = ("ProfitBeforeTax",)
DEPR_TAGS = (
    "DepreciationDepletionAndAmortisationExpense",
    "DepreciationAndAmortisationExpense",
)
DEBT_EQUITY_TAGS = ("DebtEquityRatio",)


class ObjectiveReplayError(RuntimeError):
    pass


def _payload_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "rows"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    return []


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _period(row: dict[str, Any]) -> str | None:
    return market.iso_date(_first(row, "toDate", "periodEnded", "period_end", "re_to_dt", "date"))


def _known_at(row: dict[str, Any]) -> str | None:
    return market.iso_date(_first(row, "broadcastDateTime", "exchdisstime", "broadcastDate", "filingDate", "date"))


def _revision(row: dict[str, Any]) -> str | None:
    return market.iso_date(_first(row, "revisionDate", "revisedDate", "revision_date", "revised_date"))


def _shareholding_known_at(row: dict[str, Any]) -> tuple[str | None, str]:
    revision = _revision(row)
    broadcast = market.iso_date(_first(row, "broadcastDate", "broadcastDateTime", "broadCastDate", "broadCastDateTime", "broadcast_date_time", "systemDate"))
    submission = market.iso_date(_first(row, "submissionDate", "submission_date", "filingDate", "dateOfSubmission"))
    if revision:
        candidates = [x for x in (revision, broadcast) if x]
        return (max(candidates) if candidates else revision), "REVISION_OR_BROADCAST"
    if broadcast:
        return broadcast, "BROADCAST"
    return submission, "SUBMISSION_FALLBACK_NO_BROADCAST"


def _scope(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "non-consolidated" in text or "standalone" in text:
        return "STANDALONE"
    if "consolidated" in text:
        return "CONSOLIDATED"
    return "UNKNOWN"


def _xbrl_url(row: dict[str, Any]) -> str | None:
    direct = str(row.get("xbrl") or "").strip()
    if direct.startswith("http") and not direct.endswith("/-"):
        return direct
    for key, value in row.items():
        if not isinstance(value, str) or not value.startswith("http"):
            continue
        if "xbrl" in key.lower() or value.lower().endswith((".xml", ".xbrl")):
            return value
    return None


def _financial_metadata(session: market.OfficialSession, symbol: str, start: str, anchor: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = urllib.parse.urlencode({
        "index": "equities",
        "period": "Quarterly",
        "symbol": symbol,
        "from_date": date.fromisoformat(start).strftime("%d-%m-%Y"),
        "to_date": date.fromisoformat(anchor).strftime("%d-%m-%Y"),
    })
    url = "https://www.nseindia.com/api/corporates-financial-results?" + params
    try:
        payload = session.json(url, referer=FINANCIAL_RESULTS_PAGE)
        rows = _payload_rows(payload)
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return rows, {"symbol": symbol, "source_url": url, "sha256": market.sha256(raw), "rows": len(rows), "status": "OK"}
    except Exception as exc:
        return [], {"symbol": symbol, "source_url": url, "rows": 0, "status": "ERROR", "error": type(exc).__name__}


def _select_latest_and_prior(rows: list[dict[str, Any]], anchor: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    safe = []
    for row in rows:
        period = _period(row)
        known = _known_at(row)
        url = _xbrl_url(row)
        if not period or not known or not url:
            continue
        if period > anchor or known > anchor:
            continue
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not (host == "nseindia.com" or host.endswith(".nseindia.com")):
            continue
        safe.append({**row, "_period": period, "_known": known, "_scope": _scope(row.get("consolidated")), "_xbrl": url})

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in safe:
        groups[row["_scope"]].append(row)
    if not groups:
        return None, None, {"safe_metadata_rows": 0, "scope": None, "reason": "NO_SAFE_XBRL_METADATA"}

    scope_choice = sorted(groups, key=lambda s: (len(groups[s]), s == "CONSOLIDATED"), reverse=True)[0]
    best_by_period: dict[str, dict[str, Any]] = {}
    for row in groups[scope_choice]:
        prior = best_by_period.get(row["_period"])
        if prior is None or row["_known"] > prior["_known"]:
            best_by_period[row["_period"]] = row
    ordered = sorted(best_by_period.values(), key=lambda r: r["_period"])
    if not ordered:
        return None, None, {"safe_metadata_rows": 0, "scope": scope_choice, "reason": "NO_UNIQUE_PERIODS"}

    latest = ordered[-1]
    latest_day = date.fromisoformat(latest["_period"])
    candidates = []
    for row in ordered[:-1]:
        delta = (latest_day - date.fromisoformat(row["_period"])).days
        if 325 <= delta <= 405:
            candidates.append((abs(delta - 365), row))
    prior = min(candidates, key=lambda x: (x[0], x[1]["_period"]))[1] if candidates else None
    company_name = str(latest.get("companyName") or "").strip() or None
    bank_flag = str(latest.get("bank") or "").strip().upper()
    guarded = bank_flag == "Y" or bool(company_name and FINANCIAL_NAME_RE.search(company_name))
    return latest, prior, {
        "safe_metadata_rows": len(ordered),
        "scope": scope_choice,
        "latest_period": latest["_period"],
        "prior_year_period": None if prior is None else prior["_period"],
        "company_name": company_name,
        "bank_flag": bank_flag,
        "guard_industrial_quality": guarded,
        "guard_reason": "NSE_PRE_ANCHOR_BANK_FLAG" if bank_flag == "Y" else ("PRE_ANCHOR_COMPANY_NAME_FINANCIAL_SIGNAL" if guarded else "NO_FINANCIAL_BUSINESS_SIGNAL"),
    }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _date_text(value: str | None) -> str | None:
    return market.iso_date(value)


def _facts_by_context(content: bytes) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[dict[str, Any]]]]:
    root = ET.fromstring(content)
    text_facts: dict[str, dict[str, list[str]]] = {}
    numeric: dict[str, list[dict[str, Any]]] = {}
    for elem in root.iter():
        ctx = elem.attrib.get("contextRef")
        if not ctx:
            continue
        text = (elem.text or "").strip()
        if not text:
            continue
        tag = _local(elem.tag)
        text_facts.setdefault(ctx, {}).setdefault(tag, []).append(text)
        try:
            value = float(text.replace(",", ""))
        except ValueError:
            continue
        numeric.setdefault(ctx, []).append({"tag": tag, "value": value, "unit_ref": elem.attrib.get("unitRef")})
    return text_facts, numeric


def _one(values: list[str] | None) -> str | None:
    vals = [] if not values else list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))
    return vals[0] if len(vals) == 1 else None


def _xbrl_scope(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if text == "standalone" or "non-consolidated" in text:
        return "STANDALONE"
    if "consolidated" in text:
        return "CONSOLIDATED"
    return None


def _quarter_context(text_facts: dict[str, dict[str, list[str]]], period_end: str, expected_scope: str) -> str | None:
    candidates: list[tuple[str, str | None]] = []
    end_day = date.fromisoformat(period_end)
    for ctx, tags in text_facts.items():
        start = _date_text(_one(tags.get("DateOfStartOfReportingPeriod")))
        end = _date_text(_one(tags.get("DateOfEndOfReportingPeriod")))
        if not start or not end or end != period_end:
            continue
        days = (end_day - date.fromisoformat(start)).days + 1
        if not 75 <= days <= 105:
            continue
        scope = _xbrl_scope(_one(tags.get("NatureOfReportStandaloneConsolidated")))
        if expected_scope != "UNKNOWN" and scope and scope != expected_scope:
            continue
        candidates.append((ctx, scope))
    if len(candidates) == 1:
        return candidates[0][0]
    scoped = [x for x in candidates if expected_scope != "UNKNOWN" and x[1] == expected_scope]
    return scoped[0][0] if len(scoped) == 1 else None


def _money(facts: list[dict[str, Any]], tags: Iterable[str]) -> float | None:
    for tag in tags:
        matches = [x for x in facts if x.get("tag") == tag]
        distinct = {(float(x["value"]), str(x.get("unit_ref") or "")) for x in matches}
        if len(distinct) != 1:
            continue
        value, unit = next(iter(distinct)); u = unit.upper()
        if u == "INR":
            return value / 10_000_000
        if u == "INR_MILLIONS":
            return value / 10
        if u == "INR-LAKHS":
            return value / 100
        if u == "INR_CRORES":
            return value
    return None


def _ratio(facts: list[dict[str, Any]], tags: Iterable[str]) -> float | None:
    for tag in tags:
        matches = [x for x in facts if x.get("tag") == tag]
        vals = {float(x["value"]) for x in matches if str(x.get("unit_ref") or "").upper() in {"PURE", ""}}
        if len(vals) == 1:
            return next(iter(vals))
    return None


def _parse_quarter(content: bytes, row: dict[str, Any]) -> dict[str, Any]:
    period = row["_period"]
    expected_scope = row["_scope"]
    text, numeric = _facts_by_context(content)
    ctx = _quarter_context(text, period, expected_scope)
    if not ctx:
        return {"period_end": period, "replay_grade": False, "reason": "NO_UNAMBIGUOUS_CURRENT_QUARTER_CONTEXT"}
    facts = numeric.get(ctx, [])
    revenue = _money(facts, REVENUE_TAGS)
    pat = _money(facts, PAT_TAGS)
    finance = _money(facts, FINANCE_COST_TAGS)
    pbt = _money(facts, PBT_TAGS)
    depreciation = _money(facts, DEPR_TAGS)
    debt_equity = _ratio(facts, DEBT_EQUITY_TAGS)
    return {
        "period_end": period,
        "revenue_cr": revenue,
        "pat_cr": pat,
        "finance_cost_cr": finance,
        "pbt_cr": pbt,
        "depreciation_cr": depreciation,
        "debt_equity": debt_equity,
        "replay_grade": revenue is not None and pat is not None,
        "source_sha256": market.sha256(content),
    }


def _download_quarter(session: market.OfficialSession, row: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if row is None:
        return None, None
    url = row["_xbrl"]
    try:
        content = session.request(url, referer=FINANCIAL_RESULTS_PAGE, accept="application/xml,text/xml,*/*")
        parsed = _parse_quarter(content, row)
        return parsed, {"period_end": row["_period"], "source_url": url, "sha256": market.sha256(content), "byte_size": len(content), "status": "OK"}
    except Exception as exc:
        return None, {"period_end": row["_period"], "source_url": url, "status": "ERROR", "error": type(exc).__name__}


def _safe_growth(cur: float | None, prior: float | None) -> float | None:
    if cur is None or prior is None or prior <= 0:
        return None
    return round((cur / prior - 1) * 100, 4)


def _financial_features(session: market.OfficialSession, symbol: str, start: str, anchor: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows, meta = _financial_metadata(session, symbol, start, anchor)
    latest_row, prior_row, selection = _select_latest_and_prior(rows, anchor)
    latest, latest_meta = _download_quarter(session, latest_row)
    time.sleep(0.02)
    prior, prior_meta = _download_quarter(session, prior_row)
    latest = latest or {}
    prior = prior or {}
    sales_growth = _safe_growth(market.num(latest.get("revenue_cr")), market.num(prior.get("revenue_cr")))
    pat_growth = _safe_growth(market.num(latest.get("pat_cr")), market.num(prior.get("pat_cr")))
    revenue = market.num(latest.get("revenue_cr")); pbt = market.num(latest.get("pbt_cr")); finance = market.num(latest.get("finance_cost_cr")); depreciation = market.num(latest.get("depreciation_cr"))
    interest = None if finance in (None, 0) or pbt is None else round((pbt + finance) / finance, 4)
    ebitda_margin = None if revenue in (None, 0) or pbt is None or finance is None or depreciation is None else round((pbt + finance + depreciation) / revenue * 100, 4)
    debt_equity = market.num(latest.get("debt_equity"))
    if selection.get("guard_industrial_quality"):
        debt_equity = None; interest = None; ebitda_margin = None
    return {
        "symbol": symbol,
        "latest_period": selection.get("latest_period"),
        "prior_year_period": selection.get("prior_year_period"),
        "latest_sales_growth_yoy_pct": sales_growth,
        "latest_pat_growth_yoy_pct": pat_growth,
        "debt_equity": debt_equity,
        "interest_coverage": interest,
        "ebitda_margin_pct": ebitda_margin,
        "guard_industrial_quality": bool(selection.get("guard_industrial_quality")),
        "guard_reason": selection.get("guard_reason"),
    }, {
        "metadata": meta,
        "selection": selection,
        "latest_xbrl": latest_meta,
        "prior_xbrl": prior_meta,
    }


def _shareholding_period(row: dict[str, Any]) -> str | None:
    return market.iso_date(_first(row, "date", "asOnDate", "as_on_date", "shareholdingDate", "toDate"))


def _promoter_pct(row: dict[str, Any]) -> float | None:
    return market.num(_first(row, "pr_and_prgrp", "promoterAndPromoterGroup", "promoter_pct", "promoterHolding", "promoter"))


def _shareholding_features(session: market.OfficialSession, symbol: str, start: str, anchor: str) -> tuple[dict[str, Any], dict[str, Any]]:
    queries = [
        {"index": "equities", "from_date": date.fromisoformat(start).strftime("%d-%m-%Y"), "to_date": date.fromisoformat(anchor).strftime("%d-%m-%Y"), "symbol": symbol},
        {"index": "equities", "symbol": symbol},
    ]
    rows: list[dict[str, Any]] = []
    attempts = []
    for idx, query in enumerate(queries):
        url = "https://www.nseindia.com/api/corporate-share-holdings-master?" + urllib.parse.urlencode(query)
        try:
            payload = session.json(url, referer=SHAREHOLDING_PAGE)
            current = _payload_rows(payload)
            raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            attempts.append({"source_url": url, "sha256": market.sha256(raw), "rows": len(current), "status": "OK"})
            rows.extend(current)
            if current:
                break
        except Exception as exc:
            attempts.append({"source_url": url, "rows": 0, "status": "ERROR", "error": type(exc).__name__})
        if idx == 0:
            time.sleep(0.02)

    safe_by_period: dict[str, dict[str, Any]] = {}
    unsafe_post_anchor_revisions = 0
    for row in rows:
        period = _shareholding_period(row)
        known, basis = _shareholding_known_at(row)
        promoter = _promoter_pct(row)
        revision = _revision(row)
        if period and period <= anchor and revision and revision > anchor:
            unsafe_post_anchor_revisions += 1
        if not period or not known or period > anchor or known > anchor or promoter is None:
            continue
        normalized = {"period_end": period, "known_at": known, "known_at_basis": basis, "promoter_pct": promoter}
        prior = safe_by_period.get(period)
        if prior is None or known > prior["known_at"]:
            safe_by_period[period] = normalized
    ordered = sorted(safe_by_period.values(), key=lambda x: x["period_end"])
    latest5 = ordered[-5:]
    change = None; span = None
    if len(latest5) == 5:
        span = (date.fromisoformat(latest5[-1]["period_end"]) - date.fromisoformat(latest5[0]["period_end"])).days
        if 330 <= span <= 400:
            change = round(float(latest5[-1]["promoter_pct"]) - float(latest5[0]["promoter_pct"]), 4)
    return {
        "promoter_holding_change_pp_4q": change,
        "shareholding_safe_periods": len(ordered),
        "shareholding_span_days": span,
    }, {
        "attempts": attempts,
        "safe_periods": len(ordered),
        "unsafe_post_anchor_revision_rows_rejected": unsafe_post_anchor_revisions,
    }


def _feature_spec(config: dict[str, Any], pillar: str, field: str) -> dict[str, Any]:
    for spec in config["pillar_features"][pillar]:
        if spec["field"] == field:
            return spec
    raise ObjectiveReplayError(f"frozen config field missing: {pillar}.{field}")


def _score_one(value: float | None, spec: dict[str, Any]) -> float | None:
    return market.band_fraction(value, spec)


def _score_snapshot_row(row: dict[str, Any], config: dict[str, Any], protocol: dict[str, Any]) -> dict[str, Any]:
    selected = protocol["predictor_features"]
    earned_base = covered_base = earned_expanded = covered_expanded = 0.0
    details = []
    quality_nonmissing = 0
    for item in selected:
        pillar, field = item["pillar"], item["field"]
        weight = float(item["official_100pt_slice_weight"])
        value = market.num(row.get(field))
        spec = _feature_spec(config, pillar, field)
        fraction = _score_one(value, spec)
        if field in {"debt_equity", "interest_coverage", "ebitda_margin_pct"} and value is not None:
            quality_nonmissing += 1
        if fraction is not None:
            covered_expanded += weight
            earned_expanded += weight * fraction
            if field != "promoter_holding_change_pp_4q":
                covered_base += weight
                earned_base += weight * fraction
        details.append({"pillar": pillar, "field": field, "value": value, "fraction": fraction, "slice_weight": weight})

    base_score = None if covered_base <= 0 else round(earned_base / covered_base * 100, 4)
    expanded_score = None if covered_expanded <= 0 else round(earned_expanded / covered_expanded * 100, 4)
    total_selected = float(protocol["total_selected_official_100pt_slice_weight"])
    coverage_pct = round(covered_expanded / total_selected * 100, 2) if total_selected else 0.0
    growth_ok = row.get("latest_sales_growth_yoy_pct") is not None and row.get("latest_pat_growth_yoy_pct") is not None
    quality_ok = quality_nonmissing >= 2
    grade = bool(growth_ok and quality_ok and coverage_pct >= float(protocol["scoring"]["minimum_selected_slice_coverage_pct"]))
    return {
        **row,
        "base_objective_core_score": base_score if grade else None,
        "base_plus_promoter_score": expanded_score if grade and row.get("promoter_holding_change_pp_4q") is not None else None,
        "selected_slice_coverage_pct": coverage_pct,
        "objective_core_replay_grade": grade,
        "feature_details": details,
    }


def build_predictor_snapshot(sample: list[dict[str, Any]], session: market.OfficialSession, config: dict[str, Any], protocol: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    anchor = protocol["anchor_date"]
    start = protocol["financial_filing_window_start"]
    rows = []
    financial_meta_chain = []
    share_meta_chain = []
    xbrl_hashes = []
    guarded = 0
    for i, member in enumerate(sample):
        symbol = member["symbol"]
        financial, fmeta = _financial_features(session, symbol, start, anchor)
        shareholding, smeta = _shareholding_features(session, symbol, start, anchor)
        raw = {"isin": member["isin"], "symbol": symbol, **financial, **shareholding}
        scored = _score_snapshot_row(raw, config, protocol)
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
        if i + 1 < len(sample):
            time.sleep(0.045)

    freeze_payload = []
    for r in sorted(rows, key=lambda x: (x["isin"], x["symbol"])):
        freeze_payload.append({
            "isin": r["isin"],
            "symbol": r["symbol"],
            "latest_sales_growth_yoy_pct": r.get("latest_sales_growth_yoy_pct"),
            "latest_pat_growth_yoy_pct": r.get("latest_pat_growth_yoy_pct"),
            "debt_equity": r.get("debt_equity"),
            "interest_coverage": r.get("interest_coverage"),
            "ebitda_margin_pct": r.get("ebitda_margin_pct"),
            "promoter_holding_change_pp_4q": r.get("promoter_holding_change_pp_4q"),
            "selected_slice_coverage_pct": r.get("selected_slice_coverage_pct"),
            "objective_core_replay_grade": r.get("objective_core_replay_grade"),
            "base_objective_core_score": r.get("base_objective_core_score"),
            "base_plus_promoter_score": r.get("base_plus_promoter_score"),
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
    }


def acquire_outcomes(snapshot: list[dict[str, Any]], anchor_target: str, outcome_target: str, session: market.OfficialSession) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entry_date, entry_raw, entry_meta = market.resolve_after(anchor_target, session)
    exit_date, exit_raw, exit_meta = market.resolve_on_or_before(outcome_target, session)
    entry_rows = market.parse_bhavcopy(entry_raw)
    exit_rows = market.parse_bhavcopy(exit_raw)
    entry_by_isin = {str(r.get("isin") or "").upper(): r for r in entry_rows if r.get("series") == "EQ" and r.get("isin")}
    exit_by_isin = {str(r.get("isin") or "").upper(): r for r in exit_rows if r.get("series") == "EQ" and r.get("isin")}
    action_rows, action_meta = market.fetch_actions(session, entry_date, exit_date)
    actions_by_symbol = market.normalize_actions(action_rows)

    joined = []
    statuses: dict[str, int] = defaultdict(int)
    for pred in snapshot:
        if not pred.get("objective_core_replay_grade"):
            continue
        isin = pred["isin"]
        erow = entry_by_isin.get(isin); xrow = exit_by_isin.get(isin)
        status = "OBSERVED_CALIBRATION_SAFE"; multiple = None
        if not erow or not erow.get("close"):
            status = "MISSING_ENTRY_PRICE"
        elif not xrow or not xrow.get("close"):
            status = "MISSING_EXIT_PRICE_OR_DELISTED"
        else:
            aliases = {pred["symbol"], erow["symbol"], xrow["symbol"]}
            actions = market.dedupe_actions(a for alias in aliases for a in actions_by_symbol.get(alias, []))
            factor = cumulative_backward_price_factor(actions, price_date=entry_date, target_date=exit_date)
            if not factor["calibration_safe"]:
                status = "UNRESOLVED_CORPORATE_ACTION"
            else:
                adjusted_entry = float(erow["close"]) * float(factor["price_factor"])
                if adjusted_entry > 0:
                    multiple = round(float(xrow["close"]) / adjusted_entry, 6)
                else:
                    status = "INVALID_ADJUSTED_ENTRY"
        statuses[status] += 1
        joined.append({
            "isin": isin,
            "symbol": pred["symbol"],
            "base_objective_core_score": pred.get("base_objective_core_score"),
            "base_plus_promoter_score": pred.get("base_plus_promoter_score"),
            "promoter_holding_change_pp_4q": pred.get("promoter_holding_change_pp_4q"),
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


def _rank(values: list[float]) -> list[float]:
    pairs = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values); i = 0
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
    numerator = sum((x-ma)*(y-mb) for x, y in zip(ra, rb))
    da = sum((x-ma)**2 for x in ra); db = sum((y-mb)**2 for y in rb)
    return None if da <= 0 or db <= 0 else round(numerator / math.sqrt(da*db), 4)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(r["forward_multiple_3y"]) for r in rows]
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean_multiple": round(sum(values)/len(values), 4),
        "two_x_rate_pct": round(sum(x >= 2 for x in values)/len(values)*100, 2),
        "five_x_rate_pct": round(sum(x >= 5 for x in values)/len(values)*100, 2),
        "loss_rate_pct": round(sum(x < 1 for x in values)/len(values)*100, 2),
    }


def _compare(rows: list[dict[str, Any]], score_field: str) -> dict[str, Any]:
    ranked = [r for r in rows if r.get(score_field) is not None]
    ranked.sort(key=lambda r: (float(r[score_field]), r["symbol"]), reverse=True)
    top_n = max(1, math.ceil(len(ranked)*0.25)) if ranked else 0
    top = ranked[:top_n]
    cohort = _summary(ranked); topq = _summary(top)
    return {
        "ranked_n": len(ranked),
        "cohort": cohort,
        "top_quartile": topq,
        "top_quartile_mean_lift_x": None if not cohort.get("mean_multiple") else round(topq.get("mean_multiple", 0)/cohort["mean_multiple"], 4),
        "spearman": _spearman([float(r[score_field]) for r in ranked], [float(r["forward_multiple_3y"]) for r in ranked]),
    }


def evaluate(joined: list[dict[str, Any]]) -> dict[str, Any]:
    safe = [r for r in joined if r.get("outcome_status") == "OBSERVED_CALIBRATION_SAFE" and r.get("forward_multiple_3y") is not None]
    base = _compare(safe, "base_objective_core_score")
    promoter_subset = [r for r in safe if r.get("base_plus_promoter_score") is not None]
    promoter_base = _compare(promoter_subset, "base_objective_core_score")
    promoter_expanded = _compare(promoter_subset, "base_plus_promoter_score")
    def delta(metric: str) -> float | None:
        a = promoter_expanded.get("top_quartile", {}).get(metric)
        b = promoter_base.get("top_quartile", {}).get(metric)
        return None if a is None or b is None else round(float(a)-float(b), 2)
    return {
        "base_objective_core": base,
        "promoter_extension_same_cohort": {
            "same_cohort_n": len(promoter_subset),
            "base": promoter_base,
            "base_plus_promoter": promoter_expanded,
            "delta_top_quartile_two_x_pp": delta("two_x_rate_pct"),
            "delta_top_quartile_five_x_pp": delta("five_x_rate_pct"),
            "delta_top_quartile_loss_pp": delta("loss_rate_pct"),
            "delta_top_quartile_mean_multiple": None if promoter_expanded.get("top_quartile", {}).get("mean_multiple") is None or promoter_base.get("top_quartile", {}).get("mean_multiple") is None else round(float(promoter_expanded["top_quartile"]["mean_multiple"]) - float(promoter_base["top_quartile"]["mean_multiple"]), 4),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run frozen RK-MIS objective-core historical replay")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    config = json.loads((ROOT / "config/rk_mie_scoring.json").read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_BEFORE_OUTCOME_ACQUISITION":
        raise ObjectiveReplayError("protocol is not frozen")
    anchor = protocol["anchor_date"]
    outcome = protocol["outcome_target_date"]
    sample_size = int(protocol["sample_size"])
    if protocol.get("sampling", {}).get("salt") != f"RK-MIS-OBJECTIVE-CORE|{anchor}":
        raise ObjectiveReplayError("unexpected sampling salt")

    args.output.mkdir(parents=True, exist_ok=True)
    session = market.OfficialSession(request_budget=1800, timeout=30, sleep_seconds=0.04)

    resolved_anchor, anchor_raw, anchor_meta = market.resolve_on_or_before(anchor, session)
    universe = market.company_universe(market.parse_bhavcopy(anchor_raw), resolved_anchor)
    sample = deterministic_market_sample(
        universe,
        sample_size=sample_size,
        salt=protocol["sampling"]["salt"],
        strata_fields=tuple(protocol["sampling"]["strata_fields"]),
    )
    if len(sample) != sample_size:
        raise ObjectiveReplayError(f"sample underflow: expected {sample_size}, got {len(sample)}")

    snapshot, predictor_manifest = build_predictor_snapshot(sample, session, config, protocol)
    # Chronology barrier: the snapshot is fully constructed and hashed above. Only
    # after that hash exists do we request entry/outcome prices or post-anchor actions.
    if not predictor_manifest.get("predictor_snapshot_sha256"):
        raise ObjectiveReplayError("predictor snapshot was not frozen")
    joined, outcome_manifest = acquire_outcomes(snapshot, anchor, outcome, session)
    metrics = evaluate(joined)

    grade_rows = predictor_manifest["objective_core_replay_grade_rows"]
    safe_n = metrics["base_objective_core"]["ranked_n"]
    report = {
        "version": "rk-mis-objective-core-replay-v1",
        "protocol": str(args.protocol),
        "protocol_sha256": market.sha256(args.protocol.read_bytes()),
        "anchor_target": anchor,
        "anchor_resolved_trading_date": resolved_anchor,
        "outcome_target": outcome,
        "eligible_company_universe_rows": len(universe),
        "sample_rows": len(sample),
        "objective_core_replay_grade_rows": grade_rows,
        "objective_core_score_coverage_pct": round(grade_rows/len(sample)*100, 2) if sample else 0.0,
        "promoter_extension_rows": predictor_manifest["promoter_extension_rows"],
        "calibration_safe_outcome_coverage_of_scored_pct": round(safe_n/grade_rows*100, 2) if grade_rows else 0.0,
        "metrics": metrics,
        "business_model_guard_applied": True,
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
