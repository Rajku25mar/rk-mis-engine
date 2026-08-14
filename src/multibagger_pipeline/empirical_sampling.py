from __future__ import annotations

import hashlib, math
from typing import Any, Sequence
from .empirical_common import CohortSeed, digest, is_official_url

VALID_GROUPS={"HISTORICAL_WINNER_CANDIDATE","HISTORICAL_FAILURE_CANDIDATE","NEUTRAL_CONTROL_CANDIDATE"}

def validate_stress_cohort(seeds: Sequence[CohortSeed]) -> dict[str, Any]:
    issues=[]; seen=set(); groups={}
    for seed in seeds:
        key=(seed.canonical_id,seed.anchor_date)
        if not seed.canonical_id: issues.append({"code":"MISSING_CANONICAL_ID","company":seed.company_name})
        if key in seen: issues.append({"code":"DUPLICATE_COHORT_ROW","company":seed.company_name})
        seen.add(key); groups[seed.seed_group]=groups.get(seed.seed_group,0)+1
        if seed.source_url and not is_official_url(seed.source_url): issues.append({"code":"NON_OFFICIAL_SOURCE_URL","company":seed.company_name})
        if seed.seed_group not in VALID_GROUPS: issues.append({"code":"UNKNOWN_SEED_GROUP","company":seed.company_name})
    warnings=[]
    if groups.get("HISTORICAL_WINNER_CANDIDATE",0)<10: warnings.append("FEWER_THAN_10_WINNER_STRESS_SEEDS")
    if groups.get("HISTORICAL_FAILURE_CANDIDATE",0)<10: warnings.append("FEWER_THAN_10_FAILURE_STRESS_SEEDS")
    return {"rows":len(seeds),"groups":groups,"issues":issues,"warnings":warnings,"valid":not issues,
            "probability_calibration_allowed":False,
            "reason":"STRESS_CASE_CONTROL_IS_OUTCOME_ENRICHED_AND_CANNOT_ESTIMATE_MARKET_BASE_RATES"}

def deterministic_market_sample(universe: Sequence[dict[str,Any]], *, sample_size:int, salt:str,
                                strata_fields:Sequence[str]=("market_type","sector")) -> list[dict[str,Any]]:
    eligible=[dict(r) for r in universe if r.get("is_eligible",True)]
    if sample_size<=0 or not eligible: return []
    for r in eligible:
        if not r.get("canonical_id"): r["canonical_id"]=(r.get("symbol") or "").upper()
        r["_hash"]=digest(salt,r["canonical_id"],r.get("as_of_date",""))
        r["_stratum"]=tuple(str(r.get(f,"") or "UNKNOWN").upper() for f in strata_fields)
    buckets={}
    for r in eligible: buckets.setdefault(r["_stratum"],[]).append(r)
    for rows in buckets.values(): rows.sort(key=lambda x:(x["_hash"],x["canonical_id"]))
    n=min(sample_size,len(eligible)); total=len(eligible); allocations={}; remainders=[]
    for st,rows in buckets.items():
        exact=n*len(rows)/total; base=int(math.floor(exact))
        if base==0 and n>=len(buckets): base=1
        allocations[st]=min(base,len(rows)); remainders.append((exact-math.floor(exact),st))
    used=sum(allocations.values())
    for _,st in sorted(remainders,key=lambda x:(-x[0],x[1])):
        if used>=n: break
        if allocations[st]<len(buckets[st]): allocations[st]+=1; used+=1
    selected=[]
    for st in sorted(buckets): selected.extend(buckets[st][:allocations[st]])
    selected.sort(key=lambda x:(x["_hash"],x["canonical_id"])); selected=selected[:n]
    fingerprint=hashlib.sha256(salt.encode()).hexdigest()[:12]
    for r in selected:
        r.pop("_hash",None); r["sampling_stratum"]="|".join(r.pop("_stratum",()))
        r["sampling_method"]="SHA256_OUTCOME_BLIND_STRATIFIED"; r["sampling_salt_fingerprint"]=fingerprint
    return selected
