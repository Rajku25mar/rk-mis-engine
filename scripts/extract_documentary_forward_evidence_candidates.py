from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TECH_SPEC = importlib.util.spec_from_file_location(
    "rk_mis_technical_replay_common", ROOT / "scripts/run_technical_replay.py"
)
TECH = importlib.util.module_from_spec(TECH_SPEC)
assert TECH_SPEC and TECH_SPEC.loader
TECH_SPEC.loader.exec_module(TECH)

MAX_DOCUMENT_BYTES = 15_000_000
MAX_PAGES_PER_DOCUMENT = 220
MAX_PAGE_CANDIDATES_PER_CATEGORY = 3

RUNWAY_PATTERNS = {
    "committed_capacity_expansion": [
        ("capacity", "expansion"), ("capacity", "increase"), ("new", "plant"),
        ("new", "facility"), ("adding", "capacity"), ("brownfield",), ("greenfield",),
    ],
    "multi_phase_growth_roadmap": [("phase ii",), ("phase 2",), ("phase iii",), ("phase 3",), ("next phase",), ("multiple phases",)],
    "funding_visibility": [("internal accrual",), ("internal accruals",), ("term loan",), ("fund raise",), ("cash accrual",), ("sanctioned", "loan")],
    "physical_or_operational_headroom": [("land bank",), ("spare land",), ("expandable", "capacity"), ("installed capacity", "headroom"), ("modular", "capacity")],
    "ramp_or_utilisation_headroom": [("ramp up",), ("ramp-up",), ("utilisation", "capacity"), ("utilization", "capacity"), ("underutilised",), ("underutilized",)],
}

MOAT_PATTERNS = {
    "proprietary_or_ip": [("patent",), ("proprietary",), ("know-how",), ("know how",), ("owned technology",)],
    "qualification_or_regulatory_barrier": [("approved vendor",), ("vendor approval",), ("qualification",), ("certification",), ("regulatory approval",)],
    "customer_stickiness": [("repeat order",), ("repeat orders",), ("long-term agreement",), ("long term agreement",), ("customer since",), ("design-in",)],
    "cost_process_or_scale_advantage": [("cost advantage",), ("process advantage",), ("yield improvement",), ("backward integration",), ("scale advantage",)],
    "market_position_or_limited_competition": [("market leader",), ("sole supplier",), ("single source",), ("only manufacturer",), ("limited competition",), ("limited vendor",)],
}

OPTIONALITY_PATTERNS = {
    "new_product_or_platform": [("new product",), ("product launch",), ("new platform",), ("new solution",)],
    "new_customer_or_vendor_approval": [("new customer",), ("vendor approval",), ("customer approval",), ("qualified by",)],
    "new_geography": [("new geography",), ("new market",), ("market entry",), ("entering", "market")],
    "export_expansion": [("export", "expansion"), ("export", "growth"), ("export", "market"), ("export", "customer")],
    "adjacent_vertical_or_use_case": [("adjacent",), ("new vertical",), ("new segment",), ("new use case",)],
}

ORDER_TERMS = ("order book", "orderbook", "contracted backlog", "letter of award", "purchase order")
CAPACITY_TERMS = ("capacity", "mtpa", "tpa", "tonnes per annum", "tons per annum", "mw", "gw", "units per annum")
CURRENCY_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(crore|cr|million|mn|billion|bn|lakh)?", re.I)
PERCENT_RE = re.compile(r"(?<![0-9])([0-9]+(?:\.[0-9]+)?)\s*%")
CAPACITY_VALUE_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(mtpa|tpa|tpd|mw|gw|tonnes per annum|tons per annum|units per annum)", re.I)


class ExtractionError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _subject(text: str | None) -> str:
    return (text or "").lower()


def download_priority(row: dict[str, Any]) -> int:
    s = _subject(row.get("subject"))
    families = set(row.get("keyword_families") or [])
    hits = row.get("keyword_hits") or {}
    points = 10 * len(families & {"runway", "moat", "orders"})
    points += 5 * len(set(hits.get("optionality") or []) - {"approval", "qualification"})
    if "investor presentation" in s:
        points += 12
    if "press release" in s:
        points += 10
    if "analysts/institutional investor meet" in s or "con. call" in s:
        points += 8
    url = _subject(row.get("attachment_url"))
    if "annual report" in s or "annualreport" in url or "annual_report" in url:
        points += 7
    return points


def triage_included(row: dict[str, Any]) -> bool:
    s = _subject(row.get("subject"))
    families = set(row.get("keyword_families") or [])
    hits = row.get("keyword_hits") or {}
    if families & {"runway", "moat", "orders"}:
        return True
    if set(hits.get("optionality") or []) - {"approval", "qualification"}:
        return True
    if "investor presentation" in s or "press release" in s:
        return True
    if "analysts/institutional investor meet" in s or "con. call" in s:
        return True
    url = _subject(row.get("attachment_url"))
    if "annual report" in s or "annualreport" in url or "annual_report" in url:
        return True
    return False


