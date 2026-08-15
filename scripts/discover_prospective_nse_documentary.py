from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


PROBE = _load_script("rk_mis_document_probe", "probe_documentary_forward_evidence.py")
EXTRACT = _load_script("rk_mis_document_extract", "extract_documentary_forward_evidence_candidates.py")
TECH = PROBE.TECH


class DiscoveryError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_watchlist(path: Path, maximum: int) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("symbols") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise DiscoveryError("watchlist must be a list or object containing a symbols list")
    out: list[dict[str, Any]] = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise DiscoveryError("every watchlist row must be an object")
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            raise DiscoveryError("watchlist row missing symbol")
        if symbol in seen:
            continue
        seen.add(symbol)
        isin = str(raw.get("isin") or "").strip().upper() or None
        out.append({
            "symbol": symbol,
            "isin": isin,
            "company_name": str(raw.get("company_name") or "").strip() or None,
        })
    if not out:
        raise DiscoveryError("watchlist is empty")
    if len(out) > maximum:
        raise DiscoveryError(f"watchlist exceeds maximum symbols per run: {len(out)}>{maximum}")
    return out


def infer_document_type(subject: str | None) -> str:
    text = (subject or "").lower()
    if "investor presentation" in text:
        return "INVESTOR_PRESENTATION"
    if "press release" in text:
        return "PRESS_RELEASE"
    if "con. call" in text or "conference call" in text or "analyst" in text:
        return "CONCALL_OR_INVESTOR_MEET"
    if "annual report" in text:
        return "ANNUAL_REPORT"
    if "order" in text or "award" in text:
        return "ORDER_OR_CONTRACT_UPDATE"
    if "acquisition" in text:
        return "ACQUISITION_UPDATE"
    return "EXCHANGE_ANNOUNCEMENT_ATTACHMENT"


def candidate_id(*parts: Any) -> str:
    text = "|".join("" if x is None else str(x) for x in parts)
    return "PDCQ-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24].upper()


