from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
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
MAX_RELATIONS_PER_CATEGORY_PER_SYMBOL = 4

ACTION_RE = re.compile(
    r"\b(will|plans?|planned|planning|under\s+(?:implementation|construction)|commission(?:ed|ing)?|"
    r"commerciali[sz](?:e|ed|ation)|launch(?:ed|ing)?|introduc(?:e|ed|ing)|develop(?:ed|ing|ment)?|"
    r"adding|add|increase(?:d|ing)?|expand(?:ed|ing)?|expansion|set\s*up|setting\s*up|"
    r"entered|entering|entry|signed|secured|received|approved|qualified|ramp(?:ed|ing)?|"
    r"funded|financed|sanctioned|completed|operational|started|commenced)\b",
    re.I,
)
COMPANY_ATTRIBUTION_RE = re.compile(r"\b(we|our|us|the\s+company|company's|the\s+group|group's)\b", re.I)
NEGATION_RE = re.compile(r"\b(no|not|without|cancelled|canceled|withdrawn|deferred|abandoned|indicative|aspirational)\b", re.I)
INDUSTRY_PEER_RE = re.compile(r"\b(industry|sector|peer|competitor|competition\s+has|market\s+capacity|industry\s+capacity)\b", re.I)

HIGH_INFO_SUBJECT_RE = re.compile(r"investor presentation|press release|analyst|institutional investor|con\.?\s*call|annual report", re.I)

