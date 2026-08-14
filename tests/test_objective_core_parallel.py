from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import run_objective_core_replay_parallel as parallel


class FakeSession:
    def __init__(self) -> None:
        self.requests_made = 3
        self.request_budget = 1800


class ObjectiveCoreParallelTests(unittest.TestCase):
    def test_parallel_adapter_preserves_order_and_aggregate_request_count(self) -> None:
        sample = [
            {"isin": "INE000A01001", "symbol": "AAA"},
            {"isin": "INE000A01002", "symbol": "BBB"},
            {"isin": "INE000A01003", "symbol": "CCC"},
        ]
        original = parallel._acquire_member

        def fake_acquire(member, config, protocol):
            return {
                "scored": {
                    "isin": member["isin"],
                    "symbol": member["symbol"],
                    "objective_core_replay_grade": True,
                    "base_objective_core_score": 75.0,
                    "base_plus_promoter_score": None,
                    "guard_industrial_quality": False,
                },
                "financial_meta": {"metadata": {"sha256": member["symbol"], "rows": 1}},
                "share_meta": {"attempts": [], "safe_periods": 0},
                "worker_requests": 2,
            }

        parallel._acquire_member = fake_acquire
        try:
            session = FakeSession()
            rows, manifest = parallel.build_predictor_snapshot_parallel(sample, session, {}, {})
        finally:
            parallel._acquire_member = original

        self.assertEqual([row["symbol"] for row in rows], ["AAA", "BBB", "CCC"])
        self.assertEqual(session.requests_made, 9)
        self.assertEqual(manifest["worker_official_requests_made"], 6)
        self.assertEqual(manifest["parallel_workers"], 4)
        self.assertFalse(manifest["outcomes_seen_when_snapshot_frozen"])
        self.assertTrue(manifest["predictor_snapshot_sha256"])


if __name__ == "__main__":
    unittest.main()
