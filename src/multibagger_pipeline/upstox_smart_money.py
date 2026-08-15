from __future__ import annotations

import calendar
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .prospective_smart_money import ProspectiveSmartMoneyStore, SmartMoneyObservation

UPSTOX_SHARE_HOLDINGS_URL = "https://api.upstox.com/v2/fundamentals/{isin}/share-holdings"
APPROVED_CATEGORY_MAP = {
    "mf_holding_pct": "mutual_funds",
    "fii_holding_pct": "fii",
    "promoter_holding_pct": "promoters",
}
IGNORED_PROVIDER_CATEGORIES = {"other_dii", "retail_and_other"}


def _canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _quarter_end(label: str) -> str:
    text = str(label or "").strip()
    parsed = datetime.strptime(text, "%b %Y")
    month = parsed.month
    if month not in {3, 6, 9, 12}:
        raise ValueError(f"unsupported shareholding period {text!r}; expected Mar/Jun/Sep/Dec YYYY")
    last_day = calendar.monthrange(parsed.year, month)[1]
    return date(parsed.year, month, last_day).isoformat()


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if not 0.0 <= number <= 100.0:
        raise ValueError("shareholding percentage must be between 0 and 100")
    return number


def _category_period_values(payload: dict[str, Any]) -> tuple[dict[str, dict[str, float]], set[str]]:
    if str(payload.get("status") or "").lower() != "success":
        raise ValueError("Upstox share-holdings payload status is not success")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("Upstox share-holdings payload data must be a list")

    categories: dict[str, dict[str, float]] = {}
    seen_categories: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip().lower()
        if not category:
            continue
        seen_categories.add(category)
        history = item.get("history")
        if not isinstance(history, list):
            continue
        by_period: dict[str, float] = {}
        for point in history:
            if not isinstance(point, dict):
                continue
            try:
                period = _quarter_end(str(point.get("period") or ""))
                value = _number(point.get("value"))
            except (TypeError, ValueError):
                continue
            if value is not None:
                by_period[period] = value
        if by_period:
            categories[category] = by_period
    return categories, seen_categories


def latest_observable_quarter(payload: dict[str, Any]) -> tuple[str, dict[str, float | None], dict[str, Any]]:
    """Return one current-quarter observation only.

    The provider may return several historical quarters in one response. Those older
    rows are intentionally not backfilled into the prospective point-in-time journal:
    they were not observed by RK-MIS at their historical reporting dates. The latest
    quarter common to the approved provider categories is recorded at capture time.
    """
    categories, seen = _category_period_values(payload)
    required_provider_categories = set(APPROVED_CATEGORY_MAP.values())
    available_required = [cat for cat in required_provider_categories if cat in categories]
    if not available_required:
        raise ValueError("no approved Smart Money categories found in Upstox payload")

    common_periods: set[str] | None = None
    for cat in available_required:
        periods = set(categories[cat])
        common_periods = periods if common_periods is None else common_periods & periods
    if not common_periods:
        raise ValueError("approved Upstox Smart Money categories have no common reporting quarter")

    period_end = max(common_periods)
    fields: dict[str, float | None] = {
        "mf_holding_pct": None,
        "fii_holding_pct": None,
        "promoter_holding_pct": None,
    }
    for field, category in APPROVED_CATEGORY_MAP.items():
        fields[field] = categories.get(category, {}).get(period_end)

    diagnostics = {
        "period_end": period_end,
        "provider_categories_seen": sorted(seen),
        "provider_categories_used": sorted(cat for cat in required_provider_categories if cat in categories),
        "provider_categories_ignored": sorted(seen & IGNORED_PROVIDER_CATEGORIES),
        "institutional_breadth_available": False,
        "historical_quarters_backfilled": False,
    }
    return period_end, fields, diagnostics


def observation_from_upstox_payload(
    *,
    isin: str,
    symbol: str,
    payload: dict[str, Any],
    known_at: str,
) -> tuple[SmartMoneyObservation, dict[str, Any]]:
    isin_norm = str(isin or "").strip().upper()
    symbol_norm = str(symbol or "").strip().upper()
    if not isin_norm.startswith("INE"):
        raise ValueError("valid Indian equity ISIN is required")
    if not symbol_norm:
        raise ValueError("symbol is required")

    period_end, fields, diagnostics = latest_observable_quarter(payload)
    observation = SmartMoneyObservation(
        isin=isin_norm,
        symbol=symbol_norm,
        period_end=period_end,
        known_at=str(known_at)[:10],
        source_url=UPSTOX_SHARE_HOLDINGS_URL.format(isin=isin_norm),
        source_kind="UPSTOX_COMPANY_SHARE_HOLDINGS",
        source_grade="A",
        source_sha256=_canonical_sha256(payload),
        mf_holding_pct=fields["mf_holding_pct"],
        fii_holding_pct=fields["fii_holding_pct"],
        institutional_shareholder_count=None,
        promoter_holding_pct=fields["promoter_holding_pct"],
    )
    diagnostics = {
        **diagnostics,
        "isin": isin_norm,
        "symbol": symbol_norm,
        "source_sha256": observation.source_sha256,
        "known_at": str(known_at)[:10],
    }
    return observation, diagnostics


def ingest_upstox_raw_directory(
    *,
    raw_root: str | Path,
    companies: Iterable[dict[str, Any]],
    store: ProspectiveSmartMoneyStore,
    known_at: str,
) -> dict[str, Any]:
    root = Path(raw_root)
    scanned = raw_present = appended = skipped = 0
    period_counts: Counter[str] = Counter()
    skip_reasons: Counter[str] = Counter()

    for company in companies:
        scanned += 1
        isin = str(company.get("isin") or "").strip().upper()
        symbol = str(company.get("symbol") or "").strip().upper()
        path = root / isin / "share_holdings.json"
        if not isin or not symbol or not path.exists():
            skipped += 1
            skip_reasons["RAW_NOT_AVAILABLE"] += 1
            continue
        raw_present += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            observation, diagnostics = observation_from_upstox_payload(
                isin=isin,
                symbol=symbol,
                payload=payload,
                known_at=known_at,
            )
            store.add(observation)
            appended += 1
            period_counts[diagnostics["period_end"]] += 1
        except Exception as exc:
            skipped += 1
            skip_reasons[type(exc).__name__] += 1

    return {
        "status": "OK",
        "known_at": str(known_at)[:10],
        "companies_scanned": scanned,
        "raw_shareholding_payloads_present": raw_present,
        "observations_appended_or_already_present": appended,
        "skipped": skipped,
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "latest_period_counts": dict(sorted(period_counts.items())),
        "historical_provider_quarters_backfilled": False,
        "institutional_breadth_inferred_from_other_dii": False,
        "institutional_breadth_source_available": False,
        "approved_provider_fields": ["mf_holding_pct", "fii_holding_pct", "promoter_holding_pct"],
    }
