from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable

ACTION_VERSION = "0.9.0"

BONUS_RE = re.compile(r"\bbonus\s+(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\b", re.I)
SPLIT_RE = re.compile(r"(?:face\s*value\s*)?split|sub[- ]?division", re.I)
SPLIT_VALUES_RE = re.compile(r"from\s+(?:rs\.?|re\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*/?-?\s*(?:per\s*share)?\s*to\s+(?:rs\.?|re\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
RIGHTS_RE = re.compile(r"\brights?\s+(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\b", re.I)
PREMIUM_RE = re.compile(r"premium\s*(?:rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
AT_PRICE_RE = re.compile(r"(?:at|@)\s*(?:rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
DIVIDEND_RE = re.compile(r"\b(?:interim\s+|final\s+|special\s+)?dividend\b", re.I)
DIVIDEND_AMOUNT_RE = re.compile(r"(?:rs\.?|re\.?|inr)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:per\s*share)?", re.I)
BUYBACK_RE = re.compile(r"\bbuy\s*back\b|\bbuyback\b", re.I)
MERGER_RE = re.compile(r"\bmerger\b|\bamalgamation\b|\bscheme\s+of\s+amalgamation\b", re.I)
DEMERGER_RE = re.compile(r"\bdemerger\b|\bde-merger\b|\bspin[- ]?off\b", re.I)
CONSOLIDATION_RE = re.compile(r"\bconsolidation\b|\breverse\s+split\b", re.I)

@dataclass(frozen=True)
class ParsedCorporateAction:
    action_type: str
    status: str
    purpose: str
    ex_date: str | None = None
    record_date: str | None = None
    old_face_value: float | None = None
    new_face_value: float | None = None
    new_shares: float | None = None
    old_shares: float | None = None
    subscription_price: float | None = None
    dividend_per_share: float | None = None
    price_factor: float | None = None
    share_factor: float | None = None
    reason: str | None = None
    def to_dict(self) -> dict[str, Any]: return asdict(self)

def _iso(value: str | date | None) -> str | None:
    if value is None or value == "": return None
    if isinstance(value, date): return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()

def _float(v: Any) -> float | None:
    if v is None or v == "": return None
    try: return float(v)
    except (TypeError, ValueError): return None

def _hash(*parts: Any) -> str:
    raw = "|".join("" if x is None else str(x) for x in parts)
    return hashlib.sha256(raw.encode()).hexdigest()

def parse_corporate_action(purpose: str, *, ex_date: str | date | None = None, record_date: str | date | None = None, face_value: float | None = None) -> ParsedCorporateAction:
    text = " ".join((purpose or "").split()); ex = _iso(ex_date); record = _iso(record_date)
    m = BONUS_RE.search(text)
    if m:
        new_shares, old_shares = float(m.group(1)), float(m.group(2))
        if new_shares <= 0 or old_shares <= 0: return ParsedCorporateAction("BONUS","NEEDS_REVIEW",text,ex,record,reason="INVALID_BONUS_RATIO")
        sf=(old_shares+new_shares)/old_shares
        return ParsedCorporateAction("BONUS","DETERMINISTIC",text,ex,record,new_shares=new_shares,old_shares=old_shares,price_factor=round(1/sf,12),share_factor=round(sf,12))
    if SPLIT_RE.search(text):
        sm=SPLIT_VALUES_RE.search(text)
        if not sm: return ParsedCorporateAction("SPLIT","NEEDS_REVIEW",text,ex,record,reason="SPLIT_VALUES_NOT_PARSED")
        old_fv,new_fv=float(sm.group(1)),float(sm.group(2))
        if old_fv<=0 or new_fv<=0: return ParsedCorporateAction("SPLIT","NEEDS_REVIEW",text,ex,record,reason="INVALID_FACE_VALUES")
        sf=old_fv/new_fv
        return ParsedCorporateAction("SPLIT" if sf>=1 else "CONSOLIDATION","DETERMINISTIC",text,ex,record,old_face_value=old_fv,new_face_value=new_fv,price_factor=round(new_fv/old_fv,12),share_factor=round(sf,12))
    rm=RIGHTS_RE.search(text)
    if rm:
        new_shares,old_shares=float(rm.group(1)),float(rm.group(2)); premium=PREMIUM_RE.search(text); quoted=AT_PRICE_RE.search(text); subscription=None
        if quoted: subscription=float(quoted.group(1))
        elif premium and face_value is not None: subscription=float(face_value)+float(premium.group(1))
        return ParsedCorporateAction("RIGHTS","NEEDS_MARKET_PRICE",text,ex,record,new_shares=new_shares,old_shares=old_shares,subscription_price=subscription,reason=None if subscription is not None else "SUBSCRIPTION_PRICE_NOT_FULLY_KNOWN")
    if MERGER_RE.search(text): return ParsedCorporateAction("MERGER","NEEDS_REVIEW",text,ex,record,reason="EXCHANGE_RATIO_REQUIRED")
    if DEMERGER_RE.search(text): return ParsedCorporateAction("DEMERGER","NEEDS_REVIEW",text,ex,record,reason="VALUE_ALLOCATION_REQUIRED")
    if CONSOLIDATION_RE.search(text): return ParsedCorporateAction("CONSOLIDATION","NEEDS_REVIEW",text,ex,record,reason="CONVERSION_RATIO_REQUIRED")
    if BUYBACK_RE.search(text): return ParsedCorporateAction("BUYBACK","NON_ADJUSTING",text,ex,record)
    if DIVIDEND_RE.search(text):
        dm=DIVIDEND_AMOUNT_RE.search(text); amount=float(dm.group(1)) if dm else None
        return ParsedCorporateAction("DIVIDEND","TOTAL_RETURN_ONLY",text,ex,record,dividend_per_share=amount)
    return ParsedCorporateAction("OTHER","NON_ADJUSTING",text,ex,record)

def resolve_rights_factor(action: ParsedCorporateAction | dict[str, Any], cum_rights_price: float) -> dict[str, Any]:
    a=action if isinstance(action,ParsedCorporateAction) else ParsedCorporateAction(**action)
    if a.action_type!="RIGHTS": raise ValueError("rights factor can only be resolved for RIGHTS action")
    p=_float(cum_rights_price)
    if p is None or p<=0: raise ValueError("cum_rights_price must be positive")
    if a.subscription_price is None or a.new_shares in (None,0) or a.old_shares in (None,0): return {**a.to_dict(),"status":"NEEDS_REVIEW","reason":"INCOMPLETE_RIGHTS_TERMS"}
    terp=(a.old_shares*p+a.new_shares*a.subscription_price)/(a.old_shares+a.new_shares); factor=terp/p
    return {**a.to_dict(),"status":"DETERMINISTIC_WITH_MARKET_PRICE","cum_rights_price":p,"terp":round(terp,12),"price_factor":round(factor,12),"share_factor":round((a.old_shares+a.new_shares)/a.old_shares,12),"reason":None}

def action_id(symbol: str, action: ParsedCorporateAction | dict[str, Any], source_artifact_id: str | None = None) -> str:
    a=action.to_dict() if isinstance(action,ParsedCorporateAction) else action
    return "CA-"+_hash(symbol.upper(),a.get("action_type"),a.get("purpose"),a.get("ex_date"),a.get("record_date"),a.get("price_factor"),source_artifact_id)[:20].upper()

def cumulative_backward_price_factor(actions: Iterable[ParsedCorporateAction | dict[str, Any]], *, price_date: str | date, target_date: str | date, include_rights: bool = True) -> dict[str, Any]:
    start,target=_iso(price_date),_iso(target_date)
    if start is None or target is None or start>target: raise ValueError("price_date must be <= target_date")
    factor=1.0; applied=[]; unresolved=[]
    for raw in actions:
        a=raw.to_dict() if isinstance(raw,ParsedCorporateAction) else dict(raw); ex=a.get("ex_date") or a.get("record_date")
        if not ex or not (start<_iso(ex)<=target): continue
        if a.get("action_type")=="RIGHTS" and not include_rights: continue
        pf=_float(a.get("price_factor")); status=a.get("status")
        if pf is None or status not in {"DETERMINISTIC","DETERMINISTIC_WITH_MARKET_PRICE"}:
            if a.get("action_type") in {"SPLIT","BONUS","CONSOLIDATION","RIGHTS","MERGER","DEMERGER"}: unresolved.append(a)
            continue
        factor*=pf; applied.append(a)
    return {"price_factor":round(factor,12),"applied_actions":applied,"unresolved_actions":unresolved,"calibration_safe":not unresolved}

def adjust_raw_price(raw_price: float, factor_result: dict[str, Any]) -> float | None:
    p=_float(raw_price)
    return None if p is None else round(p*float(factor_result["price_factor"]),8)
