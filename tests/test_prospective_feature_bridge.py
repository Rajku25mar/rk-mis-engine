from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_feature_bridge import combine_prospective_sources, merge_prospective_features


class ProspectiveFeatureBridgeTests(unittest.TestCase):
    def test_missing_feature_is_filled_but_existing_value_is_not_overwritten(self) -> None:
        base = [{"isin": "INE000A01001", "symbol": "AAA", "moat_evidence_score": "", "mf_holding_change_pp_4q": "1.0"}]
        prospective = [{"isin": "INE000A01001", "symbol": "AAA", "moat_evidence_score": 40.0, "mf_holding_change_pp_4q": 2.0}]
        rows, report = merge_prospective_features(base, prospective)
        self.assertEqual(rows[0]["moat_evidence_score"], 40.0)
        self.assertEqual(rows[0]["mf_holding_change_pp_4q"], "1.0")
        self.assertEqual(rows[0]["prospective_features_filled"], "moat_evidence_score")
        self.assertEqual(rows[0]["prospective_feature_conflicts"], "mf_holding_change_pp_4q")
        self.assertEqual(report["filled_feature_cells"], 1)
        self.assertEqual(report["conflict_feature_cells"], 1)

    def test_same_existing_value_is_not_a_conflict(self) -> None:
        base = [{"isin": "INE000A01001", "symbol": "AAA", "moat_evidence_score": "40"}]
        prospective = [{"isin": "INE000A01001", "symbol": "AAA", "moat_evidence_score": 40.0}]
        rows, report = merge_prospective_features(base, prospective)
        self.assertEqual(rows[0]["prospective_feature_conflicts"], "")
        self.assertEqual(report["conflict_feature_cells"], 0)

    def test_isin_mismatch_blocks_symbol_fallback(self) -> None:
        base = [{"isin": "INEBASE00001", "symbol": "AAA", "moat_evidence_score": ""}]
        prospective = [{"isin": "INEOTHER0001", "symbol": "AAA", "moat_evidence_score": 60.0}]
        rows, report = merge_prospective_features(base, prospective)
        self.assertEqual(rows[0]["moat_evidence_score"], "")
        self.assertEqual(rows[0]["prospective_identity_match_rule"], "ISIN_NO_MATCH_SYMBOL_FALLBACK_BLOCKED")
        self.assertEqual(report["filled_feature_cells"], 0)

    def test_symbol_fallback_allowed_only_when_prospective_is_symbol_only(self) -> None:
        base = [{"isin": "INEBASE00001", "symbol": "AAA", "moat_evidence_score": ""}]
        prospective = [{"isin": None, "symbol": "AAA", "moat_evidence_score": 20.0}]
        rows, _ = merge_prospective_features(base, prospective)
        self.assertEqual(rows[0]["moat_evidence_score"], 20.0)
        self.assertEqual(rows[0]["prospective_identity_match_rule"], "SYMBOL_FALLBACK_NO_PROSPECTIVE_ISIN")

    def test_documentary_and_smart_money_sources_combine_existing_field_names(self) -> None:
        documentary = [{"isin": "INE000A01001", "symbol": "AAA", "features": {"moat_evidence_score": 40.0}}]
        smart = [{"isin": "INE000A01001", "symbol": "AAA", "features": {"mf_holding_change_pp_4q": 1.2}}]
        rows = combine_prospective_sources(documentary, smart)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["moat_evidence_score"], 40.0)
        self.assertEqual(rows[0]["mf_holding_change_pp_4q"], 1.2)
        self.assertEqual(rows[0]["prospective_sources"], ["DOCUMENTARY", "SMART_MONEY"])


if __name__ == "__main__":
    unittest.main()
