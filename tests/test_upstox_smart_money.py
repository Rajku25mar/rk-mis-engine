from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_smart_money import ProspectiveSmartMoneyStore, SmartMoneyObservation
from multibagger_pipeline.upstox_smart_money import (
    ingest_upstox_raw_directory,
    observation_from_upstox_payload,
)


def payload(period_values: list[tuple[str, float, float, float, float]]) -> dict:
    # period, promoter, fii, mutual_funds, other_dii
    categories = {name: [] for name in ("promoters", "fii", "mutual_funds", "other_dii", "retail_and_other")}
    for period, promoter, fii, mf, dii in period_values:
        categories["promoters"].append({"period": period, "value": promoter})
        categories["fii"].append({"period": period, "value": fii})
        categories["mutual_funds"].append({"period": period, "value": mf})
        categories["other_dii"].append({"period": period, "value": dii})
        categories["retail_and_other"].append({"period": period, "value": 100 - promoter - fii - mf - dii})
    return {"status": "success", "data": [{"category": k, "history": v} for k, v in categories.items()]}


class UpstoxSmartMoneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProspectiveSmartMoneyStore(self.root / "smart_money.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_single_provider_response_does_not_backfill_historical_quarters(self) -> None:
        p = payload([
            ("Jun 2025", 50.0, 4.0, 2.0, 8.0),
            ("Sep 2025", 50.1, 4.2, 2.2, 8.1),
            ("Dec 2025", 50.2, 4.4, 2.4, 8.2),
            ("Mar 2026", 50.3, 4.6, 2.6, 8.3),
            ("Jun 2026", 50.5, 5.0, 3.1, 8.4),
        ])
        observation, diag = observation_from_upstox_payload(
            isin="INE000A01001", symbol="AAA", payload=p, known_at="2026-07-20"
        )
        self.assertEqual(observation.period_end, "2026-06-30")
        self.assertEqual(observation.mf_holding_pct, 3.1)
        self.assertEqual(observation.fii_holding_pct, 5.0)
        self.assertEqual(observation.promoter_holding_pct, 50.5)
        self.assertIsNone(observation.institutional_shareholder_count)
        self.assertFalse(diag["historical_quarters_backfilled"])
        self.assertIn("other_dii", diag["provider_categories_ignored"])

        self.store.add(observation)
        result = self.store.derive_features(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertEqual(result["available_features"], [])
        self.assertIn("INSUFFICIENT_DISTINCT_PERIODS:1/5", result["warnings"])

    def test_five_separate_point_in_time_captures_derive_only_supported_fields(self) -> None:
        snapshots = [
            ("Jun 2025", "2025-07-20", 50.0, 4.0, 2.0, 8.0),
            ("Sep 2025", "2025-10-20", 50.1, 4.2, 2.2, 8.1),
            ("Dec 2025", "2026-01-20", 50.2, 4.4, 2.4, 8.2),
            ("Mar 2026", "2026-04-20", 50.3, 4.6, 2.6, 8.3),
            ("Jun 2026", "2026-07-20", 50.5, 5.0, 3.1, 8.4),
        ]
        history: list[tuple[str, float, float, float, float]] = []
        for period, known, promoter, fii, mf, dii in snapshots:
            history.append((period, promoter, fii, mf, dii))
            observation, _ = observation_from_upstox_payload(
                isin="INE000A01001", symbol="AAA", payload=payload(history), known_at=known
            )
            self.store.add(observation)

        result = self.store.derive_features(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertEqual(result["span_days"], 365)
        self.assertEqual(result["features"]["mf_holding_change_pp_4q"], 1.1)
        self.assertEqual(result["features"]["fii_holding_change_pp_4q"], 1.0)
        self.assertEqual(result["features"]["promoter_holding_change_pp_4q"], 0.5)
        self.assertIsNone(result["features"]["institutional_breadth_change_4q"])
        self.assertIn(
            "INCOMPLETE_5_PERIOD_COMPONENT:institutional_breadth_change_4q",
            result["warnings"],
        )

    def test_low_grade_observations_do_not_generate_features(self) -> None:
        periods = ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
        for i, period in enumerate(periods):
            self.store.add(SmartMoneyObservation(
                isin="INE000A01001", symbol="AAA", period_end=period,
                known_at=["2025-07-15", "2025-10-15", "2026-01-15", "2026-04-15", "2026-07-15"][i],
                source_url=f"https://low-grade.example/{period}", source_kind="UNVERIFIED",
                source_grade="C", mf_holding_pct=2.0 + i * 0.2,
            ))
        result = self.store.derive_features(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertEqual(result["available_features"], [])
        self.assertIn("LOW_GRADE_OBSERVATIONS_EXCLUDED:5", result["warnings"])
        self.assertIn("INSUFFICIENT_DISTINCT_PERIODS:0/5", result["warnings"])

    def test_directory_ingestion_uses_provider_raw_layout(self) -> None:
        raw = self.root / "raw" / "INE000A01001"
        raw.mkdir(parents=True)
        (raw / "share_holdings.json").write_text(
            json.dumps(payload([("Jun 2026", 50.5, 5.0, 3.1, 8.4)])), encoding="utf-8"
        )
        report = ingest_upstox_raw_directory(
            raw_root=self.root / "raw",
            companies=[{"isin": "INE000A01001", "symbol": "AAA"}],
            store=self.store,
            known_at="2026-07-20",
        )
        self.assertEqual(report["raw_shareholding_payloads_present"], 1)
        self.assertEqual(report["observations_appended_or_already_present"], 1)
        self.assertFalse(report["historical_provider_quarters_backfilled"])
        self.assertFalse(report["institutional_breadth_inferred_from_other_dii"])


if __name__ == "__main__":
    unittest.main()
