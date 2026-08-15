from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_catalyst_loader import load_reviewed_catalyst_ledgers
from multibagger_pipeline.prospective_catalysts import ProspectiveCatalystStore

SPEC = importlib.util.spec_from_file_location("run_rk_mie", ROOT / "scripts" / "run_rk_mie.py")
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(RUNNER)

PIN = "a" * 64


class ProspectiveCatalystLoaderTests(unittest.TestCase):
    def make_ledger(self, root: Path, as_of: str = "2026-08-15") -> Path:
        path = root / "catalyst.json"
        path.write_text(json.dumps({
            "status": "REVIEWED_PRIMARY_SOURCE_CATALYST_EVIDENCE",
            "as_of_date": as_of,
            "orders": [{
                "isin": "INE000A01001", "symbol": "AAA", "snapshot_date": "2026-06-30",
                "known_at": "2026-07-15", "order_type": "CONFIRMED", "order_value_cr": 300.0,
                "aggregate_orderbook": True, "source_url": "https://issuer.example/order.pdf",
                "source_sha256": PIN, "source_grade": "A", "review_note": "verified"
            }],
            "capex": [{
                "isin": "INE000A01001", "symbol": "AAA", "project_name": "New line",
                "latest_status": "ANNOUNCED", "known_at": "2026-07-15", "announced_date": "2026-07-15",
                "latest_update_date": "2026-07-15", "target_completion_date": "2027-03-31",
                "source_url": "https://issuer.example/capex.pdf", "source_sha256": PIN,
                "source_grade": "A", "review_note": "verified"
            }]
        }), encoding="utf-8")
        return path

    def test_loader_and_explicit_ttm_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = self.make_ledger(root)
            store = ProspectiveCatalystStore(root / "cat.sqlite")
            report = load_reviewed_catalyst_ledgers(store, [ledger], scoring_as_of="2026-08-15")
            rows = RUNNER.catalyst_feature_rows(store, [{
                "isin": "INE000A01001", "symbol": "AAA", "ttm_revenue_cr": "100"
            }], as_of="2026-08-15")
            self.assertEqual(report["order_rows"], 1)
            self.assertEqual(report["capex_rows"], 1)
            self.assertEqual(rows[0]["features"]["orderbook_to_sales"], 3.0)
            self.assertEqual(rows[0]["features"]["order_quality_score"], 100.0)
            self.assertEqual(rows[0]["features"]["capex_execution_score"], 45.0)

    def test_missing_ttm_keeps_orderbook_ratio_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = self.make_ledger(root)
            store = ProspectiveCatalystStore(root / "cat.sqlite")
            load_reviewed_catalyst_ledgers(store, [ledger], scoring_as_of="2026-08-15")
            rows = RUNNER.catalyst_feature_rows(store, [{"isin": "INE000A01001", "symbol": "AAA"}], as_of="2026-08-15")
            self.assertIsNone(rows[0]["features"]["orderbook_to_sales"])
            self.assertIn("TTM_REVENUE_MISSING_FOR_ORDERBOOK_TO_SALES", rows[0]["warnings"])

    def test_future_catalyst_ledger_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = self.make_ledger(root, as_of="2026-09-01")
            store = ProspectiveCatalystStore(root / "cat.sqlite")
            with self.assertRaises(ValueError):
                load_reviewed_catalyst_ledgers(store, [ledger], scoring_as_of="2026-08-15")


if __name__ == "__main__":
    unittest.main()
