from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fundamental_replay", ROOT / "scripts/run_fundamental_growth_quality_replay.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def filing(period="2022-12-31", scope="Consolidated"):
    return {"toDate": period, "consolidated": scope}


class FundamentalReplayTests(unittest.TestCase):
    def test_replay_grade_current_quarter_context(self) -> None:
        xml = b"""<root>
          <DateOfStartOfReportingPeriod contextRef="Q">2022-10-01</DateOfStartOfReportingPeriod>
          <DateOfEndOfReportingPeriod contextRef="Q">2022-12-31</DateOfEndOfReportingPeriod>
          <NatureOfReportStandaloneConsolidated contextRef="Q">Consolidated</NatureOfReportStandaloneConsolidated>
          <RevenueFromOperations contextRef="Q" unitRef="INR_CRORES">100</RevenueFromOperations>
          <ProfitLossForPeriod contextRef="Q" unitRef="INR_CRORES">12</ProfitLossForPeriod>
          <FinanceCosts contextRef="Q" unitRef="INR_CRORES">2</FinanceCosts>
          <ProfitBeforeTax contextRef="Q" unitRef="INR_CRORES">16</ProfitBeforeTax>
          <DepreciationAndAmortisationExpense contextRef="Q" unitRef="INR_CRORES">4</DepreciationAndAmortisationExpense>
          <DebtEquityRatio contextRef="Q" unitRef="PURE">0.4</DebtEquityRatio>
        </root>"""
        q = MODULE.parse_quarter_xbrl(xml, filing(), "CONSOLIDATED")
        self.assertTrue(q["replay_grade"])
        self.assertEqual(q["revenue_cr"], 100.0)
        self.assertEqual(q["pat_cr"], 12.0)
        self.assertEqual(q["debt_equity"], 0.4)
        interest = (q["pbt_cr"] + q["finance_cost_cr"]) / q["finance_cost_cr"]
        ebitda_margin = (q["pbt_cr"] + q["finance_cost_cr"] + q["depreciation_cr"]) / q["revenue_cr"] * 100
        self.assertEqual(round(interest, 4), 9.0)
        self.assertEqual(round(ebitda_margin, 4), 22.0)

    def test_ytd_context_is_rejected(self) -> None:
        xml = b"""<root>
          <DateOfStartOfReportingPeriod contextRef="YTD">2022-04-01</DateOfStartOfReportingPeriod>
          <DateOfEndOfReportingPeriod contextRef="YTD">2022-12-31</DateOfEndOfReportingPeriod>
          <NatureOfReportStandaloneConsolidated contextRef="YTD">Consolidated</NatureOfReportStandaloneConsolidated>
          <RevenueFromOperations contextRef="YTD" unitRef="INR_CRORES">300</RevenueFromOperations>
          <ProfitLossForPeriod contextRef="YTD" unitRef="INR_CRORES">30</ProfitLossForPeriod>
        </root>"""
        q = MODULE.parse_quarter_xbrl(xml, filing(), "CONSOLIDATED")
        self.assertFalse(q["replay_grade"])

    def test_scope_mismatch_is_rejected(self) -> None:
        xml = b"""<root>
          <DateOfStartOfReportingPeriod contextRef="Q">2022-10-01</DateOfStartOfReportingPeriod>
          <DateOfEndOfReportingPeriod contextRef="Q">2022-12-31</DateOfEndOfReportingPeriod>
          <NatureOfReportStandaloneConsolidated contextRef="Q">Standalone</NatureOfReportStandaloneConsolidated>
          <RevenueFromOperations contextRef="Q" unitRef="INR_CRORES">100</RevenueFromOperations>
          <ProfitLossForPeriod contextRef="Q" unitRef="INR_CRORES">12</ProfitLossForPeriod>
        </root>"""
        q = MODULE.parse_quarter_xbrl(xml, filing(), "CONSOLIDATED")
        self.assertFalse(q["replay_grade"])

    def test_bank_guard_uses_pre_anchor_metadata(self) -> None:
        rows = [{
            "toDate": "31-Dec-2022",
            "filingDate": "15-Jan-2023",
            "companyName": "Example Bank Limited",
            "bank": "Y",
        }]
        guarded, reason, _ = MODULE.is_guarded_financial(rows, "2023-01-31")
        self.assertTrue(guarded)
        self.assertEqual(reason, "NSE_PRE_ANCHOR_BANK_FLAG")

    def test_growth_formula(self) -> None:
        self.assertEqual(MODULE.pct_growth(130, 100), 30.0)
        self.assertIsNone(MODULE.pct_growth(10, 0))


if __name__ == "__main__":
    unittest.main()