# High-precision category-specific relation requirements. Each alternative is a tuple
# of regexes that all must match within one sentence or adjacent sentence pair.
PATTERNS: dict[str, dict[str, list[tuple[re.Pattern[str], ...]]]] = {
    "runway": {
        "committed_capacity_expansion": [
            (re.compile(r"\bcapacity\s+expansion\b|\bexpand(?:ing|ed)?\s+(?:the\s+)?capacity\b|\badding\s+(?:new\s+)?capacity\b", re.I), ACTION_RE),
            (re.compile(r"\bgreenfield\b|\bbrownfield\b", re.I), re.compile(r"\bplant|facility|capacity|project\b", re.I), ACTION_RE),
            (re.compile(r"\bnew\s+(?:plant|facility|manufacturing\s+unit|production\s+line)\b", re.I), ACTION_RE),
        ],
        "multi_phase_growth_roadmap": [
            (re.compile(r"\bphase\s*(?:ii|iii|2|3)\b|\bnext\s+phase\b|\bmultiple\s+phases\b", re.I), re.compile(r"\bplant|facility|capacity|expansion|project|rollout\b", re.I)),
        ],
        "funding_visibility": [
            (re.compile(r"\binternal\s+accruals?\b|\bcash\s+accruals?\b", re.I), re.compile(r"\bcapex|capital\s+expenditure|expansion|project|plant|facility\b", re.I)),
            (re.compile(r"\bterm\s+loan\b|\bsanctioned\s+(?:loan|finance|facility)\b", re.I), re.compile(r"\bcapex|capital\s+expenditure|expansion|project|plant|facility\b", re.I)),
            (re.compile(r"\bfund\s+rais(?:e|ing)|\bcapital\s+raise\b", re.I), re.compile(r"\bcapex|expansion|project|plant|facility\b", re.I), ACTION_RE),
        ],
        "physical_or_operational_headroom": [
            (re.compile(r"\bland\s+bank\b|\bspare\s+land\b|\bavailable\s+land\b", re.I), re.compile(r"\bexpand|expansion|capacity|plant|facility\b", re.I)),
            (re.compile(r"\bexpandable\b|\bmodular\b", re.I), re.compile(r"\bcapacity|plant|facility|infrastructure\b", re.I)),
        ],
        "ramp_or_utilisation_headroom": [
            (re.compile(r"\bramp[- ]?up\b|\bramping\b", re.I), re.compile(r"\bcapacity|plant|facility|line|commissioned\b", re.I)),
            (re.compile(r"\bunderutili[sz]ed\b|\butili[sz]ation\s+headroom\b", re.I), re.compile(r"\bcapacity|plant|facility|line\b", re.I)),
        ],
    },
    "moat": {
        "proprietary_or_ip": [
            (re.compile(r"\bpatent(?:ed|s)?\b", re.I), re.compile(r"\bproduct|process|technology|design|platform|formulation|solution\b", re.I)),
            (re.compile(r"\bproprietary\b|\bowned\s+technology\b|\bknow[- ]how\b", re.I), re.compile(r"\bproduct|process|technology|design|platform|manufactur|solution|capability\b", re.I)),
        ],
        "qualification_or_regulatory_barrier": [
            (re.compile(r"\bapproved\s+vendor\b|\bvendor\s+approval\b|\bapproved\s+supplier\b|\bqualified\s+(?:vendor|supplier)\b", re.I),),
            (re.compile(r"\bqualified\s+by\b|\bapproved\s+by\b", re.I), re.compile(r"\bcustomer|oem|agency|authority|regulator|railway|defen[cs]e|aerospace\b", re.I)),
            (re.compile(r"\bregulatory\s+approval\b", re.I), re.compile(r"\bproduct|market|facility|plant|drug|device|export\b", re.I)),
        ],
        "customer_stickiness": [
            (re.compile(r"\brepeat\s+orders?\b|\brepeat\s+business\b", re.I),),
            (re.compile(r"\blong[- ]term\s+(?:supply\s+)?agreement\b|\blong[- ]term\s+contract\b", re.I),),
            (re.compile(r"\bcustomer\s+since\b", re.I), re.compile(r"\b19\d{2}|20\d{2}\b", re.I)),
            (re.compile(r"\bdesign[- ]in\b|\bdesign\s+win\b|\bembedded\s+qualification\b", re.I),),
        ],
        "cost_process_or_scale_advantage": [
            (re.compile(r"\bbackward\s+integration\b", re.I), re.compile(r"\bcost|supply|margin|raw\s+material|process|control\b", re.I)),
            (re.compile(r"\bcost\s+advantage\b|\bprocess\s+advantage\b|\bscale\s+advantage\b", re.I),),
            (re.compile(r"\byield\s+improvement\b", re.I), re.compile(r"\bprocess|manufactur|cost|margin\b", re.I)),
        ],
        "market_position_or_limited_competition": [
            (re.compile(r"\bmarket\s+leader(?:ship)?\b", re.I), re.compile(r"\bproduct|segment|category|market|industry\b", re.I)),
            (re.compile(r"\bsole\s+supplier\b|\bsingle[- ]source\b|\bonly\s+manufacturer\b|\bonly\s+producer\b|\blimited\s+qualified\s+(?:vendors?|suppliers?|competition)\b", re.I),),
        ],
    },
    "optionality": {
        "new_product_or_platform": [
            (re.compile(r"\bnew\s+(?:product|platform|solution|offering)s?\b", re.I), re.compile(r"\blaunch|commerciali[sz]|introduc|develop|approval|production|manufactur|rollout\b", re.I)),
            (re.compile(r"\bproduct\s+launch\b|\blaunched\s+(?:a\s+)?new\b", re.I),),
        ],
        "new_customer_or_vendor_approval": [
            (re.compile(r"\bnew\s+customers?\b|\bcustomer\s+addition\b|\bonboard(?:ed|ing)\s+(?:a\s+)?new\s+customer\b", re.I), re.compile(r"\border|revenue|business|commercial|supply|contract|approval|qualification\b", re.I)),
            (re.compile(r"\bnew\s+(?:vendor|supplier)\s+approval\b|\bnewly\s+approved\s+(?:vendor|supplier)\b", re.I),),
        ],
        "new_geography": [
            (re.compile(r"\benter(?:ed|ing)?\s+(?:the\s+)?(?:new\s+)?market\b|\bmarket\s+entry\b|\bnew\s+geograph(?:y|ies)\b", re.I), ACTION_RE),
            (re.compile(r"\bexpansion\s+into\b|\bexpand(?:ing|ed)?\s+into\b", re.I), re.compile(r"\bmarket|country|region|geograph\b", re.I)),
        ],
        "export_expansion": [
            (re.compile(r"\bexport\s+(?:expansion|growth|market|markets|customer|customers)\b", re.I), re.compile(r"\bnew|expand|increase|enter|add|launch|approval|order|customer|market\b", re.I)),
            (re.compile(r"\bnew\s+export\s+(?:market|customer|order)s?\b", re.I),),
        ],
        "adjacent_vertical_or_use_case": [
            (re.compile(r"\badjacent\s+(?:vertical|market|segment|use\s+case)\b|\bnew\s+vertical\b|\bnew\s+use\s+case\b", re.I), re.compile(r"\bproduct|technology|solution|capability|enter|launch|commercial\b", re.I)),
        ],
    },
}


