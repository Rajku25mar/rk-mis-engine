from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TECH_SPEC = importlib.util.spec_from_file_location(
    "rk_mis_technical_replay_common", ROOT / "scripts/run_technical_replay.py"
)
TECH = importlib.util.module_from_spec(TECH_SPEC)
assert TECH_SPEC and TECH_SPEC.loader
TECH_SPEC.loader.exec_module(TECH)

MAX_DOCUMENT_BYTES = 15_000_000
NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
PCT = r"\b([0-9]+(?:\.[0-9]+)?)\s*%"
CUR = r"(?:₹|rs\.?|inr)\s*" + NUM + r"\s*(crore|cr|million|mn|billion|bn|lakh|lakhs)?"
UNIT = r"(mtpa|tpa|tpd|mt|tonnes?|tons?|mw|mwp|gw|gwp|units?(?:\s+per\s+annum)?|million\s+units?|mn\s+units?|lakh\s+units?|lakhs\s+units?)"

DIRECT_PCT_PATTERNS = [
    re.compile(r"capacity.{0,70}(?:increase|increased|expand|expanded|expansion|enhance|enhanced|addition|additional).{0,45}\bby\b.{0,20}?" + PCT, re.I),
    re.compile(r"(?:increase|increased|expand|expanded|enhance|enhanced).{0,45}capacity.{0,45}\bby\b.{0,20}?" + PCT, re.I),
]
FROM_TO_PATTERNS = [
    re.compile(r"capacity.{0,80}\bfrom\b\s*" + NUM + r"\s*" + UNIT + r".{0,80}\bto\b\s*" + NUM + r"\s*" + UNIT, re.I),
    re.compile(r"\bfrom\b\s*" + NUM + r"\s*" + UNIT + r".{0,80}\bto\b\s*" + NUM + r"\s*" + UNIT + r".{0,80}capacity", re.I),
]
EXISTING_NEW_PATTERNS = [
    re.compile(r"(?:existing|current|installed|present|pre[- ]expansion)\s+capacity.{0,40}" + NUM + r"\s*" + UNIT + r".{0,120}(?:new|proposed|post[- ]expansion|enhanced|expanded|total)\s+capacity.{0,40}" + NUM + r"\s*" + UNIT, re.I),
    re.compile(r"(?:new|proposed|post[- ]expansion|enhanced|expanded|total)\s+capacity.{0,40}" + NUM + r"\s*" + UNIT + r".{0,120}(?:existing|current|installed|present|pre[- ]expansion)\s+capacity.{0,40}" + NUM + r"\s*" + UNIT, re.I),
]
INCREMENTAL_BASE_PATTERNS = [
    re.compile(r"(?:additional|incremental|adding|add)\s+capacity.{0,40}" + NUM + r"\s*" + UNIT + r".{0,120}(?:existing|current|installed|present|base)\s+capacity.{0,40}" + NUM + r"\s*" + UNIT, re.I),
    re.compile(r"(?:existing|current|installed|present|base)\s+capacity.{0,40}" + NUM + r"\s*" + UNIT + r".{0,120}(?:additional|incremental|adding|add)\s+capacity.{0,40}" + NUM + r"\s*" + UNIT, re.I),
]
ORDERBOOK_PATTERNS = [
    re.compile(r"(?:order\s*book|orderbook|contracted\s+backlog).{0,80}(?:stands?\s+at|stood\s+at|is|of|at|amounts?\s+to|valued\s+at).{0,35}" + CUR, re.I),
    re.compile(CUR + r".{0,35}(?:order\s*book|orderbook|contracted\s+backlog)", re.I),
]
NEGATION_RE = re.compile(r"\b(no|not|without|cancelled|canceled|withdrawn|indicative)\b", re.I)


class NumericExtractionError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fnum(value: str) -> float:
    return float(value.replace(",", ""))


def normalize_unit(unit: str) -> str:
    u = " ".join(unit.lower().split())
    aliases = {
        "ton": "ton", "tons": "ton", "tonne": "ton", "tonnes": "ton",
        "mt": "mt", "tpa": "tpa", "mtpa": "mtpa", "tpd": "tpd",
        "mw": "mw", "mwp": "mwp", "gw": "gw", "gwp": "gwp",
        "unit": "unit", "units": "unit", "units per annum": "unit_per_annum",
        "million unit": "million_unit", "million units": "million_unit",
        "mn unit": "million_unit", "mn units": "million_unit",
        "lakh unit": "lakh_unit", "lakh units": "lakh_unit",
        "lakhs unit": "lakh_unit", "lakhs units": "lakh_unit",
    }
    return aliases.get(u, u)


def split_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\s*[\r\n]+\s*", cleaned)
    return [x.strip() for x in parts if x.strip()]


