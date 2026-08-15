from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bse_discovery", ROOT / "scripts" / "discover_prospective_bse_documentary.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class BSEDocumentaryDiscoveryTests(unittest.TestCase):
    def test_attachment_candidates_are_official_only(self) -> None:
        row = {
            "NSURL": "https://www.bseindia.com/xml-data/corpfiling/AttachLive/test.pdf",
            "ATTACHMENTNAME": "another.pdf",
        }
        urls = MODULE.attachment_candidates(row)
        self.assertIn("https://www.bseindia.com/xml-data/corpfiling/AttachLive/test.pdf", urls)
        self.assertIn("https://www.bseindia.com/xml-data/corpfiling/AttachLive/another.pdf", urls)
        self.assertIn("https://www.bseindia.com/xml-data/corpfiling/AttachHis/another.pdf", urls)
        self.assertTrue(all(MODULE.official_bse_url(url) for url in urls))

    def test_non_bse_absolute_url_is_rejected(self) -> None:
        self.assertFalse(MODULE.official_bse_url("https://example.com/a.pdf"))
        self.assertTrue(MODULE.official_bse_url("https://api.bseindia.com/a"))

    def test_timestamp_normalization(self) -> None:
        self.assertTrue(MODULE.normalize_timestamp("2026-08-15T10:30:00").startswith("2026-08-15T10:30:00"))
        self.assertTrue(MODULE.normalize_timestamp("15/08/2026 10:30:00").startswith("2026-08-15T10:30:00"))

    def test_priority_detects_order_and_presentation(self) -> None:
        policy = {
            "discovery": {
                "priority_subject_fragments": ["Investor Presentation", "Award_of_Order_Receipt_of_Order"],
                "keyword_families": {"orders": ["order book"]},
            }
        }
        score, hits = MODULE.priority({"NEWSSUB": "Announcement under Regulation 30 (LODR)-Award_of_Order_Receipt_of_Order"}, policy)
        self.assertEqual(score, 2)
        self.assertTrue(hits)


if __name__ == "__main__":
    unittest.main()
