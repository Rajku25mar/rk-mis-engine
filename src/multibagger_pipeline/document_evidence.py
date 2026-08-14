from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable

SOURCE_GRADE_WEIGHT = {"A": 1.0, "B": 0.85, "C": 0.65, "D": 0.4}
REVIEW_STATES = {"PENDING", "APPROVED", "REJECTED"}
CLAIM_TYPES = {
    "MOAT_EVIDENCE",
    "ENTRY_BARRIER",
    "CUSTOMER_STICKINESS",
    "PRICING_POWER",
    "ORDER_BOOK",
    "ORDER_QUALITY",
    "CAPEX",
    "CAPACITY",
    "NEW_PRODUCT",
    "EXPORT_OPTIONALITY",
    "MANAGEMENT_PROMISE",
    "MANAGEMENT_DELIVERY",
    "GOVERNANCE_EVENT",
    "CUSTOMER_EVIDENCE",
    "OTHER_CATALYST",
}


def _iso(value: str | date | None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def _sha(*parts: Any) -> str:
    raw = "|".join("" if x is None else str(x) for x in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    symbol: str
    claim_type: str
    statement: str
    source_url: str
    document_type: str
    document_date: str
    known_at: str
    source_grade: str = "A"
    metric: str | None = None
    value: float | None = None
    unit: str | None = None
    due_date: str | None = None
    extraction_confidence: float = 0.0
    review_state: str = "PENDING"
    reviewer: str | None = None
    conflict_group: str | None = None
    source_sha256: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "claim_type", self.claim_type.strip().upper())
        object.__setattr__(self, "document_type", self.document_type.strip().upper())
        object.__setattr__(self, "source_grade", self.source_grade.strip().upper())
        object.__setattr__(self, "review_state", self.review_state.strip().upper())
        object.__setattr__(self, "document_date", _iso(self.document_date) or "")
        object.__setattr__(self, "known_at", _iso(self.known_at) or "")
        object.__setattr__(self, "due_date", _iso(self.due_date))
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.claim_type not in CLAIM_TYPES:
            raise ValueError(f"unsupported claim_type: {self.claim_type}")
        if self.source_grade not in SOURCE_GRADE_WEIGHT:
            raise ValueError(f"unsupported source_grade: {self.source_grade}")
        if self.review_state not in REVIEW_STATES:
            raise ValueError(f"unsupported review_state: {self.review_state}")
        if not self.document_date or not self.known_at:
            raise ValueError("document_date and known_at are required")
        if not 0 <= float(self.extraction_confidence) <= 1:
            raise ValueError("extraction_confidence must be between 0 and 1")

    @property
    def evidence_id(self) -> str:
        return "EVD-" + _sha(
            self.symbol,
            self.claim_type,
            self.statement,
            self.source_url,
            self.document_date,
            self.known_at,
            self.metric,
            self.value,
            self.unit,
        )[:20].upper()

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, **asdict(self)}


def point_in_time_status(record: EvidenceRecord, anchor_date: str | date) -> tuple[bool, str]:
    anchor = _iso(anchor_date)
    if anchor is None:
        raise ValueError("anchor_date is required")
    if record.document_date > anchor:
        return False, "DOCUMENT_AFTER_ANCHOR"
    if record.known_at > anchor:
        return False, "KNOWN_AFTER_ANCHOR"
    if record.due_date and record.due_date < record.document_date:
        return False, "DUE_DATE_BEFORE_DOCUMENT"
    return True, "POINT_IN_TIME_SAFE"


def scoring_eligibility(
    record: EvidenceRecord,
    anchor_date: str | date,
    *,
    minimum_confidence: float = 0.78,
    allowed_source_grades: Iterable[str] = ("A", "B"),
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    safe, reason = point_in_time_status(record, anchor_date)
    if not safe:
        reasons.append(reason)
    allowed = {str(x).upper() for x in allowed_source_grades}
    if record.source_grade not in allowed:
        reasons.append("SOURCE_GRADE_NOT_SCORE_ELIGIBLE")
    if record.review_state != "APPROVED":
        reasons.append("HUMAN_REVIEW_NOT_APPROVED")
    if float(record.extraction_confidence) < minimum_confidence:
        reasons.append("LOW_EXTRACTION_CONFIDENCE")
    if record.conflict_group:
        reasons.append("UNRESOLVED_SOURCE_CONFLICT")
    if not record.source_url.startswith("https://"):
        reasons.append("NON_HTTPS_SOURCE")
    return not reasons, reasons


def evidence_strength(record: EvidenceRecord, anchor_date: str | date) -> float | None:
    eligible, _ = scoring_eligibility(record, anchor_date)
    if not eligible:
        return None
    return round(
        SOURCE_GRADE_WEIGHT[record.source_grade] * float(record.extraction_confidence) * 100,
        2,
    )


def build_feature_evidence_index(
    records: Iterable[EvidenceRecord],
    anchor_date: str | date,
) -> dict[str, list[dict[str, Any]]]:
    mapping = {
        "MOAT_EVIDENCE": "moat_evidence_score",
        "ENTRY_BARRIER": "entry_barrier_score",
        "CUSTOMER_STICKINESS": "customer_stickiness_score",
        "PRICING_POWER": "pricing_power_score",
        "ORDER_BOOK": "orderbook_to_sales",
        "ORDER_QUALITY": "order_quality_score",
        "CAPACITY": "planned_capacity_increase_pct",
        "CAPEX": "capex_execution_score",
        "NEW_PRODUCT": "new_product_export_optionalities_score",
        "EXPORT_OPTIONALITY": "new_product_export_optionalities_score",
        "MANAGEMENT_PROMISE": "promise_delivery_pct",
        "MANAGEMENT_DELIVERY": "promise_delivery_pct",
        "GOVERNANCE_EVENT": "governance_evidence_score",
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        feature = mapping.get(record.claim_type)
        if not feature:
            continue
        eligible, reasons = scoring_eligibility(record, anchor_date)
        payload = record.to_dict()
        payload["score_eligible"] = eligible
        payload["score_ineligibility_reasons"] = reasons
        payload["evidence_strength"] = evidence_strength(record, anchor_date)
        out.setdefault(feature, []).append(payload)
    for values in out.values():
        values.sort(key=lambda x: (x["known_at"], x["evidence_id"]))
    return out


def freeze_evidence_snapshot(records: Iterable[EvidenceRecord], anchor_date: str | date) -> dict[str, Any]:
    anchor = _iso(anchor_date)
    rows = []
    rejected = []
    for record in records:
        safe, reason = point_in_time_status(record, anchor or "")
        if safe:
            rows.append(record.to_dict())
        else:
            rejected.append({"evidence_id": record.evidence_id, "symbol": record.symbol, "reason": reason})
    rows.sort(key=lambda x: (x["symbol"], x["known_at"], x["evidence_id"]))
    digest = _sha(anchor, rows)
    return {
        "anchor_date": anchor,
        "safe_evidence_rows": len(rows),
        "rejected_future_or_invalid_rows": len(rejected),
        "snapshot_sha256": digest,
        "records": rows,
        "rejected": rejected,
        "outcomes_seen": False,
    }
