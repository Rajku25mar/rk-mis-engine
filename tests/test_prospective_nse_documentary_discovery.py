from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prospective_nse_documentary", ROOT / "scripts" / "discover_prospective_nse_documentary.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ProspectiveNSEDocumentaryDiscoveryTests(unittest.TestCase):
    def test_watchlist_deduplicates_and_enforces_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "watch.json"
            p.write_text(json.dumps({"symbols": [
                {"symbol": " aaa ", "isin": "INE1"},
                {"symbol": "AAA", "isin": "INE1"},
                {"symbol": "BBB"},
            ]}), encoding="utf-8")
            rows = MODULE.load_watchlist(p, 2)
            self.assertEqual([r["symbol"] for r in rows], ["AAA", "BBB"])
            with self.assertRaises(MODULE.DiscoveryError):
                MODULE.load_watchlist(p, 1)

    def test_queue_candidates_are_pending_and_never_scores(self) -> None:
        document = {
            "symbol": "AAA",
            "isin": "INE000A01001",
            "company_name": "AAA Limited",
            "known_at": "2026-08-01T10:00:00",
            "subject": "Investor Presentation",
            "source_url": "https://nsearchives.nseindia.com/test.pdf",
            "source_sha256": "a" * 64,
            "document_type": "INVESTOR_PRESENTATION",
            "page_candidates": [{
                "page": 7,
                "runway": {"committed_capacity_expansion": [["capacity", "expansion"]]},
                "moat": {"proprietary_or_ip": [["proprietary"]]},
                "orderbook_numeric_candidate": {"order_terms": ["order book"], "currency_values_on_page": [{"value": "100", "unit": "CR"}]},
            }],
        }
        rows = MODULE.flatten_page_candidates(document)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["review_state"] == "PENDING" for r in rows))
        self.assertTrue(all(r["score_eligible"] is False for r in rows))
        self.assertEqual(len({r["basis_key"] for r in rows}), 1)
        self.assertEqual({r["page"] for r in rows}, {7})

    def test_document_type_inference(self) -> None:
        self.assertEqual(MODULE.infer_document_type("Investor Presentation"), "INVESTOR_PRESENTATION")
        self.assertEqual(MODULE.infer_document_type("Award of Order"), "ORDER_OR_CONTRACT_UPDATE")
        self.assertEqual(MODULE.infer_document_type("General Update"), "EXCHANGE_ANNOUNCEMENT_ATTACHMENT")


if __name__ == "__main__":
    unittest.main()
