from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import time
import urllib.parse
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

BASE = load_module("prospective_doc_base", "discover_prospective_nse_documentary.py")
PROBE = BASE.PROBE
EXTRACT = BASE.EXTRACT
TECH = BASE.TECH

ENDPOINT = "https://www.nseindia.com/api/corporate-announcements"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_sme_announcements(session: Any, symbol: str, start: str, end: str):
    query = urllib.parse.urlencode({
        "index": "sme",
        "symbol": symbol,
        "from_date": date.fromisoformat(start).strftime("%d-%m-%Y"),
        "to_date": date.fromisoformat(end).strftime("%d-%m-%Y"),
    })
    url = ENDPOINT + "?" + query
    try:
        payload = session.json(url, referer=PROBE.REFERER)
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        rows = PROBE.payload_rows(payload)
        normalized = [PROBE.normalize_announcement(r) for r in rows]
        normalized = [r for r in normalized if r.get("symbol") == symbol]
        return normalized, {"url": url, "status": "OK", "rows": len(normalized), "sha256": sha256(raw)}
    except Exception as exc:
        return [], {"url": url, "status": "ERROR", "rows": 0, "error": type(exc).__name__}


def main() -> None:
    p = argparse.ArgumentParser(description="Bounded current NSE SME primary-document discovery for RK-MIS")
    p.add_argument("--watchlist", type=Path, required=True)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--from-date", required=True)
    p.add_argument("--to-date", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    bounds = policy["bounds"]
    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    if end < start or (end - start).days > int(bounds["maximum_lookback_days"]):
        raise BASE.DiscoveryError("invalid bounded date window")

    watchlist = BASE.load_watchlist(args.watchlist, int(bounds["maximum_symbols_per_run"]))
    max_docs = int(bounds["maximum_documents_per_symbol"])
    session = TECH.OfficialSession(request_budget=max(30, len(watchlist) * (max_docs + 3) + 10), timeout=35, sleep_seconds=0.03)

    documents = []
    queue = []
    announcement_status = Counter()
    document_status = Counter()
    announcement_rows_by_symbol = {}
    selected_docs_by_symbol = {}
    source_hashes = []

    for idx, member in enumerate(watchlist):
        rows, meta = fetch_sme_announcements(session, member["symbol"], args.from_date, args.to_date)
        announcement_status[meta["status"]] += 1
        announcement_rows_by_symbol[member["symbol"]] = len(rows)
        selected = PROBE.select_candidates(rows, policy, args.to_date, max_per_symbol=12)
        selected = [r for r in selected if EXTRACT.triage_included(r)]
        selected.sort(key=lambda r: (EXTRACT.download_priority(r), r.get("known_at") or "", r.get("subject") or ""), reverse=True)
        selected = selected[:max_docs]
        selected_docs_by_symbol[member["symbol"]] = len(selected)

        for candidate in selected:
            record = {
                "symbol": member["symbol"],
                "isin": member.get("isin") or candidate.get("isin"),
                "company_name": member.get("company_name") or candidate.get("company_name"),
                "known_at": candidate.get("known_at"),
                "subject": candidate.get("subject"),
                "source_url": candidate.get("attachment_url"),
                "document_type": BASE.infer_document_type(candidate.get("subject")),
                "source_sha256": None,
                "page_count": None,
                "status": None,
                "page_candidates": [],
            }
            try:
                raw = session.request(record["source_url"], referer=PROBE.REFERER, accept="application/pdf,*/*")
                if len(raw) > int(bounds["maximum_document_bytes"]):
                    record["status"] = "SKIPPED_OVERSIZE"
                elif not raw.startswith(b"%PDF"):
                    record["status"] = "SKIPPED_NON_PDF"
                else:
                    digest = sha256(raw)
                    page_candidates, pages, parse_error = EXTRACT.extract_pdf_candidates(raw)
                    record["source_sha256"] = digest
                    record["page_count"] = pages
                    record["page_candidates"] = page_candidates
                    record["status"] = "PARSE_ERROR" if parse_error else "PARSED"
                    if parse_error:
                        record["parse_error"] = parse_error
                    source_hashes.append(f"{member['symbol']}|{digest}")
                    if page_candidates:
                        queue.extend(BASE.flatten_page_candidates(record))
            except Exception as exc:
                record["status"] = "DOWNLOAD_ERROR"
                record["error"] = type(exc).__name__
            document_status[str(record["status"])] += 1
            documents.append({k: v for k, v in record.items() if k != "page_candidates"})
        if idx + 1 < len(watchlist):
            time.sleep(0.08)

    queue.sort(key=lambda r: (r["symbol"], r.get("known_at") or "", r["page"], r["evidence_family"], r["evidence_category"]))
    report = {
        "version": "rk-mis-prospective-nse-sme-documentary-discovery-v2",
        "segment": "sme",
        "window": {"from": args.from_date, "to": args.to_date},
        "watchlist_symbols": len(watchlist),
        "announcement_request_status": dict(sorted(announcement_status.items())),
        "announcement_rows_by_symbol": announcement_rows_by_symbol,
        "selected_documents_by_symbol": selected_docs_by_symbol,
        "documents_considered": len(documents),
        "document_status_counts": dict(sorted(document_status.items())),
        "pending_review_candidates": len(queue),
        "candidate_family_counts": dict(sorted(Counter(r["evidence_family"] for r in queue).items())),
        "source_hash_chain_sha256": sha256("\n".join(sorted(source_hashes)).encode("utf-8")),
        "official_requests_made": session.requests_made,
        "automated_candidates_are_scores": False,
        "review_required_before_scoring": True,
        "raw_pdf_published": False,
        "raw_pdf_text_published": False,
        "official_100_point_score_mutated": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "review_queue.json").write_text(json.dumps({
        "version": "rk-mis-prospective-documentary-review-queue-v2",
        "review_state": "ALL_PENDING",
        "candidates": queue,
        "raw_document_text_included": False,
        "automatic_score_included": False,
    }, indent=2), encoding="utf-8")
    (args.output / "document_manifest.json").write_text(json.dumps({
        "documents": documents,
        "raw_document_bytes_included": False,
        "raw_document_text_included": False,
    }, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
