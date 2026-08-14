from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


class ReviewValidationError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relation_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("sample_isin") or "").upper(),
        str(row.get("sample_symbol") or "").upper(),
        str(row.get("known_at") or ""),
        str(row.get("source_sha256") or ""),
        str(row.get("page") or ""),
        str(row.get("evidence_family") or ""),
        str(row.get("evidence_category") or ""),
        str(row.get("pattern_id") or ""),
    )


def flatten_relations(payload: dict[str, Any]) -> dict[tuple[str, ...], dict[str, Any]]:
    out: dict[tuple[str, ...], dict[str, Any]] = {}
    for doc in payload.get("documents") or []:
        base = {
            "sample_isin": doc.get("sample_isin"),
            "sample_symbol": doc.get("sample_symbol"),
            "known_at": doc.get("known_at"),
            "source_url": doc.get("source_url"),
            "source_sha256": doc.get("source_sha256"),
            "document_subject": doc.get("subject"),
        }
        for page in doc.get("page_relations") or []:
            page_no = page.get("page")
            for rel in page.get("relations") or []:
                row = {**base, **rel, "page": page_no}
                key = relation_key(row)
                if key in out:
                    raise ReviewValidationError(f"duplicate structured relation key: {key}")
                out[key] = row
    return out


def category_guard_passes(relation: dict[str, Any]) -> tuple[bool, str]:
    if relation.get("negation_flag"):
        return False, "NEGATION_FLAG"
    if relation.get("industry_or_peer_context_flag") and not relation.get("company_attribution_flag"):
        return False, "UNATTRIBUTED_INDUSTRY_OR_PEER_CONTEXT"
    if not (relation.get("company_attribution_flag") or relation.get("high_information_document_subject_flag")):
        return False, "NO_COMPANY_ATTRIBUTION_OR_HIGH_INFORMATION_DOCUMENT_CONTEXT"

    family = relation.get("evidence_family")
    category = relation.get("evidence_category")
    matched = " ".join(str(x) for x in (relation.get("matched_terms") or [])).lower()
    action = bool(relation.get("action_or_status_flag"))

    if (family, category) in {
        ("runway", "committed_capacity_expansion"),
        ("optionality", "new_product_or_platform"),
        ("optionality", "new_geography"),
    } and not action:
        return False, "CATEGORY_REQUIRES_ACTION_OR_STATUS_FLAG"

    if (family, category) == ("moat", "market_position_or_limited_competition"):
        inherently_company_specific = any(
            phrase in matched
            for phrase in ("sole supplier", "single-source", "single source", "only manufacturer", "only producer", "limited qualified")
        )
        if not relation.get("company_attribution_flag") and not inherently_company_specific:
            return False, "MARKET_LEADER_REQUIRES_COMPANY_ATTRIBUTION"

    return True, "PASS"


def validate(relations_payload: dict[str, Any], ledger: dict[str, Any], anchor: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    universe = flatten_relations(relations_payload)
    validated = []
    status_counts = Counter()
    approved_categories: dict[str, set[tuple[str, str]]] = defaultdict(set)
    seen_review_keys = set()

    for review in ledger.get("reviews") or []:
        state = str(review.get("review_state") or "").upper()
        if state not in {"APPROVED", "REJECTED", "PENDING"}:
            raise ReviewValidationError(f"invalid review_state {state!r}")
        key = relation_key(review)
        if key in seen_review_keys:
            raise ReviewValidationError(f"duplicate review key: {key}")
        seen_review_keys.add(key)
        relation = universe.get(key)
        if relation is None:
            raise ReviewValidationError(f"review does not map to frozen structured relation: {key}")
        if str(relation.get("known_at") or "")[:10] > anchor:
            raise ReviewValidationError(f"post-anchor relation reviewed: {key}")
        guard_ok, guard_reason = category_guard_passes(relation)
        if state == "APPROVED" and not guard_ok:
            raise ReviewValidationError(f"approved relation fails frozen guard {guard_reason}: {key}")
        merged = {
            "sample_isin": relation.get("sample_isin"),
            "sample_symbol": relation.get("sample_symbol"),
            "known_at": relation.get("known_at"),
            "source_url": relation.get("source_url"),
            "source_sha256": relation.get("source_sha256"),
            "page": relation.get("page"),
            "evidence_family": relation.get("evidence_family"),
            "evidence_category": relation.get("evidence_category"),
            "pattern_id": relation.get("pattern_id"),
            "matched_terms": relation.get("matched_terms"),
            "company_attribution_flag": relation.get("company_attribution_flag"),
            "action_or_status_flag": relation.get("action_or_status_flag"),
            "industry_or_peer_context_flag": relation.get("industry_or_peer_context_flag"),
            "high_information_document_subject_flag": relation.get("high_information_document_subject_flag"),
            "review_state": state,
            "review_note": review.get("review_note"),
            "guard_check": guard_reason,
        }
        validated.append(merged)
        status_counts[state] += 1
        if state == "APPROVED":
            approved_categories[str(relation.get("sample_symbol"))].add((str(relation.get("evidence_family")), str(relation.get("evidence_category"))))

    per_symbol = {}
    potential_eligible = 0
    for symbol, cats in sorted(approved_categories.items()):
        families = {fam for fam, _ in cats}
        eligible = len(families) >= 2 and bool(families & {"runway", "moat"})
        if eligible:
            potential_eligible += 1
        per_symbol[symbol] = {
            "approved_categories": sorted([f"{fam}.{cat}" for fam, cat in cats]),
            "approved_families": sorted(families),
            "qualitative_protocol_eligibility_possible": eligible,
        }

    report = {
        "anchor_date": anchor,
        "structured_relation_universe_rows": len(universe),
        "reviewed_relation_rows": len(validated),
        "review_status_counts": dict(sorted(status_counts.items())),
        "symbols_with_any_approved_category": len(approved_categories),
        "ranking_eligible_symbols_from_approved_categories": potential_eligible,
        "future_outcomes_seen": False,
        "review_guard_validation": "PASS",
    }
    return validated, {"report": report, "per_symbol": per_symbol}


def main() -> None:
    p = argparse.ArgumentParser(description="Validate blinded qualitative documentary review ledger against frozen structured relations")
    p.add_argument("--relations", type=Path, required=True)
    p.add_argument("--review-policy", type=Path, required=True)
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    relations = json.loads(args.relations.read_text(encoding="utf-8"))
    policy = json.loads(args.review_policy.read_text(encoding="utf-8"))
    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    if policy.get("status") != "LOCKED_BEFORE_RELATION_RESULT_REVIEW_AND_OUTCOME_LOAD":
        raise ReviewValidationError("review policy not frozen")
    anchor = str(ledger.get("anchor_date") or "")
    if not anchor:
        raise ReviewValidationError("ledger missing anchor_date")
    if ledger.get("future_outcomes_seen") not in (False, None):
        raise ReviewValidationError("ledger indicates future outcome exposure")

    validated, summary = validate(relations, ledger, anchor)
    output = {
        "version": "rk-mis-validated-qualitative-documentary-review-ledger-v1",
        "anchor_date": anchor,
        "future_outcomes_seen": False,
        "relations_sha256": sha256(args.relations.read_bytes()),
        "review_policy_sha256": sha256(args.review_policy.read_bytes()),
        "input_ledger_sha256": sha256(args.ledger.read_bytes()),
        "reviews": validated,
        **summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output["report"], indent=2))


if __name__ == "__main__":
    main()