def flatten_page_candidates(document: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    basis_prefix = "BASIS-" + hashlib.sha256(
        f"{document.get('source_sha256')}|{document.get('source_url')}".encode("utf-8")
    ).hexdigest()[:16].upper()
    for page in document.get("page_candidates") or []:
        page_no = int(page.get("page") or 0)
        for family_key, family_name in (("runway", "RUNWAY"), ("moat", "MOAT"), ("optionality", "OPTIONALITY")):
            mapping = page.get(family_key) or {}
            if not isinstance(mapping, dict):
                continue
            for category, term_groups in sorted(mapping.items()):
                terms = sorted({term for group in (term_groups or []) for term in group})
                out.append({
                    "candidate_id": candidate_id(document.get("source_sha256"), page_no, family_name, category),
                    "symbol": document.get("symbol"),
                    "isin": document.get("isin"),
                    "company_name": document.get("company_name"),
                    "known_at": document.get("known_at"),
                    "document_date": (document.get("known_at") or "")[:10] or None,
                    "subject": document.get("subject"),
                    "source_url": document.get("source_url"),
                    "source_sha256": document.get("source_sha256"),
                    "document_type": document.get("document_type"),
                    "page": page_no,
                    "evidence_family": family_name,
                    "evidence_category": category,
                    "matched_terms": terms,
                    "numeric_candidates": None,
                    "basis_key": f"{basis_prefix}-P{page_no}",
                    "review_state": "PENDING",
                    "score_eligible": False,
                    "requires_source_review": True,
                })
        if page.get("orderbook_numeric_candidate"):
            out.append({
                "candidate_id": candidate_id(document.get("source_sha256"), page_no, "ORDER", "order_book"),
                "symbol": document.get("symbol"),
                "isin": document.get("isin"),
                "company_name": document.get("company_name"),
                "known_at": document.get("known_at"),
                "document_date": (document.get("known_at") or "")[:10] or None,
                "subject": document.get("subject"),
                "source_url": document.get("source_url"),
                "source_sha256": document.get("source_sha256"),
                "document_type": document.get("document_type"),
                "page": page_no,
                "evidence_family": "ORDER",
                "evidence_category": "order_book",
                "matched_terms": (page["orderbook_numeric_candidate"].get("order_terms") or []),
                "numeric_candidates": page["orderbook_numeric_candidate"],
                "basis_key": f"{basis_prefix}-P{page_no}",
                "review_state": "PENDING",
                "score_eligible": False,
                "requires_source_review": True,
            })
        if page.get("capacity_numeric_candidate"):
            out.append({
                "candidate_id": candidate_id(document.get("source_sha256"), page_no, "CAPACITY", "capacity"),
                "symbol": document.get("symbol"),
                "isin": document.get("isin"),
                "company_name": document.get("company_name"),
                "known_at": document.get("known_at"),
                "document_date": (document.get("known_at") or "")[:10] or None,
                "subject": document.get("subject"),
                "source_url": document.get("source_url"),
                "source_sha256": document.get("source_sha256"),
                "document_type": document.get("document_type"),
                "page": page_no,
                "evidence_family": "CAPACITY",
                "evidence_category": "capacity",
                "matched_terms": (page["capacity_numeric_candidate"].get("capacity_terms") or []),
                "numeric_candidates": page["capacity_numeric_candidate"],
                "basis_key": f"{basis_prefix}-P{page_no}",
                "review_state": "PENDING",
                "score_eligible": False,
                "requires_source_review": True,
            })
    # One deterministic row per category/page/document.
    dedup = {row["candidate_id"]: row for row in out}
    return [dedup[key] for key in sorted(dedup)]


def main() -> None:
    p = argparse.ArgumentParser(description="Bounded current NSE primary-document discovery for RK-MIS")
    p.add_argument("--watchlist", type=Path, required=True)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--from-date", required=True)
    p.add_argument("--to-date", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    if policy.get("status") != "IMPLEMENTED_BOUNDED_PRIMARY_DOCUMENT_DISCOVERY_POLICY":
        raise DiscoveryError("prospective documentary policy is not active")
    bounds = policy["bounds"]
    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    if end < start:
        raise DiscoveryError("to-date cannot be before from-date")
    if (end - start).days > int(bounds["maximum_lookback_days"]):
        raise DiscoveryError("requested document window exceeds frozen maximum lookback")
    if end > date.today():
        raise DiscoveryError("to-date cannot be in the future")

    watchlist = load_watchlist(args.watchlist, int(bounds["maximum_symbols_per_run"]))
    max_docs = int(bounds["maximum_documents_per_symbol"])
    session = TECH.OfficialSession(
        request_budget=max(30, len(watchlist) * (max_docs + 2) + 10),
        timeout=35,
        sleep_seconds=0.03,
    )

    documents: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    announcement_status = Counter()
    document_status = Counter()
    symbols_with_candidates = set()
    source_hashes: list[str] = []

    for idx, member in enumerate(watchlist):
        rows, meta = PROBE.fetch_symbol_announcements(session, member["symbol"], args.from_date, args.to_date)
        announcement_status[meta["status"]] += 1
        selected = PROBE.select_candidates(rows, policy, args.to_date, max_per_symbol=12)
        selected = [row for row in selected if EXTRACT.triage_included(row)]
        selected.sort(
            key=lambda row: (EXTRACT.download_priority(row), row.get("known_at") or "", row.get("subject") or ""),
            reverse=True,
        )
        selected = selected[:max_docs]

        for candidate in selected:
            url = candidate.get("attachment_url")
            record = {
                "symbol": member["symbol"],
                "isin": member.get("isin") or candidate.get("isin"),
                "company_name": member.get("company_name") or candidate.get("company_name"),
                "known_at": candidate.get("known_at"),
                "subject": candidate.get("subject"),
                "source_url": url,
                "document_type": infer_document_type(candidate.get("subject")),
                "source_sha256": None,
                "page_count": None,
                "status": None,
                "page_candidates": [],
            }
            try:
                raw = session.request(
                    url,
                    referer=PROBE.REFERER,
                    accept="application/pdf,*/*",
                )
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
                        symbols_with_candidates.add(member["symbol"])
                        queue.extend(flatten_page_candidates(record))
            except Exception as exc:
                record["status"] = "DOWNLOAD_ERROR"
                record["error"] = type(exc).__name__
            document_status[str(record["status"])] += 1
            # Persist only metadata in output; never raw PDF bytes or page text.
            documents.append({
                key: value for key, value in record.items()
                if key != "page_candidates"
            })
        if idx + 1 < len(watchlist):
            time.sleep(0.08)

    queue.sort(key=lambda row: (row["symbol"], row.get("known_at") or "", row["page"], row["evidence_family"], row["evidence_category"]))
    report = {
        "version": "rk-mis-prospective-nse-documentary-discovery-v1",
        "policy_sha256": sha256(args.policy.read_bytes()),
        "watchlist_sha256": sha256(args.watchlist.read_bytes()),
        "window": {"from": args.from_date, "to": args.to_date},
        "watchlist_symbols": len(watchlist),
        "announcement_request_status": dict(sorted(announcement_status.items())),
        "documents_considered": len(documents),
        "document_status_counts": dict(sorted(document_status.items())),
        "symbols_with_review_candidates": len(symbols_with_candidates),
        "pending_review_candidates": len(queue),
        "candidate_family_counts": dict(sorted(Counter(row["evidence_family"] for row in queue).items())),
        "source_hash_chain_sha256": sha256("\n".join(sorted(source_hashes)).encode("utf-8")),
        "official_requests_made": session.requests_made,
        "automated_candidates_are_scores": False,
        "review_required_before_scoring": True,
        "raw_pdf_published": False,
        "raw_pdf_text_published": False,
        "official_100_point_score_mutated": False,
        "missing_data_imputed": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "review_queue.json").write_text(json.dumps({
        "version": "rk-mis-prospective-documentary-review-queue-v1",
        "generated_for_window": report["window"],
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
