from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

REVENUE_TAGS = ("RevenueFromOperations", "RevenueFromSaleOfProductsAndServices", "RevenueFromSaleOfProducts")
PAT_TAGS = ("ProfitLossForPeriod", "ProfitLossForPeriodFromContinuingOperations")
FINANCE_COST_TAGS = ("FinanceCosts", "FinanceCost")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _date(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:11], fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _duration_days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def _scope_from_filing(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if "non-consolidated" in text or "standalone" in text:
        return "STANDALONE"
    if "consolidated" in text:
        return "CONSOLIDATED"
    return None


def _scope_from_xbrl(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if text == "standalone" or "non-consolidated" in text:
        return "STANDALONE"
    if "consolidated" in text:
        return "CONSOLIDATED"
    return None


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
            "decimals": elem.attrib.get("decimals"),
        })
    return text_facts, numeric


def _one(values: list[str] | None) -> str | None:
    vals = [] if not values else list(dict.fromkeys(v.strip() for v in values if v.strip()))
    return vals[0] if len(vals) == 1 else None


def _choose_current_quarter_context(
    text_facts: dict[str, dict[str, list[str]]],
    period_end: str,
    expected_scope: str | None,
) -> tuple[str | None, list[str]]:
    candidates: list[tuple[str, int, str | None]] = []
    for ctx, tags in text_facts.items():
        start = _date(_one(tags.get("DateOfStartOfReportingPeriod")))
        end = _date(_one(tags.get("DateOfEndOfReportingPeriod")))
        if not start or not end or end != period_end:
            continue
        days = _duration_days(start, end)
        if not 75 <= days <= 105:
            continue
        scope = _scope_from_xbrl(_one(tags.get("NatureOfReportStandaloneConsolidated")))
        if expected_scope and scope and scope != expected_scope:
            continue
        candidates.append((ctx, days, scope))
    if len(candidates) == 1:
        return candidates[0][0], []
    if not candidates:
        return None, ["NO_UNAMBIGUOUS_CURRENT_QUARTER_CONTEXT"]
    scoped = [x for x in candidates if expected_scope and x[2] == expected_scope]
    if len(scoped) == 1:
        return scoped[0][0], []
    return None, ["AMBIGUOUS_CURRENT_QUARTER_CONTEXT:" + ",".join(x[0] for x in candidates)]


def _metric(numeric_facts: list[dict[str, Any]], tags: tuple[str, ...], metric: str) -> tuple[float | None, dict[str, Any] | None, list[str]]:
    for tag in tags:
        matches = [x for x in numeric_facts if x["tag"] == tag]
        if not matches:
            continue
        distinct = {(x["value"], x.get("unit_ref")) for x in matches}
        if len(distinct) != 1:
            return None, None, [f"AMBIGUOUS_{metric.upper()}_{tag}"]
        fact = matches[0]
        unit = (fact.get("unit_ref") or "").upper()
        if unit not in {"INR", "INR_MILLIONS", "INR-LAKHS", "INR_CRORES"}:
            return None, None, [f"UNSUPPORTED_{metric.upper()}_UNIT:{fact.get('unit_ref')}"]
        value = float(fact["value"])
        if unit == "INR":
            crore = value / 10_000_000.0
        elif unit == "INR_MILLIONS":
            crore = value / 10.0
        elif unit == "INR-LAKHS":
            crore = value / 100.0
        else:
            crore = value
        warnings = [] if tag == tags[0] else [f"{metric.upper()}_FALLBACK_TAG:{tag}"]
        return round(crore, 6), fact, warnings
    return None, None, [f"MISSING_{metric.upper()}"]


@dataclass
class NormalizedQuarter:
    symbol: str
    period_end: str
    filing_date: str
    scope: str | None
    quarter_context_ref: str | None
    revenue_cr: float | None
    pat_cr: float | None
    finance_cost_cr: float | None
    source_url: str
    source_sha256: str
    warnings: list[str]
    replay_grade: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_filing_xbrl(content: bytes, filing_row: dict[str, Any], *, anchor_date: str) -> NormalizedQuarter:
    """Normalize one filing using an explicit point-in-time anchor.

    `anchor_date` is deliberately mandatory. This prevents a historical replay from
    accidentally inheriting a stale module-level anchor through a default argument.
    """
    symbol = str(filing_row.get("symbol") or "").strip().upper()
    period_end = _date(filing_row.get("toDate") or filing_row.get("period_end")) or "9999-12-31"
    filing_date = _date(filing_row.get("filingDate") or filing_row.get("filing_date")) or "9999-12-31"
    source_url = str(filing_row.get("xbrl") or filing_row.get("source_url") or "")
    expected_scope = _scope_from_filing(filing_row.get("consolidated"))
    warnings: list[str] = []
    if filing_date > anchor_date:
        warnings.append("FILING_AFTER_ANCHOR")
    if period_end > anchor_date:
        warnings.append("PERIOD_AFTER_ANCHOR")

    text_facts, numeric = _facts_by_context(content)
    ctx, ctx_warnings = _choose_current_quarter_context(text_facts, period_end, expected_scope)
    warnings.extend(ctx_warnings)
    revenue = pat = finance = None
    xbrl_scope = None
    if ctx:
        xbrl_scope = _scope_from_xbrl(_one(text_facts.get(ctx, {}).get("NatureOfReportStandaloneConsolidated")))
        if expected_scope and xbrl_scope and expected_scope != xbrl_scope:
            warnings.append(f"SCOPE_MISMATCH:{expected_scope}!={xbrl_scope}")
        facts = numeric.get(ctx, [])
        revenue, _, w = _metric(facts, REVENUE_TAGS, "revenue"); warnings.extend(w)
        pat, _, w = _metric(facts, PAT_TAGS, "pat"); warnings.extend(w)
        finance, _, w = _metric(facts, FINANCE_COST_TAGS, "finance_cost"); warnings.extend(w)

    hard_prefixes = (
        "NO_UNAMBIGUOUS", "AMBIGUOUS_CURRENT", "FILING_AFTER_ANCHOR",
        "PERIOD_AFTER_ANCHOR", "SCOPE_MISMATCH", "AMBIGUOUS_REVENUE",
        "AMBIGUOUS_PAT", "UNSUPPORTED_REVENUE_UNIT", "UNSUPPORTED_PAT_UNIT",
    )
    hard = any(any(w.startswith(p) for p in hard_prefixes) for w in warnings)
    return NormalizedQuarter(
        symbol=symbol,
        period_end=period_end,
        filing_date=filing_date,
        scope=xbrl_scope or expected_scope,
        quarter_context_ref=ctx,
        revenue_cr=revenue,
        pat_cr=pat,
        finance_cost_cr=finance,
        source_url=source_url,
        source_sha256=hashlib.sha256(content).hexdigest(),
        warnings=warnings,
        replay_grade=bool(ctx and revenue is not None and pat is not None and not hard),
    )
