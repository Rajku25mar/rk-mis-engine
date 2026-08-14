from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_smart_money import ProspectiveSmartMoneyStore, SmartMoneyObservation


class ProspectiveSmartMoneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProspectiveSmartMoneyStore(Path(self.tmp.name) / "smart_money.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def obs(self, period: str, known: str, mf: float | None, fii: float | None, count: int | None, promoter: float | None = 50.0, url: str | None = None) -> SmartMoneyObservation:
        return SmartMoneyObservation(
            isin="INE000A01001",
            symbol="AAA",
            period_end=period,
            known_at=known,
            source_url=url or f"https://provider.example/{period}/{known}",
            source_kind="AUTHORIZED_PROVIDER",
            source_grade="A",
            mf_holding_pct=mf,
            fii_holding_pct=fii,
            institutional_shareholder_count=count,
            promoter_holding_pct=promoter,
        )

    def test_five_period_365_day_history_derives_existing_features(self) -> None:
        rows = [
            self.obs("2025-06-30", "2025-07-15", 2.0, 4.0, 100, 51.0),
            self.obs("2025-09-30", "2025-10-15", 2.2, 4.1, 102, 51.0),
            self.obs("2025-12-31", "2026-01-15", 2.4, 4.4, 105, 51.2),
            self.obs("2026-03-31", "2026-04-15", 2.7, 4.7, 108, 51.3),
            self.obs("2026-06-30", "2026-07-15", 3.1, 5.0, 112, 51.5),
        ]
        self.store.add_many(rows)
        result = self.store.derive_features(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertEqual(result["span_days"], 365)
        self.assertEqual(result["features"]["mf_holding_change_pp_4q"], 1.1)
        self.assertEqual(result["features"]["fii_holding_change_pp_4q"], 1.0)
        self.assertEqual(result["features"]["institutional_breadth_change_4q"], 12.0)
        self.assertEqual(result["features"]["promoter_holding_change_pp_4q"], 0.5)
        self.assertEqual(len(result["available_features"]), 4)

    def test_post_asof_revision_does_not_rewrite_point_in_time_snapshot(self) -> None:
        self.store.add_many([
            self.obs("2025-06-30", "2025-07-15", 2.0, 4.0, 100),
            self.obs("2025-09-30", "2025-10-15", 2.1, 4.1, 101),
            self.obs("2025-12-31", "2026-01-15", 2.2, 4.2, 102),
            self.obs("2026-03-31", "2026-04-15", 2.3, 4.3, 103),
            self.obs("2026-06-30", "2026-07-15", 3.0, 5.0, 110, url="https://provider.example/original"),
            self.obs("2026-06-30", "2026-08-15", 8.0, 9.0, 999, url="https://provider.example/later-revision"),
        ])
        result = self.store.derive_features(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertEqual(result["features"]["mf_holding_change_pp_4q"], 1.0)
        self.assertEqual(result["features"]["institutional_breadth_change_4q"], 10.0)

    def test_missing_intermediate_component_is_not_imputed(self) -> None:
        self.store.add_many([
            self.obs("2025-06-30", "2025-07-15", 2.0, 4.0, 100),
            self.obs("2025-09-30", "2025-10-15", 2.2, None, 102),
            self.obs("2025-12-31", "2026-01-15", 2.4, 4.4, 105),
            self.obs("2026-03-31", "2026-04-15", 2.7, 4.7, 108),
            self.obs("2026-06-30", "2026-07-15", 3.1, 5.0, 112),
        ])
        result = self.store.derive_features(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertEqual(result["features"]["mf_holding_change_pp_4q"], 1.1)
        self.assertIsNone(result["features"]["fii_holding_change_pp_4q"])
        self.assertIn("INCOMPLETE_5_PERIOD_COMPONENT:fii_holding_change_pp_4q", result["warnings"])

    def test_invalid_span_blocks_all_4q_features(self) -> None:
        self.store.add_many([
            self.obs("2025-09-30", "2025-10-10", 2.0, 4.0, 100),
            self.obs("2025-12-31", "2026-01-10", 2.1, 4.1, 101),
            self.obs("2026-03-31", "2026-04-10", 2.2, 4.2, 102),
            self.obs("2026-06-30", "2026-07-10", 2.3, 4.3, 103),
            self.obs("2026-09-30", "2026-10-10", 2.4, 4.4, 104),
        ])
        result = self.store.derive_features(isin="INE000A01001", as_of_date="2026-10-31")
        self.assertEqual(result["span_days"], 365)
        self.assertTrue(result["available_features"])
        # A deliberately shorter set must fail closed.
        second = ProspectiveSmartMoneyStore(Path(self.tmp.name) / "short.sqlite")
        second.add_many([
            self.obs("2026-01-31", "2026-02-05", 2.0, 4.0, 100),
            self.obs("2026-03-31", "2026-04-05", 2.1, 4.1, 101),
            self.obs("2026-05-31", "2026-06-05", 2.2, 4.2, 102),
            self.obs("2026-07-31", "2026-08-05", 2.3, 4.3, 103),
            self.obs("2026-09-30", "2026-10-05", 2.4, 4.4, 104),
        ])
        short = second.derive_features(isin="INE000A01001", as_of_date="2026-10-31")
        self.assertEqual(short["available_features"], [])
        self.assertTrue(any(x.startswith("INVALID_4Q_SPAN_DAYS:") for x in short["warnings"]))

    def test_period_after_known_at_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add(self.obs("2026-06-30", "2026-06-01", 2.0, 4.0, 100))


if __name__ == "__main__":
    unittest.main()