def apply_triage(candidate_index: list[dict[str, Any]], triage_lock: dict[str, Any]) -> list[dict[str, Any]]:
    cap = int(triage_lock["maximum_documents_per_symbol"])
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidate_index:
        if triage_included(row):
            by_symbol[str(row["sample_symbol"])].append(row)
    selected = []
    for symbol in sorted(by_symbol):
        rows = sorted(
            by_symbol[symbol],
            key=lambda r: (download_priority(r), r.get("known_at") or "", r.get("subject") or ""),
            reverse=True,
        )[:cap]
        for row in rows:
            selected.append({**row, "download_priority": download_priority(row)})
    expected = (
        triage_lock.get("estimated_from_frozen_metadata_only")
        or triage_lock.get("estimated_from_same_frozen_metadata_only")
        or {}
    )
    if expected:
        if len(selected) != int(expected["documents_retained"]):
            raise ExtractionError(f"frozen triage mismatch: expected {expected['documents_retained']} docs, got {len(selected)}")
        if len(by_symbol) != int(expected["symbols_retained"]):
            raise ExtractionError(f"frozen triage mismatch: expected {expected['symbols_retained']} symbols, got {len(by_symbol)}")
    return selected


def page_matches(text: str, patterns: dict[str, list[tuple[str, ...]]]) -> dict[str, list[list[str]]]:
    low = " ".join(text.lower().split())
    out: dict[str, list[list[str]]] = {}
    for category, alternatives in patterns.items():
        hits = []
        for terms in alternatives:
            if all(term in low for term in terms):
                hits.append(list(terms))
        if hits:
            out[category] = hits
    return out


def numeric_page_candidates(text: str) -> dict[str, Any]:
    low = " ".join(text.lower().split())
    out: dict[str, Any] = {}
    if any(term in low for term in ORDER_TERMS):
        amounts = []
        for value, unit in CURRENCY_RE.findall(text):
            amounts.append({"value": value.replace(",", ""), "unit": (unit or "UNSPECIFIED").upper()})
        out["orderbook_numeric_candidate"] = {
            "order_terms": [t for t in ORDER_TERMS if t in low],
            "currency_values_on_page": amounts[:12],
        }
    if any(term in low for term in CAPACITY_TERMS):
        percents = [float(x) for x in PERCENT_RE.findall(text)[:12]]
        capacities = [
            {"value": v.replace(",", ""), "unit": u.upper()}
            for v, u in CAPACITY_VALUE_RE.findall(text)[:12]
        ]
        if percents or capacities:
            out["capacity_numeric_candidate"] = {
                "capacity_terms": [t for t in CAPACITY_TERMS if t in low],
                "percent_values_on_page": percents,
                "capacity_values_on_page": capacities,
            }
    return out


