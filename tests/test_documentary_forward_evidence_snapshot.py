from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "documentary_snapshot", ROOT / "scripts/build_documentary_forward_evidence_snapshot.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class DocumentarySnapshotTests(unittest.TestCase):
    def test_qualitative_category_counts_once(self) -> None:
        records = [
            {"review_state":"APPROVED","known_at":"2023-07-01","evidence_family":"moat","evidence_category":"proprietary_or_ip"},
            {"review_state":"APPROVED","known_at":"2023-07-02","evidence_family":"moat","evidence_category":"proprietary_or_ip"},
            {"review_state":"APPROVED","known_at":"2023-07-03","evidence_family":"moat","evidence_category":"customer_stickiness"},
        ]
        score = MODULE.qualitative_score(records, "moat", MODULE.MOAT_CATEGORIES)
        self.assertEqual(score, 40.0)

    def test_post_anchor_approval_is_excluded(self) -> None:
        records = [
            {"review_state":"APPROVED","known_at":"2023-08-01","evidence_family":"runway","evidence_category":"committed_capacity_expansion"}
        ]
        self.assertEqual(MODULE.approved(records, "2023-07-31"), [])

    def test_numeric_direct_ratio_preferred(self) -> None:
        records = [
            {"metric_name":"orderbook_to_sales","metric_value":2.5,"known_at":"2023-07-20"},
            {"metric_name":"orderbook_cr","metric_value":100,"known_at":"2023-07-20"},
            {"metric_name":"revenue_cr","metric_value":100,"known_at":"2023-07-20"},
        ]
        self.assertEqual(MODULE.numeric_metric(records, "orderbook_to_sales"), 2.5)

    def test_capacity_components_require_same_unit(self) -> None:
        records = [
            {"metric_name":"incremental_capacity","metric_value":60,"metric_unit":"TPA","known_at":"2023-07-20"},
            {"metric_name":"pre_expansion_capacity","metric_value":100,"metric_unit":"MW","known_at":"2023-07-20"},
        ]
        self.assertIsNone(MODULE.numeric_metric(records, "planned_capacity_increase_pct"))

    def test_ranking_requires_numeric_and_documentary_coverage(self) -> None:
        protocol = {
            "anchor_date":"2023-07-31",
            "features":[
                {"field":"orderbook_to_sales","experimental_slice_weight":20,"direction":"higher","bands":[[3,1],[2,.8],[1,.5],[.5,.2]]},
                {"field":"planned_capacity_increase_pct","experimental_slice_weight":20,"direction":"higher","bands":[[100,1],[60,.8],[30,.5],[15,.25]]},
                {"field":"reinvestment_runway_score","experimental_slice_weight":20,"direction":"higher","bands":[[80,1],[65,.75],[50,.5],[35,.25]]},
                {"field":"moat_evidence_score","experimental_slice_weight":20,"direction":"higher","bands":[[85,1],[70,.75],[55,.5],[40,.25]]},
                {"field":"new_product_export_optionalities_score","experimental_slice_weight":20,"direction":"higher","bands":[[80,1],[65,.75],[50,.5],[35,.25]]},
            ],
            "ranking_eligibility":{
                "minimum_feature_coverage_count":3,
                "must_include_at_least_one_numeric_catalyst":True,
                "must_include_at_least_one_approved_documentary_score":True,
            },
        }
        reviews = [
            {"sample_isin":"INE000A01001","sample_symbol":"ABC","review_state":"APPROVED","known_at":"2023-07-01","evidence_family":"numeric_catalyst","evidence_category":"orderbook","metric_name":"orderbook_to_sales","metric_value":2.2},
            {"sample_isin":"INE000A01001","sample_symbol":"ABC","review_state":"APPROVED","known_at":"2023-07-02","evidence_family":"runway","evidence_category":"committed_capacity_expansion"},
            {"sample_isin":"INE000A01001","sample_symbol":"ABC","review_state":"APPROVED","known_at":"2023-07-03","evidence_family":"runway","evidence_category":"funding_visibility"},
            {"sample_isin":"INE000A01001","sample_symbol":"ABC","review_state":"APPROVED","known_at":"2023-07-04","evidence_family":"moat","evidence_category":"proprietary_or_ip"},
            {"sample_isin":"INE000A01001","sample_symbol":"ABC","review_state":"APPROVED","known_at":"2023-07-05","evidence_family":"moat","evidence_category":"customer_stickiness"},
        ]
        rows, manifest = MODULE.build_snapshot(protocol, reviews)
        self.assertTrue(rows[0]["ranking_eligible"])
        self.assertIsNotNone(rows[0]["documentary_partial_score"])
        self.assertEqual(manifest["ranking_eligible_rows"], 1)


if __name__ == "__main__":
    unittest.main()
