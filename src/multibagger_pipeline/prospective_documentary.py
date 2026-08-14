from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

REVIEW_STATES = {"PENDING", "APPROVED", "REJECTED"}
SOURCE_GRADES = {"A", "B", "C", "D"}
MIN_CONFIDENCE = 0.78

QUALITATIVE_FAMILIES = {
    "RUNWAY": {
        "committed_capacity_expansion",
        "multi_phase_growth_roadmap",
        "funding_visibility",
        "physical_or_operational_headroom",
        "ramp_or_utilisation_headroom",
    },
    "MOAT": {
        "proprietary_or_ip",
        "qualification_or_regulatory_barrier",
        "customer_stickiness",
        "cost_process_or_scale_advantage",
        "market_position_or_limited_competition",
    },
    "OPTIONALITY": {
        "new_product_or_platform",
        "new_customer_or_vendor_approval",
        "new_geography",
        "export_expansion",
        "adjacent_vertical_or_use_case",
    },
}
AUXILIARY_FAMILIES = {
    "ORDER": {"order_book", "order_quality"},
    "CAPACITY": {"capacity", "capex"},
    "MANAGEMENT": {"management_promise", "management_delivery"},
    "GOVERNANCE": {"governance_event"},
    "CUSTOMER": {"customer_evidence"},
    "OTHER": {"other_catalyst"},
}
ALLOWED_FAMILIES = {**QUALITATIVE_FAMILIES, **AUXILIARY_FAMILIES}
FEATURE_BY_FAMILY = {
    "RUNWAY": "reinvestment_runway_score",
    "MOAT": "moat_evidence_score",
    "OPTIONALITY": "new_product_export_optionalities_score",
}


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(value: str | date | None) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def _sha(*parts: Any) -> str:
    return hashlib.sha256("|".join("" if x is None else str(x) for x in parts).encode("utf-8")).hexdigest()


def _identity(isin: str | None, symbol: str | None) -> str:
    if isin and str(isin).strip():
        return "ISIN:" + str(isin).strip().upper()
    if symbol and str(symbol).strip():
        return "SYMBOL:" + str(symbol).strip().upper()
    raise ValueError("claim requires ISIN or symbol")


