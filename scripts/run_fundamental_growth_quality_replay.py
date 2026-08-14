from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TECH_SPEC = importlib.util.spec_from_file_location(
    "rk_mis_technical_replay_common", ROOT / "scripts/run_technical_replay.py"
)
TECH = importlib.util.module_from_spec(TECH_SPEC)
assert TECH_SPEC and TECH_SPEC.loader
TECH_SPEC.loader.exec_module(TECH)

REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
FINANCIAL_NAME_RE = re.compile(
    r"\b(finance|financial|finserv|credit|leasing|securities|housing finance)\b",
    re.IGNORECASE,
)
REVENUE_TAGS = ("RevenueFromOperations", "RevenueFromSaleOfProductsAndServices", "RevenueFromSaleOfProducts")
PAT_TAGS = ("ProfitLossForPeriod", "ProfitLossForPeriodFromContinuingOperations")
FINANCE_COST_TAGS = ("FinanceCosts", "FinanceCost")
PBT_TAGS = ("ProfitBeforeTax",)
DEPR_TAGS = ("DepreciationDepletionAndAmortisationExpense", "DepreciationAndAmortisationExpense")
DEBT_EQUITY_TAGS = ("DebtEquityRatio",)
SUPPORTED_MONEY_UNITS = {"INR", "INR_MILLIONS", "INR-LAKHS", "INR_CRORES"}


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


def payload_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "rows"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    return []


def known_at(row: dict[str, Any]) -> str | None:
    for key in ("broadcastDateTime", "exchdisstime", "filingDate", "broadcastDate"):
        parsed = robust_date(row.get(key))
        if parsed:
            return parsed
    return None


def period_end(row: dict[str, Any]) -> str | None:
    for key in ("toDate", "periodEnded", "period_end", "re_to_dt", "date"):
        parsed = robust_date(row.get(key))
        if parsed:
            return parsed
    return None


def filing_scope(row: dict[str, Any]) -> str:
    text = str(row.get("consolidated") or "").strip().lower()
    if "non-consolidated" in text or "standalone" in text:
        return "STANDALONE"
    if "consolidated" in text:
        return "CONSOLIDATED"
    return "UNKNOWN"


def xbrl_url(row: dict[str, Any]) -> str | None:
    candidates = []
    for key, value in row.items():
        if isinstance(value, str) and value.startswith("http"):
            if "xbrl" in key.lower() or value.lower().endswith((".xml", ".xbrl")):
                candidates.append(value)
    for url in candidates:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host == "nsearchives.nseindia.com" and not url.endswith("/-"):
            return url
    return None


def safe_filing(row: dict[str, Any], anchor: str) -> bool:
    p = period_end(row)
    k = known_at(row)
    return bool(p and k and p <= anchor and k <= anchor and xbrl_url(row))


def is_guarded_financial(rows: list[dict[str, Any]], anchor: str) -> tuple[bool, str, str | None]:
    safe = [r for r in rows if (known_at(r) or "9999-12-31") <= anchor]
    if not safe:
        return False, "NO_PRE_ANCHOR_FILING_METADATA", None
    safe.sort(key=lambda r: (known_at(r) or "", period_end(r) or ""))
    latest = safe[-1]
    company_name = str(latest.get("companyName") or "").strip() or None
    if str(latest.get("bank") or "").strip().upper() == "Y":
        return True, "NSE_PRE_ANCHOR_BANK_FLAG", company_name
    if company_name and FINANCIAL_NAME_RE.search(company_name):
        return True, "PRE_ANCHOR_COMPANY_NAME_FINANCIAL_SIGNAL", company_name
    return False, "NO_FINANCIAL_BUSINESS_SIGNAL", company_name


def fetch_financial_metadata(
    session: Any,
    symbol: str,
    start: str,
    anchor: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    f = date.fromisoformat(start).strftime("%d-%m-%Y")
    t = date.fromisoformat(anchor).strftime("%d-%m-%Y")
    queries = [
        {"index": "equities", "period": "Quarterly", "symbol": symbol, "from_date": f, "to_date": t},
        {"index": "equities", "period": "Quarterly", "symbol": symbol},
    ]
    attempts = []
    combined = []
    hashes = []
    for idx, query in enumerate(queries):
        url = "https://www.nseindia.com/api/corporates-financial-results?" + urllib.parse.urlencode(query)
        try:
            payload = session.json(url, referer=REFERER)
            rows = payload_rows(payload)
            canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            h = sha256(canonical)
            attempts.append({"url": url, "status": "OK", "rows": len(rows), "sha256": h})
            hashes.append(h)
            combined.extend(rows)
            if rows and idx == 0:
                break
        except Exception as exc:
            attempts.append({"url": url, "status": "ERROR", "rows": 0, "error": type(exc).__name__})
        time.sleep(0.04)
    seen = set(); deduped = []
    for row in combined:
        key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key); deduped.append(row)
    return deduped, attempts, sha256("|".join(hashes).encode("utf-8")) if hashes else sha256(b"")


