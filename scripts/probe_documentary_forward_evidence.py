from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime
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

REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
ENDPOINT = "https://www.nseindia.com/api/corporate-announcements"


class ProbeError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def robust_datetime(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    text = str(value).strip()
    fmts = (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d-%b-%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
    )
    for fmt in fmts:
        try:
            dt = datetime.strptime(text[:26], fmt)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            pass
    # Some NSE payloads append timezone fragments or use longer text. Retry common
    # date-only prefixes without accepting an unparseable future timestamp.
    for width, fmt in ((11, "%d-%b-%Y"), (10, "%d-%m-%Y"), (10, "%Y-%m-%d")):
        try:
            return datetime.strptime(text[:width], fmt).isoformat(timespec="seconds")
        except ValueError:
            pass
    return None


def payload_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "rows", "results"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    return []


def first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def normalize_announcement(row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(first(row, "symbol", "SYMBOL") or "").strip().upper()
    isin = str(first(row, "sm_isin", "isin", "ISIN") or "").strip().upper() or None
    company = str(first(row, "sm_name", "companyName", "company_name", "COMPANY NAME") or "").strip() or None
    subject = str(first(row, "desc", "subject", "SUBJECT") or "").strip()
    details = str(first(row, "attchmntText", "details", "description", "DETAILS") or "").strip()
    known = robust_datetime(first(row, "exchdisstime", "an_dt", "broadcastDateTime", "broadcastDate", "sort_date"))
    attachment = str(first(row, "attchmntFile", "attachment", "fileUrl", "ATTACHMENT") or "").strip() or None
    if attachment and attachment.startswith("/"):
        attachment = "https://archives.nseindia.com" + attachment
    return {
        "symbol": symbol,
        "isin": isin,
        "company_name": company,
        "subject": subject,
        "details": details,
        "known_at": known,
        "attachment_url": attachment,
    }


def official_attachment(url: str | None) -> bool:
    if not url or not url.startswith("https://"):
        return False
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in {"nseindia.com", "archives.nseindia.com", "nsearchives.nseindia.com"} or host.endswith(".nseindia.com")


def safe_by_anchor(row: dict[str, Any], anchor: str) -> bool:
    known = row.get("known_at")
    return bool(known and known[:10] <= anchor)


def keyword_hits(row: dict[str, Any], protocol: dict[str, Any]) -> dict[str, list[str]]:
    text = " ".join([row.get("subject") or "", row.get("details") or ""]).lower()
    hits: dict[str, list[str]] = {}
    for family, keywords in protocol["discovery"]["keyword_families"].items():
        found = []
        for kw in keywords:
            pattern = r"(?<![a-z0-9])" + re.escape(str(kw).lower()) + r"(?![a-z0-9])"
            if re.search(pattern, text):
                found.append(str(kw))
        if found:
            hits[family] = found
    return hits


def priority_subject_match(subject: str, protocol: dict[str, Any]) -> bool:
    s = (subject or "").lower()
    return any(x.lower() in s or s in x.lower() for x in protocol["discovery"]["priority_subjects"] if x)


def select_candidates(rows: list[dict[str, Any]], protocol: dict[str, Any], anchor: str, max_per_symbol: int = 12) -> list[dict[str, Any]]:
    candidates = []
    seen = set()
    for row in rows:
        if not row.get("symbol") or not safe_by_anchor(row, anchor) or not official_attachment(row.get("attachment_url")):
            continue
        hits = keyword_hits(row, protocol)
        priority = priority_subject_match(row.get("subject") or "", protocol)
        if not hits and not priority:
            continue
        key = (row["symbol"], row.get("known_at"), row.get("attachment_url"))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "symbol": row["symbol"],
            "isin": row.get("isin"),
            "company_name": row.get("company_name"),
            "known_at": row.get("known_at"),
            "subject": row.get("subject"),
            "attachment_url": row.get("attachment_url"),
            "keyword_families": sorted(hits),
            "keyword_hits": hits,
            "priority_subject_match": priority,
        })
    candidates.sort(
        key=lambda r: (
            len(r["keyword_families"]),
            bool(r["priority_subject_match"]),
            r.get("known_at") or "",
            r.get("subject") or "",
        ),
        reverse=True,
    )
    return candidates[:max_per_symbol]


