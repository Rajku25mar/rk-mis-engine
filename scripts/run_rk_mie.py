from __future__ import annotations

import argparse
import csv
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
from multibagger_pipeline.rk_mie import build_rk_mie


def prospective_rows(documentary_db: Path | None, smart_money_db: Path | None, as_of: str) -> list[dict]:
    documentary = [] if documentary_db is None else ProspectiveDocumentEvidenceStore(documentary_db).derive_all(as_of_date=as_of)
    smart_money = [] if smart_money_db is None else ProspectiveSmartMoneyStore(smart_money_db).derive_all(as_of_date=as_of)
    return combine_prospective_sources(documentary, smart_money)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RK Multibagger Intelligence Engine")
    parser.add_argument("--input", type=Path, default=ROOT / "data/input/company_intelligence.csv")
    parser.add_argument("--settings", type=Path, default=ROOT / "config/rk_mie_scoring.json")
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "data/processed")
    parser.add_argument("--documentary-db", type=Path, default=None, help="Optional prospective documentary evidence SQLite database")
    parser.add_argument("--smart-money-db", type=Path, default=None, help="Optional prospective Smart Money evidence SQLite database")
    parser.add_argument("--as-of", default=None, help="Point-in-time date required when prospective evidence DBs are supplied")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    input_path = args.input
    prospective_report = None
    if args.documentary_db is not None or args.smart_money_db is not None:
        if not args.as_of:
            raise ValueError("--as-of is required when prospective evidence DBs are supplied")
        base_rows = read_csv(args.input)
        p_rows = prospective_rows(args.documentary_db, args.smart_money_db, args.as_of)
        merged_rows, prospective_report = merge_prospective_features(base_rows, p_rows)
        prospective_report.update({
            "as_of_date": args.as_of,
            "documentary_db_supplied": args.documentary_db is not None,
            "smart_money_db_supplied": args.smart_money_db is not None,
            "scoring_started": prospective_report["conflict_feature_cells"] == 0,
        })
        write_report(args.output / "prospective_merge_report.json", prospective_report)
        if prospective_report["conflict_feature_cells"]:
            raise RuntimeError(
                f"prospective feature conflicts detected: {prospective_report['conflict_feature_cells']}; "
                "scoring stopped before RK-MIS result generation"
            )
        input_path = args.output / "company_intelligence_with_prospective_evidence.csv"
        write_csv(input_path, merged_rows)

    results, quality = build_rk_mie(input_path, args.settings, args.previous)
    if prospective_report is not None:
        quality = {**quality, "prospective_evidence_merge": prospective_report}
    payload = {"quality": quality, "results": results}
    (args.output / "rk_mie_latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    fields = [
        "symbol", "company_name", "sector", "rk_mie_score", "data_coverage",
        "classification", "thesis_trend", "funnel_tier", "hard_red_flags",
        "warning_flags", "10x", "20x", "50x"
    ]
    with (args.output / "rk_mie_latest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in results:
            writer.writerow({
                "symbol": row["symbol"],
                "company_name": row["company_name"],
                "sector": row["sector"],
                "rk_mie_score": row["rk_mie_score"],
                "data_coverage": row["data_coverage"],
                "classification": row["classification"],
                "thesis_trend": row["thesis_trend"],
                "funnel_tier": row["funnel_tier"],
                "hard_red_flags": "|".join(row["hard_red_flags"]),
                "warning_flags": "|".join(row["warning_flags"]),
                "10x": row["feasibility"]["targets"]["10x"]["assessment"],
                "20x": row["feasibility"]["targets"]["20x"]["assessment"],
                "50x": row["feasibility"]["targets"]["50x"]["assessment"],
            })
    print(json.dumps(quality, indent=2))


if __name__ == "__main__":
    main()
