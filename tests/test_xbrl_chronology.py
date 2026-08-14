from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.xbrl_normalizer import normalize_filing_xbrl


XBRL = b'''<?xml version="1.0" encoding="UTF-8"?>
<xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in="urn:test">
  <in:DateOfStartOfReportingPeriod contextRef="Q1">2020-10-01</in:DateOfStartOfReportingPeriod>
  <in:DateOfEndOfReportingPeriod contextRef="Q1">2020-12-31</in:DateOfEndOfReportingPeriod>
  <in:NatureOfReportStandaloneConsolidated contextRef="Q1">Consolidated</in:NatureOfReportStandaloneConsolidated>
  <in:RevenueFromOperations contextRef="Q1" unitRef="INR_CRORES">500</in:RevenueFromOperations>
  <in:ProfitLossForPeriod contextRef="Q1" unitRef="INR_CRORES">50</in:ProfitLossForPeriod>
  <in:FinanceCosts contextRef="Q1" unitRef="INR_CRORES">5</in:FinanceCosts>
</xbrl>
'''

ROW = {
    "symbol": "TEST",
    "toDate": "2020-12-31",
    "filingDate": "2021-01-20",
    "consolidated": "Consolidated",
    "xbrl": "https://example.invalid/test.xml",
}


class XbrlChronologyTests(unittest.TestCase):
    def test_2021_anchor_accepts_pre_anchor_filing(self) -> None:
        result = normalize_filing_xbrl(XBRL, ROW, anchor_date="2021-01-29")
        self.assertTrue(result.replay_grade)
        self.assertNotIn("FILING_AFTER_ANCHOR", result.warnings)
        self.assertEqual(result.revenue_cr, 500)
        self.assertEqual(result.pat_cr, 50)

    def test_2019_anchor_rejects_same_future_filing(self) -> None:
        result = normalize_filing_xbrl(XBRL, ROW, anchor_date="2019-01-31")
        self.assertFalse(result.replay_grade)
        self.assertIn("FILING_AFTER_ANCHOR", result.warnings)
        self.assertIn("PERIOD_AFTER_ANCHOR", result.warnings)

    def test_anchor_argument_is_mandatory(self) -> None:
        with self.assertRaises(TypeError):
            normalize_filing_xbrl(XBRL, ROW)  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
