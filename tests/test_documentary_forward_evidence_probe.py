from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "documentary_probe", ROOT / "scripts/probe_documentary_forward_evidence.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

PROTOCOL = {
    "discovery": {
        "priority_subjects": ["Investor Presentation", "Press Release", "Award of Order / Receipt of Order"],
        "keyword_families": {
            "runway": ["capacity", "expansion", "capex"],
            "moat": ["patent", "certification", "approved vendor"],
            "orders": ["order book", "purchase order"],
            "optionality": ["new product", "export"],
        },
    }
}


class DocumentaryProbeTests(unittest.TestCase):
    def test_normalize_nse_announcement(self) -> None:
        raw = {
            "symbol": "ABC",
            "sm_name": "ABC Limited",
            "sm_isin": "INE000A01001",
            "desc": "Investor Presentation",
            "attchmntText": "Capacity expansion and export plan",
            "attchmntFile": "https://archives.nseindia.com/corporate/ABC_1.pdf",
            "exchdisstime": "31-Jul-2023 18:42:10",
        }
        row = MODULE.normalize_announcement(raw)
        self.assertEqual(row["known_at"], "2023-07-31T18:42:10")
        self.assertEqual(row["symbol"], "ABC")
        self.assertTrue(MODULE.official_attachment(row["attachment_url"]))

    def test_post_anchor_announcement_rejected(self) -> None:
        row = {
            "symbol": "ABC",
            "isin": "INE000A01001",
            "company_name": "ABC Limited",
            "subject": "Investor Presentation",
            "details": "capacity expansion",
            "known_at": "2023-08-01T09:00:00",
            "attachment_url": "https://archives.nseindia.com/corporate/ABC_2.pdf",
        }
        selected = MODULE.select_candidates([row], PROTOCOL, "2023-07-31")
        self.assertEqual(selected, [])

    def test_non_nse_attachment_rejected(self) -> None:
        row = {
            "symbol": "ABC",
            "isin": "INE000A01001",
            "company_name": "ABC Limited",
            "subject": "Investor Presentation",
            "details": "capacity expansion",
            "known_at": "2023-07-30T09:00:00",
            "attachment_url": "https://example.com/ABC.pdf",
        }
        selected = MODULE.select_candidates([row], PROTOCOL, "2023-07-31")
        self.assertEqual(selected, [])

    def test_keyword_candidate_enters_review_queue_not_score(self) -> None:
        row = {
            "symbol": "ABC",
            "isin": "INE000A01001",
            "company_name": "ABC Limited",
            "subject": "Press Release",
            "details": "The company announced capacity expansion and a new product for export markets.",
            "known_at": "2023-07-30T09:00:00",
            "attachment_url": "https://archives.nseindia.com/corporate/ABC_3.pdf",
        }
        selected = MODULE.select_candidates([row], PROTOCOL, "2023-07-31")
        self.assertEqual(len(selected), 1)
        self.assertEqual(set(selected[0]["keyword_families"]), {"runway", "optionality"})
        self.assertNotIn("score", selected[0])


if __name__ == "__main__":
    unittest.main()
