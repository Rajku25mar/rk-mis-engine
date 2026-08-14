from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import probe_smart_money_4q_coverage as probe


class SmartMoneyCoverageTests(unittest.TestCase):
    def test_latest_distinct_safe_xbrls_excludes_post_anchor_revision(self) -> None:
        rows = [
            {
                "date": "30-Jun-2021",
                "submissionDate": "15-Jul-2021",
                "broadcastDate": "15-Jul-2021",
                "xbrl": "https://nsearchives.nseindia.com/a.xml",
            },
            {
                "date": "30-Sep-2021",
                "submissionDate": "15-Oct-2021",
                "broadcastDate": "15-Oct-2021",
                "xbrl": "https://nsearchives.nseindia.com/b.xml",
            },
            {
                "date": "31-Dec-2021",
                "submissionDate": "15-Jan-2022",
                "broadcastDate": "15-Jan-2022",
                "xbrl": "https://nsearchives.nseindia.com/c.xml",
            },
            {
                "date": "31-Mar-2022",
                "submissionDate": "15-Apr-2022",
                "broadcastDate": "15-Apr-2022",
                "xbrl": "https://nsearchives.nseindia.com/d-old.xml",
            },
            {
                "date": "31-Mar-2022",
                "submissionDate": "20-Apr-2022",
                "broadcastDate": "20-Apr-2022",
                "xbrl": "https://nsearchives.nseindia.com/d-new.xml",
            },
            {
                "date": "30-Jun-2022",
                "submissionDate": "15-Jul-2022",
                "broadcastDate": "15-Jul-2022",
                "xbrl": "https://nsearchives.nseindia.com/e.xml",
            },
            {
                "date": "30-Jun-2022",
                "submissionDate": "15-Jul-2022",
                "broadcastDate": "15-Jul-2022",
                "revisionDate": "05-Aug-2022",
                "xbrl": "https://nsearchives.nseindia.com/e-post-anchor-revision.xml",
            },
        ]
        selected = probe.latest_distinct_safe_xbrls(rows, "2022-07-29", 5)
        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[-2]["source_url"], "https://nsearchives.nseindia.com/d-new.xml")
        self.assertEqual(selected[-1]["source_url"], "https://nsearchives.nseindia.com/e.xml")

    def test_locked_mappings_must_each_be_unambiguous(self) -> None:
        xml = b'''<?xml version="1.0"?>
        <xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
              xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
              xmlns:shp="urn:test">
          <xbrli:context id="MF"><xbrli:entity><xbrli:identifier scheme="x">x</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="shp:CategoryOfShareholdersAxis">shp:MutualFundsOrUtiMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity></xbrli:context>
          <xbrli:context id="FII"><xbrli:entity><xbrli:identifier scheme="x">x</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="shp:CategoryOfShareholdersAxis">shp:InstitutionsForeignPortfolioInvestorMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity></xbrli:context>
          <xbrli:context id="INST"><xbrli:entity><xbrli:identifier scheme="x">x</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="shp:CategoryOfShareholdersAxis">shp:InstitutionsMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity></xbrli:context>
          <shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="MF" unitRef="pure">3.1</shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
          <shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="FII" unitRef="pure">8.2</shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
          <shp:NumberOfShareholders contextRef="INST" unitRef="shares">42</shp:NumberOfShareholders>
        </xbrl>'''
        lock = {
            "approved_component_mappings": {
                "mf_holding_change_pp_4q": {"category_member": "MutualFundsOrUtiMember", "fact": "ShareholdingAsAPercentageOfTotalNumberOfShares"},
                "fii_holding_change_pp_4q": {"category_member": "InstitutionsForeignPortfolioInvestorMember", "fact": "ShareholdingAsAPercentageOfTotalNumberOfShares"},
                "institutional_breadth_change_4q": {"category_member": "InstitutionsMember", "fact": "NumberOfShareholders"},
            }
        }
        mapped, source_hash = probe.mapping_complete_for_period(xml, lock)
        self.assertEqual(mapped, {
            "mf_holding_change_pp_4q": True,
            "fii_holding_change_pp_4q": True,
            "institutional_breadth_change_4q": True,
        })
        self.assertEqual(len(source_hash), 64)


if __name__ == "__main__":
    unittest.main()
