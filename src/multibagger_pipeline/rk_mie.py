from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MISSING = {"", "-", "NA", "N/A", "null", "None"}


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() in MISSING:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _compare(value: float, op: str, threshold: float) -> bool:
    return {
        "eq": value == threshold,
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
    }[op]


def _band_score(value: float | None, spec: dict[str, Any]) -> tuple[float, bool]:
    """Return earned feature points and whether the feature had evidence."""
    if value is None:
        return 0.0, False
    weight = float(spec["weight"])
    direction = spec.get("direction", "higher")
    bands = [(float(t), float(f)) for t, f in spec["bands"]]
    if direction == "higher":
        bands.sort(key=lambda item: item[0], reverse=True)
        for threshold, fraction in bands:
            if value >= threshold:
                return round(weight * fraction, 4), True
    elif direction == "lower":
        bands.sort(key=lambda item: item[0])
        for threshold, fraction in bands:
            if value <= threshold:
                return round(weight * fraction, 4), True
    else:
        raise ValueError(f"unsupported scoring direction: {direction}")
    return 0.0, True


def _score_pillar(row: dict[str, str], features: list[dict[str, Any]]) -> dict[str, Any]:
    total_weight = sum(float(spec["weight"]) for spec in features)
    earned = 0.0
    evidenced_weight = 0.0
    details: list[dict[str, Any]] = []
    for spec in features:
        value = _number(row.get(spec["field"]))
        points, present = _band_score(value, spec)
        earned += points
        if present:
            evidenced_weight += float(spec["weight"])
        details.append({
            "field": spec["field"],
            "value": value,
            "points": round(points, 2),
            "max_points": spec["weight"],
            "evidence_present": present,
        })
    score = round(earned / total_weight * 100, 2) if total_weight else 0.0
    coverage = round(evidenced_weight / total_weight * 100, 2) if total_weight else 0.0
    return {"score": score, "coverage": coverage, "features": details}


def _evaluate_flags(row: dict[str, str], specs: list[dict[str, Any]]) -> list[str]:
    flags: list[str] = []
    for spec in specs:
        value = _number(row.get(spec["field"]))
        if value is not None and _compare(value, spec["op"], float(spec["value"])):
            flags.append(spec["code"])
    return flags


def required_cagr(target_multiple: float, years: int) -> float:
    if target_multiple <= 0 or years <= 0:
        raise ValueError("target_multiple and years must be positive")
    return round((target_multiple ** (1 / years) - 1) * 100, 2)


def _feasibility_label(required_eps_cagr: float | None, required_tam_share: float | None, settings: dict[str, Any]) -> str:
    if required_eps_cagr is None:
        return "INSUFFICIENT_DATA"
    if required_eps_cagr <= settings["credible_eps_cagr_pct"] and (
        required_tam_share is None or required_tam_share <= settings["max_credible_tam_share_pct"]
    ):
        return "CREDIBLE_PATH"
    if required_eps_cagr <= settings["plausible_eps_cagr_pct"] and (
        required_tam_share is None or required_tam_share <= settings["max_plausible_tam_share_pct"]
    ):
        return "PLAUSIBLE_BUT_DEMANDING"
    if required_eps_cagr <= settings["stretch_eps_cagr_pct"]:
        return "STRETCH"
    return "VERY_LOW_FEASIBILITY"


