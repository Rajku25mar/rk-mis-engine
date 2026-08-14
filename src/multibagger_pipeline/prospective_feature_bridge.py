from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROSPECTIVE_FEATURE_FIELDS = (
    "reinvestment_runway_score",
    "moat_evidence_score",
    "new_product_export_optionalities_score",
    "mf_holding_change_pp_4q",
    "fii_holding_change_pp_4q",
    "institutional_breadth_change_4q",
    "promoter_holding_change_pp_4q",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _same_value(a: Any, b: Any, tolerance: float = 1e-6) -> bool:
    if _missing(a) and _missing(b):
        return True
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return _text(a) == _text(b)


def _identity_indexes(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any] | None]]:
    by_isin: dict[str, dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any] | None] = {}
    for row in rows:
        isin = _text(row.get("isin")).upper()
        symbol = _text(row.get("symbol")).upper()
        if isin:
            prior = by_isin.get(isin)
            if prior is not None and prior is not row:
                raise ValueError(f"duplicate prospective ISIN {isin}")
            by_isin[isin] = row
        if symbol:
            if symbol in by_symbol and by_symbol[symbol] is not row:
                by_symbol[symbol] = None
            else:
                by_symbol[symbol] = row
    return by_isin, by_symbol


def _match(base: dict[str, Any], by_isin: dict[str, dict[str, Any]], by_symbol: dict[str, dict[str, Any] | None]) -> tuple[dict[str, Any] | None, str | None]:
    isin = _text(base.get("isin")).upper()
    symbol = _text(base.get("symbol")).upper()
    if isin:
        row = by_isin.get(isin)
        if row is not None:
            return row, "ISIN"
        # Do not fall back to symbol if both sides carry conflicting ISIN identities.
        symbol_row = by_symbol.get(symbol) if symbol else None
        if symbol_row is not None and _text(symbol_row.get("isin")):
            return None, "ISIN_NO_MATCH_SYMBOL_FALLBACK_BLOCKED"
        return symbol_row, "SYMBOL_FALLBACK_NO_PROSPECTIVE_ISIN" if symbol_row else None
    if symbol:
        row = by_symbol.get(symbol)
        if row is None:
            return None, None
        return row, "SYMBOL"
    return None, None


def merge_prospective_features(
    base_rows: list[dict[str, Any]],
    prospective_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_isin, by_symbol = _identity_indexes(prospective_rows)
    output = []
    fill_counts: Counter[str] = Counter()
    conflict_counts: Counter[str] = Counter()
    match_counts: Counter[str] = Counter()
    unmatched = 0

    for src in base_rows:
        row = dict(src)
        prospective, match_rule = _match(src, by_isin, by_symbol)
        filled: list[str] = []
        conflicts: list[str] = []
        if prospective is None:
            unmatched += 1
        else:
            match_counts[match_rule or "UNKNOWN"] += 1
            for field in PROSPECTIVE_FEATURE_FIELDS:
                incoming = prospective.get(field)
                if incoming is None:
                    continue
                existing = row.get(field)
                if _missing(existing):
                    row[field] = incoming
                    filled.append(field)
                    fill_counts[field] += 1
                elif not _same_value(existing, incoming):
                    conflicts.append(field)
                    conflict_counts[field] += 1
        row["prospective_features_filled"] = "|".join(filled)
        row["prospective_feature_conflicts"] = "|".join(conflicts)
        row["prospective_identity_match_rule"] = match_rule or ""
        output.append(row)

    report = {
        "base_rows": len(base_rows),
        "prospective_rows": len(prospective_rows),
        "matched_rows": sum(match_counts.values()),
        "unmatched_rows": unmatched,
        "match_rule_counts": dict(sorted(match_counts.items())),
        "filled_feature_cells": sum(fill_counts.values()),
        "filled_feature_counts": dict(sorted(fill_counts.items())),
        "conflict_feature_cells": sum(conflict_counts.values()),
        "conflict_feature_counts": dict(sorted(conflict_counts.items())),
        "merge_rule": "FILL_MISSING_ONLY_NEVER_OVERWRITE_CONFLICT",
        "identity_rule": "ISIN_FIRST_SYMBOL_ONLY_WHEN_IDENTITY_SAFE",
        "score_weights_changed": False,
        "missing_data_imputed": False,
    }
    return output, report


def combine_prospective_sources(
    documentary_rows: Iterable[dict[str, Any]],
    smart_money_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    symbol_only: dict[str, str] = {}
    for source_name, rows in (("DOCUMENTARY", documentary_rows), ("SMART_MONEY", smart_money_rows)):
        for src in rows:
            isin = _text(src.get("isin")).upper()
            symbol = _text(src.get("symbol")).upper()
            key = "ISIN:" + isin if isin else "SYMBOL:" + symbol
            if not symbol:
                continue
            if not isin:
                prior = symbol_only.get(symbol)
                if prior and prior != key:
                    raise ValueError(f"ambiguous symbol-only prospective identity {symbol}")
                symbol_only[symbol] = key
            row = combined.setdefault(key, {"isin": isin or None, "symbol": symbol, "prospective_sources": []})
            if row.get("isin") and isin and row["isin"] != isin:
                raise ValueError(f"prospective identity conflict for {symbol}")
            if row.get("symbol") and symbol and row["symbol"] != symbol:
                raise ValueError(f"prospective symbol conflict for {key}")
            row["prospective_sources"].append(source_name)
            features = src.get("features") if isinstance(src.get("features"), dict) else src
            for field in PROSPECTIVE_FEATURE_FIELDS:
                value = features.get(field) if isinstance(features, dict) else None
                if value is None:
                    continue
                if field in row and row[field] is not None and not _same_value(row[field], value):
                    raise ValueError(f"prospective source conflict for {key} field {field}")
                row[field] = value
    for row in combined.values():
        row["prospective_sources"] = sorted(set(row["prospective_sources"]))
    return [combined[key] for key in sorted(combined)]


def read_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: str | Path, report: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
