from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SPEC = importlib.util.spec_from_file_location(
    "qualitative_holdout",
    ROOT / "scripts/run_qualitative_documentary_holdout.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class DocumentaryHoldoutGateTests(unittest.TestCase):
    def test_under_minimum_stops_before_official_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            protocol = {
                "status": "PREREGISTERED_AFTER_PRE_OUTCOME_DATA_AVAILABILITY_DIAGNOSIS_BEFORE_REVIEW_AND_OUTCOME_LOAD",
                "anchor_date": "2023-07-31",
                "outcome_target_date": "2026-07-31",
                "evaluation": {
                    "minimum_ranking_eligible_rows": 30,
                    "calibration_safe_outcome_minimum_pct_of_scored": 75
                }
            }
            snapshot = {
                "rows": [
                    {
                        "sample_isin": f"INE{i:09d}"[:12],
                        "sample_symbol": f"T{i}",
                        "ranking_eligible": True,
                        "qualitative_documentary_partial_score": 70.0,
                    }
                    for i in range(29)
                ]
            }
            manifest = {
                "anchor_date": "2023-07-31",
                "outcomes_seen_when_predictor_snapshot_frozen": False,
                "missing_data_imputed": False,
                "predictor_snapshot_sha256": "abc",
            }
            (base / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
            (base / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
            (base / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            argv = [
                "run_qualitative_documentary_holdout.py",
                "--protocol", str(base / "protocol.json"),
                "--snapshot", str(base / "snapshot.json"),
                "--snapshot-manifest", str(base / "manifest.json"),
                "--output", str(base / "out"),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                MODULE.TECH,
                "OfficialSession",
                side_effect=AssertionError("future-price session must not be created"),
            ):
                with self.assertRaisesRegex(MODULE.DocumentaryHoldoutError, "PRE_OUTCOME_FEASIBILITY_STOP"):
                    MODULE.main()

    def test_frozen_decision_rule_requires_non_adverse_loss_and_nonnegative_rank(self) -> None:
        good = {
            "spearman": 0.05,
            "top_quartile_minus_cohort": {
                "mean_multiple": 0.2,
                "five_x_pp": 0.0,
                "loss_pp": 3.0,
            },
        }
        bad_loss = {
            "spearman": 0.05,
            "top_quartile_minus_cohort": {
                "mean_multiple": 0.2,
                "five_x_pp": 5.0,
                "loss_pp": 6.0,
            },
        }
        bad_rank = {
            "spearman": -0.01,
            "top_quartile_minus_cohort": {
                "mean_multiple": 0.2,
                "five_x_pp": 5.0,
                "loss_pp": 0.0,
            },
        }
        self.assertEqual(MODULE._decision(good), "POSITIVE_DIAGNOSTIC_FURTHER_HOLDOUTS_JUSTIFIED")
        self.assertEqual(MODULE._decision(bad_loss), "NO_POSITIVE_DIAGNOSTIC_NO_WEIGHT_CHANGE")
        self.assertEqual(MODULE._decision(bad_rank), "NO_POSITIVE_DIAGNOSTIC_NO_WEIGHT_CHANGE")


if __name__ == "__main__":
    unittest.main()
