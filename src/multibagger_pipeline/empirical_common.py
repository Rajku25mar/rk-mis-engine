from __future__ import annotations

import csv, hashlib, json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

EMPIRICAL_ENGINE_VERSION = "1.0.0"
OFFICIAL_HOSTS = ("nseindia.com", "bseindia.com", "sebi.gov.in")
CASE_CONTROL_MODES = {"STRESS_CASE_CONTROL", "CASE_CONTROL"}
MARKET_MODES = {"MARKET_SAMPLE", "MARKET_WALK_FORWARD"}
DEFAULT_CONFIG: dict[str, Any] = {
    "hit_multiples": [2.0, 5.0, 10.0],
    "selected_tiers": ["TOP_100_DISCOVERY", "TOP_30_HIGH_CONVICTION", "TOP_10_RK_DIAMOND"],
    "minimum_market_observations": 250,
    "minimum_selected_observations": 30,
    "minimum_outcome_coverage_pct": 85.0,
    "minimum_adjusted_price_coverage_pct": 90.0,
    "minimum_failure_case_count": 10,
    "minimum_high_return_case_count": 10,
    "bootstrap_samples": 1000,
    "bootstrap_seed": 11031982,
    "high_return_case_multiple": 5.0,
    "failure_case_multiple": 0.5,
    "top_score_quantile": 0.25,
}

def is_official_url(url: str | None) -> bool:
    host = (urlparse(url).hostname or "").lower() if url else ""
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_HOSTS)

def as_float(value: Any) -> float | None:
    if value in (None, ""): return None
    try: return float(value)
    except (TypeError, ValueError): return None

def digest(*parts: Any) -> str:
    raw = "|".join(json.dumps(p, sort_keys=True, default=str) if isinstance(p, (dict,list,tuple)) else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()

def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path: cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return cfg

@dataclass(frozen=True)
class CohortSeed:
    canonical_id: str
    company_name: str
    seed_symbol: str = ""
    seed_group: str = "UNCLASSIFIED"
    sector: str = ""
    market_type: str = "MAINBOARD"
    anchor_date: str = ""
    source_url: str = ""
    notes: str = ""
    def to_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class EmpiricalObservation:
    canonical_id: str
    symbol: str
    company_name: str
    anchor_date: str
    cohort_mode: str
    rk_mie_score: float
    funnel_tier: str
    classification: str = ""
    sector: str = ""
    market_type: str = "MAINBOARD"
    entry_price: float | None = None
    exit_price: float | None = None
    forward_multiple: float | None = None
    horizon_days: int = 1095
    outcome_status: str = "MISSING"
    price_state: str = "UNKNOWN"
    seed_group: str = ""
    source_provenance: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]: return asdict(self)

def load_cohort_csv(path: str | Path) -> list[CohortSeed]:
    out=[]
    with Path(path).open(newline="",encoding="utf-8-sig") as h:
        for r in csv.DictReader(h):
            out.append(CohortSeed(
                (r.get("canonical_id") or "").strip(), (r.get("company_name") or "").strip(),
                (r.get("seed_symbol") or "").strip().upper(), (r.get("seed_group") or "UNCLASSIFIED").strip().upper(),
                (r.get("sector") or "").strip(), (r.get("market_type") or "MAINBOARD").strip().upper(),
                (r.get("anchor_date") or "").strip(), (r.get("source_url") or "").strip(), (r.get("notes") or "").strip()))
    return out