class RelationError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\s*[\r\n]+\s*", cleaned)
    return [x.strip() for x in parts if x.strip()]


def sentence_pairs(sentences: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, sentence in enumerate(sentences):
        out.append((i, sentence))
        if i + 1 < len(sentences):
            out.append((i, sentence + " " + sentences[i + 1]))
    return out


def relation_candidates(text: str, subject: str | None) -> list[dict[str, Any]]:
    results = []
    seen = set()
    high_info = bool(HIGH_INFO_SUBJECT_RE.search(subject or ""))
    for sentence_index, chunk in sentence_pairs(split_sentences(text)):
        neg = bool(NEGATION_RE.search(chunk))
        peer = bool(INDUSTRY_PEER_RE.search(chunk))
        attr = bool(COMPANY_ATTRIBUTION_RE.search(chunk))
        action = bool(ACTION_RE.search(chunk))
        for family, categories in PATTERNS.items():
            for category, alternatives in categories.items():
                for alt_index, regexes in enumerate(alternatives, start=1):
                    matched = []
                    ok = True
                    for regex in regexes:
                        match = regex.search(chunk)
                        if not match:
                            ok = False
                            break
                        matched.append(match.group(0)[:120])
                    if not ok:
                        continue
                    key = (family, category, sentence_index, alt_index)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        "evidence_family": family,
                        "evidence_category": category,
                        "pattern_id": f"{family.upper()}_{category.upper()}_{alt_index}",
                        "matched_terms": matched,
                        "company_attribution_flag": attr,
                        "action_or_status_flag": action,
                        "negation_flag": neg,
                        "industry_or_peer_context_flag": peer,
                        "high_information_document_subject_flag": high_info,
                        "sentence_index": sentence_index,
                    })
    return results