def evaluate_multiple_feasibility(row: dict[str, str], settings: dict[str, Any], targets: tuple[int, ...] = (10, 20, 50)) -> dict[str, Any]:
    years = int(_number(row.get("feasibility_years")) or settings.get("years_to_test", 10))
    current_price = _number(row.get("current_price"))
    current_eps = _number(row.get("current_eps_ttm"))
    shares_cr = _number(row.get("shares_cr"))
    terminal_pe = _number(row.get("terminal_pe_assumption")) or float(settings["feasibility"]["default_terminal_pe"])
    net_margin_pct = _number(row.get("sustainable_net_margin_pct")) or float(settings["feasibility"]["default_sustainable_net_margin_pct"])
    total_dilution_pct = _number(row.get("total_dilution_pct_assumption")) or float(settings["feasibility"]["default_total_dilution_pct"])
    horizon_tam_cr = _number(row.get("estimated_tam_cr_at_horizon"))
    capacity_revenue_cr = _number(row.get("planned_revenue_capacity_cr_at_horizon"))

    results: dict[str, Any] = {
        "years": years,
        "terminal_pe_assumption": terminal_pe,
        "sustainable_net_margin_pct": net_margin_pct,
        "targets": {},
    }
    for target in targets:
        required_eps = None
        required_eps_cagr = None
        required_pat_cr = None
        required_revenue_cr = None
        required_tam_share_pct = None
        capacity_coverage = None
        if current_price is not None and current_eps not in (None, 0) and terminal_pe > 0:
            required_eps = current_price * target / terminal_pe
            if required_eps > 0 and current_eps > 0:
                required_eps_cagr = (required_eps / current_eps) ** (1 / years) * 100 - 100
            if shares_cr is not None:
                diluted_shares_cr = shares_cr * (1 + total_dilution_pct / 100)
                required_pat_cr = required_eps * diluted_shares_cr
                if net_margin_pct > 0:
                    required_revenue_cr = required_pat_cr / (net_margin_pct / 100)
                    if horizon_tam_cr and horizon_tam_cr > 0:
                        required_tam_share_pct = required_revenue_cr / horizon_tam_cr * 100
                    if capacity_revenue_cr and capacity_revenue_cr > 0:
                        capacity_coverage = capacity_revenue_cr / required_revenue_cr if required_revenue_cr else None
        label = _feasibility_label(required_eps_cagr, required_tam_share_pct, settings["feasibility"])
        if capacity_coverage is not None and capacity_coverage < 0.7 and label in {"CREDIBLE_PATH", "PLAUSIBLE_BUT_DEMANDING"}:
            label = "CAPACITY_GAP"
        results["targets"][f"{target}x"] = {
            "market_return_cagr_pct": required_cagr(target, years),
            "required_eps": round(required_eps, 2) if required_eps is not None else None,
            "required_eps_cagr_pct": round(required_eps_cagr, 2) if required_eps_cagr is not None else None,
            "required_pat_cr": round(required_pat_cr, 2) if required_pat_cr is not None else None,
            "required_revenue_cr": round(required_revenue_cr, 2) if required_revenue_cr is not None else None,
            "required_tam_share_pct": round(required_tam_share_pct, 2) if required_tam_share_pct is not None else None,
            "planned_capacity_coverage_x": round(capacity_coverage, 2) if capacity_coverage is not None else None,
            "assessment": label,
        }
    return results


def _classification(score: float, bands: list[list[Any]]) -> str:
    for threshold, label in sorted(bands, key=lambda item: float(item[0]), reverse=True):
        if score >= float(threshold):
            return str(label)
    return "REJECT"


def _thesis_trend(score: float, previous_score: float | None) -> str:
    if previous_score is None:
        return "NEW"
    delta = score - previous_score
    if delta >= 3:
        return "STRENGTHENING"
    if delta <= -3:
        return "WEAKENING"
    return "STABLE"


