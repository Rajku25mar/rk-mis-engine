from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_smart_money import ProspectiveSmartMoneyStore
from multibagger_pipeline.upstox_smart_money import ingest_upstox_raw_directory


def _companies(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("companies") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("universe JSON must be a company list or an object with a companies list")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest current Upstox shareholding snapshots into the RK-MIS prospective Smart Money journal")
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--known-at", help="Capture date YYYY-MM-DD; defaults to current UTC date")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    known_at = args.known_at or datetime.now(UTC).date().isoformat()
    store = ProspectiveSmartMoneyStore(args.db)
    report = ingest_upstox_raw_directory(
        raw_root=args.raw_root,
        companies=_companies(args.universe),
        store=store,
        known_at=known_at,
    )
    report["journal_db"] = str(args.db)
    report["policy"] = {
        "provider_historical_quarters_backfilled": False,
        "only_latest_currently_observable_quarter_journaled": True,
        "other_dii_mapped_to_institutional_breadth": False,
        "4q_feature_requires_five_point_in_time_periods": True,
        "4q_span_days": "330-400",
        "scoring_eligible_source_grades": ["A", "B"],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
