from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


DISCOVERY = load("prospective_discovery", "discover_prospective_nse_documentary.py")
PROBE = DISCOVERY.PROBE
TECH = DISCOVERY.TECH


def main() -> None:
    p = argparse.ArgumentParser(description="Metadata-only diagnosis for bounded NSE documentary discovery")
    p.add_argument("--watchlist", type=Path, required=True)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--from-date", required=True)
    p.add_argument("--to-date", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    watchlist = DISCOVERY.load_watchlist(args.watchlist, int(policy["bounds"]["maximum_symbols_per_run"]))
    session = TECH.OfficialSession(request_budget=max(20, len(watchlist) * 3 + 5), timeout=30, sleep_seconds=0.03)
    diagnostics: list[dict[str, Any]] = []

    for member in watchlist:
        rows, meta = PROBE.fetch_symbol_announcements(session, member["symbol"], args.from_date, args.to_date)
        with_attachment = [r for r in rows if PROBE.official_attachment(r.get("attachment_url"))]
        priority = [r for r in with_attachment if PROBE.priority_subject_match(r.get("subject") or "", policy)]
        keyword = [r for r in with_attachment if PROBE.keyword_hits(r, policy)]
        selected = PROBE.select_candidates(rows, policy, args.to_date, max_per_symbol=12)
        diagnostics.append({
            "symbol": member["symbol"],
            "request_status": meta.get("status"),
            "rows": len(rows),
            "rows_with_official_attachment": len(with_attachment),
            "rows_priority_subject": len(priority),
            "rows_keyword_match": len(keyword),
            "selected_before_pdf_triage": len(selected),
            "subject_samples": sorted({str(r.get("subject") or "")[:120] for r in rows if r.get("subject")})[:8],
            "attachment_shape_counts": {
                "absolute_https": sum(str(r.get("attachment_url") or "").startswith("https://") for r in rows),
                "missing": sum(not r.get("attachment_url") for r in rows),
            },
        })

    report = {
        "version": "rk-mis-prospective-nse-announcement-diagnostic-v1",
        "window": {"from": args.from_date, "to": args.to_date},
        "symbols": diagnostics,
        "official_requests_made": session.requests_made,
        "future_prices_loaded": False,
        "raw_document_text_loaded": False,
        "score_mutated": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
