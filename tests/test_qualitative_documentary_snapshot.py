from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qual_snapshot", ROOT / "scripts/build_qualitative_documentary_snapshot.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class QualitativeSnapshotTests(unittest.TestCase):
    def protocol(self):
        return {
            "anchor_date":"2023-07-31",
            "features":[
                {"field":"reinvestment_runway_score","effective_100_point_weight":2.4,"direction":"higher","bands":[[80,1.0],[65,.75],[50,.5],[35,.25]]},
                {"field":"moat_evidence_score","effective_100_point_weight":4.2,"direction":"higher","bands":[[85,1.0],[70,.75],[55,.5],[40,.25]]},
                {"field":"new_product_export_optionalities_score","effective_100_point_weight":2.0,"direction":"higher","bands":[[80,1.0],[65,.75],[50,.5],[35,.25]]},
            ],
            "ranking_eligibility":{"minimum_covered_features":2,"must_cover_runway_or_moat":True},
        }

    def test_duplicate_category_counts_once(self):
        reviews=[
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-01","review_state":"APPROVED","evidence_family":"runway","evidence_category":"funding_visibility"},
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-02","review_state":"APPROVED","evidence_family":"runway","evidence_category":"funding_visibility"},
        ]
        feat=MODULE.build_features(reviews,"2023-07-31")
        self.assertEqual(feat["reinvestment_runway_score"],20.0)

    def test_two_features_with_runway_is_eligible(self):
        reviews=[
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-01","review_state":"APPROVED","evidence_family":"runway","evidence_category":"funding_visibility"},
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-02","review_state":"APPROVED","evidence_family":"runway","evidence_category":"committed_capacity_expansion"},
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-03","review_state":"APPROVED","evidence_family":"optionality","evidence_category":"new_product_or_platform"},
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-04","review_state":"APPROVED","evidence_family":"optionality","evidence_category":"export_expansion"},
        ]
        rows,manifest=MODULE.build_snapshot(self.protocol(),reviews)
        self.assertTrue(rows[0]["ranking_eligible"])
        self.assertEqual(set(rows[0]["covered_features"]),{"reinvestment_runway_score","new_product_export_optionalities_score"})
        self.assertEqual(manifest["ranking_eligible_rows"],1)

    def test_one_feature_not_eligible(self):
        reviews=[
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-01","review_state":"APPROVED","evidence_family":"moat","evidence_category":"proprietary_or_ip"},
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-02","review_state":"APPROVED","evidence_family":"moat","evidence_category":"customer_stickiness"},
        ]
        rows,_=MODULE.build_snapshot(self.protocol(),reviews)
        self.assertFalse(rows[0]["ranking_eligible"])

    def test_pending_and_post_anchor_do_not_score(self):
        reviews=[
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-01","review_state":"PENDING","evidence_family":"runway","evidence_category":"funding_visibility"},
            {"sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-08-01","review_state":"APPROVED","evidence_family":"moat","evidence_category":"proprietary_or_ip"},
        ]
        feat=MODULE.build_features(reviews,"2023-07-31")
        self.assertIsNone(feat["reinvestment_runway_score"])
        self.assertIsNone(feat["moat_evidence_score"])


if __name__ == "__main__":
    unittest.main()
