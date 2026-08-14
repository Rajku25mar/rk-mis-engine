from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

TRACKED_FIELDS = (
    "mf_holding_pct",
    "fii_holding_pct",
    "institutional_shareholder_count",
    "promoter_holding_pct",
)
DERIVED_FIELDS = {
    "mf_holding_pct": "mf_holding_change_pp_4q",
    "fii_holding_pct": "fii_holding_change_pp_4q",
    "institutional_shareholder_count": "institutional_breadth_change_4q",
    "promoter_holding_pct": "promoter_holding_change_pp_4q",
}
SOURCE_GRADES = {"A", "B", "C", "D"}


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(value: str) -> str:
    return date.fromisoformat(str(value)[:10]).isoformat()


def _identity(isin: str | None, symbol: str | None) -> str:
    if isin and str(isin).strip():
        return "ISIN:" + str(isin).strip().upper()
    if symbol and str(symbol).strip():
        return "SYMBOL:" + str(symbol).strip().upper()
    raise ValueError("observation requires ISIN or symbol")


def _sha(*parts: Any) -> str:
    return hashlib.sha256("|".join("" if x is None else str(x) for x in parts).encode("utf-8")).hexdigest()


def _pct(value: Any, field: str) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if not 0.0 <= number <= 100.0:
        raise ValueError(f"{field} must be between 0 and 100")
    return number


def _count(value: Any) -> int | None:
    if value in (None, ""):
        return None
    number = int(value)
    if number < 0:
        raise ValueError("institutional_shareholder_count must be non-negative")
    return number


@dataclass(frozen=True)
class SmartMoneyObservation:
    symbol: str
    period_end: str
    known_at: str
    source_url: str
    source_kind: str
    source_grade: str = "A"
    isin: str | None = None
    source_sha256: str | None = None
    mf_holding_pct: float | None = None
    fii_holding_pct: float | None = None
    institutional_shareholder_count: int | None = None
    promoter_holding_pct: float | None = None

    def normalized(self) -> dict[str, Any]:
        symbol = str(self.symbol or "").strip().upper()
        isin = None if not self.isin else str(self.isin).strip().upper()
        period_end = _iso(self.period_end)
        known_at = _iso(self.known_at)
        if period_end > known_at:
            raise ValueError("period_end cannot be after known_at")
        grade = str(self.source_grade or "").upper()
        if grade not in SOURCE_GRADES:
            raise ValueError(f"invalid source_grade {grade!r}")
        source_url = str(self.source_url or "").strip()
        source_kind = str(self.source_kind or "").strip().upper()
        if not source_url or not source_kind:
            raise ValueError("source_url and source_kind are required")
        source_hash = None if not self.source_sha256 else str(self.source_sha256).strip().lower()
        if source_hash and (len(source_hash) != 64 or any(c not in "0123456789abcdef" for c in source_hash)):
            raise ValueError("source_sha256 must be a 64-character lowercase hex digest")
        identity = _identity(isin, symbol)
        payload = {
            "identity": identity,
            "isin": isin,
            "symbol": symbol,
            "period_end": period_end,
            "known_at": known_at,
            "source_url": source_url,
            "source_kind": source_kind,
            "source_grade": grade,
            "source_sha256": source_hash,
            "mf_holding_pct": _pct(self.mf_holding_pct, "mf_holding_pct"),
            "fii_holding_pct": _pct(self.fii_holding_pct, "fii_holding_pct"),
            "institutional_shareholder_count": _count(self.institutional_shareholder_count),
            "promoter_holding_pct": _pct(self.promoter_holding_pct, "promoter_holding_pct"),
        }
        payload["observation_id"] = "SMO-" + _sha(
            identity,
            period_end,
            known_at,
            source_url,
            source_hash,
            json.dumps({k: payload[k] for k in TRACKED_FIELDS}, sort_keys=True),
        )[:24].upper()
        return payload


