from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_catalyst_loader import load_reviewed_catalyst_ledgers
from multibagger_pipeline.prospective_catalysts import ProspectiveCatalystStore
from multibagger_pipeline.prospective_documentary import ProspectiveDocumentEvidenceStore
from multibagger_pipeline.prospective_feature_bridge import (
    combine_prospective_sources,
    merge_prospective_features,
    read_csv,
    write_csv,
    write_report,
)
from multibagger_pipeline.prospective_ledger_loader import load_reviewed_documentary_ledgers
from multibagger_pipeline.prospective_smart_money import ProspectiveSmartMoneyStore
from multibagger_pipeline.rk_mie import build_rk_mie


def _number(row: dict[str, Any], *fields: str) -> float | None:
    for field in fields:
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            continue
    return None


def _base_row_for_identity(base_rows: list[dict[str, Any]], isin: str | None, symbol: str | None) -> dict[str, Any] | None:
    isin_key = str(isin or "").strip().upper()
    symbol_key = str(symbol or "").strip().upper()
    if isin_key:
        matches = [row for row in base_rows if str(row.get("isin") or "").strip().upper() == isin_key]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"duplicate base ISIN {isin_key}")
    if symbol_key:
        matches = [row for row in base_rows if str(row.get("symbol") or "").strip().upper() == symbol_key]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"ambiguous base symbol {symbol_key}")
    return None


def catalyst_feature_rows(
    store: ProspectiveCatalystStore,
    base_rows: list[dict[str, Any]],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in store.identities():
        base = _base_row_for_identity(base_rows, item.get("isin"), item.get("symbol"))
        ttm_revenue = None
        if base is not None:
            ttm_revenue = _number(base, "ttm_revenue_cr", "revenue_ttm_cr", "sales_ttm_cr")
        result = store.derive_features(
            isin=item.get("isin"),
            symbol=item.get("symbol"),
            as_of_date=as_of,
            ttm_revenue_cr=ttm_revenue,
        )
        result["ttm_revenue_from_base_input"] = ttm_revenue
        rows.append(result)
    return rows


def prospective_rows(
    documentary_db: Path | None,
    smart_money_db: Path | None,
    catalyst_db: Path | None,
    base_rows: list[dict[str, Any]],
    as_of: str,
) -> list[dict]:
    documentary = [] if documentary_db is None else ProspectiveDocumentEvidenceStore(documentary_db).derive_all(as_of_date=as_of)
    smart_money = [] if smart_money_db is None else ProspectiveSmartMoneyStore(smart_money_db).derive_all(as_of_date=as_of)
    catalyst = [] if catalyst_db is None else catalyst_feature_rows(
        ProspectiveCatalystStore(catalyst_db), base_rows, as_of=as_of
    )
    return combine_prospective_sources(documentary, smart_money, catalyst)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RK Multibagger Intelligence Engine")
    parser.add_argument("--input", type=Path, default=ROOT / "data/input/company_intelligence.csv")
    parser.add_argument("--settings", type=Path, default=ROOT / "config/rk_mie_scoring.json")
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "data/processed")
    parser.add_argument("--documentary-db", type=Path, default=None, help="Optional prospective documentary evidence SQLite database")
    parser.add_argument(
        "--documentary-ledger", type=Path, nargs="+", default=None,
        help="Reviewed prospective documentary JSON ledgers loaded into a temporary point-in-time store before scoring",
    )
    parser.add_argument("--smart-money-db", type=Path, default=None, help="Optional prospective Smart Money evidence SQLite database")
    parser.add_argument("--catalyst-db", type=Path, default=None, help="Optional reviewed prospective catalyst SQLite database")
    parser.add_argument(
        "--catalyst-ledger", type=Path, nargs="+", default=None,
        help="Reviewed prospective order/capex catalyst JSON ledgers loaded into a temporary point-in-time store",
    )
    parser.add_argument("--as-of", default=None, help="Point-in-time date required when prospective evidence is supplied")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    if args.documentary_db is not None and args.documentary_ledger:
        raise ValueError("supply either --documentary-db or --documentary-ledger, not both")
    if args.catalyst_db is not None and args.catalyst_ledger:
        raise ValueError("supply either --catalyst-db or --catalyst-ledger, not both")

    temp_dirs: list[tempfile.TemporaryDirectory] = []
    documentary_db = args.documentary_db
    catalyst_db = args.catalyst_db
    documentary_ledger_report = None
    catalyst_ledger_report = None

    try:
        if args.documentary_ledger:
            if not args.as_of:
                raise ValueError("--as-of is required when --documentary-ledger is supplied")
            temp = tempfile.TemporaryDirectory(prefix="rk-mis-documentary-")
            temp_dirs.append(temp)
            documentary_db = Path(temp.name) / "prospective_documentary.sqlite"
            documentary_ledger_report = load_reviewed_documentary_ledgers(
                ProspectiveDocumentEvidenceStore(documentary_db),
                args.documentary_ledger,
                scoring_as_of=args.as_of,
            )

        if args.catalyst_ledger:
            if not args.as_of:
                raise ValueError("--as-of is required when --catalyst-ledger is supplied")
            temp = tempfile.TemporaryDirectory(prefix="rk-mis-catalyst-")
            temp_dirs.append(temp)
            catalyst_db = Path(temp.name) / "prospective_catalysts.sqlite"
            catalyst_ledger_report = load_reviewed_catalyst_ledgers(
                ProspectiveCatalystStore(catalyst_db),
                args.catalyst_ledger,
                scoring_as_of=args.as_of,
            )

        input_path = args.input
        prospective_report = None
        prospective_supplied = documentary_db is not None or args.smart_money_db is not None or catalyst_db is not None
        if prospective_supplied:
            if not args.as_of:
                raise ValueError("--as-of is required when prospective evidence is supplied")
            base_rows = read_csv(args.input)
            p_rows = prospective_rows(documentary_db, args.smart_money_db, catalyst_db, base_rows, args.as_of)
            merged_rows, prospective_report = merge_prospective_features(base_rows, p_rows)
            prospective_report.update({
                "as_of_date": args.as_of,
                "documentary_db_supplied": args.documentary_db is not None,
                "documentary_ledgers_supplied": 0 if not args.documentary_ledger else len(args.documentary_ledger),
                "documentary_ledger_load": documentary_ledger_report,
                "smart_money_db_supplied": args.smart_money_db is not None,
                "catalyst_db_supplied": args.catalyst_db is not None,
                "catalyst_ledgers_supplied": 0 if not args.catalyst_ledger else len(args.catalyst_ledger),
                "catalyst_ledger_load": catalyst_ledger_report,
                "orderbook_to_sales_denominator_rule": "EXPLICIT_TTM_REVENUE_FIELD_ONLY",
                "accepted_ttm_revenue_fields": ["ttm_revenue_cr", "revenue_ttm_cr", "sales_ttm_cr"],
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
    finally:
        for temp in temp_dirs:
            temp.cleanup()


if __name__ == "__main__":
    main()
