from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.rk_mie import build_rk_mie, required_cagr, score_company


class RKMieTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = json.loads((ROOT / "config/rk_mie_scoring.json").read_text())

    def strong_row(self) -> dict[str, str]:
        row = {"symbol": "GEM", "company_name": "Evidence Gem", "sector": "Industrial"}
        for features in self.settings["pillar_features"].values():
            for spec in features:
                field = spec["field"]
                direction = spec["direction"]
                thresholds = [float(b[0]) for b in spec["bands"]]
                row[field] = str(max(thresholds) + 5 if direction == "higher" else min(thresholds))
        row.update({
            "current_price": "100",
            "current_eps_ttm": "5",
            "shares_cr": "10",
            "terminal_pe_assumption": "25",
            "sustainable_net_margin_pct": "12",
            "estimated_tam_cr_at_horizon": "50000",
            "planned_revenue_capacity_cr_at_horizon": "12000"
        })
        return row

    def test_return_cagr_math(self) -> None:
        self.assertAlmostEqual(required_cagr(10, 10), 25.89, places=2)
        self.assertAlmostEqual(required_cagr(20, 10), 34.93, places=2)
        self.assertAlmostEqual(required_cagr(50, 10), 47.88, places=2)

    def test_weights_sum_to_100(self) -> None:
        self.assertEqual(sum(self.settings["pillar_weights"].values()), 100)

    def test_hard_governance_flag_overrides_score(self) -> None:
        row = self.strong_row()
        row["qualified_audit_opinion"] = "1"
        result = score_company(row, self.settings)
        self.assertGreaterEqual(result["rk_mie_score"], 85)
        self.assertEqual(result["classification"], "AVOID")
        self.assertIn("QUALIFIED_AUDIT_OPINION", result["hard_red_flags"])

    def test_missing_evidence_reduces_coverage(self) -> None:
        result = score_company({"symbol": "EMPTY"}, self.settings)
        self.assertEqual(result["rk_mie_score"], 0)
        self.assertEqual(result["data_coverage"], 0)
        self.assertEqual(result["classification"], "INSUFFICIENT_EVIDENCE")

    def test_build_assigns_diamond_funnel(self) -> None:
        row = self.strong_row()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            fields = sorted(row.keys())
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            results, quality = build_rk_mie(path, ROOT / "config/rk_mie_scoring.json")
        self.assertEqual(quality["companies_scored"], 1)
        self.assertEqual(results[0]["funnel_tier"], "TOP_10_RK_DIAMOND")


if __name__ == "__main__":
    unittest.main()
