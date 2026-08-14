from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "rk_mis_qualitative_review_validator",
    ROOT / "scripts/validate_qualitative_documentary_review_ledger.py",
)
VALIDATOR = importlib.util.module_from_spec(VALIDATOR_SPEC)
assert VALIDATOR_SPEC and VALIDATOR_SPEC.loader
VALIDATOR_SPEC.loader.exec_module(VALIDATOR)


class ReviewCompilerError(RuntimeError):
    pass


def _decision(relation: dict[str, Any], anchor: str) -> tuple[str, str]:
    known_at = str(relation.get("known_at") or "")[:10]
    if not known_at or known_at > anchor:
        return "REJECTED", "POST_ANCHOR_OR_MISSING_KNOWN_AT"

    guard_ok, guard_reason = VALIDATOR.category_guard_passes(relation)
    if not guard_ok:
        return "REJECTED", f"FROZEN_GUARD:{guard_reason}"

    family = str(relation.get("evidence_family") or "")
    category = str(relation.get("evidence_category") or "")
    action = bool(relation.get("action_or_status_flag"))
    attr = bool(relation.get("company_attribution_flag"))
    high_info = bool(relation.get("high_information_document_subject_flag"))
    matched = " ".join(str(x).lower() for x in (relation.get("matched_terms") or []))

    # The strict relation extractor already requires the exact frozen multi-term
    # pattern for each category. This compiler deliberately adds another conservative
    # layer rather than inferring beyond those structured terms.
    if family == "runway":
        if category == "committed_capacity_expansion" and not action:
            return "REJECTED", "CAPACITY_EXPANSION_WITHOUT_ACTION_STATUS"
        if category == "funding_visibility" and not any(x in matched for x in ("internal accrual", "cash accrual", "term loan", "sanctioned", "fund rais", "capital raise")):
            return "PENDING", "FUNDING_SOURCE_NOT_EXPLICIT_IN_MATCHED_TERMS"
        if category in {"physical_or_operational_headroom", "ramp_or_utilisation_headroom"} and not (attr or high_info):
            return "PENDING", "HEADROOM_COMPANY_LINKAGE_AMBIGUOUS"

    if family == "moat":
        if category == "qualification_or_regulatory_barrier" and any(x in matched for x in ("iso", "certification")) and not any(x in matched for x in ("approved vendor", "vendor approval", "approved supplier", "qualified vendor", "qualified supplier", "qualified by", "approved by", "regulatory approval")):
            return "REJECTED", "GENERIC_CERTIFICATION_NOT_A_QUALIFICATION_BARRIER"
        if category == "market_position_or_limited_competition" and "market leader" in matched and not attr:
            return "REJECTED", "MARKET_LEADER_WITHOUT_COMPANY_ATTRIBUTION"

    if family == "optionality":
        if category in {"new_product_or_platform", "new_geography"} and not action:
            return "REJECTED", "OPTIONALITY_CATEGORY_WITHOUT_ACTION_STATUS"
        if category == "export_expansion" and not any(x in matched for x in ("export expansion", "export growth", "export market", "export customer", "new export")):
            return "PENDING", "EXPORT_ACTION_NOT_EXPLICIT"

    # APPROVED here means 'mechanically approved under the frozen blinded policy'.
    # It is not an official RK-MIS score and does not imply a future return claim.
    return "APPROVED", "PASSES_FROZEN_STRICT_RELATION_AND_REVIEW_GUARDS"


def build_ledger(relations_payload: dict[str, Any], review_policy: dict[str, Any], anchor: str) -> dict[str, Any]:
    if review_policy.get("status") != "LOCKED_BEFORE_RELATION_RESULT_REVIEW_AND_OUTCOME_LOAD":
        raise ReviewCompilerError("review policy is not frozen")
    universe = VALIDATOR.flatten_relations(relations_payload)
    reviews = []
    for _, relation in sorted(universe.items(), key=lambda item: item[0]):
        state, note = _decision(relation, anchor)
        reviews.append({
            "sample_isin": relation.get("sample_isin"),
            "sample_symbol": relation.get("sample_symbol"),
            "known_at": relation.get("known_at"),
            "source_url": relation.get("source_url"),
            "source_sha256": relation.get("source_sha256"),
            "page": relation.get("page"),
            "evidence_family": relation.get("evidence_family"),
            "evidence_category": relation.get("evidence_category"),
            "pattern_id": relation.get("pattern_id"),
            "review_state": state,
            "review_note": note,
        })
    return {
        "version": "rk-mis-blinded-qualitative-model-review-ledger-v1",
        "anchor_date": anchor,
        "future_outcomes_seen": False,
        "review_method": "CONSERVATIVE_STRUCTURED_RELATION_POLICY_COMPILER",
        "coverage_target_used_for_decisions": False,
        "reviews": reviews,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Build a conservative blinded qualitative review ledger from frozen structured relations")
    p.add_argument("--relations", type=Path, required=True)
    p.add_argument("--review-policy", type=Path, required=True)
    p.add_argument("--anchor", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    relations = json.loads(args.relations.read_text(encoding="utf-8"))
    policy = json.loads(args.review_policy.read_text(encoding="utf-8"))
    ledger = build_ledger(relations, policy, args.anchor)

    # Validate the generated ledger immediately against the frozen validator. This
    # guarantees the compiler cannot approve a relation that the locked guard rejects.
    _, summary = VALIDATOR.validate(relations, ledger, args.anchor)
    ledger["validator_report"] = summary["report"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    print(json.dumps(summary["report"], indent=2))


if __name__ == "__main__":
    main()
