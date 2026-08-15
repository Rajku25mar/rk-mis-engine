from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


BSE = load("bse_probe", "probe_bse_documentary_metadata.py")
NSE_DISCOVERY = load("nse_discovery", "discover_prospective_nse_documentary.py")
EXTRACT = NSE_DISCOVERY.EXTRACT


class DiscoveryError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def official_bse_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        p = urllib.parse.urlparse(str(url))
    except Exception:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == "bseindia.com" or host.endswith(".bseindia.com"))


def normalize_timestamp(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    text = str(value).strip()
    epoch = re.search(r"/Date\((\d{10,13})", text)
    if epoch:
        number = int(epoch.group(1))
        if number > 10_000_000_000:
            number //= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")
    clean = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(clean)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S",
        "%d %b %Y %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d",
        "%d/%m/%Y", "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    return None


def known_at(row: dict[str, Any]) -> str | None:
    r = {str(k).upper(): v for k, v in row.items()}
    for key in ("NEWS_SUBMISSION_DT", "DISSEMDT", "NEWS_DT", "DT_TM"):
        value = normalize_timestamp(r.get(key))
        if value:
            return value
    return None


def subject_text(row: dict[str, Any]) -> str:
    r = {str(k).upper(): v for k, v in row.items()}
    parts = [r.get("NEWSSUB"), r.get("HEADLINE"), r.get("CATEGORYNAME"), r.get("SUBCATNAME")]
    return " | ".join(str(x).strip() for x in parts if x not in (None, "", "-"))


def priority(row: dict[str, Any], policy: dict[str, Any]) -> tuple[int, list[str]]:
    text = subject_text(row).lower()
    hits = []
    for fragment in policy["discovery"]["priority_subject_fragments"]:
        if str(fragment).lower() in text:
            hits.append(str(fragment))
    for family, terms in policy["discovery"]["keyword_families"].items():
        for term in terms:
            if str(term).lower() in text:
                hits.append(f"{family}:{term}")
    return (2 if any(x.lower() in text for x in ("award", "receipt of order", "investor presentation", "earnings call transcript")) else 1 if hits else 0), sorted(set(hits))


def build_api_url(code: str, start: str, end: str, page: int) -> str:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    query = urllib.parse.urlencode({
        "pageno": str(page),
        "strCat": "-1",
        "strPrevDate": d0.strftime("%Y%m%d"),
        "strScrip": code,
        "strSearch": "P",
        "strToDate": d1.strftime("%Y%m%d"),
        "strType": "C",
        "subcategory": "-1",
    })
    return BSE.API + "?" + query


def fetch_rows(code: str, start: str, end: str, max_pages: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    seen = set()
    for page in range(1, max_pages + 1):
        url = build_api_url(code, start, end, page)
        payload, req_meta = BSE.request_json(url)
        rows = BSE.rows_from_payload(payload)
        page_added = 0
        for row in rows:
            upper = {str(k).upper(): v for k, v in row.items()}
            row_code = str(upper.get("SCRIP_CD") or "").strip()
            if row_code != code:
                continue
            key = str(upper.get("NEWSID") or upper.get("BSENEWSID") or upper.get("RN") or sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")))
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(row)
            page_added += 1
        meta.append({"page": page, **req_meta, "rows": len(rows), "accepted_rows": page_added})
        if req_meta.get("status") != "OK" or not rows or page_added == 0:
            break
        time.sleep(0.05)
    return all_rows, meta


def attachment_candidates(row: dict[str, Any]) -> list[str]:
    r = {str(k).upper(): v for k, v in row.items()}
    found: list[str] = []
    for key in ("NSURL", "ATTACHMENTNAME"):
        value = str(r.get(key) or "").strip()
        if not value:
            continue
        if value.startswith("https://") and official_bse_url(value):
            found.append(value)
        elif value.startswith("/"):
            found.append("https://www.bseindia.com" + value)
        elif key == "ATTACHMENTNAME":
            name = urllib.parse.quote(value.split("/")[-1])
            found.extend([
                f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}",
                f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{name}",
            ])
    return list(dict.fromkeys(url for url in found if official_bse_url(url)))


def download_pdf(urls: list[str], max_bytes: int) -> tuple[bytes | None, str | None, str | None]:
    last_error = None
    for url in urls:
        req = urllib.request.Request(url, headers={
            "User-Agent": BSE.USER_AGENT,
            "Accept": "application/pdf,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": BSE.REFERER,
        })
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                final = resp.geturl()
                if not official_bse_url(final):
                    last_error = "REDIRECT_OUTSIDE_BSE"
                    continue
                raw = resp.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    last_error = "OVERSIZE"
                    continue
                if not raw.startswith(b"%PDF"):
                    last_error = "NON_PDF"
                    continue
                return raw, final, None
        except Exception as exc:
            last_error = type(exc).__name__
    return None, None, last_error


def main() -> None:
    p = argparse.ArgumentParser(description="Bounded current BSE primary-document discovery for RK-MIS")
    p.add_argument("--watchlist", type=Path, required=True)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--from-date", required=True)
    p.add_argument("--to-date", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    if policy.get("status") != "IMPLEMENTED_BOUNDED_PRIMARY_DOCUMENT_DISCOVERY_POLICY":
        raise DiscoveryError("BSE prospective documentary policy is not active")
    bounds = policy["bounds"]
    start, end = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
    if end < start or (end - start).days > int(bounds["maximum_lookback_days"]):
        raise DiscoveryError("requested BSE window exceeds frozen bounds")
    if end > date.today():
        raise DiscoveryError("to-date cannot be in the future")

    watchlist = BSE.load_bse_watchlist(args.watchlist, int(bounds["maximum_scrips_per_run"]))
    queue: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    api_meta: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    symbols_with_candidates = set()
    source_hashes: list[str] = []

    for member in watchlist:
        rows, pages = fetch_rows(
            member["scrip_code"], args.from_date, args.to_date, int(bounds["maximum_api_pages_per_scrip"])
        )
        api_meta.append({"scrip_code": member["scrip_code"], "pages": pages, "announcement_rows": len(rows)})
        candidates = []
        for row in rows:
            score, hits = priority(row, policy)
            urls = attachment_candidates(row)
            when = known_at(row)
            if score <= 0 or not urls or not when:
                continue
            if when[:10] > args.to_date:
                continue
            candidates.append((score, when, hits, row, urls))
        candidates.sort(key=lambda item: (item[0], item[1], subject_text(item[3])), reverse=True)
        candidates = candidates[: int(bounds["maximum_documents_per_scrip"])]

        for score, when, hits, row, urls in candidates:
            raw, final_url, error = download_pdf(urls, int(bounds["maximum_document_bytes"]))
            record = {
                "scrip_code": member["scrip_code"],
                "symbol": member["symbol"],
                "company_name": member["company_name"],
                "known_at": when,
                "subject": subject_text(row),
                "priority_hits": hits,
                "source_url": final_url,
                "source_sha256": None,
                "document_type": NSE_DISCOVERY.infer_document_type(subject_text(row)),
                "status": None,
                "page_count": None,
            }
            if raw is None:
                record["status"] = "DOWNLOAD_ERROR"
                record["error"] = error
            else:
                digest = sha256(raw)
                page_candidates, pages_count, parse_error = EXTRACT.extract_pdf_candidates(raw)
                if pages_count and pages_count > int(bounds["maximum_pdf_pages"]):
                    record["status"] = "SKIPPED_TOO_MANY_PAGES"
                else:
                    record["source_sha256"] = digest
                    record["page_count"] = pages_count
                    record["status"] = "PARSE_ERROR" if parse_error else "PARSED"
                    if parse_error:
                        record["parse_error"] = parse_error
                    source_hashes.append(f"{member['scrip_code']}|{digest}")
                    if page_candidates:
                        doc = {
                            "symbol": member["symbol"],
                            "isin": None,
                            "company_name": member["company_name"],
                            "known_at": when,
                            "subject": subject_text(row),
                            "source_url": final_url,
                            "source_sha256": digest,
                            "document_type": record["document_type"],
                            "page_candidates": page_candidates,
                        }
                        new_rows = NSE_DISCOVERY.flatten_page_candidates(doc)
                        queue.extend(new_rows)
                        for item in new_rows:
                            family_counts[item["evidence_family"]] += 1
                        if new_rows:
                            symbols_with_candidates.add(member["symbol"])
            statuses[str(record["status"])] += 1
            manifest.append(record)
            time.sleep(0.04)

    queue.sort(key=lambda row: (row["symbol"], row.get("known_at") or "", row["page"], row["evidence_family"], row["evidence_category"]))
    report = {
        "version": "rk-mis-prospective-bse-documentary-discovery-v1",
        "policy_sha256": sha256(args.policy.read_bytes()),
        "watchlist_sha256": sha256(args.watchlist.read_bytes()),
        "window": {"from": args.from_date, "to": args.to_date},
        "watchlist_scrips": len(watchlist),
        "api_metadata": api_meta,
        "documents_considered": len(manifest),
        "document_status_counts": dict(sorted(statuses.items())),
        "symbols_with_review_candidates": len(symbols_with_candidates),
        "pending_review_candidates": len(queue),
        "candidate_family_counts": dict(sorted(family_counts.items())),
        "source_hash_chain_sha256": sha256("\n".join(sorted(source_hashes)).encode("utf-8")),
        "automated_candidates_are_scores": False,
        "review_required_before_scoring": True,
        "raw_api_payload_published": False,
        "raw_pdf_published": False,
        "raw_pdf_text_published": False,
        "official_100_point_score_mutated": False,
        "missing_data_imputed": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "review_queue.json").write_text(json.dumps({
        "version": "rk-mis-prospective-bse-documentary-review-queue-v1",
        "generated_for_window": report["window"],
        "review_state": "ALL_PENDING",
        "candidates": queue,
        "raw_document_text_included": False,
        "automatic_score_included": False,
    }, indent=2), encoding="utf-8")
    (args.output / "document_manifest.json").write_text(json.dumps({
        "documents": manifest,
        "raw_document_bytes_included": False,
        "raw_document_text_included": False,
    }, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