class ProspectiveSmartMoneyStore:
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
                CREATE TABLE IF NOT EXISTS smart_money_observations(
                    observation_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    isin TEXT,
                    symbol TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    known_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_grade TEXT NOT NULL,
                    source_sha256 TEXT,
                    mf_holding_pct REAL,
                    fii_holding_pct REAL,
                    institutional_shareholder_count INTEGER,
                    promoter_holding_pct REAL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_smart_money_identity_period
                    ON smart_money_observations(identity, period_end, known_at);
                CREATE INDEX IF NOT EXISTS idx_smart_money_known_at
                    ON smart_money_observations(known_at);
                """
            )

    def add(self, observation: SmartMoneyObservation | dict[str, Any]) -> str:
        obj = observation if isinstance(observation, SmartMoneyObservation) else SmartMoneyObservation(**observation)
        row = obj.normalized()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO smart_money_observations(
                    observation_id,identity,isin,symbol,period_end,known_at,source_url,source_kind,source_grade,
                    source_sha256,mf_holding_pct,fii_holding_pct,institutional_shareholder_count,promoter_holding_pct,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["observation_id"], row["identity"], row["isin"], row["symbol"], row["period_end"], row["known_at"],
                    row["source_url"], row["source_kind"], row["source_grade"], row["source_sha256"], row["mf_holding_pct"],
                    row["fii_holding_pct"], row["institutional_shareholder_count"], row["promoter_holding_pct"], utcnow(),
                ),
            )
        return row["observation_id"]

    def add_many(self, observations: Iterable[SmartMoneyObservation | dict[str, Any]]) -> list[str]:
        return [self.add(row) for row in observations]

    def identities(self) -> list[dict[str, str | None]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT identity, MAX(isin) AS isin, MAX(symbol) AS symbol FROM smart_money_observations GROUP BY identity ORDER BY identity"
            ).fetchall()
        return [dict(row) for row in rows]

    def _point_in_time_period_rows(self, identity: str, as_of_date: str) -> list[dict[str, Any]]:
        as_of = _iso(as_of_date)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM smart_money_observations
                WHERE identity=? AND period_end<=? AND known_at<=?
                ORDER BY period_end, known_at, observation_id
                """,
                (identity, as_of, as_of),
            ).fetchall()
        latest_by_period: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            period = item["period_end"]
            prior = latest_by_period.get(period)
            if prior is None or (item["known_at"], item["observation_id"]) > (prior["known_at"], prior["observation_id"]):
                latest_by_period[period] = item
        return [latest_by_period[p] for p in sorted(latest_by_period)]

    def derive_features(
        self,
        *,
        isin: str | None = None,
        symbol: str | None = None,
        as_of_date: str,
        required_periods: int = 5,
        span_min_days: int = 330,
        span_max_days: int = 400,
    ) -> dict[str, Any]:
        identity = _identity(isin, symbol)
        periods = self._point_in_time_period_rows(identity, as_of_date)
        selected = periods[-required_periods:]
        warnings: list[str] = []
        features = {name: None for name in DERIVED_FIELDS.values()}
        if len(selected) < required_periods:
            warnings.append(f"INSUFFICIENT_DISTINCT_PERIODS:{len(selected)}/{required_periods}")
            return {
                "identity": identity,
                "as_of_date": _iso(as_of_date),
                "selected_periods": [row["period_end"] for row in selected],
                "span_days": None,
                "features": features,
                "available_features": [],
                "warnings": warnings,
                "point_in_time_safe": True,
            }

        span = (date.fromisoformat(selected[-1]["period_end"]) - date.fromisoformat(selected[0]["period_end"])).days
        if not span_min_days <= span <= span_max_days:
            warnings.append(f"INVALID_4Q_SPAN_DAYS:{span};EXPECTED:{span_min_days}-{span_max_days}")
            return {
                "identity": identity,
                "as_of_date": _iso(as_of_date),
                "selected_periods": [row["period_end"] for row in selected],
                "span_days": span,
                "features": features,
                "available_features": [],
                "warnings": warnings,
                "point_in_time_safe": True,
            }

        for source_field, derived_field in DERIVED_FIELDS.items():
            values = [row.get(source_field) for row in selected]
            if any(value is None for value in values):
                warnings.append(f"INCOMPLETE_5_PERIOD_COMPONENT:{derived_field}")
                continue
            features[derived_field] = round(float(values[-1]) - float(values[0]), 6)

        available = [field for field, value in features.items() if value is not None]
        return {
            "identity": identity,
            "as_of_date": _iso(as_of_date),
            "selected_periods": [row["period_end"] for row in selected],
            "span_days": span,
            "features": features,
            "available_features": available,
            "warnings": warnings,
            "point_in_time_safe": True,
        }

    def derive_all(self, *, as_of_date: str) -> list[dict[str, Any]]:
        out = []
        for item in self.identities():
            result = self.derive_features(isin=item.get("isin"), symbol=item.get("symbol"), as_of_date=as_of_date)
            result["isin"] = item.get("isin")
            result["symbol"] = item.get("symbol")
            out.append(result)
        return out
