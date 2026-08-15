from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
REFERER = "https://www.bseindia.com/corporates/ann.html"
USER_AGENT = "Mozilla/5.0 (compatible; RK-MIS-Bounded-Documentary-Probe/1.0; shortlist-only-research)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_bse_watchlist(path: Path, maximum: int = 20) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("bse_adapter_required") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("watchlist does not contain bse_adapter_required")
    out = []
    seen = set()
    for row in rows:
        code = str(row.get("scrip_code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            raise ValueError(f"invalid BSE scrip code {code!r}")
        if code in seen:
            continue
        seen.add(code)
        out.append({
            "scrip_code": code,
            "symbol": str(row.get("symbol") or "").strip().upper(),
            "company_name": str(row.get("company_name") or "").strip(),
        })
    if not out or len(out) > maximum:
        raise ValueError(f"BSE watchlist size outside 1..{maximum}")
    return out


def request_json(url: str) -> tuple[Any, dict[str, Any]]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": REFERER,
        "Origin": "https://www.bseindia.com",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            final = resp.geturl()
            host = (urllib.parse.urlparse(final).hostname or "").lower()
            if not (host == "bseindia.com" or host.endswith(".bseindia.com")):
                raise RuntimeError("BSE API redirected outside official BSE domain")
            return json.loads(raw.decode("utf-8-sig")), {
                "status": "OK",
                "http_status": getattr(resp, "status", None),
                "sha256": sha256(raw),
                "bytes": len(raw),
            }
    except Exception as exc:
        return None, {"status": "ERROR", "error": type(exc).__name__}


def rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("Table", "table", "Data", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def build_url(code: str, start: str, end: str, style: str) -> str:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    if style == "yyyymmdd":
        f, t = d0.strftime("%Y%m%d"), d1.strftime("%Y%m%d")
    elif style == "ddmmyyyy":
        f, t = d0.strftime("%d/%m/%Y"), d1.strftime("%d/%m/%Y")
    else:
        raise ValueError(style)
    query = urllib.parse.urlencode({
        "pageno": "1",
        "strCat": "-1",
        "strPrevDate": f,
        "strScrip": code,
        "strSearch": "P",
        "strToDate": t,
        "strType": "C",
        "subcategory": "-1",
    })
    return API + "?" + query


def norm_key_map(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).strip().upper(): v for k, v in row.items()}


def classify_rows(rows: list[dict[str, Any]], code: str) -> dict[str, Any]:
    subjects = []
    field_counts: Counter[str] = Counter()
    attachment_fields: Counter[str] = Counter()
    code_matches = 0
    likely_attachment_rows = 0
    for raw in rows:
        row = norm_key_map(raw)
        for key in row:
            field_counts[key] += 1
        row_code = str(row.get("SCRIP_CD") or row.get("SCRIP_CODE") or row.get("SCRIPCD") or "").strip()
        if row_code == code:
            code_matches += 1
        subject = str(row.get("NEWSSUB") or row.get("HEADLINE") or row.get("SUBJECT") or "").strip()
        if subject:
            subjects.append(subject[:160])
        has_attachment = False
        for key in row:
            if any(token in key for token in ("ATTACH", "NSURL", "URL", "FILE")):
                value = row.get(key)
                if value not in (None, "", "-"):
                    attachment_fields[key] += 1
                    has_attachment = True
        likely_attachment_rows += int(has_attachment)
    return {
        "rows": len(rows),
        "rows_matching_requested_scrip_code": code_matches,
        "rows_with_attachment_or_url_metadata": likely_attachment_rows,
        "attachment_field_counts": dict(sorted(attachment_fields.items())),
        "field_names": sorted(field_counts),
        "subject_samples": sorted(set(subjects))[:12],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Bounded metadata-only probe of official BSE corporate announcements")
    p.add_argument("--watchlist", type=Path, required=True)
    p.add_argument("--from-date", required=True)
    p.add_argument("--to-date", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    start, end = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
    if end < start or (end - start).days > 120:
        raise ValueError("BSE probe window must be 0..120 days")
    watchlist = load_bse_watchlist(args.watchlist)
    results = []
    total_requests = 0
    for member in watchlist:
        attempts = []
        accepted = None
        for style in ("yyyymmdd", "ddmmyyyy"):
            url = build_url(member["scrip_code"], args.from_date, args.to_date, style)
            payload, meta = request_json(url)
            total_requests += 1
            rows = rows_from_payload(payload)
            attempt = {
                "date_style": style,
                "request_status": meta,
                **classify_rows(rows, member["scrip_code"]),
            }
            attempts.append(attempt)
            if meta.get("status") == "OK" and rows:
                accepted = attempt
                break
        results.append({
            **member,
            "accepted_attempt": accepted,
            "attempts": attempts,
        })

    report = {
        "version": "rk-mis-bse-documentary-metadata-probe-v1",
        "source_domain": "api.bseindia.com",
        "endpoint": API,
        "window": {"from": args.from_date, "to": args.to_date},
        "watchlist_rows": len(watchlist),
        "official_requests_made": total_requests,
        "results": results,
        "raw_announcement_payload_published": False,
        "raw_attachment_published": False,
        "future_prices_loaded": False,
        "automatic_score_created": False,
        "purpose": "ACQUISITION_COMPATIBILITY_ONLY",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
