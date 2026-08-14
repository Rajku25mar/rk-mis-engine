from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SPEC = importlib.util.spec_from_file_location(
    "blinded_review",
    ROOT / "scripts/build_blinded_qualitative_review_ledger.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

POLICY = {"status": "LOCKED_BEFORE_RELATION_RESULT_REVIEW_AND_OUTCOME_LOAD"}


def payload(relation):
    return {
        "documents": [{
            "sample_isin": "INE000000001",
            "sample_symbol": "TEST",
            "known_at": "2023-07-01T10:00:00",
            "source_url": "https://nsearchives.nseindia.com/test.pdf",
            "source_sha256": "abc",
            "subject": "Investor Presentation",
            "page_relations": [{"page": 3, "relations": [relation]}],
        }]
    }


class BlindedReviewCompilerTests(unittest.TestCase):
    def test_committed_capacity_with_action_can_be_approved(self):
        rel = {
            "evidence_family": "runway",
            "evidence_category": "committed_capacity_expansion",
            "pattern_id": "RUNWAY_COMMITTED_CAPACITY_EXPANSION_1",
            "matched_terms": ["capacity expansion", "planned"],
            "company_attribution_flag": True,
            "action_or_status_flag": True,
            "negation_flag": False,
            "industry_or_peer_context_flag": False,
            "high_information_document_subject_flag": True,
        }
        ledger = MODULE.build_ledger(payload(rel), POLICY, "2023-07-31")
        self.assertEqual(ledger["reviews"][0]["review_state"], "APPROVED")

    def test_market_leader_without_attribution_is_rejected(self):
        rel = {
            "evidence_family": "moat",
            "evidence_category": "market_position_or_limited_competition",
            "pattern_id": "MOAT_MARKET_POSITION_OR_LIMITED_COMPETITION_1",
            "matched_terms": ["market leader", "segment"],
            "company_attribution_flag": False,
            "action_or_status_flag": False,
            "negation_flag": False,
            "industry_or_peer_context_flag": False,
            "high_information_document_subject_flag": True,
        }
        ledger = MODULE.build_ledger(payload(rel), POLICY, "2023-07-31")
        self.assertEqual(ledger["reviews"][0]["review_state"], "REJECTED")

    def test_post_anchor_relation_is_rejected(self):
        rel = {
            "evidence_family": "moat",
            "evidence_category": "customer_stickiness",
            "pattern_id": "MOAT_CUSTOMER_STICKINESS_1",
            "matched_terms": ["repeat orders"],
            "company_attribution_flag": True,
            "action_or_status_flag": False,
            "negation_flag": False,
            "industry_or_peer_context_flag": False,
            "high_information_document_subject_flag": True,
        }
        p = payload(rel)
        p["documents"][0]["known_at"] = "2023-08-01T10:00:00"
        ledger = MODULE.build_ledger(p, POLICY, "2023-07-31")
        self.assertEqual(ledger["reviews"][0]["review_state"], "REJECTED")

    def test_negated_relation_can_never_be_approved(self):
        rel = {
            "evidence_family": "optionality",
            "evidence_category": "new_product_or_platform",
            "pattern_id": "OPTIONALITY_NEW_PRODUCT_OR_PLATFORM_1",
            "matched_terms": ["new product", "launch"],
            "company_attribution_flag": True,
            "action_or_status_flag": True,
            "negation_flag": True,
            "industry_or_peer_context_flag": False,
            "high_information_document_subject_flag": True,
        }
        ledger = MODULE.build_ledger(payload(rel), POLICY, "2023-07-31")
        self.assertEqual(ledger["reviews"][0]["review_state"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
