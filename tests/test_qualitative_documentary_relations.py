from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualitative_relations", ROOT / "scripts/extract_qualitative_documentary_relations.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class QualitativeRelationTests(unittest.TestCase):
    def categories(self, text, subject="Investor Presentation"):
        return {(x["evidence_family"], x["evidence_category"]) for x in MODULE.relation_candidates(text, subject)}

    def test_committed_capacity_expansion(self):
        cats = self.categories("We are implementing a brownfield capacity expansion at our existing plant.")
        self.assertIn(("runway", "committed_capacity_expansion"), cats)

    def test_generic_capacity_opportunity_rejected(self):
        cats = self.categories("The industry has a large capacity expansion opportunity over the next decade.")
        self.assertNotIn(("runway", "committed_capacity_expansion"), cats)

    def test_funding_visibility(self):
        cats = self.categories("Our expansion capex will be funded through internal accruals.")
        self.assertIn(("runway", "funding_visibility"), cats)

    def test_repeat_order_is_stickiness(self):
        cats = self.categories("The company received repeat orders from a strategic customer.")
        self.assertIn(("moat", "customer_stickiness"), cats)

    def test_generic_iso_certification_not_qualification_barrier(self):
        cats = self.categories("The facility is ISO 9001 certified.")
        self.assertNotIn(("moat", "qualification_or_regulatory_barrier"), cats)

    def test_approved_vendor_is_qualification_barrier(self):
        cats = self.categories("We have become an approved vendor for the customer.")
        self.assertIn(("moat", "qualification_or_regulatory_barrier"), cats)

    def test_market_leader(self):
        cats = self.categories("The company is the market leader in the niche product segment.")
        self.assertIn(("moat", "market_position_or_limited_competition"), cats)

    def test_generic_leading_player_not_market_leader(self):
        cats = self.categories("We are a leading player with a strong brand.")
        self.assertNotIn(("moat", "market_position_or_limited_competition"), cats)

    def test_new_product_requires_action(self):
        cats = self.categories("We launched a new product platform for industrial customers.")
        self.assertIn(("optionality", "new_product_or_platform"), cats)
        cats2 = self.categories("New product categories include several opportunities.")
        self.assertNotIn(("optionality", "new_product_or_platform"), cats2)

    def test_export_mix_alone_not_expansion(self):
        cats = self.categories("Exports account for 35% of revenue.")
        self.assertNotIn(("optionality", "export_expansion"), cats)

    def test_new_export_market(self):
        cats = self.categories("We secured a new export customer and are entering a new export market.")
        self.assertIn(("optionality", "export_expansion"), cats)


if __name__ == "__main__":
    unittest.main()
