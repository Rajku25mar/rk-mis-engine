from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("review_validator",ROOT/"scripts/validate_qualitative_documentary_review_ledger.py")
MODULE=importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def relation(**overrides):
    base={
        "sample_isin":"INE1","sample_symbol":"ABC","known_at":"2023-07-01T10:00:00",
        "source_url":"https://archives.nseindia.com/a.pdf","source_sha256":"abc","page":3,
        "evidence_family":"runway","evidence_category":"committed_capacity_expansion",
        "pattern_id":"RUNWAY_COMMITTED_CAPACITY_EXPANSION_1","matched_terms":["capacity expansion","planned"],
        "company_attribution_flag":True,"action_or_status_flag":True,"negation_flag":False,
        "industry_or_peer_context_flag":False,"high_information_document_subject_flag":True,
    }
    base.update(overrides); return base


def relations_payload(rows):
    docs={}
    for r in rows:
        k=(r["sample_symbol"],r["source_sha256"])
        doc=docs.setdefault(k,{"sample_isin":r["sample_isin"],"sample_symbol":r["sample_symbol"],"known_at":r["known_at"],"source_url":r["source_url"],"source_sha256":r["source_sha256"],"subject":"Investor Presentation","page_relations":[]})
        page=next((p for p in doc["page_relations"] if p["page"]==r["page"]),None)
        if page is None:
            page={"page":r["page"],"relations":[]}; doc["page_relations"].append(page)
        page["relations"].append({k:v for k,v in r.items() if k not in {"sample_isin","sample_symbol","known_at","source_url","source_sha256","page"}})
    return {"documents":list(docs.values())}


def review_from(r,state="APPROVED"):
    keys=("sample_isin","sample_symbol","known_at","source_sha256","page","evidence_family","evidence_category","pattern_id")
    out={k:r[k] for k in keys}; out.update({"review_state":state,"review_note":"test"}); return out


class ReviewValidatorTests(unittest.TestCase):
    def test_approved_relation_maps_and_passes(self):
        r=relation()
        validated,summary=MODULE.validate(relations_payload([r]),{"reviews":[review_from(r)]},"2023-07-31")
        self.assertEqual(validated[0]["review_state"],"APPROVED")
        self.assertEqual(summary["report"]["symbols_with_any_approved_category"],1)

    def test_unmapped_approval_rejected(self):
        r=relation(); review=review_from(r); review["source_sha256"]="different"
        with self.assertRaises(MODULE.ReviewValidationError):
            MODULE.validate(relations_payload([r]),{"reviews":[review]},"2023-07-31")

    def test_action_required_category_cannot_be_approved_without_action(self):
        r=relation(action_or_status_flag=False)
        with self.assertRaises(MODULE.ReviewValidationError):
            MODULE.validate(relations_payload([r]),{"reviews":[review_from(r)]},"2023-07-31")

    def test_market_leader_without_company_attribution_rejected(self):
        r=relation(evidence_family="moat",evidence_category="market_position_or_limited_competition",pattern_id="MOAT_MARKET_POSITION_OR_LIMITED_COMPETITION_1",matched_terms=["market leader","segment"],company_attribution_flag=False,action_or_status_flag=False)
        with self.assertRaises(MODULE.ReviewValidationError):
            MODULE.validate(relations_payload([r]),{"reviews":[review_from(r)]},"2023-07-31")

    def test_two_approved_families_make_eligibility_possible(self):
        r1=relation()
        r2=relation(source_sha256="def",source_url="https://archives.nseindia.com/b.pdf",page=4,evidence_family="moat",evidence_category="customer_stickiness",pattern_id="MOAT_CUSTOMER_STICKINESS_1",matched_terms=["repeat orders"],action_or_status_flag=False)
        _,summary=MODULE.validate(relations_payload([r1,r2]),{"reviews":[review_from(r1),review_from(r2)]},"2023-07-31")
        self.assertEqual(summary["report"]["ranking_eligible_symbols_from_approved_categories"],1)

if __name__=="__main__": unittest.main()
