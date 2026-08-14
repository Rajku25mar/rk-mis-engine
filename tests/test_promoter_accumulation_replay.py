from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "promoter_replay", ROOT / "scripts/run_promoter_accumulation_replay.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class PromoterReplayTests(unittest.TestCase):
    def test_post_anchor_revision_is_not_safe(self) -> None:
        raw = {
            "toDate": "31-Dec-2022",
            "submissionDate": "10-Jan-2023",
            "broadcastDate": "10-Jan-2023",
            "revisionDate": "05-Feb-2023",
            "pr_and_prgrp": "55.25",
        }
        row = MODULE.normalize_shareholding_row(raw)
        self.assertEqual(row["period_end"], "2022-12-31")
        self.assertEqual(row["version_known_at"], "2023-02-05")
        self.assertFalse(MODULE.safe_at_anchor(row, "2023-01-31"))

    def test_five_quarter_snapshots_produce_4q_change(self) -> None:
        rows = []
        for period, known, pct in [
            ("2021-12-31", "2022-01-15", 50.0),
            ("2022-03-31", "2022-04-15", 50.1),
            ("2022-06-30", "2022-07-15", 50.2),
            ("2022-09-30", "2022-10-15", 50.4),
            ("2022-12-31", "2023-01-15", 51.25),
        ]:
            rows.append({
                "period_end": period,
                "version_known_at": known,
                "promoter_pct": pct,
                "revision_date": None,
            })
        feature = MODULE.promoter_change_feature(rows, "2023-01-31", 330, 400)
        self.assertEqual(feature["five_snapshot_span_days"], 365)
        self.assertEqual(feature["promoter_holding_change_pp_4q"], 1.25)

    def test_missing_snapshot_blocks_replay_grade_feature(self) -> None:
        rows = [
            {"period_end": "2022-03-31", "version_known_at": "2022-04-15", "promoter_pct": 50.0},
            {"period_end": "2022-06-30", "version_known_at": "2022-07-15", "promoter_pct": 50.2},
            {"period_end": "2022-09-30", "version_known_at": "2022-10-15", "promoter_pct": 50.4},
            {"period_end": "2022-12-31", "version_known_at": "2023-01-15", "promoter_pct": 51.0},
        ]
        feature = MODULE.promoter_change_feature(rows, "2023-01-31", 330, 400)
        self.assertIsNone(feature["promoter_holding_change_pp_4q"])

    def test_existing_scoring_bands_are_preserved(self) -> None:
        spec = {
            "direction": "higher",
            "bands": [[1.0, 1.0], [0.25, 0.7], [0.0, 0.3], [-1.0, 0.1]],
        }
        self.assertEqual(MODULE.fraction(1.2, spec), 1.0)
        self.assertEqual(MODULE.fraction(0.5, spec), 0.7)
        self.assertEqual(MODULE.fraction(0.0, spec), 0.3)
        self.assertEqual(MODULE.fraction(-0.5, spec), 0.1)
        self.assertEqual(MODULE.fraction(-2.0, spec), 0.0)


if __name__ == "__main__":
    unittest.main()
