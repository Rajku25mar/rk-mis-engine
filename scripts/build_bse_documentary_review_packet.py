from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


DISC = load("bse_discovery", "discover_prospective_bse_documentary.py")
EXTRACT = DISC.EXTRACT
BSE = DISC.BSE


def clean(text: str) -> str:
    return " ".join((text or "").split())


def excerpt(text: str, needles: list[str], limit: int) -> str:
    flat = clean(text)
    low = flat.lower()
    positions = [low.find(n.lower()) for n in needles if n and low.find(n.lower()) >= 0]
    if not positions:
        return flat[:limit]
    pos = min(positions)
    radius = max(80, limit // 2)
    start = max(0, pos - radius)
    end = min(len(flat), start + limit)
    if end - start < limit and start > 0:
        start = max(0, end - limit)
    snippet = flat[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(flat):
        snippet = snippet.rstrip() + "…"
    return snippet[: limit + 1]


def page_records(text: str, page: int, max_chars: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family_key, family_name, patterns in (
        ("runway", "RUNWAY", EXTRACT.RUNWAY_PATTERNS),
        ("moat", "MOAT", EXTRACT.MOAT_PATTERNS),
        ("optionality", "OPTIONALITY", EXTRACT.OPTIONALITY_PATTERNS),
    ):
        matches = EXTRACT.page_matches(text, patterns)
        for category, alternatives in sorted(matches.items()):
            needles = sorted({term for alt in alternatives for term in alt})
            rows.append({
                "page": page,
                "evidence_family": family_name,
                "evidence_category": category,
                "matched_terms": needles,
                "numeric_candidates": None,
                "context_excerpt": excerpt(text, needles, max_chars),
            })
    numeric = EXTRACT.numeric_page_candidates(text)
    if numeric.get("orderbook_numeric_candidate"):
        item = numeric["orderbook_numeric_candidate"]
        needles = list(item.get("order_terms") or [])
        rows.append({
            "page": page,
            "evidence_family": "ORDER",
            "evidence_category": "order_book",
            "matched_terms": needles,
            "numeric_candidates": item,
            "context_excerpt": excerpt(text, needles, max_chars),
        })
    if numeric.get("capacity_numeric_candidate"):
        item = numeric["capacity_numeric_candidate"]
        needles = list(item.get("capacity_terms") or [])
        rows.append({
            "page": page,
            "evidence_family": "CAPACITY",
            "evidence_category": "capacity",
            "matched_terms": needles,
            "numeric_candidates": item,
            "context_excerpt": excerpt(text, needles, max_chars),
        })
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="Build bounded page-local review excerpts for shortlisted BSE primary documents")
    p.add_argument("--watchlist", type=Path, required=True)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--from-date", required=True)
    p.add_argument("--to-date", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    bounds = policy["bounds"]
    publication = policy["publication"]
    if not publication.get("bounded_review_excerpt_allowed"):
        raise ValueError("policy does not permit bounded review excerpts")
    max_chars = int(bounds["maximum_review_excerpt_chars"])
    max_per_doc = int(bounds["maximum_review_excerpts_per_document"])
    if max_chars > 480:
        raise ValueError("review excerpt limit may not exceed 480 characters")

    start, end = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
    if end < start or (end - start).days > int(bounds["maximum_lookback_days"]):
        raise ValueError("review packet window exceeds frozen bounds")
    if end > date.today():
        raise ValueError("to-date cannot be in the future")

    watchlist = BSE.load_bse_watchlist(args.watchlist, int(bounds["maximum_scrips_per_run"]))
    packet: list[dict[str, Any]] = []
    doc_status: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()

    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError("pypdf is required") from exc

    for member in watchlist:
        rows, _meta = DISC.fetch_rows(
            member["scrip_code"], args.from_date, args.to_date, int(bounds["maximum_api_pages_per_scrip"])
        )
        candidates = []
        for row in rows:
            score, hits = DISC.priority(row, policy)
            urls = DISC.attachment_candidates(row)
            when = DISC.known_at(row)
            if score <= 0 or not urls or not when or when[:10] > args.to_date:
                continue
            candidates.append((score, when, hits, row, urls))
        candidates.sort(key=lambda item: (item[0], item[1], DISC.subject_text(item[3])), reverse=True)
        candidates = candidates[: int(bounds["maximum_documents_per_scrip"])]

        for score, when, priority_hits, row, urls in candidates:
            raw, final_url, error = DISC.download_pdf(urls, int(bounds["maximum_document_bytes"]))
            if raw is None:
                doc_status[f"DOWNLOAD_ERROR:{error}"] += 1
                continue
            digest = DISC.sha256(raw)
            try:
                reader = PdfReader(io.BytesIO(raw))
            except Exception:
                doc_status["PDF_PARSE_ERROR"] += 1
                continue
            if len(reader.pages) > int(bounds["maximum_pdf_pages"]):
                doc_status["SKIPPED_TOO_MANY_PAGES"] += 1
                continue

            candidates_for_doc: list[dict[str, Any]] = []
            for page_no, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    continue
                if not text.strip():
                    continue
                candidates_for_doc.extend(page_records(text, page_no, max_chars))
            candidates_for_doc.sort(key=lambda r: (r["page"], r["evidence_family"], r["evidence_category"]))
            candidates_for_doc = candidates_for_doc[:max_per_doc]
            for item in candidates_for_doc:
                family_counts[item["evidence_family"]] += 1
                packet.append({
                    "scrip_code": member["scrip_code"],
                    "symbol": member["symbol"],
                    "company_name": member["company_name"],
                    "known_at": when,
                    "subject": DISC.subject_text(row),
                    "priority_hits": priority_hits,
                    "source_url": final_url,
                    "source_sha256": digest,
                    **item,
                    "review_state": "PENDING",
                    "score_eligible": False,
                })
            doc_status["PARSED"] += 1

    packet.sort(key=lambda r: (r["symbol"], r["known_at"], r["source_sha256"], r["page"], r["evidence_family"], r["evidence_category"]))
    report = {
        "version": "rk-mis-bse-documentary-review-packet-v1",
        "window": {"from": args.from_date, "to": args.to_date},
        "watchlist_scrips": len(watchlist),
        "documents_status": dict(sorted(doc_status.items())),
        "review_excerpt_rows": len(packet),
        "family_counts": dict(sorted(family_counts.items())),
        "maximum_excerpt_chars": max_chars,
        "full_page_text_published": False,
        "raw_pdf_published": False,
        "automatic_score_created": False,
        "review_required": True,
    }
    if any(len(row["context_excerpt"]) > max_chars + 1 for row in packet):
        raise ValueError("review excerpt exceeded frozen character limit")

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "review_packet.json").write_text(json.dumps({
        "version": "rk-mis-bse-documentary-review-packet-v1",
        "review_state": "ALL_PENDING",
        "rows": packet,
        "full_page_text_included": False,
        "raw_pdf_included": False,
        "automatic_score_included": False,
    }, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
