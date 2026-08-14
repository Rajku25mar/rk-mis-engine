from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "documentary_extraction", ROOT / "scripts/extract_documentary_forward_evidence_candidates.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class DocumentaryExtractionTests(unittest.TestCase):
    def test_triage_prefers_high_information_documents(self) -> None:
        rows = [
            {"sample_symbol":"ABC","subject":"General updates","known_at":"2023-01-01","attachment_url":"https://nsearchives.nseindia.com/a.pdf","keyword_families":[],"keyword_hits":{}},
            {"sample_symbol":"ABC","subject":"Investor Presentation","known_at":"2023-02-01","attachment_url":"https://nsearchives.nseindia.com/b.pdf","keyword_families":[],"keyword_hits":{}},
            {"sample_symbol":"ABC","subject":"Press Release","known_at":"2023-03-01","attachment_url":"https://nsearchives.nseindia.com/c.pdf","keyword_families":["runway"],"keyword_hits":{"runway":["capacity"]}},
            {"sample_symbol":"ABC","subject":"Updates","known_at":"2023-04-01","attachment_url":"https://nsearchives.nseindia.com/d.pdf","keyword_families":["moat"],"keyword_hits":{"moat":["patent"]}},
        ]
        lock = {"maximum_documents_per_symbol":2}
        selected = MODULE.apply_triage(rows, lock)
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]["attachment_url"], "https://nsearchives.nseindia.com/c.pdf")
        # Frozen triage: Investor Presentation = 12 priority points; a plain
        # one-family moat metadata hit = 10. Therefore b.pdf correctly ranks second.
        self.assertEqual(selected[1]["attachment_url"], "https://nsearchives.nseindia.com/b.pdf")

    def test_generic_approval_optionality_alone_is_not_downloaded(self) -> None:
        row = {"subject":"Dividend","attachment_url":"https://nsearchives.nseindia.com/a.pdf","keyword_families":["optionality"],"keyword_hits":{"optionality":["approval"]}}
        self.assertFalse(MODULE.triage_included(row))

    def test_page_pattern_candidates_are_not_scores(self) -> None:
        text = "The company is undertaking a greenfield capacity expansion. The plant will ramp up over the next year."
        hits = MODULE.page_matches(text, MODULE.RUNWAY_PATTERNS)
        self.assertIn("committed_capacity_expansion", hits)
        self.assertIn("ramp_or_utilisation_headroom", hits)
        self.assertNotIn("score", hits)

    def test_orderbook_numeric_candidate(self) -> None:
        text = "The order book stands at INR 1,250 crore as at June 2023."
        out = MODULE.numeric_page_candidates(text)
        self.assertIn("orderbook_numeric_candidate", out)
        vals = out["orderbook_numeric_candidate"]["currency_values_on_page"]
        self.assertTrue(any(x["value"] == "1250" for x in vals))

    def test_capacity_numeric_candidate(self) -> None:
        text = "Installed capacity is 100,000 TPA and the expansion is expected to increase capacity by 60%."
        out = MODULE.numeric_page_candidates(text)
        self.assertIn("capacity_numeric_candidate", out)
        self.assertIn(60.0, out["capacity_numeric_candidate"]["percent_values_on_page"])
        self.assertTrue(any(x["unit"] == "TPA" for x in out["capacity_numeric_candidate"]["capacity_values_on_page"]))


if __name__ == "__main__":
    unittest.main()
