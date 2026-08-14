from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import probe_smart_money_xbrl_schema as schema_probe
import run_promoter_accumulation_replay as promoter
import run_technical_replay as market
from multibagger_pipeline.empirical_sampling import deterministic_market_sample

CATEGORY_AXIS = "CategoryOfShareholdersAxis"


def _numeric_text(text: str | None) -> bool:
    if text is None:
        return False
    try:
        float(str(text).replace(",", "").strip())
        return True
    except ValueError:
        return False


def parse_contexts_and_fact_meta(content: bytes) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    root = ET.fromstring(content)
    contexts: dict[str, dict[str, Any]] = {}
    facts: list[dict[str, Any]] = []

    for elem in root.iter():
        name = schema_probe.local_name(elem.tag)
        if name == "context":
            ctx_id = elem.attrib.get("id")
            if not ctx_id:
                continue
            members = []
            typed_count = 0
            for child in elem.iter():
                child_name = schema_probe.local_name(child.tag)
                if child_name == "explicitMember":
                    members.append({
                        "dimension": schema_probe.qname_tail(child.attrib.get("dimension")),
                        "member": schema_probe.qname_tail(child.text),
                    })
                elif child_name == "typedMember":
                    typed_count += 1
            contexts[ctx_id] = {"members": members, "typed_member_count": typed_count}
            continue

        ctx = elem.attrib.get("contextRef")
        if not ctx or not _numeric_text(elem.text):
            continue
        facts.append({
            "fact": name,
            "context_ref": ctx,
            "unit_ref": elem.attrib.get("unitRef"),
        })
    return contexts, facts


def mapping_meta(
    contexts: dict[str, dict[str, Any]],
    facts: list[dict[str, Any]],
    *,
    member: str,
    fact: str,
) -> dict[str, Any]:
    target_contexts = []
    for ctx_id, ctx in contexts.items():
        category_members = [
            m.get("member")
            for m in ctx.get("members", [])
            if m.get("dimension") == CATEGORY_AXIS
        ]
        if member not in category_members:
            continue
        if int(ctx.get("typed_member_count", 0)) != 0:
            continue
        target_contexts.append(ctx_id)

    matched = [row for row in facts if row["fact"] == fact and row["context_ref"] in target_contexts]
    units = Counter(str(row.get("unit_ref") or "") for row in matched)
    return {
        "aggregate_context_count": len(target_contexts),
        "numeric_fact_count": len(matched),
        "unit_ref_counts": dict(sorted(units.items())),
        "unambiguous": len(target_contexts) == 1 and len(matched) == 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe NSE shareholding XBRL context-to-fact mappings without exposing values")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_PRE_OUTCOME_CONTEXT_TO_FACT_MAPPING_DIAGNOSIS":
        raise RuntimeError("mapping protocol is not frozen")
    if protocol.get("future_outcomes_seen") is not False:
        raise RuntimeError("mapping probe must remain pre-outcome")

    anchor = protocol["anchor_date"]
    window = protocol["shareholding_window"]
    sample_size = int(protocol["sample_size"])
    max_docs = int(protocol["max_xbrl_documents"])
    targets = protocol["target_mappings"]
    session = market.OfficialSession(request_budget=120, timeout=30, sleep_seconds=0.04)

    resolved_anchor, anchor_raw, anchor_meta = market.resolve_on_or_before(anchor, session)
    universe = market.company_universe(market.parse_bhavcopy(anchor_raw), resolved_anchor)
    sample = deterministic_market_sample(
        universe,
        sample_size=sample_size,
        salt=protocol["sample_salt"],
        strata_fields=("market_type",),
    )

    documents = []
    source_hashes = []
    summary_counts: Counter[str] = Counter()

    for member_row in sample:
        if len(documents) >= max_docs:
            break
        raw_rows, _, _ = promoter.fetch_symbol_shareholding(
            session,
            member_row["symbol"],
            window["from_date"],
            anchor,
        )
        selected = schema_probe.latest_safe_xbrl(raw_rows, anchor)
        if not selected:
            continue
        try:
            content = session.request(
                selected["source_url"],
                referer=promoter.REFERER,
                accept="application/xml,text/xml,*/*",
            )
            contexts, facts = parse_contexts_and_fact_meta(content)
        except Exception as exc:
            documents.append({
                "sample_symbol": member_row["symbol"],
                "period_end": selected.get("period_end"),
                "known_at": selected.get("known_at"),
                "status": "ERROR",
                "error": type(exc).__name__,
            })
            continue

        mf = mapping_meta(contexts, facts, **targets["mf_holding"])
        inst = mapping_meta(contexts, facts, **targets["institutional_breadth"])
        fpi_rows = []
        for candidate in targets["fii_holding_candidates"]:
            meta = mapping_meta(contexts, facts, **candidate)
            fpi_rows.append({"member": candidate["member"], **meta})

        if mf["unambiguous"]:
            summary_counts["mf_unambiguous_docs"] += 1
        if inst["unambiguous"]:
            summary_counts["institutional_breadth_unambiguous_docs"] += 1
        for fpi in fpi_rows:
            if fpi["unambiguous"]:
                summary_counts[f"fpi_unambiguous::{fpi['member']}"] += 1

        source_hash = hashlib.sha256(content).hexdigest()
        source_hashes.append(f"{member_row['isin']}|{source_hash}")
        documents.append({
            "sample_symbol": member_row["symbol"],
            "period_end": selected.get("period_end"),
            "known_at": selected.get("known_at"),
            "status": "PARSED",
            "source_sha256": source_hash,
            "mf_holding_mapping": mf,
            "institutional_breadth_mapping": inst,
            "fii_holding_candidate_mappings": fpi_rows,
        })

    parsed = sum(row.get("status") == "PARSED" for row in documents)
    threshold = 8
    report = {
        "version": "rk-mis-smart-money-xbrl-mapping-probe-v1",
        "status": "PRE_OUTCOME_CONTEXT_TO_FACT_MAPPING_DIAGNOSIS_COMPLETE",
        "protocol": str(args.protocol),
        "protocol_sha256": hashlib.sha256(args.protocol.read_bytes()).hexdigest(),
        "anchor_target": anchor,
        "anchor_resolved_trading_date": resolved_anchor,
        "eligible_company_universe_rows": len(universe),
        "sample_rows": len(sample),
        "xbrl_documents_attempted": len(documents),
        "xbrl_documents_parsed": parsed,
        "required_unambiguous_documents": threshold,
        "mapping_summary_counts": dict(sorted(summary_counts.items())),
        "documents": documents,
        "source_hash_chain_sha256": hashlib.sha256("\n".join(sorted(source_hashes)).encode("utf-8")).hexdigest(),
        "anchor_source": anchor_meta,
        "official_requests_made": session.requests_made,
        "numeric_values_published": False,
        "raw_xbrl_published": False,
        "future_outcomes_seen": False,
        "decision_note": "A mapping must reach the preregistered 8-of-10 unambiguous threshold. FPI candidate selection remains fail-closed if more than one candidate is viable without a frozen semantic distinction.",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
