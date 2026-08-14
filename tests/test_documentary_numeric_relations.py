from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("numeric_relations",ROOT/"scripts/extract_documentary_numeric_relations.py")
MODULE=importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

class NumericRelationTests(unittest.TestCase):
    def test_explicit_capacity_increase_by_percent(self):
        rows=MODULE.capacity_relations("The expansion will increase production capacity by 60% when commissioned.")
        self.assertTrue(any(r["pattern_id"]=="CAPACITY_INCREASE_BY_PERCENT" and r["planned_capacity_increase_pct_candidate"]==60.0 for r in rows))

    def test_growth_guidance_not_capacity_metric(self):
        rows=MODULE.capacity_relations("We expect revenue growth of 30% to 35%. Our capacity expansion is progressing well.")
        self.assertFalse(any(r["pattern_id"]=="CAPACITY_INCREASE_BY_PERCENT" for r in rows))

    def test_capacity_from_to_same_unit(self):
        rows=MODULE.capacity_relations("Installed capacity will increase from 100000 TPA to 160000 TPA after the expansion.")
        rel=next(r for r in rows if r["pattern_id"]=="CAPACITY_FROM_TO")
        self.assertEqual(rel["planned_capacity_increase_pct_candidate"],60.0)
        self.assertEqual(rel["capacity_unit"],"tpa")

    def test_capacity_from_to_mismatched_units_rejected(self):
        rows=MODULE.capacity_relations("Capacity will move from 100 MW to 1 GW after the expansion.")
        self.assertFalse(any(r["pattern_id"]=="CAPACITY_FROM_TO" for r in rows))

    def test_orderbook_value(self):
        rows=MODULE.orderbook_relations("The order book stands at INR 1,250 crore as of June 2023.")
        self.assertTrue(any(r["orderbook_value_candidate"]==1250.0 and r["scale"]=="CRORE" for r in rows))

    def test_pipeline_is_not_orderbook(self):
        rows=MODULE.orderbook_relations("The tender pipeline and order book opportunity is INR 2,000 crore.")
        self.assertEqual(rows,[])

if __name__=="__main__":
    unittest.main()
