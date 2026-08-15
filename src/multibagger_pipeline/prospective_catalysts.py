from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

ORDER_TYPES = {"CONFIRMED", "FRAMEWORK", "LOI", "L1", "MOU", "PIPELINE"}
ORDER_WEIGHTS = {"CONFIRMED": 1.0, "FRAMEWORK": 0.8, "LOI": 0.65, "L1": 0.5, "MOU": 0.25, "PIPELINE": 0.1}
CAPEX_STATUSES = {
    "ANNOUNCED", "APPROVAL", "LAND_ACQUIRED", "EQUIPMENT_ORDERED", "UNDER_CONSTRUCTION",
    "TRIAL_PRODUCTION", "COMMISSIONED", "RAMPING", "COMPLETE", "DELAYED", "CANCELLED",
}
CAPEX_STATUS_SCORES = {
    "ANNOUNCED": 45, "APPROVAL": 55, "LAND_ACQUIRED": 60, "EQUIPMENT_ORDERED": 65,
    "UNDER_CONSTRUCTION": 72, "TRIAL_PRODUCTION": 86, "COMMISSIONED": 96, "RAMPING": 90,
    "COMPLETE": 100, "DELAYED": 30, "CANCELLED": 0,
}
SCORING_SOURCE_GRADES = {"A", "B"}


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def identity(isin: str | None, symbol: str | None) -> str:
    if isin and str(isin).strip():
        return "ISIN:" + str(isin).strip().upper()
    if symbol and str(symbol).strip():
        return "SYMBOL:" + str(symbol).strip().upper()
    raise ValueError("catalyst evidence requires ISIN or symbol")


def digest(*parts: Any) -> str:
    return hashlib.sha256("|".join("" if x is None else str(x) for x in parts).encode("utf-8")).hexdigest()


def pct_or_none(value: Any, name: str) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return number


