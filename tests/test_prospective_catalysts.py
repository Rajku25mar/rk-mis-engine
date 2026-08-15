from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_catalysts import ProspectiveCatalystStore

PIN = "a" * 64


class ProspectiveCatalystTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProspectiveCatalystStore(Path(self.tmp.name) / "cat.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_confirmed_aggregate_orderbook_uses_frozen_quality_and_sales_ratio(self) -> None:
        self.store.add_order({
            "isin": "INE000A01001", "symbol": "AAA", "snapshot_date": "2026-06-30",
            "known_at": "2026-07-15", "order_type": "CONFIRMED", "order_value_cr": 300.0,
            "aggregate_orderbook": True, "source_url": "https://issuer.example/order.pdf",
            "source_sha256": PIN, "source_grade": "A"
        })
        result = self.store.derive_features(isin="INE000A01001", as_of_date="2026-08-15", ttm_revenue_cr=100.0)
        self.assertEqual(result["features"]["order_quality_score"], 100.0)
        self.assertEqual(result["features"]["orderbook_to_sales"], 3.0)

    def test_nonaggregate_purchase_order_never_becomes_orderbook_to_sales(self) -> None:
        self.store.add_order({
            "symbol": "AAA", "snapshot_date": "2026-08-01", "known_at": "2026-08-01",
            "order_type": "CONFIRMED", "order_value_cr": 10.0, "aggregate_orderbook": False,
            "source_url": "https://issuer.example/po.pdf", "source_sha256": PIN, "source_grade": "A"
        })
        result = self.store.derive_features(symbol="AAA", as_of_date="2026-08-15", ttm_revenue_cr=20.0)
        self.assertEqual(result["features"]["order_quality_score"], 100.0)
        self.assertIsNone(result["features"]["orderbook_to_sales"])
        self.assertIn("LATEST_ORDER_EVIDENCE_IS_NOT_AGGREGATE_ORDERBOOK", result["warnings"])

    def test_future_known_order_does_not_leak_backward(self) -> None:
        self.store.add_order({
            "symbol": "AAA", "snapshot_date": "2026-06-30", "known_at": "2026-09-01",
            "order_type": "CONFIRMED", "order_value_cr": 100.0, "aggregate_orderbook": True,
            "source_url": "https://issuer.example/future.pdf", "source_sha256": PIN, "source_grade": "A"
        })
        result = self.store.derive_features(symbol="AAA", as_of_date="2026-08-15", ttm_revenue_cr=100.0)
        self.assertIsNone(result["features"]["order_quality_score"])

    def test_order_type_weights_match_frozen_bridge(self) -> None:
        for kind, value in (("CONFIRMED", 100.0), ("LOI", 100.0)):
            self.store.add_order({
                "symbol": "AAA", "snapshot_date": "2026-08-01", "known_at": "2026-08-01",
                "order_type": kind, "order_value_cr": value, "aggregate_orderbook": True,
                "source_url": f"https://issuer.example/{kind}.pdf", "source_sha256": PIN, "source_grade": "A"
            })
        result = self.store.derive_features(symbol="AAA", as_of_date="2026-08-15")
        self.assertEqual(result["features"]["order_quality_score"], 82.5)

    def test_capex_execution_uses_frozen_status_and_delay_penalty(self) -> None:
        self.store.add_capex({
            "symbol": "AAA", "project_name": "New line", "latest_status": "UNDER_CONSTRUCTION",
            "announced_date": "2026-01-01", "target_completion_date": "2026-06-01",
            "latest_update_date": "2026-07-01", "known_at": "2026-07-01",
            "planned_capex_cr": 100.0, "source_url": "https://issuer.example/capex.pdf",
            "source_sha256": PIN, "source_grade": "A"
        })
        result = self.store.derive_features(symbol="AAA", as_of_date="2026-08-15")
        # UNDER_CONSTRUCTION 72 less 5 points for 30 days delay.
        self.assertEqual(result["features"]["capex_execution_score"], 67.0)
        self.assertIsNone(result["features"]["planned_capacity_increase_pct"])


if __name__ == "__main__":
    unittest.main()
