from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_documentary import ProspectiveDocumentEvidenceStore
from multibagger_pipeline.prospective_ledger_loader import load_reviewed_documentary_ledgers

PIN = "a" * 64


class ProspectiveLedgerLoaderTests(unittest.TestCase):
    def make_ledger(self, root: Path, as_of: str = "2026-08-15") -> Path:
        path = root / "ledger.json"
        path.write_text(json.dumps({
            "status": "REVIEWED_PRIMARY_SOURCE_EVIDENCE",
            "as_of_date": as_of,
            "review_policy": {"reviewer": "TEST", "reviewed_at": as_of},
            "decisions": [{
                "claim": {
                    "isin": "INE000A01001",
                    "symbol": "AAA",
                    "evidence_family": "RUNWAY",
                    "evidence_category": "committed_capacity_expansion",
                    "statement": "New capacity is under construction.",
                    "source_url": "https://issuer.example/source.pdf",
                    "document_type": "INVESTOR_PRESENTATION",
                    "document_date": "2026-08-01",
                    "known_at": "2026-08-01",
                    "source_grade": "A",
                    "extraction_confidence": 0.95,
                    "source_sha256": PIN,
                    "basis_key": "AAA-CAPEX-1"
                },
                "review": {"state": "APPROVED", "note": "verified"}
            }]
        }), encoding="utf-8")
        return path

    def test_reviewed_ledger_loads_into_point_in_time_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = self.make_ledger(root)
            store = ProspectiveDocumentEvidenceStore(root / "doc.sqlite")
            report = load_reviewed_documentary_ledgers(store, [ledger], scoring_as_of="2026-08-15")
            row = store.derive_snapshot(isin="INE000A01001", as_of_date="2026-08-15")
            self.assertEqual(report["decision_rows"], 1)
            self.assertEqual(row["features"]["reinvestment_runway_score"], 20.0)

    def test_future_ledger_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = self.make_ledger(root, as_of="2026-09-01")
            store = ProspectiveDocumentEvidenceStore(root / "doc.sqlite")
            with self.assertRaises(ValueError):
                load_reviewed_documentary_ledgers(store, [ledger], scoring_as_of="2026-08-15")

    def test_duplicate_path_is_loaded_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = self.make_ledger(root)
            store = ProspectiveDocumentEvidenceStore(root / "doc.sqlite")
            report = load_reviewed_documentary_ledgers(store, [ledger, ledger], scoring_as_of="2026-08-15")
            self.assertEqual(report["ledger_count"], 1)
            self.assertEqual(report["decision_rows"], 1)


if __name__ == "__main__":
    unittest.main()
