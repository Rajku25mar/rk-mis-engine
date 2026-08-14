from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import run_promoter_accumulation_replay as promoter
import run_technical_replay as market
from multibagger_pipeline.empirical_sampling import deterministic_market_sample

INSTITUTIONAL_RE = re.compile(
    r"mutual|foreign|portfolio|institution|insurance|pension|provident|bank|fund|fii|fpi|dii|financialinstitution",
    re.I,
)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def qname_tail(text: str | None) -> str | None:
    if not text:
        return None
    return str(text).strip().split(":")[-1]


def official_xbrl_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if not url.startswith("https://") or url.endswith("/-"):
        return None
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "nseindia.com" or host.endswith(".nseindia.com"):
        return url
    return None


def latest_safe_xbrl(raw_rows: list[dict[str, Any]], anchor: str) -> dict[str, Any] | None:
    candidates = []
    for raw in raw_rows:
        norm = promoter.normalize_shareholding_row(raw)
        url = official_xbrl_url(raw.get("xbrl"))
        if not url or not promoter.safe_at_anchor(norm, anchor):
            continue
        candidates.append({
            "period_end": norm.get("period_end"),
            "known_at": norm.get("version_known_at"),
            "known_at_basis": norm.get("known_at_basis"),
            "source_url": url,
        })
    if not candidates:
        return None
    candidates.sort(key=lambda r: (str(r.get("period_end") or ""), str(r.get("known_at") or "")))
    return candidates[-1]


def inspect_xbrl(content: bytes) -> dict[str, Any]:
    root = ET.fromstring(content)
    element_names: Counter[str] = Counter()
    member_names: Counter[str] = Counter()
    dimension_names: Counter[str] = Counter()
    schema_refs: Counter[str] = Counter()

    for elem in root.iter():
        name = local_name(elem.tag)
        element_names[name] += 1
        if name == "explicitMember":
            member = qname_tail(elem.text)
            if member:
                member_names[member] += 1
            dimension = qname_tail(elem.attrib.get("dimension"))
            if dimension:
                dimension_names[dimension] += 1
        if name == "schemaRef":
            href = elem.attrib.get("{http://www.w3.org/1999/xlink}href") or elem.attrib.get("href")
            if href:
                schema_refs[Path(str(href)).name] += 1

    return {
        "element_names": element_names,
        "member_names": member_names,
        "dimension_names": dimension_names,
        "schema_refs": schema_refs,
    }


def counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"name": key, "occurrences": int(counter[key])} for key in sorted(counter)]


def matched_names(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"name": key, "occurrences": int(counter[key])}
        for key in sorted(counter)
        if INSTITUTIONAL_RE.search(key)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe pre-anchor NSE shareholding XBRL taxonomy names without publishing holdings")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_PRE_OUTCOME_XBRL_TAXONOMY_DIAGNOSIS":
        raise RuntimeError("XBRL taxonomy probe protocol is not frozen")
    if protocol.get("future_outcomes_seen") is not False:
        raise RuntimeError("XBRL taxonomy probe must remain pre-outcome")

    anchor = protocol["anchor_date"]
    sample_size = int(protocol["sample_size"])
    max_docs = int(protocol["max_xbrl_documents"])
    window = protocol["shareholding_window"]
    session = market.OfficialSession(request_budget=100, timeout=30, sleep_seconds=0.04)

    resolved_anchor, anchor_raw, anchor_meta = market.resolve_on_or_before(anchor, session)
    universe = market.company_universe(market.parse_bhavcopy(anchor_raw), resolved_anchor)
    sample = deterministic_market_sample(
        universe,
        sample_size=sample_size,
        salt=protocol["sample_salt"],
        strata_fields=("market_type",),
    )

    all_elements: Counter[str] = Counter()
    all_members: Counter[str] = Counter()
    all_dimensions: Counter[str] = Counter()
    all_schema_refs: Counter[str] = Counter()
    xbrl_hashes: list[str] = []
    docs = []

    for member in sample:
        if len(docs) >= max_docs:
            break
        rows, attempts, _ = promoter.fetch_symbol_shareholding(
            session,
            member["symbol"],
            window["from_date"],
            anchor,
        )
        selected = latest_safe_xbrl(rows, anchor)
        if not selected:
            continue
        try:
            content = session.request(
                selected["source_url"],
                referer=promoter.REFERER,
                accept="application/xml,text/xml,*/*",
            )
            if not content.lstrip().startswith(b"<"):
                raise RuntimeError("not XML")
            inspected = inspect_xbrl(content)
        except Exception as exc:
            docs.append({
                "sample_symbol": member["symbol"],
                "period_end": selected.get("period_end"),
                "known_at": selected.get("known_at"),
                "status": "ERROR",
                "error": type(exc).__name__,
            })
            continue
        source_hash = hashlib.sha256(content).hexdigest()
        xbrl_hashes.append(f"{member['isin']}|{source_hash}")
        all_elements.update(inspected["element_names"])
        all_members.update(inspected["member_names"])
        all_dimensions.update(inspected["dimension_names"])
        all_schema_refs.update(inspected["schema_refs"])
        docs.append({
            "sample_symbol": member["symbol"],
            "period_end": selected.get("period_end"),
            "known_at": selected.get("known_at"),
            "known_at_basis": selected.get("known_at_basis"),
            "status": "PARSED",
            "source_sha256": source_hash,
        })

    report = {
        "version": "rk-mis-smart-money-xbrl-taxonomy-probe-v1",
        "status": "PRE_OUTCOME_XBRL_TAXONOMY_DIAGNOSIS_COMPLETE",
        "protocol": str(args.protocol),
        "protocol_sha256": hashlib.sha256(args.protocol.read_bytes()).hexdigest(),
        "anchor_target": anchor,
        "anchor_resolved_trading_date": resolved_anchor,
        "eligible_company_universe_rows": len(universe),
        "sample_rows": len(sample),
        "xbrl_documents_attempted": len(docs),
        "xbrl_documents_parsed": sum(d.get("status") == "PARSED" for d in docs),
        "document_chronology": docs,
        "element_names": counter_rows(all_elements),
        "explicit_member_names": counter_rows(all_members),
        "dimension_names": counter_rows(all_dimensions),
        "schema_reference_names": counter_rows(all_schema_refs),
        "candidate_institutional_element_names": matched_names(all_elements),
        "candidate_institutional_member_names": matched_names(all_members),
        "candidate_institutional_dimension_names": matched_names(all_dimensions),
        "source_hash_chain_sha256": hashlib.sha256("\n".join(sorted(xbrl_hashes)).encode("utf-8")).hexdigest(),
        "anchor_source": anchor_meta,
        "official_requests_made": session.requests_made,
        "numeric_holding_values_published": False,
        "raw_xbrl_published": False,
        "future_outcomes_seen": False,
        "decision_note": "Taxonomy-name matches are not yet numeric features. A separate mapping/coverage protocol is required before any Smart Money score or outcome test.",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
