from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_documentary import ProspectiveDocumentEvidenceStore
from multibagger_pipeline.prospective_feature_bridge import (
    combine_prospective_sources,
    merge_prospective_features,
    read_csv,
    write_csv,
    write_report,
)
from multibagger_pipeline.prospective_smart_money import ProspectiveSmartMoneyStore


def documentary_rows(path: Path | None, as_of: str) -> list[dict]:
    if path is None:
        return []
    store = ProspectiveDocumentEvidenceStore(path)
    return store.derive_all(as_of_date=as_of)


def smart_money_rows(path: Path | None, as_of: str) -> list[dict]:
    if path is None:
        return []
    store = ProspectiveSmartMoneyStore(path)
    return store.derive_all(as_of_date=as_of)


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge qualified prospective evidence into RK-MIS input without overwriting conflicts")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--documentary-db", type=Path)
    parser.add_argument("--smart-money-db", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-conflicts", action="store_true", help="Write output and return success even when conflicting pre-existing feature values are detected")
    args = parser.parse_args()

    base = read_csv(args.input)
    doc = documentary_rows(args.documentary_db, args.as_of)
    smart = smart_money_rows(args.smart_money_db, args.as_of)
    prospective = combine_prospective_sources(doc, smart)
    merged, report = merge_prospective_features(base, prospective)
    report.update({
        "as_of_date": args.as_of,
        "documentary_identities": len(doc),
        "smart_money_identities": len(smart),
        "combined_prospective_identities": len(prospective),
        "output_written": True,
        "conflict_policy": "FAIL_EXIT_BY_DEFAULT_OUTPUT_PRESERVES_EXISTING_VALUES",
    })
    write_csv(args.output, merged)
    write_report(args.report, report)
    print(json.dumps(report, indent=2))
    if report["conflict_feature_cells"] and not args.allow_conflicts:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