def select_latest_and_prior(rows: list[dict[str, Any]], anchor: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    safe = [r for r in rows if safe_filing(r, anchor)]
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in safe:
        key = (period_end(row) or "", filing_scope(row))
        prior = best.get(key)
        if prior is None or (known_at(row) or "") > (known_at(prior) or ""):
            best[key] = row
    by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in best.values():
        by_scope[filing_scope(row)].append(row)
    if not by_scope:
        return None, None, None
    scope = sorted(
        by_scope,
        key=lambda s: (len(by_scope[s]), s == "CONSOLIDATED"),
        reverse=True,
    )[0]
    selected = sorted(by_scope[scope], key=lambda r: period_end(r) or "")
    latest = selected[-1]
    latest_date = date.fromisoformat(period_end(latest))
    candidates = []
    for row in selected[:-1]:
        diff = (latest_date - date.fromisoformat(period_end(row))).days
        if 325 <= diff <= 405:
            candidates.append((abs(diff - 365), period_end(row), row))
    prior = sorted(candidates, key=lambda x: (x[0], x[1]))[0][2] if candidates else None
    return latest, prior, scope


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _facts_by_context(content: bytes) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[dict[str, Any]]]]:
    root = ET.fromstring(content)
    text_facts: dict[str, dict[str, list[str]]] = {}
    numeric: dict[str, list[dict[str, Any]]] = {}
    for elem in root.iter():
        ctx = elem.attrib.get("contextRef")
        if not ctx:
            continue
        tag = _local(elem.tag)
        text = (elem.text or "").strip()
        if not text:
            continue
        text_facts.setdefault(ctx, {}).setdefault(tag, []).append(text)
        try:
            value = float(text.replace(",", ""))
        except ValueError:
            continue
        numeric.setdefault(ctx, []).append({
            "tag": tag,
            "value": value,
            "unit_ref": elem.attrib.get("unitRef"),
        })
    return text_facts, numeric


def _one(values: list[str] | None) -> str | None:
    vals = [] if not values else list(dict.fromkeys(v.strip() for v in values if v.strip()))
    return vals[0] if len(vals) == 1 else None