def sentence_pairs(sentences: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, s in enumerate(sentences):
        out.append((i, s))
        if i + 1 < len(sentences):
            out.append((i, s + " " + sentences[i + 1]))
    return out


def has_negation_near(match_text: str) -> bool:
    return bool(NEGATION_RE.search(match_text))


def capacity_relations(text: str) -> list[dict[str, Any]]:
    out = []
    pairs = sentence_pairs(split_sentences(text))
    seen = set()
    for sentence_index, chunk in pairs:
        for pat in DIRECT_PCT_PATTERNS:
            for m in pat.finditer(chunk):
                if has_negation_near(m.group(0)):
                    continue
                value = float(m.group(1))
                if not (0 < value <= 1000):
                    continue
                key = ("CAPACITY_INCREASE_BY_PERCENT", round(value, 8))
                if key not in seen:
                    seen.add(key)
                    out.append({
                        "pattern_id": key[0],
                        "planned_capacity_increase_pct_candidate": value,
                        "sentence_index": sentence_index,
                    })
        for pat in FROM_TO_PATTERNS:
            for m in pat.finditer(chunk):
                if has_negation_near(m.group(0)):
                    continue
                old, old_unit, new, new_unit = fnum(m.group(1)), normalize_unit(m.group(2)), fnum(m.group(3)), normalize_unit(m.group(4))
                if old <= 0 or new <= old or old_unit != new_unit:
                    continue
                pct = round((new - old) / old * 100.0, 6)
                key = ("CAPACITY_FROM_TO", old, old_unit, new, new_unit)
                if key not in seen:
                    seen.add(key)
                    out.append({"pattern_id":key[0],"old_capacity":old,"new_capacity":new,"capacity_unit":old_unit,"planned_capacity_increase_pct_candidate":pct,"sentence_index":sentence_index})
        for pat in EXISTING_NEW_PATTERNS:
            for m in pat.finditer(chunk):
                if has_negation_near(m.group(0)):
                    continue
                a, au, b, bu = fnum(m.group(1)), normalize_unit(m.group(2)), fnum(m.group(3)), normalize_unit(m.group(4))
                if a <= 0 or b <= 0 or au != bu or a == b:
                    continue
                old, new = (a, b) if b > a else (b, a)
                pct = round((new-old)/old*100.0, 6)
                key=("CAPACITY_EXISTING_NEW",old,au,new)
                if key not in seen:
                    seen.add(key)
                    out.append({"pattern_id":key[0],"old_capacity":old,"new_capacity":new,"capacity_unit":au,"planned_capacity_increase_pct_candidate":pct,"sentence_index":sentence_index})
        for pat in INCREMENTAL_BASE_PATTERNS:
            for m in pat.finditer(chunk):
                if has_negation_near(m.group(0)):
                    continue
                a, au, b, bu = fnum(m.group(1)), normalize_unit(m.group(2)), fnum(m.group(3)), normalize_unit(m.group(4))
                if a <= 0 or b <= 0 or au != bu:
                    continue
                key=("CAPACITY_INCREMENTAL_BASE",a,au,b)
                if key not in seen:
                    seen.add(key)
                    out.append({"pattern_id":key[0],"captured_capacity_1":a,"captured_capacity_2":b,"capacity_unit":au,"planned_capacity_increase_pct_candidate":None,"sentence_index":sentence_index,"review_note":"REVIEW_CAPTURE_ORDER_BEFORE_DERIVATION"})
    return out


def orderbook_relations(text: str) -> list[dict[str, Any]]:
    out=[]; seen=set()
    for sentence_index, chunk in sentence_pairs(split_sentences(text)):
        low=chunk.lower()
        if "pipeline" in low or "tender" in low or "expected order" in low or "order opportunity" in low:
            continue
        for pat in ORDERBOOK_PATTERNS:
            for m in pat.finditer(chunk):
                if has_negation_near(m.group(0)):
                    continue
                groups=[g for g in m.groups() if g is not None]
                value=None; scale="UNSPECIFIED"
                for idx,g in enumerate(groups):
                    if re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?", str(g)):
                        value=fnum(str(g))
                        if idx+1 < len(groups) and re.fullmatch(r"crore|cr|million|mn|billion|bn|lakh|lakhs", str(groups[idx+1]), re.I):
                            scale=str(groups[idx+1]).upper()
                        break
                if value is None or value <= 0:
                    continue
                key=(round(value,8),scale)
                if key not in seen:
                    seen.add(key)
                    out.append({"pattern_id":"ORDERBOOK_VALUE","orderbook_value_candidate":value,"currency":"INR","scale":scale,"sentence_index":sentence_index})
    return out


def load_relevant_pages(claim_candidates: dict[str, Any]) -> list[dict[str, Any]]:
    selected=[]
    for doc in claim_candidates.get("documents") or []:
        pages=[]
        for p in doc.get("page_candidates") or []:
            if p.get("capacity_numeric_candidate") or p.get("orderbook_numeric_candidate"):
                pages.append(int(p["page"]))
        if pages:
            selected.append({
                "sample_symbol":doc.get("sample_symbol"),
                "sample_isin":doc.get("sample_isin"),
                "known_at":doc.get("known_at"),
                "subject":doc.get("subject"),
                "source_url":doc.get("source_url"),
                "source_sha256_expected":doc.get("source_sha256"),
                "pages":sorted(set(pages)),
            })
    return selected


def main() -> None:
    p=argparse.ArgumentParser(description="Strictly extract explicit numeric catalyst relationships from pre-anchor documentary candidate pages")
    p.add_argument("--claim-candidates",type=Path,required=True)
    p.add_argument("--numeric-lock",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    lock=json.loads(args.numeric_lock.read_text(encoding="utf-8"))
    if lock.get("status") != "LOCKED_BEFORE_NUMERIC_RELATION_EXTRACTION_AND_OUTCOME_LOAD":
        raise NumericExtractionError("numeric review rules not frozen")
    payload=json.loads(args.claim_candidates.read_text(encoding="utf-8"))
    selected=load_relevant_pages(payload)
    session=TECH.OfficialSession(request_budget=len(selected)+10,timeout=35,sleep_seconds=0.02)
    results=[]; status=Counter(); source_hashes=[]
    symbols_with_capacity=set(); symbols_with_orderbook=set()
    for i,doc in enumerate(selected):
        rec={**doc,"download_status":None,"source_sha256":None,"page_relations":[]}
        try:
            raw=session.request(doc["source_url"],referer="https://www.nseindia.com/companies-listing/corporate-filings-announcements",accept="application/pdf,*/*")
            if len(raw)>MAX_DOCUMENT_BYTES or not raw.startswith(b"%PDF"):
                rec["download_status"]="INVALID_OR_OVERSIZE"
            else:
                h=sha256(raw); rec["source_sha256"]=h
                if doc.get("source_sha256_expected") and h != doc["source_sha256_expected"]:
                    rec["download_status"]="HASH_MISMATCH"
                else:
                    from pypdf import PdfReader
                    reader=PdfReader(io.BytesIO(raw)); rec["download_status"]="PARSED"
                    for page_no in doc["pages"]:
                        if page_no<1 or page_no>len(reader.pages):
                            continue
                        try: text=reader.pages[page_no-1].extract_text() or ""
                        except Exception: continue
                        caps=capacity_relations(text); obs=orderbook_relations(text)
                        if caps or obs:
                            rec["page_relations"].append({"page":page_no,"capacity_relations":caps,"orderbook_relations":obs})
                            if caps: symbols_with_capacity.add(str(doc["sample_symbol"]))
                            if obs: symbols_with_orderbook.add(str(doc["sample_symbol"]))
                    source_hashes.append(f"{doc['sample_isin']}|{h}")
        except Exception as exc:
            rec["download_status"]="DOWNLOAD_OR_PARSE_ERROR"; rec["error"]=type(exc).__name__
        status[rec["download_status"]]+=1; results.append(rec)
        if i+1<len(selected): time.sleep(0.04)
    report={
        "version":"rk-mis-documentary-numeric-relation-extraction-v1",
        "numeric_lock_sha256":sha256(args.numeric_lock.read_bytes()),
        "input_claim_candidates_sha256":sha256(args.claim_candidates.read_bytes()),
        "documents_with_numeric_candidate_pages":len(selected),
        "document_status_counts":dict(sorted(status.items())),
        "symbols_with_strict_capacity_relation_candidates":len(symbols_with_capacity),
        "symbols_with_strict_orderbook_value_candidates":len(symbols_with_orderbook),
        "strict_capacity_symbols":sorted(symbols_with_capacity),
        "strict_orderbook_symbols":sorted(symbols_with_orderbook),
        "source_hash_chain_sha256":sha256("\n".join(sorted(source_hashes)).encode("utf-8")),
        "future_outcome_prices_loaded":False,
        "raw_pdf_files_published":False,
        "raw_page_text_published":False,
        "structured_relation_candidates_are_scores":False,
        "next_gate":"REVIEW_STRUCTURED_RELATIONS; IF FEWER_THAN_30 COMPANIES CAN_HAVE_AN_APPROVED_NUMERIC_CATALYST_THE_HOLDOUT_CANNOT_MEET_ITS_FROZEN_ELIGIBILITY_MINIMUM"
    }
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    (args.output/"numeric_relations.json").write_text(json.dumps({"report":report,"documents":results,"review_state":"ALL_RELATIONS_PENDING_REVIEW","raw_page_text_included":False},indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))

if __name__=="__main__":
    main()
