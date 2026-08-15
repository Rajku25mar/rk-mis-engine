from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bse_review_packet", ROOT / "scripts" / "build_bse_documentary_review_packet.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class BSEReviewPacketTests(unittest.TestCase):
    def test_excerpt_is_bounded_and_local(self) -> None:
        text = "A " * 150 + "order book stands at Rs 250 crore for execution over twelve months. " + "B " * 150
        out = MODULE.excerpt(text, ["order book"], 200)
        self.assertLessEqual(len(out), 201)
        self.assertIn("order book", out.lower())
        self.assertNotEqual(MODULE.clean(text), out)

    def test_page_records_find_reviewable_families(self) -> None:
        text = (
            "The company is undertaking capacity expansion at the new plant. "
            "The order book is Rs 300 crore. It has received vendor approval for a new customer."
        )
        rows = MODULE.page_records(text, 5, 240)
        families = {r["evidence_family"] for r in rows}
        self.assertIn("RUNWAY", families)
        self.assertIn("ORDER", families)
        self.assertIn("MOAT", families)
        self.assertIn("OPTIONALITY", families)
        self.assertTrue(all(len(r["context_excerpt"]) <= 241 for r in rows))


if __name__ == "__main__":
    unittest.main()
