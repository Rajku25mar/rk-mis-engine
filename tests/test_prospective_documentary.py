from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.prospective_documentary import ProspectiveDocumentClaim, ProspectiveDocumentEvidenceStore

PIN = "a" * 64


class ProspectiveDocumentaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProspectiveDocumentEvidenceStore(Path(self.tmp.name) / "documentary.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def claim(
        self,
        family: str,
        category: str,
        statement: str,
        *,
        known_at: str = "2026-07-01",
        confidence: float = 0.9,
        grade: str = "A",
        basis_key: str | None = None,
        promise_id: str | None = None,
        source_sha256: str | None = PIN,
    ) -> ProspectiveDocumentClaim:
        return ProspectiveDocumentClaim(
            isin="INE000A01001",
            symbol="AAA",
            evidence_family=family,
            evidence_category=category,
            statement=statement,
            source_url=f"https://issuer.example/{category}/{statement.replace(' ', '-')}",
            document_type="INVESTOR_PRESENTATION",
            document_date="2026-06-30",
            known_at=known_at,
            source_grade=grade,
            extraction_confidence=confidence,
            source_sha256=source_sha256,
            basis_key=basis_key,
            promise_id=promise_id,
        )

    def approve(self, claim_id: str, reviewed_at: str = "2026-07-02") -> None:
        self.store.review(claim_id, "APPROVED", reviewed_at=reviewed_at, reviewer="MODEL_REVIEW", note="evidence verified")

    def test_pending_claim_never_scores(self) -> None:
        self.store.add_claim(self.claim("MOAT", "proprietary_or_ip", "Company owns a patented process"))
        result = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertIsNone(result["features"]["moat_evidence_score"])
        self.assertEqual(result["eligible_claim_count"], 0)

    def test_two_independent_approved_categories_score_40(self) -> None:
        a = self.store.add_claim(self.claim("RUNWAY", "committed_capacity_expansion", "New line is under construction"))
        b = self.store.add_claim(self.claim("RUNWAY", "funding_visibility", "Expansion is funded from internal accruals"))
        self.approve(a); self.approve(b)
        result = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertEqual(result["features"]["reinvestment_runway_score"], 40.0)
        self.assertEqual(result["approved_categories"]["RUNWAY"], ["committed_capacity_expansion", "funding_visibility"])

    def test_review_after_asof_does_not_leak_backward(self) -> None:
        claim_id = self.store.add_claim(self.claim("MOAT", "customer_stickiness", "Repeat orders received from a qualified customer"))
        self.approve(claim_id, reviewed_at="2026-08-15")
        july = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-07-31")
        august = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-08-31")
        self.assertIsNone(july["features"]["moat_evidence_score"])
        self.assertEqual(august["features"]["moat_evidence_score"], 20.0)

    def test_same_factual_basis_cannot_double_count_categories(self) -> None:
        basis = "BASIS-SAME-FACT"
        a = self.store.add_claim(self.claim("MOAT", "proprietary_or_ip", "Single factual sentence", basis_key=basis))
        b = self.store.add_claim(self.claim("MOAT", "cost_process_or_scale_advantage", "Same underlying factual sentence restated", basis_key=basis))
        self.approve(a); self.approve(b)
        result = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertIsNone(result["features"]["moat_evidence_score"])
        self.assertTrue(any(x.startswith("CROSS_CATEGORY_SHARED_FACTUAL_BASIS:MOAT") for x in result["warnings"]))

    def test_same_factual_basis_cannot_score_multiple_families(self) -> None:
        basis = "BASIS-CROSS-FAMILY"
        a = self.store.add_claim(self.claim("RUNWAY", "committed_capacity_expansion", "One expansion fact", basis_key=basis))
        b = self.store.add_claim(self.claim("OPTIONALITY", "new_product_or_platform", "Same expansion fact relabelled", basis_key=basis))
        self.approve(a); self.approve(b)
        result = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertIsNone(result["features"]["reinvestment_runway_score"])
        self.assertIsNone(result["features"]["new_product_export_optionalities_score"])
        self.assertIn("CROSS_FAMILY_SHARED_FACTUAL_BASIS:1", result["warnings"])

    def test_unpinned_binary_cannot_score_even_if_approved(self) -> None:
        claim_id = self.store.add_claim(self.claim(
            "MOAT", "proprietary_or_ip", "Unpinned primary document", source_sha256=None
        ))
        self.approve(claim_id)
        result = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertIsNone(result["features"]["moat_evidence_score"])
        self.assertEqual(result["eligible_claim_count"], 0)
        self.assertEqual(result["ineligible_claim_count"], 1)

    def test_low_confidence_and_grade_c_claims_are_ineligible(self) -> None:
        a = self.store.add_claim(self.claim("OPTIONALITY", "new_product_or_platform", "New platform launched", confidence=0.6))
        b = self.store.add_claim(self.claim("OPTIONALITY", "export_expansion", "New export customer added", grade="C"))
        self.approve(a); self.approve(b)
        result = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-07-31")
        self.assertIsNone(result["features"]["new_product_export_optionalities_score"])
        self.assertEqual(result["ineligible_claim_count"], 2)

    def test_management_execution_remains_separate_even_with_three_linked_closures(self) -> None:
        for idx in range(3):
            promise_id = f"P{idx+1}"
            p = self.store.add_claim(self.claim("MANAGEMENT", "management_promise", f"Promise {idx+1}", promise_id=promise_id))
            d = self.store.add_claim(self.claim("MANAGEMENT", "management_delivery", f"Delivery evidence {idx+1}", promise_id=promise_id))
            self.approve(p); self.approve(d)
        result = self.store.derive_snapshot(isin="INE000A01001", as_of_date="2026-07-31")
        management = result["management_execution"]
        self.assertEqual(management["linked_closed_promise_ids"], 3)
        self.assertFalse(management["score_available"])
        self.assertIsNone(management["promise_delivery_pct"])
        self.assertEqual(management["reason"], "SEPARATE_MANAGEMENT_EXECUTION_ENGINE_REQUIRED")

    def test_document_date_after_known_at_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_claim(ProspectiveDocumentClaim(
                isin="INE000A01001",
                symbol="AAA",
                evidence_family="MOAT",
                evidence_category="proprietary_or_ip",
                statement="Test",
                source_url="https://issuer.example/test",
                document_type="FILING",
                document_date="2026-07-10",
                known_at="2026-07-01",
                source_grade="A",
                extraction_confidence=0.9,
                source_sha256=PIN,
            ))


if __name__ == "__main__":
    unittest.main()