def load_pages(claim_candidates: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    for doc in claim_candidates.get("documents") or []:
        pages = sorted({
            int(p["page"])
            for p in doc.get("page_candidates") or []
            if p.get("runway") or p.get("moat") or p.get("optionality")
        })
        if pages:
            selected.append({
                "sample_symbol": doc.get("sample_symbol"),
                "sample_isin": doc.get("sample_isin"),
                "known_at": doc.get("known_at"),
                "subject": doc.get("subject"),
                "source_url": doc.get("source_url"),
                "source_sha256_expected": doc.get("source_sha256"),
                "pages": pages,
            })
    return selected


def main() -> None:
    p = argparse.ArgumentParser(description="Extract strict structured qualitative documentary relations for blinded review")
    p.add_argument("--claim-candidates", type=Path, required=True)
    p.add_argument("--review-lock", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    lock = json.loads(args.review_lock.read_text(encoding="utf-8"))
    if lock.get("status") != "LOCKED_BEFORE_STRICT_RELATION_EXTRACTION_REVIEW_AND_OUTCOME_LOAD":
        raise RelationError("qualitative review grammar not frozen")
    payload = json.loads(args.claim_candidates.read_text(encoding="utf-8"))
    selected = load_pages(payload)
    session = TECH.OfficialSession(request_budget=len(selected) + 10, timeout=35, sleep_seconds=0.02)

    documents = []
    statuses = Counter()
    source_hashes = []
    symbol_categories: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    relation_counts = Counter()

    for i, doc in enumerate(selected):
        record = {**doc, "download_status": None, "source_sha256": None, "page_relations": []}
        try:
            raw = session.request(
                doc["source_url"],
                referer="https://www.nseindia.com/companies-listing/corporate-filings-announcements",
                accept="application/pdf,*/*",
            )
            if len(raw) > MAX_DOCUMENT_BYTES or not raw.startswith(b"%PDF"):
                record["download_status"] = "INVALID_OR_OVERSIZE"
            else:
                h = sha256(raw)
                record["source_sha256"] = h
                if doc.get("source_sha256_expected") and h != doc["source_sha256_expected"]:
                    record["download_status"] = "HASH_MISMATCH"
                else:
                    from pypdf import PdfReader
                    reader = PdfReader(io.BytesIO(raw))
                    record["download_status"] = "PARSED"
                    per_category = Counter()
                    for page_no in doc["pages"]:
                        if page_no < 1 or page_no > len(reader.pages):
                            continue
                        try:
                            text = reader.pages[page_no - 1].extract_text() or ""
                        except Exception:
                            continue
                        rels = relation_candidates(text, doc.get("subject"))
                        kept = []
                        for rel in rels:
                            key = (rel["evidence_family"], rel["evidence_category"])
                            if per_category[key] >= MAX_RELATIONS_PER_CATEGORY_PER_SYMBOL:
                                continue
                            per_category[key] += 1
                            kept.append(rel)
                            symbol_categories[str(doc["sample_symbol"])][rel["evidence_family"]].add(rel["evidence_category"])
                            relation_counts[(rel["evidence_family"], rel["evidence_category"])] += 1
                        if kept:
                            record["page_relations"].append({"page": page_no, "relations": kept})
                    source_hashes.append(f"{doc['sample_isin']}|{h}")
        except Exception as exc:
            record["download_status"] = "DOWNLOAD_OR_PARSE_ERROR"
            record["error"] = type(exc).__name__
        statuses[str(record["download_status"])] += 1
        documents.append(record)
        if i + 1 < len(selected):
            time.sleep(0.04)

    possible_two_feature = 0
    possible_runway_or_moat = 0
    per_symbol = {}
    for symbol, families in sorted(symbol_categories.items()):
        covered = [fam for fam in ("runway", "moat", "optionality") if families.get(fam)]
        passes = len(covered) >= 2 and bool(families.get("runway") or families.get("moat"))
        if len(covered) >= 2:
            possible_two_feature += 1
        if passes:
            possible_runway_or_moat += 1
        per_symbol[symbol] = {
            "families_with_strict_relation_candidate": covered,
            "categories": {fam: sorted(cats) for fam, cats in families.items()},
            "mechanical_review_upper_bound_eligible": passes,
        }

    report = {
        "version": "rk-mis-strict-qualitative-documentary-relations-v1",
        "review_lock_sha256": sha256(args.review_lock.read_bytes()),
        "input_claim_candidates_sha256": sha256(args.claim_candidates.read_bytes()),
        "documents_with_qualitative_candidate_pages": len(selected),
        "document_status_counts": dict(sorted(statuses.items())),
        "symbols_with_any_strict_relation_candidate": len(symbol_categories),
        "symbols_with_two_or_more_relation_families": possible_two_feature,
        "mechanical_review_upper_bound_eligible_under_qualitative_protocol": possible_runway_or_moat,
        "relation_counts_by_category": {
            f"{fam}.{cat}": count for (fam, cat), count in sorted(relation_counts.items())
        },
        "source_hash_chain_sha256": sha256("\n".join(sorted(source_hashes)).encode("utf-8")),
        "future_outcome_prices_loaded": False,
        "raw_pdf_files_published": False,
        "raw_page_text_published": False,
        "strict_relations_are_scores": False,
        "next_gate": "BLINDED_MODEL_OR_HUMAN_REVIEW_OF_STRUCTURED_RELATIONS_BEFORE_PREDICTOR_SNAPSHOT",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "qualitative_relations.json").write_text(
        json.dumps({
            "report": report,
            "per_symbol": per_symbol,
            "documents": documents,
            "review_state": "ALL_RELATIONS_PENDING_REVIEW",
            "raw_page_text_included": False,
        }, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