def _scope_from_xbrl(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if text == "standalone" or "non-consolidated" in text:
        return "STANDALONE"
    if "consolidated" in text:
        return "CONSOLIDATED"
    return None


def choose_quarter_context(text_facts: dict[str, dict[str, list[str]]], expected_period: str, expected_scope: str) -> str | None:
    candidates = []
    for ctx, tags in text_facts.items():
        start = robust_date(_one(tags.get("DateOfStartOfReportingPeriod")))
        end = robust_date(_one(tags.get("DateOfEndOfReportingPeriod")))
        if not start or not end or end != expected_period:
            continue
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        if not 75 <= days <= 105:
            continue
        scope = _scope_from_xbrl(_one(tags.get("NatureOfReportStandaloneConsolidated")))
        if scope and expected_scope != "UNKNOWN" and scope != expected_scope:
            continue
        candidates.append((ctx, scope))
    if len(candidates) == 1:
        return candidates[0][0]
    exact = [ctx for ctx, scope in candidates if scope == expected_scope]
    return exact[0] if len(exact) == 1 else None


def money_metric(facts: list[dict[str, Any]], tags: tuple[str, ...]) -> float | None:
    for tag in tags:
        matches = [x for x in facts if x.get("tag") == tag]
        distinct = {(float(x["value"]), str(x.get("unit_ref") or "")) for x in matches}
        if not matches:
            continue
        if len(distinct) != 1:
            return None
        value, unit = next(iter(distinct))
        u = unit.upper()
        if u not in SUPPORTED_MONEY_UNITS:
            return None
        if u == "INR":
            return value / 10_000_000.0
        if u == "INR_MILLIONS":
            return value / 10.0
        if u == "INR-LAKHS":
            return value / 100.0
        return value
    return None


def scalar_metric(facts: list[dict[str, Any]], tags: tuple[str, ...], allowed_units: set[str]) -> float | None:
    for tag in tags:
        matches = [x for x in facts if x.get("tag") == tag and str(x.get("unit_ref") or "").upper() in allowed_units]
        values = {float(x["value"]) for x in matches}
        if len(values) == 1:
            return next(iter(values))
        if len(values) > 1:
            return None
    return None


def parse_quarter_xbrl(content: bytes, filing: dict[str, Any], expected_scope: str) -> dict[str, Any]:
    p = period_end(filing)
    if not p:
        return {"replay_grade": False}
    text, numeric = _facts_by_context(content)
    ctx = choose_quarter_context(text, p, expected_scope)
    if not ctx:
        return {"replay_grade": False, "period_end": p}
    facts = numeric.get(ctx, [])
    revenue = money_metric(facts, REVENUE_TAGS)
    pat = money_metric(facts, PAT_TAGS)
    finance = money_metric(facts, FINANCE_COST_TAGS)
    pbt = money_metric(facts, PBT_TAGS)
    depreciation = money_metric(facts, DEPR_TAGS)
    de = scalar_metric(facts, DEBT_EQUITY_TAGS, {"PURE"})
    return {
        "replay_grade": revenue is not None and pat is not None,
        "period_end": p,
        "revenue_cr": revenue,
        "pat_cr": pat,
        "finance_cost_cr": finance,
        "pbt_cr": pbt,
        "depreciation_cr": depreciation,
        "debt_equity": de,
        "context_ref": ctx,
        "source_sha256": sha256(content),
    }


def pct_growth(current: float | None, prior: float | None) -> float | None:
    if current is None or prior in (None, 0):
        return None
    return round((current / prior - 1.0) * 100.0, 4)


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


def validate_protocol_against_config(protocol: dict[str, Any], config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for f in protocol["features"]:
        config_spec = next(x for x in config["pillar_features"][f["pillar"]] if x["field"] == f["field"])
        if config_spec["direction"] != f["direction"] or config_spec["bands"] != f["bands"]:
            raise ReplayError(f"frozen protocol no longer matches config for {f['field']}")
        out[f["field"]] = f
    return out


def build_predictor_snapshot(
    sample: list[dict[str, Any]],
    session: Any,
    protocol: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    feature_specs = validate_protocol_against_config(protocol, config)
    anchor = protocol["anchor_date"]
    start = protocol["filing_window"]["from_date"]
    total_weight = float(protocol["slice_total_effective_100_point_weight"])
    min_covered = total_weight * 0.70
    snapshot = []
    metadata_hashes = []
    xbrl_hashes = []
    guarded = 0
    metadata_ok = metadata_error = 0
    feature_coverage = defaultdict(int)
    xbrl_fetch_errors = 0

    for i, member in enumerate(sample):
        rows, attempts, metadata_hash = fetch_financial_metadata(session, member["symbol"], start, anchor)
        metadata_hashes.append(f"{member['isin']}|{metadata_hash}")
        metadata_ok += sum(a["status"] == "OK" for a in attempts)
        metadata_error += sum(a["status"] == "ERROR" for a in attempts)
        guard, guard_reason, company_name = is_guarded_financial(rows, anchor)
        if guard:
            guarded += 1
            snapshot.append({
                "isin": member["isin"],
                "symbol": member["symbol"],
                "company_name": company_name,
                "guarded_financial_business": True,
                "guard_reason": guard_reason,
                "ranking_eligible": False,
                "slice_score": None,
                "covered_effective_weight": 0.0,
            })
            continue
        latest_row, prior_row, scope = select_latest_and_prior(rows, anchor)
        latest = prior = None
        for label, row in (("latest", latest_row), ("prior", prior_row)):
            if row is None:
                continue
            try:
                url = xbrl_url(row)
                content = session.request(url, referer=REFERER, accept="application/xml,text/xml,*/*")
                parsed = parse_quarter_xbrl(content, row, scope or "UNKNOWN")
                xbrl_hashes.append(f"{member['isin']}|{label}|{parsed.get('source_sha256') or sha256(content)}")
                if label == "latest":
                    latest = parsed
                else:
                    prior = parsed
            except Exception:
                xbrl_fetch_errors += 1
        values = {
            "latest_sales_growth_yoy_pct": pct_growth(
                None if not latest else latest.get("revenue_cr"),
                None if not prior else prior.get("revenue_cr"),
            ),
            "latest_pat_growth_yoy_pct": pct_growth(
                None if not latest else latest.get("pat_cr"),
                None if not prior else prior.get("pat_cr"),
            ),
            "debt_equity": None if not latest else latest.get("debt_equity"),
            "interest_coverage": None,
            "ebitda_margin_pct": None,
        }
        if latest:
            fin = latest.get("finance_cost_cr")
            pbt = latest.get("pbt_cr")
            dep = latest.get("depreciation_cr")
            rev = latest.get("revenue_cr")
            if fin not in (None, 0) and pbt is not None:
                values["interest_coverage"] = round((float(pbt) + float(fin)) / float(fin), 4)
            if rev not in (None, 0) and pbt is not None and fin is not None and dep is not None:
                values["ebitda_margin_pct"] = round((float(pbt) + float(fin) + float(dep)) / float(rev) * 100.0, 4)

        earned = covered = 0.0
        feature_details = {}
        for field, f in feature_specs.items():
            value = values.get(field)
            frac = band_fraction(value, f)
            eff = float(f["effective_100_point_weight"])
            if frac is not None:
                covered += eff
                earned += eff * frac
                feature_coverage[field] += 1
            feature_details[field] = {"value": value, "fraction": frac, "effective_weight": eff}
        eligible = covered + 1e-9 >= min_covered
        score = round(earned / covered * 100.0, 4) if eligible and covered > 0 else None
        snapshot.append({
            "isin": member["isin"],
            "symbol": member["symbol"],
            "company_name": company_name,
            "scope": scope,
            "guarded_financial_business": False,
            "guard_reason": guard_reason,
            "latest_period": None if not latest else latest.get("period_end"),
            "prior_year_period": None if not prior else prior.get("period_end"),
            **values,
            "covered_effective_weight": round(covered, 4),
            "slice_score": score,
            "ranking_eligible": eligible,
            "feature_details": feature_details,
        })
        if i + 1 < len(sample):
            time.sleep(0.055)

    freeze_payload = [
        {
            "isin": r["isin"],
            "symbol": r["symbol"],
            "ranking_eligible": r.get("ranking_eligible"),
            "slice_score": r.get("slice_score"),
            "covered_effective_weight": r.get("covered_effective_weight"),
            **{f: r.get(f) for f in feature_specs},
        }
        for r in sorted(snapshot, key=lambda x: (x["isin"], x["symbol"]))
    ]
    manifest = {
        "anchor_date": anchor,
        "sample_rows": len(sample),
        "guarded_financial_business_rows": guarded,
        "ranking_eligible_rows": sum(bool(r.get("ranking_eligible")) for r in snapshot),
        "feature_coverage_counts": dict(sorted(feature_coverage.items())),
        "metadata_api_attempts_ok": metadata_ok,
        "metadata_api_attempts_error": metadata_error,
        "xbrl_fetch_errors": xbrl_fetch_errors,
        "metadata_source_hash_chain_sha256": sha256("\n".join(sorted(metadata_hashes)).encode("utf-8")),
        "xbrl_source_hash_chain_sha256": sha256("\n".join(sorted(xbrl_hashes)).encode("utf-8")),
        "predictor_snapshot_sha256": sha256(json.dumps(freeze_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")),
        "outcomes_seen_when_snapshot_frozen": False,
    }
    return snapshot, manifest


def acquire_outcomes(snapshot: list[dict[str, Any]], anchor_target: str, outcome_target: str, session: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entry_date, entry_raw, entry_meta = TECH.resolve_after(anchor_target, session)
    exit_date, exit_raw, exit_meta = TECH.resolve_on_or_before(outcome_target, session)
    entry_rows = TECH.parse_bhavcopy(entry_raw)
    exit_rows = TECH.parse_bhavcopy(exit_raw)
    entry_by_isin = {str(r.get("isin") or "").upper(): r for r in entry_rows if r.get("series") == "EQ" and r.get("isin")}
    exit_by_isin = {str(r.get("isin") or "").upper(): r for r in exit_rows if r.get("series") == "EQ" and r.get("isin")}
    action_rows, action_meta = TECH.fetch_actions(session, entry_date, exit_date)
    actions_by_symbol = TECH.normalize_actions(action_rows)
    joined = []
    statuses = defaultdict(int)
    for pred in snapshot:
        if not pred.get("ranking_eligible"):
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
            actions = TECH.dedupe_actions(a for alias in aliases for a in actions_by_symbol.get(alias, []))
            factor_result = TECH.cumulative_backward_price_factor(actions, price_date=entry_date, target_date=exit_date)
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
            "slice_score": pred["slice_score"],
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
    num = sum((x-ma)*(y-mb) for x, y in zip(ra, rb))
    da = sum((x-ma)**2 for x in ra); db = sum((y-mb)**2 for y in rb)
    return None if da <= 0 or db <= 0 else round(num / math.sqrt(da*db), 4)


def outcome_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [float(r["forward_multiple_3y"]) for r in rows]
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
    safe = [r for r in joined if r["outcome_status"] == "OBSERVED_CALIBRATION_SAFE" and r["forward_multiple_3y"] is not None]
    safe.sort(key=lambda r: (-float(r["slice_score"]), r["symbol"]))
    top_n = max(1, math.ceil(len(safe)*0.25)) if safe else 0
    top = safe[:top_n]
    cohort = outcome_summary(safe); topq = outcome_summary(top)
    return {
        "ranked_calibration_safe_n": len(safe),
        "cohort": cohort,
        "top_quartile": topq,
        "top_quartile_mean_lift_x": None if not cohort.get("mean_multiple") else round(topq.get("mean_multiple", 0)/cohort["mean_multiple"], 4),
        "spearman_slice_score_vs_forward_multiple": spearman(
            [float(r["slice_score"]) for r in safe],
            [float(r["forward_multiple_3y"]) for r in safe],
        ),
        "top_quartile_minus_cohort": {
            "two_x_pp": round(topq.get("two_x_rate_pct", 0)-cohort.get("two_x_rate_pct", 0), 2),
            "five_x_pp": round(topq.get("five_x_rate_pct", 0)-cohort.get("five_x_rate_pct", 0), 2),
            "loss_pp": round(topq.get("loss_rate_pct", 0)-cohort.get("loss_rate_pct", 0), 2),
            "mean_multiple": round(topq.get("mean_multiple", 0)-cohort.get("mean_multiple", 0), 4),
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Run frozen 2023 RK-MIS fundamental growth and quality holdout")
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--implementation-lock", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    lock = json.loads(args.implementation_lock.read_text(encoding="utf-8"))
    if protocol.get("status") != "PREREGISTERED_BEFORE_2023_OUTCOME_LOAD" or lock.get("status") != "LOCKED_BEFORE_OUTCOME_LOAD":
        raise ReplayError("fundamental replay rules are not frozen")
    config = json.loads((ROOT / "config/rk_mie_scoring.json").read_text(encoding="utf-8"))
    anchor = protocol["anchor_date"]
    outcome = protocol["outcome_target_date"]
    sample_size = int(protocol["sample_size"])
    args.output.mkdir(parents=True, exist_ok=True)
    session = TECH.OfficialSession(request_budget=650, timeout=30, sleep_seconds=0.025)
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
    eligible = predictor_manifest["ranking_eligible_rows"]
    safe = metrics["ranked_calibration_safe_n"]
    outcome_coverage = round(safe/eligible*100, 2) if eligible else 0.0
    interpretation_ready = (
        eligible >= int(protocol["evaluation"]["minimum_ranking_eligible_rows"])
        and outcome_coverage >= float(protocol["evaluation"]["calibration_safe_outcome_minimum_pct_of_scored"])
    )
    report = {
        "version": "rk-mis-fundamental-growth-quality-holdout-result-v1",
        "protocol": str(args.protocol),
        "protocol_sha256": sha256(args.protocol.read_bytes()),
        "implementation_lock": str(args.implementation_lock),
        "implementation_lock_sha256": sha256(args.implementation_lock.read_bytes()),
        "anchor_target": anchor,
        "anchor_resolved_trading_date": resolved_anchor,
        "outcome_target": outcome,
        "eligible_company_universe_rows": len(universe),
        "sample_rows": sample_size,
        "ranking_eligible_rows": eligible,
        "ranking_eligible_coverage_pct": round(eligible/sample_size*100, 2) if sample_size else 0.0,
        "calibration_safe_outcome_coverage_of_scored_pct": outcome_coverage,
        "minimum_interpretation_tests_pass": interpretation_ready,
        "metrics": metrics,
        "scope": protocol["scope"],
        "full_growth_runway_claim": False,
        "full_financial_quality_claim": False,
        "official_100_point_score_mutated": False,
        "alpha_claim": False,
        "no_post_result_tuning": True,
    }
    manifest = {
        "anchor_source": anchor_meta,
        "predictor": predictor_manifest,
        "outcomes": outcome_manifest,
        "official_requests_made": session.requests_made,
        "raw_exchange_or_xbrl_files_published": False,
        "public_artifact_contains_only_derived_aggregate_results_and_source_metadata": True,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