def extract_pdf_candidates(content: bytes) -> tuple[list[dict[str, Any]], int, str | None]:
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(content))
    except Exception as exc:
        return [], 0, type(exc).__name__
    page_count = len(reader.pages)
    candidates = []
    category_counts = Counter()
    for page_idx, page in enumerate(reader.pages[:MAX_PAGES_PER_DOCUMENT], start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        if not text.strip():
            continue
        qualitative = {
            "runway": page_matches(text, RUNWAY_PATTERNS),
            "moat": page_matches(text, MOAT_PATTERNS),
            "optionality": page_matches(text, OPTIONALITY_PATTERNS),
        }
        numeric = numeric_page_candidates(text)
        page_record: dict[str, Any] = {"page": page_idx}
        for family, hits in qualitative.items():
            if hits:
                accepted = {}
                for category, terms in hits.items():
                    if category_counts[(family, category)] < MAX_PAGE_CANDIDATES_PER_CATEGORY:
                        accepted[category] = terms
                        category_counts[(family, category)] += 1
                if accepted:
                    page_record[family] = accepted
        if numeric:
            page_record.update(numeric)
        if len(page_record) > 1:
            candidates.append(page_record)
    return candidates, page_count, None


def main() -> None:
    p = argparse.ArgumentParser(description="Download preselected NSE documents and extract page-level evidence candidates")
    p.add_argument("--candidate-index", type=Path, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--rubric", type=Path, required=True)
    p.add_argument("--triage-lock", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    rubric = json.loads(args.rubric.read_text(encoding="utf-8"))
    triage_lock = json.loads(args.triage_lock.read_text(encoding="utf-8"))
    if protocol.get("status") != "PREREGISTERED_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD":
        raise ExtractionError("protocol not frozen")
    if rubric.get("status") != "LOCKED_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD":
        raise ExtractionError("rubric not frozen")
    allowed_triage_statuses = {
        "LOCKED_AFTER_METADATA_PROBE_BEFORE_DOCUMENT_DOWNLOAD_AND_OUTCOME_LOAD",
        "LOCKED_AFTER_FIRST_EXTRACTION_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD",
    }
    if triage_lock.get("status") not in allowed_triage_statuses:
        raise ExtractionError("download triage not frozen")

    payload = json.loads(args.candidate_index.read_text(encoding="utf-8"))
    index = payload.get("candidate_index") or []
    selected = apply_triage(index, triage_lock)
    session = TECH.OfficialSession(request_budget=len(selected) + 10, timeout=35, sleep_seconds=0.02)

    output_rows = []
    statuses = Counter()
    total_downloaded_bytes = 0
    source_hashes = []
    symbols_with_any_candidate = set()
    qualitative_symbol_counts: dict[str, set[str]] = defaultdict(set)
    numeric_symbol_counts: dict[str, set[str]] = defaultdict(set)

    for i, row in enumerate(selected):
        url = row.get("attachment_url")
        record = {
            "sample_symbol": row.get("sample_symbol"),
            "sample_isin": row.get("sample_isin"),
            "known_at": row.get("known_at"),
            "subject": row.get("subject"),
            "source_url": url,
            "download_priority": row.get("download_priority"),
            "status": None,
            "source_sha256": None,
            "byte_size": None,
            "page_count": None,
            "page_candidates": [],
        }
        try:
            raw = session.request(url, referer="https://www.nseindia.com/companies-listing/corporate-filings-announcements", accept="application/pdf,*/*")
            if len(raw) > MAX_DOCUMENT_BYTES:
                record["status"] = "SKIPPED_OVERSIZE"
                record["byte_size"] = len(raw)
            elif not raw.startswith(b"%PDF"):
                record["status"] = "SKIPPED_NON_PDF"
                record["byte_size"] = len(raw)
            else:
                h = sha256(raw)
                candidates, pages, parse_error = extract_pdf_candidates(raw)
                record["source_sha256"] = h
                record["byte_size"] = len(raw)
                record["page_count"] = pages
                record["page_candidates"] = candidates
                record["status"] = "PARSE_ERROR" if parse_error else "PARSED"
                if parse_error:
                    record["parse_error"] = parse_error
                total_downloaded_bytes += len(raw)
                source_hashes.append(f"{row.get('sample_isin')}|{h}")
                if candidates:
                    symbols_with_any_candidate.add(str(row.get("sample_symbol")))
                    for page in candidates:
                        for family in ("runway", "moat", "optionality"):
                            if page.get(family):
                                qualitative_symbol_counts[family].add(str(row.get("sample_symbol")))
                        if page.get("orderbook_numeric_candidate"):
                            numeric_symbol_counts["orderbook"].add(str(row.get("sample_symbol")))
                        if page.get("capacity_numeric_candidate"):
                            numeric_symbol_counts["capacity"].add(str(row.get("sample_symbol")))
        except Exception as exc:
            record["status"] = "DOWNLOAD_ERROR"
            record["error"] = type(exc).__name__
        statuses[str(record["status"])] += 1
        output_rows.append(record)
        if i + 1 < len(selected):
            time.sleep(0.045)

    report = {
        "version": "rk-mis-documentary-forward-evidence-extraction-v1",
        "anchor_date": protocol["anchor_date"],
        "protocol_sha256": sha256(args.protocol.read_bytes()),
        "rubric_sha256": sha256(args.rubric.read_bytes()),
        "triage_lock_sha256": sha256(args.triage_lock.read_bytes()),
        "input_candidate_index_sha256": sha256(args.candidate_index.read_bytes()),
        "selected_symbols": len({x["sample_symbol"] for x in selected}),
        "selected_documents": len(selected),
        "document_status_counts": dict(sorted(statuses.items())),
        "total_downloaded_bytes": total_downloaded_bytes,
        "symbols_with_any_page_candidate": len(symbols_with_any_candidate),
        "symbols_with_qualitative_candidates": {k: len(v) for k, v in sorted(qualitative_symbol_counts.items())},
        "symbols_with_numeric_candidates": {k: len(v) for k, v in sorted(numeric_symbol_counts.items())},
        "source_hash_chain_sha256": sha256("\n".join(sorted(source_hashes)).encode("utf-8")),
        "future_outcome_prices_loaded": False,
        "raw_pdf_files_published": False,
        "raw_pdf_text_published": False,
        "automated_candidates_are_scores": False,
        "next_gate": "REVIEW_PAGE_CANDIDATES_AGAINST_FROZEN_RUBRIC_BEFORE_SCORING_OR_OUTCOME_LOAD",
    }
    result = {
        "report": report,
        "documents": output_rows,
        "review_state": "ALL_CANDIDATES_PENDING_REVIEW",
        "raw_document_text_included": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "claim_candidates.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
