from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.rk_mie import build_rk_mie


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RK Multibagger Intelligence Engine")
    parser.add_argument("--input", type=Path, default=ROOT / "data/input/company_intelligence.csv")
    parser.add_argument("--settings", type=Path, default=ROOT / "config/rk_mie_scoring.json")
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "data/processed")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    results, quality = build_rk_mie(args.input, args.settings, args.previous)
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