def fetch_symbol_announcements(session: Any, symbol: str, start: str, end: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = urllib.parse.urlencode({
        "index": "equities",
        "symbol": symbol,
        "from_date": date.fromisoformat(start).strftime("%d-%m-%Y"),
        "to_date": date.fromisoformat(end).strftime("%d-%m-%Y"),
    })
    url = ENDPOINT + "?" + query
    try:
        payload = session.json(url, referer=REFERER)
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        rows = payload_rows(payload)
        normalized = [normalize_announcement(r) for r in rows]
        # Do not trust server-side symbol filtering blindly.
        normalized = [r for r in normalized if r.get("symbol") == symbol]
        return normalized, {"url": url, "status": "OK", "rows": len(normalized), "sha256": sha256(raw)}
    except Exception as exc:
        return [], {"url": url, "status": "ERROR", "rows": 0, "error": type(exc).__name__}


def main() -> None:
    p = argparse.ArgumentParser(description="Probe bounded pre-anchor NSE documentary evidence metadata")
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--rubric", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    rubric = json.loads(args.rubric.read_text(encoding="utf-8"))
    if protocol.get("status") != "PREREGISTERED_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD":
        raise ProbeError("protocol is not frozen in preregistered state")
    if rubric.get("status") != "LOCKED_BEFORE_DOCUMENT_REVIEW_AND_OUTCOME_LOAD":
        raise ProbeError("rubric is not frozen")

    anchor = protocol["anchor_date"]
    start = protocol["document_window"]["from_date"]
    sample_size = int(protocol["sample_size"])
    session = TECH.OfficialSession(request_budget=180, timeout=30, sleep_seconds=0.03)
    resolved_anchor, anchor_raw, anchor_meta = TECH.resolve_on_or_before(anchor, session)
    universe = TECH.company_universe(TECH.parse_bhavcopy(anchor_raw), resolved_anchor)
    sample = TECH.deterministic_market_sample(
        universe,
        sample_size=sample_size,
        salt=protocol["sample_salt"],
        strata_fields=("market_type",),
    )
    if len(sample) != sample_size:
        raise ProbeError(f"sample underflow: expected {sample_size}, got {len(sample)}")

    all_candidates = []
    source_hashes = []
    request_status = Counter()
    announcements_total = 0
    symbols_with_announcements = 0
    symbols_with_candidates = 0
    family_symbols: dict[str, set[str]] = defaultdict(set)

    for i, member in enumerate(sample):
        rows, meta = fetch_symbol_announcements(session, member["symbol"], start, anchor)
        request_status[meta["status"]] += 1
        if meta.get("sha256"):
            source_hashes.append(f"{member['isin']}|{meta['sha256']}")
        announcements_total += len(rows)
        if rows:
            symbols_with_announcements += 1
        candidates = select_candidates(rows, protocol, anchor)
        if candidates:
            symbols_with_candidates += 1
        for candidate in candidates:
            # Anchor sample ISIN is authoritative even if announcement metadata lacks ISIN.
            candidate["sample_isin"] = member["isin"]
            candidate["sample_symbol"] = member["symbol"]
            all_candidates.append(candidate)
            for family in candidate["keyword_families"]:
                family_symbols[family].add(member["symbol"])
        if i + 1 < len(sample):
            time.sleep(0.08)

    all_candidates.sort(key=lambda r: (r["sample_symbol"], r.get("known_at") or "", r.get("subject") or ""))
    report = {
        "version": "rk-mis-documentary-forward-evidence-probe-v1",
        "protocol": str(args.protocol),
        "protocol_sha256": sha256(args.protocol.read_bytes()),
        "rubric": str(args.rubric),
        "rubric_sha256": sha256(args.rubric.read_bytes()),
        "anchor_target": anchor,
        "anchor_resolved_trading_date": resolved_anchor,
        "document_window": [start, anchor],
        "eligible_company_universe_rows": len(universe),
        "sample_rows": len(sample),
        "announcement_rows_returned_for_sample": announcements_total,
        "symbols_with_any_announcement": symbols_with_announcements,
        "symbols_with_candidate_document": symbols_with_candidates,
        "candidate_documents": len(all_candidates),
        "candidate_document_coverage_pct": round(symbols_with_candidates / len(sample) * 100, 2) if sample else 0.0,
        "symbols_by_keyword_family": {k: len(v) for k, v in sorted(family_symbols.items())},
        "request_status_counts": dict(sorted(request_status.items())),
        "metadata_source_hash_chain_sha256": sha256("\n".join(sorted(source_hashes)).encode("utf-8")),
        "future_outcome_prices_loaded": False,
        "documents_downloaded": False,
        "automatic_keyword_hits_are_scores": False,
        "next_gate": "DOWNLOAD_AND_REVIEW_ONLY_IF_METADATA_COVERAGE_IS_SUFFICIENT",
    }
    source_metadata = {
        "anchor_source": anchor_meta,
        "official_requests_made": session.requests_made,
        "candidate_index": all_candidates,
        "raw_announcement_payloads_published": False,
        "raw_documents_published": False,
        "public_content": "DERIVED_CANDIDATE_METADATA_AND_SOURCE_URLS_ONLY",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "candidate_index.json").write_text(json.dumps(source_metadata, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
