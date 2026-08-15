from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .prospective_documentary import ProspectiveDocumentEvidenceStore


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_reviewed_documentary_ledgers(
    store: ProspectiveDocumentEvidenceStore,
    ledger_paths: Iterable[str | Path],
    *,
    scoring_as_of: str | None = None,
) -> dict[str, Any]:
    """Load reviewed point-in-time documentary ledgers into an evidence store.

    The ledger's own review dates remain authoritative. `scoring_as_of` is only a
    fail-closed guard: a ledger snapshot dated after the scoring date is rejected.
    """
    loaded = []
    total_decisions = 0
    states: dict[str, int] = {}
    seen_paths = set()

    for raw_path in ledger_paths:
        path = Path(raw_path)
        resolved = str(path.resolve())
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("status") != "REVIEWED_PRIMARY_SOURCE_EVIDENCE":
            raise ValueError(f"ledger is not reviewed primary-source evidence: {path}")
        ledger_as_of = str(ledger.get("as_of_date") or "")[:10]
        if not ledger_as_of:
            raise ValueError(f"ledger missing as_of_date: {path}")
        if scoring_as_of and ledger_as_of > str(scoring_as_of)[:10]:
            raise ValueError(
                f"ledger snapshot {ledger_as_of} is after scoring as-of {str(scoring_as_of)[:10]}: {path}"
            )
        decisions = ledger.get("decisions") or []
        if not isinstance(decisions, list):
            raise ValueError(f"ledger decisions must be a list: {path}")
        reviewed_at_default = str(ledger.get("review_policy", {}).get("reviewed_at") or ledger_as_of)
        reviewer_default = str(ledger.get("review_policy", {}).get("reviewer") or "RK_MIS_REVIEW")
        for item in decisions:
            if not isinstance(item, dict) or not isinstance(item.get("claim"), dict):
                raise ValueError(f"ledger decision missing claim object: {path}")
            review = item.get("review") or {}
            state = str(review.get("state") or "").upper()
            if state not in {"APPROVED", "REJECTED", "PENDING"}:
                raise ValueError(f"invalid review state {state!r} in {path}")
            claim_id = store.add_claim(item["claim"])
            store.review(
                claim_id,
                state,
                reviewed_at=str(review.get("reviewed_at") or reviewed_at_default),
                reviewer=str(review.get("reviewer") or reviewer_default),
                note=str(review.get("note") or ""),
            )
            total_decisions += 1
            states[state] = states.get(state, 0) + 1
        loaded.append({
            "path": str(path),
            "sha256": _sha256(path),
            "as_of_date": ledger_as_of,
            "decision_rows": len(decisions),
        })

    chain = hashlib.sha256(
        "\n".join(f"{row['path']}|{row['sha256']}" for row in loaded).encode("utf-8")
    ).hexdigest()
    return {
        "ledgers": loaded,
        "ledger_count": len(loaded),
        "decision_rows": total_decisions,
        "review_state_counts": dict(sorted(states.items())),
        "ledger_chain_sha256": chain,
        "scoring_as_of": None if scoring_as_of is None else str(scoring_as_of)[:10],
        "missing_data_imputed": False,
    }