def validate_sha(value: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ValueError("source_sha256 is required and must be lowercase 64-character hex")
    return text


@dataclass(frozen=True)
class OrderEvidence:
    symbol: str
    snapshot_date: str
    known_at: str
    order_type: str
    order_value_cr: float
    source_url: str
    source_sha256: str
    source_grade: str = "A"
    isin: str | None = None
    aggregate_orderbook: bool = False
    customer_name: str | None = None
    customer_quality_score: float | None = None
    cancellation_risk_score: float | None = None
    execution_months: float | None = None
    expected_margin_pct: float | None = None

    def normalized(self) -> dict[str, Any]:
        symbol = str(self.symbol or "").strip().upper()
        isin = None if not self.isin else str(self.isin).strip().upper()
        snapshot = iso(self.snapshot_date)
        known = iso(self.known_at)
        if snapshot > known:
            raise ValueError("order snapshot_date cannot be after known_at")
        kind = str(self.order_type or "").upper()
        if kind not in ORDER_TYPES:
            raise ValueError(f"unsupported order_type {kind!r}")
        value = float(self.order_value_cr)
        if value <= 0:
            raise ValueError("order_value_cr must be positive")
        grade = str(self.source_grade or "").upper()
        if grade not in {"A", "B", "C", "D"}:
            raise ValueError("unsupported source_grade")
        source_url = str(self.source_url or "").strip()
        if not source_url.startswith("https://"):
            raise ValueError("order source_url must be https")
        source_hash = validate_sha(self.source_sha256)
        return {
            "order_id": "ORD-" + digest(identity(isin, symbol), snapshot, known, kind, value, source_url, source_hash)[:24].upper(),
            "identity": identity(isin, symbol),
            "isin": isin,
            "symbol": symbol,
            "snapshot_date": snapshot,
            "known_at": known,
            "order_type": kind,
            "order_value_cr": value,
            "aggregate_orderbook": int(bool(self.aggregate_orderbook)),
            "customer_name": None if not self.customer_name else str(self.customer_name),
            "customer_quality_score": pct_or_none(self.customer_quality_score, "customer_quality_score"),
            "cancellation_risk_score": pct_or_none(self.cancellation_risk_score, "cancellation_risk_score"),
            "execution_months": None if self.execution_months is None else float(self.execution_months),
            "expected_margin_pct": None if self.expected_margin_pct is None else float(self.expected_margin_pct),
            "source_url": source_url,
            "source_sha256": source_hash,
            "source_grade": grade,
        }


@dataclass(frozen=True)
class CapexEvidence:
    symbol: str
    project_name: str
    latest_status: str
    known_at: str
    source_url: str
    source_sha256: str
    source_grade: str = "A"
    isin: str | None = None
    capex_id: str | None = None
    announced_date: str | None = None
    target_completion_date: str | None = None
    planned_capex_cr: float | None = None
    latest_update_date: str | None = None

    def normalized(self) -> dict[str, Any]:
        symbol = str(self.symbol or "").strip().upper()
        isin = None if not self.isin else str(self.isin).strip().upper()
        status = str(self.latest_status or "").upper()
        if status not in CAPEX_STATUSES:
            raise ValueError(f"unsupported capex status {status!r}")
        known = iso(self.known_at)
        announced = None if not self.announced_date else iso(self.announced_date)
        target = None if not self.target_completion_date else iso(self.target_completion_date)
        update = iso(self.latest_update_date or known)
        if update > known or (announced and announced > known):
            raise ValueError("capex dates cannot be after known_at")
        source_url = str(self.source_url or "").strip()
        if not source_url.startswith("https://"):
            raise ValueError("capex source_url must be https")
        source_hash = validate_sha(self.source_sha256)
        grade = str(self.source_grade or "").upper()
        if grade not in {"A", "B", "C", "D"}:
            raise ValueError("unsupported source_grade")
        delay = 0
        if target and status not in {"COMPLETE", "COMMISSIONED", "CANCELLED"}:
            delay = max(0, (date.fromisoformat(update) - date.fromisoformat(target)).days)
        project_name = " ".join(str(self.project_name or "").split())
        if not project_name:
            raise ValueError("project_name is required")
        capex_id = self.capex_id or "CPX-" + digest(identity(isin, symbol), project_name)[:20].upper()
        return {
            "capex_id": capex_id,
            "identity": identity(isin, symbol),
            "isin": isin,
            "symbol": symbol,
            "project_name": project_name,
            "announced_date": announced,
            "target_completion_date": target,
            "latest_status": status,
            "planned_capex_cr": None if self.planned_capex_cr is None else float(self.planned_capex_cr),
            "latest_update_date": update,
            "known_at": known,
            "delay_days": delay,
            "source_url": source_url,
            "source_sha256": source_hash,
            "source_grade": grade,
        }


class ProspectiveCatalystStore:
    """Point-in-time journal for already-reviewed order and capex catalyst evidence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS order_evidence(
                  order_id TEXT PRIMARY KEY, identity TEXT NOT NULL, isin TEXT, symbol TEXT NOT NULL,
                  snapshot_date TEXT NOT NULL, known_at TEXT NOT NULL, order_type TEXT NOT NULL,
                  order_value_cr REAL NOT NULL, aggregate_orderbook INTEGER NOT NULL,
                  customer_name TEXT, customer_quality_score REAL, cancellation_risk_score REAL,
                  execution_months REAL, expected_margin_pct REAL, source_url TEXT NOT NULL,
                  source_sha256 TEXT NOT NULL, source_grade TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_order_evidence_identity_date
                  ON order_evidence(identity,snapshot_date,known_at);
                CREATE TABLE IF NOT EXISTS capex_evidence(
                  capex_id TEXT NOT NULL, evidence_id TEXT PRIMARY KEY, identity TEXT NOT NULL, isin TEXT,
                  symbol TEXT NOT NULL, project_name TEXT NOT NULL, announced_date TEXT,
                  target_completion_date TEXT, latest_status TEXT NOT NULL, planned_capex_cr REAL,
                  latest_update_date TEXT NOT NULL, known_at TEXT NOT NULL, delay_days INTEGER NOT NULL,
                  source_url TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_grade TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_capex_evidence_identity_date
                  ON capex_evidence(identity,latest_update_date,known_at);
                """
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def add_order(self, evidence: OrderEvidence | dict[str, Any]) -> str:
        obj = evidence if isinstance(evidence, OrderEvidence) else OrderEvidence(**evidence)
        row = obj.normalized()
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO order_evidence(
                   order_id,identity,isin,symbol,snapshot_date,known_at,order_type,order_value_cr,
                   aggregate_orderbook,customer_name,customer_quality_score,cancellation_risk_score,
                   execution_months,expected_margin_pct,source_url,source_sha256,source_grade,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*[row[k] for k in (
                    "order_id","identity","isin","symbol","snapshot_date","known_at","order_type","order_value_cr",
                    "aggregate_orderbook","customer_name","customer_quality_score","cancellation_risk_score",
                    "execution_months","expected_margin_pct","source_url","source_sha256","source_grade"
                )], utcnow()),
            )
        return row["order_id"]

    def add_capex(self, evidence: CapexEvidence | dict[str, Any]) -> str:
        obj = evidence if isinstance(evidence, CapexEvidence) else CapexEvidence(**evidence)
        row = obj.normalized()
        evidence_id = "CPXE-" + digest(row["capex_id"], row["known_at"], row["latest_status"], row["source_sha256"])[:24].upper()
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO capex_evidence(
                   capex_id,evidence_id,identity,isin,symbol,project_name,announced_date,target_completion_date,
                   latest_status,planned_capex_cr,latest_update_date,known_at,delay_days,source_url,source_sha256,
                   source_grade,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["capex_id"], evidence_id, row["identity"], row["isin"], row["symbol"], row["project_name"],
                    row["announced_date"], row["target_completion_date"], row["latest_status"], row["planned_capex_cr"],
                    row["latest_update_date"], row["known_at"], row["delay_days"], row["source_url"],
                    row["source_sha256"], row["source_grade"], utcnow(),
                ),
            )
        return evidence_id

    def identities(self) -> list[dict[str, str | None]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT identity,MAX(isin) isin,MAX(symbol) symbol FROM (
                     SELECT identity,isin,symbol FROM order_evidence
                     UNION ALL SELECT identity,isin,symbol FROM capex_evidence)
                   GROUP BY identity ORDER BY identity"""
            ).fetchall()
        return [dict(row) for row in rows]

    def derive_features(
        self,
        *,
        as_of_date: str,
        isin: str | None = None,
        symbol: str | None = None,
        ttm_revenue_cr: float | None = None,
        capex_delay_penalty_per_30_days: float = 5.0,
        capex_delay_penalty_cap: float = 40.0,
    ) -> dict[str, Any]:
        ident = identity(isin, symbol)
        as_of = iso(as_of_date)
        features: dict[str, float | None] = {
            "order_quality_score": None,
            "orderbook_to_sales": None,
            "capex_execution_score": None,
            "planned_capacity_increase_pct": None,
        }
        warnings: list[str] = []
        provenance: dict[str, Any] = {}

        with self.connect() as conn:
            orders = [dict(r) for r in conn.execute(
                """SELECT * FROM order_evidence WHERE identity=? AND snapshot_date<=? AND known_at<=?
                   AND source_grade IN ('A','B') ORDER BY snapshot_date,known_at,created_at,order_id""",
                (ident, as_of, as_of),
            ).fetchall()]
            capex_rows = [dict(r) for r in conn.execute(
                """SELECT * FROM capex_evidence WHERE identity=? AND latest_update_date<=? AND known_at<=?
                   AND source_grade IN ('A','B') ORDER BY latest_update_date,known_at,created_at,evidence_id""",
                (ident, as_of, as_of),
            ).fetchall()]

        aggregate = [r for r in orders if int(r["aggregate_orderbook"])]
        aggregate_used = False
        if aggregate:
            latest_snapshot = max(r["snapshot_date"] for r in aggregate)
            selected_orders = [r for r in aggregate if r["snapshot_date"] == latest_snapshot]
            aggregate_used = True
        elif orders:
            latest_snapshot = max(r["snapshot_date"] for r in orders)
            selected_orders = [r for r in orders if r["snapshot_date"] == latest_snapshot]
        else:
            selected_orders = []
            latest_snapshot = None

        if selected_orders:
            total = sum(float(r["order_value_cr"]) for r in selected_orders)
            quality_value = 0.0
            for r in selected_orders:
                customer = 1.0 if r["customer_quality_score"] is None else max(0.0, min(1.0, float(r["customer_quality_score"]) / 100.0))
                cancellation = 1.0 if r["cancellation_risk_score"] is None else max(0.0, 1.0 - float(r["cancellation_risk_score"]) / 100.0)
                quality_value += float(r["order_value_cr"]) * ORDER_WEIGHTS.get(r["order_type"], 0.0) * customer * cancellation
            if total > 0:
                features["order_quality_score"] = round(quality_value / total * 100, 2)
                provenance["order_quality_score"] = {
                    "snapshot_date": latest_snapshot,
                    "orders": len(selected_orders),
                    "total_order_value_cr": round(total, 2),
                    "quality_adjusted_order_value_cr": round(quality_value, 2),
                    "method": "frozen order-type/customer/cancellation weighting",
                }
                if aggregate_used and ttm_revenue_cr is not None and float(ttm_revenue_cr) > 0:
                    features["orderbook_to_sales"] = round(quality_value / float(ttm_revenue_cr), 3)
                    provenance["orderbook_to_sales"] = {
                        "snapshot_date": latest_snapshot,
                        "quality_adjusted_order_value_cr": round(quality_value, 2),
                        "ttm_revenue_cr": float(ttm_revenue_cr),
                        "method": "quality-adjusted aggregate order book / TTM revenue",
                    }
                elif aggregate_used:
                    warnings.append("TTM_REVENUE_MISSING_FOR_ORDERBOOK_TO_SALES")
                else:
                    warnings.append("LATEST_ORDER_EVIDENCE_IS_NOT_AGGREGATE_ORDERBOOK")

        latest_by_project: dict[str, dict[str, Any]] = {}
        for row in capex_rows:
            prior = latest_by_project.get(row["capex_id"])
            key = (row["known_at"], row["latest_update_date"], row["created_at"], row["evidence_id"])
            prior_key = None if prior is None else (prior["known_at"], prior["latest_update_date"], prior["created_at"], prior["evidence_id"])
            if prior is None or key > prior_key:
                latest_by_project[row["capex_id"]] = row
        projects = list(latest_by_project.values())
        if projects:
            weighted = 0.0
            total_weight = 0.0
            components = []
            for r in projects:
                base = float(CAPEX_STATUS_SCORES.get(r["latest_status"], 40))
                delay = max(0, int(r["delay_days"] or 0))
                penalty = min(float(capex_delay_penalty_cap), delay / 30.0 * float(capex_delay_penalty_per_30_days))
                score = max(0.0, base - penalty)
                weight = float(r["planned_capex_cr"]) if r["planned_capex_cr"] not in (None, 0) else 1.0
                weighted += score * weight
                total_weight += weight
                components.append({
                    "capex_id": r["capex_id"], "status": r["latest_status"], "delay_days": delay,
                    "score": round(score, 2), "weight": weight,
                })
            features["capex_execution_score"] = round(weighted / total_weight, 2)
            provenance["capex_execution_score"] = {
                "projects": components,
                "method": "frozen status score minus delay penalty, capex-weighted",
            }

        return {
            "identity": ident,
            "isin": None if not isin else str(isin).upper(),
            "symbol": None if not symbol else str(symbol).upper(),
            "as_of_date": as_of,
            "features": features,
            "available_features": [k for k, v in features.items() if v is not None],
            "provenance": provenance,
            "warnings": warnings,
            "missing_data_imputed": False,
            "point_in_time_safe": True,
        }
