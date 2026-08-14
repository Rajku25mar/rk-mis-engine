from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import run_promoter_accumulation_replay as promoter
import run_technical_replay as market
from multibagger_pipeline.empirical_sampling import deterministic_market_sample

INSTITUTIONAL_NAME_RE = re.compile(
    r"(?:^|_)(?:mf|fii|fpi|dii)(?:_|$)|mutual|foreign|institution|insurance|pension|provident|bank|fund",
    re.I,
)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _field_name_candidates(fields: list[str]) -> list[str]:
    return sorted(field for field in fields if INSTITUTIONAL_NAME_RE.search(field))


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe historical NSE shareholding response schema without publishing holdings")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_PRE_OUTCOME_SCHEMA_AND_COVERAGE_DIAGNOSIS":
        raise RuntimeError("schema probe protocol is not frozen")
    if protocol.get("future_outcomes_seen") is not False:
        raise RuntimeError("schema probe must remain pre-outcome")

    anchor = protocol["anchor_date"]
    sample_size = int(protocol["sample_size"])
    window = protocol["shareholding_window"]
    session = market.OfficialSession(request_budget=120, timeout=30, sleep_seconds=0.04)

    resolved_anchor, anchor_raw, anchor_meta = market.resolve_on_or_before(anchor, session)
    universe = market.company_universe(market.parse_bhavcopy(anchor_raw), resolved_anchor)
    sample = deterministic_market_sample(
        universe,
        sample_size=sample_size,
        salt=protocol["sample_salt"],
        strata_fields=("market_type",),
    )
    if len(sample) != sample_size:
        raise RuntimeError(f"sample underflow: expected {sample_size}, got {len(sample)}")

    nonnull: Counter[str] = Counter()
    types: dict[str, Counter[str]] = defaultdict(Counter)
    presence: Counter[str] = Counter()
    source_hashes: list[str] = []
    total_rows = 0
    symbols_with_rows = 0
    attempts_ok = 0
    attempts_error = 0

    for member in sample:
        rows, attempts, source_hash = promoter.fetch_symbol_shareholding(
            session,
            member["symbol"],
            window["from_date"],
            anchor,
        )
        source_hashes.append(f"{member['isin']}|{source_hash}")
        attempts_ok += sum(a.get("status") == "OK" for a in attempts)
        attempts_error += sum(a.get("status") == "ERROR" for a in attempts)
        if rows:
            symbols_with_rows += 1
        total_rows += len(rows)
        symbol_fields: set[str] = set()
        for row in rows:
            for field, value in row.items():
                field = str(field)
                symbol_fields.add(field)
                types[field][_type_name(value)] += 1
                if value not in (None, "", "-"):
                    nonnull[field] += 1
        for field in symbol_fields:
            presence[field] += 1

    fields = sorted(set(presence) | set(nonnull) | set(types))
    field_summary = [
        {
            "field": field,
            "symbols_present": int(presence[field]),
            "nonnull_rows": int(nonnull[field]),
            "type_counts": dict(sorted(types[field].items())),
        }
        for field in fields
    ]
    candidates = _field_name_candidates(fields)

    report = {
        "version": "rk-mis-smart-money-shareholding-schema-probe-v1",
        "status": "PRE_OUTCOME_SCHEMA_DIAGNOSIS_COMPLETE",
        "protocol": str(args.protocol),
        "protocol_sha256": hashlib.sha256(args.protocol.read_bytes()).hexdigest(),
        "anchor_target": anchor,
        "anchor_resolved_trading_date": resolved_anchor,
        "eligible_company_universe_rows": len(universe),
        "sample_rows": len(sample),
        "symbols_with_shareholding_rows": symbols_with_rows,
        "total_shareholding_rows_examined": total_rows,
        "response_field_count": len(fields),
        "candidate_institutional_field_names": candidates,
        "field_summary": field_summary,
        "api_attempts_ok": attempts_ok,
        "api_attempts_error": attempts_error,
        "source_hash_chain_sha256": hashlib.sha256("\n".join(sorted(source_hashes)).encode("utf-8")).hexdigest(),
        "anchor_source": anchor_meta,
        "official_requests_made": session.requests_made,
        "raw_shareholding_values_published": False,
        "company_level_holding_values_published": False,
        "future_outcomes_seen": False,
        "decision_note": "Field-name matches are schema candidates only. Ambiguous institutional semantics must not be scored.",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
