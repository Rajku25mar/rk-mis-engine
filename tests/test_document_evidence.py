from __future__ import annotations

import unittest

from multibagger_pipeline.document_evidence import (
    EvidenceRecord,
    build_feature_evidence_index,
    freeze_evidence_snapshot,
    scoring_eligibility,
)


class DocumentEvidenceTests(unittest.TestCase):
    def test_future_known_at_is_rejected(self) -> None:
        record = EvidenceRecord(
            symbol="TEST",
            claim_type="ORDER_BOOK",
            statement="Order book is Rs 500 crore",
            source_url="https://www.nseindia.com/example",
            document_type="EXCHANGE_FILING",
            document_date="2023-01-20",
            known_at="2023-02-02",
            source_grade="A",
            metric="order_book",
            value=500,
            unit="crore",
            extraction_confidence=0.95,
            review_state="APPROVED",
        )
        eligible, reasons = scoring_eligibility(record, "2023-01-31")
        self.assertFalse(eligible)
        self.assertIn("KNOWN_AFTER_ANCHOR", reasons)

    def test_unapproved_evidence_cannot_score(self) -> None:
        record = EvidenceRecord(
            symbol="TEST",
            claim_type="MOAT_EVIDENCE",
            statement="Only domestic supplier qualified by customer",
            source_url="https://www.nseindia.com/example",
            document_type="INVESTOR_PRESENTATION",
            document_date="2023-01-20",
            known_at="2023-01-20",
            source_grade="A",
            extraction_confidence=0.92,
            review_state="PENDING",
        )
        eligible, reasons = scoring_eligibility(record, "2023-01-31")
        self.assertFalse(eligible)
        self.assertIn("HUMAN_REVIEW_NOT_APPROVED", reasons)

    def test_approved_pre_anchor_evidence_maps_to_feature(self) -> None:
        record = EvidenceRecord(
            symbol="TEST",
            claim_type="CAPACITY",
            statement="Capacity planned to increase by 60 percent",
            source_url="https://www.nseindia.com/example",
            document_type="INVESTOR_PRESENTATION",
            document_date="2023-01-10",
            known_at="2023-01-10",
            source_grade="A",
            metric="planned_capacity_increase_pct",
            value=60,
            unit="%",
            extraction_confidence=0.90,
            review_state="APPROVED",
        )
        index = build_feature_evidence_index([record], "2023-01-31")
        self.assertTrue(index["planned_capacity_increase_pct"][0]["score_eligible"])

    def test_freeze_snapshot_excludes_future_records(self) -> None:
        safe = EvidenceRecord(
            symbol="TEST",
            claim_type="ENTRY_BARRIER",
            statement="Qualification cycle exceeds two years",
            source_url="https://www.nseindia.com/example",
            document_type="ANNUAL_REPORT",
            document_date="2022-09-01",
            known_at="2022-09-01",
            extraction_confidence=0.88,
            review_state="APPROVED",
        )
        future = EvidenceRecord(
            symbol="TEST",
            claim_type="ORDER_QUALITY",
            statement="Large order received after anchor",
            source_url="https://www.nseindia.com/example2",
            document_type="EXCHANGE_FILING",
            document_date="2023-02-05",
            known_at="2023-02-05",
            extraction_confidence=0.95,
            review_state="APPROVED",
        )
        snap = freeze_evidence_snapshot([safe, future], "2023-01-31")
        self.assertEqual(snap["safe_evidence_rows"], 1)
        self.assertEqual(snap["rejected_future_or_invalid_rows"], 1)
        self.assertFalse(snap["outcomes_seen"])


if __name__ == "__main__":
    unittest.main()
