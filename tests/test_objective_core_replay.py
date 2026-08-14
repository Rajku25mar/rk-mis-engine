from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("objective_core_replay", ROOT / "scripts/run_objective_core_replay.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

CONFIG = json.loads((ROOT / "config/rk_mie_scoring.json").read_text(encoding="utf-8"))
PROTOCOL = json.loads((ROOT / "validation/2023/objective_core_protocol_v1.json").read_text(encoding="utf-8"))


class ObjectiveCoreReplayTests(unittest.TestCase):
    def test_safe_growth_rejects_non_positive_denominator(self) -> None:
        self.assertIsNone(MODULE._safe_growth(10.0, 0.0))
        self.assertIsNone(MODULE._safe_growth(10.0, -5.0))
        self.assertEqual(MODULE._safe_growth(120.0, 100.0), 20.0)

    def test_replay_grade_requires_growth_and_two_quality_metrics(self) -> None:
        row = {
            "isin": "INE000000001",
            "symbol": "TEST",
            "latest_sales_growth_yoy_pct": 25.0,
            "latest_pat_growth_yoy_pct": 30.0,
            "debt_equity": 0.4,
            "interest_coverage": 8.0,
            "ebitda_margin_pct": None,
            "promoter_holding_change_pp_4q": None,
        }
        scored = MODULE._score_snapshot_row(row, CONFIG, PROTOCOL)
        self.assertTrue(scored["objective_core_replay_grade"])
        self.assertIsNotNone(scored["base_objective_core_score"])
        self.assertIsNone(scored["base_plus_promoter_score"])

    def test_one_quality_metric_is_fail_closed(self) -> None:
        row = {
            "isin": "INE000000001",
            "symbol": "TEST",
            "latest_sales_growth_yoy_pct": 25.0,
            "latest_pat_growth_yoy_pct": 30.0,
            "debt_equity": 0.4,
            "interest_coverage": None,
            "ebitda_margin_pct": None,
            "promoter_holding_change_pp_4q": 0.5,
        }
        scored = MODULE._score_snapshot_row(row, CONFIG, PROTOCOL)
        self.assertFalse(scored["objective_core_replay_grade"])
        self.assertIsNone(scored["base_objective_core_score"])
        self.assertIsNone(scored["base_plus_promoter_score"])

    def test_promoter_extension_does_not_mutate_base_score(self) -> None:
        base = {
            "isin": "INE000000001",
            "symbol": "TEST",
            "latest_sales_growth_yoy_pct": 25.0,
            "latest_pat_growth_yoy_pct": 30.0,
            "debt_equity": 0.4,
            "interest_coverage": 8.0,
            "ebitda_margin_pct": 18.0,
            "promoter_holding_change_pp_4q": None,
        }
        with_promoter = dict(base)
        with_promoter["promoter_holding_change_pp_4q"] = 1.5
        a = MODULE._score_snapshot_row(base, CONFIG, PROTOCOL)
        b = MODULE._score_snapshot_row(with_promoter, CONFIG, PROTOCOL)
        self.assertEqual(a["base_objective_core_score"], b["base_objective_core_score"])
        self.assertIsNone(a["base_plus_promoter_score"])
        self.assertIsNotNone(b["base_plus_promoter_score"])

    def test_missing_values_are_not_imputed_as_zero(self) -> None:
        row = {
            "isin": "INE000000001",
            "symbol": "TEST",
            "latest_sales_growth_yoy_pct": 25.0,
            "latest_pat_growth_yoy_pct": 30.0,
            "debt_equity": 0.4,
            "interest_coverage": 8.0,
            "ebitda_margin_pct": None,
            "promoter_holding_change_pp_4q": None,
        }
        scored = MODULE._score_snapshot_row(row, CONFIG, PROTOCOL)
        detail = {x["field"]: x for x in scored["feature_details"]}
        self.assertIsNone(detail["ebitda_margin_pct"]["fraction"])
        self.assertIsNone(detail["promoter_holding_change_pp_4q"]["fraction"])


if __name__ == "__main__":
    unittest.main()