@dataclass(frozen=True)
class ProspectiveDocumentClaim:
    symbol: str
    evidence_family: str
    evidence_category: str
    statement: str
    source_url: str
    document_type: str
    document_date: str
    known_at: str
    source_grade: str = "A"
    extraction_confidence: float = 0.0
    isin: str | None = None
    source_sha256: str | None = None
    value: float | None = None
    unit: str | None = None
    due_date: str | None = None
    conflict_group: str | None = None
    basis_key: str | None = None
    promise_id: str | None = None

    def normalized(self) -> dict[str, Any]:
        symbol = str(self.symbol or "").strip().upper()
        isin = None if not self.isin else str(self.isin).strip().upper()
        family = str(self.evidence_family or "").strip().upper()
        category = str(self.evidence_category or "").strip().lower()
        if family not in ALLOWED_FAMILIES or category not in ALLOWED_FAMILIES[family]:
            raise ValueError(f"unsupported documentary category {family}.{category}")
        document_date = _iso(self.document_date)
        known_at = _iso(self.known_at)
        if not document_date or not known_at:
            raise ValueError("document_date and known_at are required")
        if document_date > known_at:
            raise ValueError("document_date cannot be after known_at")
        grade = str(self.source_grade or "").strip().upper()
        if grade not in SOURCE_GRADES:
            raise ValueError(f"unsupported source_grade {grade!r}")
        confidence = float(self.extraction_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("extraction_confidence must be between 0 and 1")
        source_url = str(self.source_url or "").strip()
        document_type = str(self.document_type or "").strip().upper()
        statement = " ".join(str(self.statement or "").split())
        if not statement or not source_url or not document_type:
            raise ValueError("statement, source_url and document_type are required")
        source_hash = None if not self.source_sha256 else str(self.source_sha256).strip().lower()
        if source_hash and (len(source_hash) != 64 or any(c not in "0123456789abcdef" for c in source_hash)):
            raise ValueError("source_sha256 must be lowercase 64-character hex")
        due_date = _iso(self.due_date)
        if due_date and due_date < document_date:
            raise ValueError("due_date cannot be before document_date")
        identity = _identity(isin, symbol)
        basis = str(self.basis_key or "").strip() or ("BASIS-" + _sha(source_url, statement)[:20].upper())
        claim_id = "PDC-" + _sha(
            identity, family, category, statement, source_url, document_date, known_at, self.value, self.unit, basis
        )[:24].upper()
        return {
            "claim_id": claim_id,
            "identity": identity,
            "isin": isin,
            "symbol": symbol,
            "evidence_family": family,
            "evidence_category": category,
            "statement": statement,
            "source_url": source_url,
            "document_type": document_type,
            "document_date": document_date,
            "known_at": known_at,
            "source_grade": grade,
            "extraction_confidence": confidence,
            "source_sha256": source_hash,
            "value": None if self.value is None else float(self.value),
            "unit": None if self.unit is None else str(self.unit),
            "due_date": due_date,
            "conflict_group": None if not self.conflict_group else str(self.conflict_group),
            "basis_key": basis,
            "promise_id": None if not self.promise_id else str(self.promise_id),
        }


class ProspectiveDocumentEvidenceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS prospective_document_claims(
                    claim_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    isin TEXT,
                    symbol TEXT NOT NULL,
                    evidence_family TEXT NOT NULL,
                    evidence_category TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    document_type TEXT NOT NULL,
                    document_date TEXT NOT NULL,
                    known_at TEXT NOT NULL,
                    source_grade TEXT NOT NULL,
                    extraction_confidence REAL NOT NULL,
                    source_sha256 TEXT,
                    value REAL,
                    unit TEXT,
                    due_date TEXT,
                    conflict_group TEXT,
                    basis_key TEXT NOT NULL,
                    promise_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prospective_document_reviews(
                    review_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    review_state TEXT NOT NULL,
                    reviewer TEXT,
                    review_note TEXT,
                    reviewed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(claim_id) REFERENCES prospective_document_claims(claim_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pdc_identity_known
                    ON prospective_document_claims(identity, known_at, document_date);
                CREATE INDEX IF NOT EXISTS idx_pdc_review_claim_time
                    ON prospective_document_reviews(claim_id, reviewed_at);
                """
            )

    def add_claim(self, claim: ProspectiveDocumentClaim | dict[str, Any]) -> str:
        obj = claim if isinstance(claim, ProspectiveDocumentClaim) else ProspectiveDocumentClaim(**claim)
        row = obj.normalized()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO prospective_document_claims(
                    claim_id,identity,isin,symbol,evidence_family,evidence_category,statement,source_url,document_type,
                    document_date,known_at,source_grade,extraction_confidence,source_sha256,value,unit,due_date,
                    conflict_group,basis_key,promise_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["claim_id"], row["identity"], row["isin"], row["symbol"], row["evidence_family"],
                    row["evidence_category"], row["statement"], row["source_url"], row["document_type"],
                    row["document_date"], row["known_at"], row["source_grade"], row["extraction_confidence"],
                    row["source_sha256"], row["value"], row["unit"], row["due_date"], row["conflict_group"],
                    row["basis_key"], row["promise_id"], utcnow(),
                ),
            )
        return row["claim_id"]

    def add_claims(self, claims: Iterable[ProspectiveDocumentClaim | dict[str, Any]]) -> list[str]:
        return [self.add_claim(claim) for claim in claims]

    def review(
        self,
        claim_id: str,
        state: str,
        *,
        reviewed_at: str,
        reviewer: str | None = None,
        note: str | None = None,
    ) -> str:
        state = str(state or "").strip().upper()
        if state not in REVIEW_STATES:
            raise ValueError(f"invalid review state {state!r}")
        reviewed = _iso(reviewed_at)
        if reviewed is None:
            raise ValueError("reviewed_at is required")
        with self.connect() as conn:
            claim = conn.execute("SELECT claim_id,known_at FROM prospective_document_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if claim is None:
                raise KeyError(f"unknown claim_id {claim_id}")
            if reviewed < claim["known_at"]:
                raise ValueError("reviewed_at cannot be before claim known_at")
            review_id = "PDR-" + _sha(claim_id, state, reviewed, reviewer, note)[:24].upper()
            conn.execute(
                "INSERT OR IGNORE INTO prospective_document_reviews VALUES(?,?,?,?,?,?,?)",
                (review_id, claim_id, state, reviewer, note, reviewed, utcnow()),
            )
        return review_id

    def identities(self) -> list[dict[str, str | None]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT identity,MAX(isin) AS isin,MAX(symbol) AS symbol FROM prospective_document_claims GROUP BY identity ORDER BY identity"
            ).fetchall()
        return [dict(row) for row in rows]

    def _latest_review(self, claim_id: str, as_of: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM prospective_document_reviews
                WHERE claim_id=? AND reviewed_at<=?
                ORDER BY reviewed_at DESC, review_id DESC LIMIT 1
                """,
                (claim_id, as_of),
            ).fetchone()
        return None if row is None else dict(row)

    def eligible_claims(self, *, identity: str, as_of_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        as_of = _iso(as_of_date)
        if as_of is None:
            raise ValueError("as_of_date is required")
        with self.connect() as conn:
            claims = [dict(row) for row in conn.execute(
                """
                SELECT * FROM prospective_document_claims
                WHERE identity=? AND document_date<=? AND known_at<=?
                ORDER BY known_at,claim_id
                """,
                (identity, as_of, as_of),
            ).fetchall()]
        eligible = []
        rejected = []
        for claim in claims:
            reasons = []
            review = self._latest_review(claim["claim_id"], as_of)
            if review is None or review["review_state"] != "APPROVED":
                reasons.append("REVIEW_NOT_APPROVED_AS_OF_DATE")
            if claim["source_grade"] not in {"A", "B"}:
                reasons.append("SOURCE_GRADE_NOT_SCORE_ELIGIBLE")
            if float(claim["extraction_confidence"]) < MIN_CONFIDENCE:
                reasons.append("LOW_EXTRACTION_CONFIDENCE")
            if claim.get("conflict_group"):
                reasons.append("UNRESOLVED_SOURCE_CONFLICT")
            if not str(claim["source_url"]).startswith("https://"):
                reasons.append("NON_HTTPS_SOURCE")
            payload = {**claim, "effective_review": review, "eligibility_reasons": reasons}
            if reasons:
                rejected.append(payload)
            else:
                eligible.append(payload)
        return eligible, rejected

    def derive_snapshot(self, *, isin: str | None = None, symbol: str | None = None, as_of_date: str) -> dict[str, Any]:
        identity = _identity(isin, symbol)
        eligible, rejected = self.eligible_claims(identity=identity, as_of_date=as_of_date)
        features = {feature: None for feature in FEATURE_BY_FAMILY.values()}
        category_evidence: dict[str, list[str]] = {family: [] for family in QUALITATIVE_FAMILIES}
        warnings: list[str] = []

        for family, categories in QUALITATIVE_FAMILIES.items():
            family_claims = [c for c in eligible if c["evidence_family"] == family and c["evidence_category"] in categories]
            basis_to_categories: dict[str, set[str]] = {}
            for claim in family_claims:
                basis_to_categories.setdefault(claim["basis_key"], set()).add(claim["evidence_category"])
            conflicted_bases = {basis for basis, cats in basis_to_categories.items() if len(cats) > 1}
            if conflicted_bases:
                warnings.append(f"CROSS_CATEGORY_SHARED_FACTUAL_BASIS:{family}:{len(conflicted_bases)}")
            safe_categories = sorted({
                claim["evidence_category"]
                for claim in family_claims
                if claim["basis_key"] not in conflicted_bases
            })
            category_evidence[family] = safe_categories
            if safe_categories:
                features[FEATURE_BY_FAMILY[family]] = float(min(100, len(safe_categories) * 20))

        management_promises = [c for c in eligible if c["evidence_family"] == "MANAGEMENT" and c["evidence_category"] == "management_promise"]
        management_deliveries = [c for c in eligible if c["evidence_family"] == "MANAGEMENT" and c["evidence_category"] == "management_delivery"]
        closed_ids = {
            c["promise_id"] for c in management_deliveries if c.get("promise_id")
        } & {
            c["promise_id"] for c in management_promises if c.get("promise_id")
        }
        management = {
            "approved_promise_claims": len(management_promises),
            "approved_delivery_claims": len(management_deliveries),
            "linked_closed_promise_ids": len(closed_ids),
            "minimum_closed_promises_for_score": 3,
            "promise_delivery_pct": None,
            "score_available": False,
            "reason": (
                "SEPARATE_MANAGEMENT_EXECUTION_ENGINE_REQUIRED"
                if len(closed_ids) >= 3
                else f"INSUFFICIENT_CLOSED_PROMISES:{len(closed_ids)}/3"
            ),
        }

        nonqualitative = {
            "approved_order_claims": sum(c["evidence_family"] == "ORDER" for c in eligible),
            "approved_capacity_capex_claims": sum(c["evidence_family"] == "CAPACITY" for c in eligible),
            "approved_governance_claims": sum(c["evidence_family"] == "GOVERNANCE" for c in eligible),
        }
        return {
            "identity": identity,
            "as_of_date": _iso(as_of_date),
            "features": features,
            "available_features": [name for name, value in features.items() if value is not None],
            "approved_categories": category_evidence,
            "eligible_claim_count": len(eligible),
            "ineligible_claim_count": len(rejected),
            "management_execution": management,
            "nonqualitative_evidence_counts": nonqualitative,
            "warnings": warnings,
            "missing_data_imputed": False,
            "point_in_time_safe": True,
        }

    def derive_all(self, *, as_of_date: str) -> list[dict[str, Any]]:
        out = []
        for item in self.identities():
            result = self.derive_snapshot(isin=item.get("isin"), symbol=item.get("symbol"), as_of_date=as_of_date)
            result["isin"] = item.get("isin")
            result["symbol"] = item.get("symbol")
            out.append(result)
        return out