def score_company(row: dict[str, str], settings: dict[str, Any], previous_score: float | None = None) -> dict[str, Any]:
    pillars: dict[str, Any] = {}
    weighted_score = 0.0
    weighted_coverage = 0.0
    for pillar, pillar_weight in settings["pillar_weights"].items():
        result = _score_pillar(row, settings["pillar_features"][pillar])
        pillars[pillar] = result
        weighted_score += result["score"] * float(pillar_weight) / 100
        weighted_coverage += result["coverage"] * float(pillar_weight) / 100

    score = round(weighted_score, 2)
    coverage = round(weighted_coverage, 2)
    hard_flags = _evaluate_flags(row, settings.get("hard_red_flags", []))
    warning_flags = _evaluate_flags(row, settings.get("warning_flags", []))
    classification = "AVOID" if hard_flags else _classification(score, settings["classification_bands"])
    if not hard_flags and coverage < float(settings["minimum_global_coverage"]):
        classification = "INSUFFICIENT_EVIDENCE"

    return {
        "symbol": row.get("symbol", "").strip().upper(),
        "company_name": row.get("company_name", "").strip(),
        "sector": row.get("sector", "").strip(),
        "rk_mie_score": score,
        "data_coverage": coverage,
        "classification": classification,
        "thesis_trend": _thesis_trend(score, previous_score),
        "hard_red_flags": hard_flags,
        "warning_flags": warning_flags,
        "pillars": pillars,
        "feasibility": evaluate_multiple_feasibility(row, settings),
        "source_urls": [url.strip() for url in row.get("source_urls", "").split("|") if url.strip()],
    }


def _load_previous_scores(path: Path | None) -> dict[str, float]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results", payload if isinstance(payload, list) else [])
    return {row["symbol"]: float(row["rk_mie_score"]) for row in results if row.get("symbol") and row.get("rk_mie_score") is not None}


def _apply_funnel(results: list[dict[str, Any]], settings: dict[str, Any]) -> None:
    funnel = settings["funnel"]
    eligible = [
        row for row in results
        if row["classification"] not in {"AVOID", "INSUFFICIENT_EVIDENCE", "REJECT"}
        and row["data_coverage"] >= settings["minimum_global_coverage"]
    ]
    for row in results:
        row["funnel_tier"] = "OUTSIDE"
    discovery = [row for row in eligible if row["rk_mie_score"] >= funnel["discovery_min_score"]][: funnel["discovery_limit"]]
    for row in discovery:
        row["funnel_tier"] = "TOP_100_DISCOVERY"
    high = [row for row in eligible if row["rk_mie_score"] >= funnel["high_conviction_min_score"]][: funnel["high_conviction_limit"]]
    for row in high:
        row["funnel_tier"] = "TOP_30_HIGH_CONVICTION"
    diamonds = [
        row for row in eligible
        if row["rk_mie_score"] >= funnel["diamond_min_score"]
        and row["data_coverage"] >= settings["diamond_minimum_coverage"]
    ][: funnel["diamond_limit"]]
    for row in diamonds:
        row["funnel_tier"] = "TOP_10_RK_DIAMOND"


def build_rk_mie(input_path: Path, settings_path: Path, previous_path: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if round(sum(float(v) for v in settings["pillar_weights"].values()), 8) != 100:
        raise ValueError("RK-MIE pillar weights must sum to 100")
    previous = _load_previous_scores(previous_path)
    rows = _read_csv(input_path)
    results = [score_company(row, settings, previous.get(row.get("symbol", "").strip().upper())) for row in rows if row.get("symbol", "").strip()]
    results.sort(key=lambda row: (row["classification"] != "AVOID", row["rk_mie_score"], row["data_coverage"]), reverse=True)
    _apply_funnel(results, settings)
    quality = {
        "engine": "RK-MIE",
        "version": settings.get("version", "unknown"),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "companies_received": len(rows),
        "companies_scored": len(results),
        "hard_flagged": sum(bool(row["hard_red_flags"]) for row in results),
        "insufficient_evidence": sum(row["classification"] == "INSUFFICIENT_EVIDENCE" for row in results),
        "top_100": sum(row["funnel_tier"] in {"TOP_100_DISCOVERY", "TOP_30_HIGH_CONVICTION", "TOP_10_RK_DIAMOND"} for row in results),
        "top_30": sum(row["funnel_tier"] in {"TOP_30_HIGH_CONVICTION", "TOP_10_RK_DIAMOND"} for row in results),
        "top_10": sum(row["funnel_tier"] == "TOP_10_RK_DIAMOND" for row in results),
    }
    return results, quality
