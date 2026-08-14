from __future__ import annotations

import argparse
import csv
import hashlib
import http.cookiejar
import io
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(ROOT / "src"))

from multibagger_pipeline.corporate_action_normalizer import (
    cumulative_backward_price_factor,
    parse_corporate_action,
)
from multibagger_pipeline.empirical_sampling import deterministic_market_sample

ARCHIVE_HOSTS = (
    "https://nsearchives.nseindia.com",
    "https://www1.nseindia.com",
    "https://www.nseindia.com",
)
NSE_HOME = "https://www.nseindia.com/"
NSE_ACTIONS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
USER_AGENT = "Mozilla/5.0 (compatible; RK-MIS-Public-Historical-Replay/1.0; bounded-manual-research)"


class ReplayError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def num(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def iso_date(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%y"):
        try:
            from datetime import datetime
            return datetime.strptime(text[:11], fmt).date().isoformat()
        except ValueError:
            pass
    return None


class OfficialSession:
    def __init__(self, *, timeout: int = 30, request_budget: int = 1000, sleep_seconds: float = 0.03):
        self.timeout = timeout
        self.request_budget = request_budget
        self.sleep_seconds = sleep_seconds
        self.requests_made = 0
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.warmed = False

    def request(self, url: str, *, referer: str | None = None, accept: str = "*/*") -> bytes:
        if self.requests_made >= self.request_budget:
            raise ReplayError("official-source request budget exhausted")
        self.requests_made += 1
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-IN,en;q=0.9",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
                host = (urllib.parse.urlparse(resp.geturl()).hostname or "").lower()
                if not (host == "nseindia.com" or host.endswith(".nseindia.com")):
                    raise ReplayError("official request redirected outside NSE")
                return payload
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise ReplayError(f"request failed: {url}: {exc}") from exc

    def warm(self) -> None:
        if not self.warmed:
            self.request(NSE_ACTIONS_PAGE, referer=NSE_HOME, accept="text/html,*/*")
            self.warmed = True

    def json(self, url: str) -> Any:
        self.warm()
        raw = self.request(url, referer=NSE_ACTIONS_PAGE, accept="application/json,text/plain,*/*")
        return json.loads(raw.decode("utf-8-sig"))


def bhavcopy_location(day: str) -> tuple[str, str]:
    d = date.fromisoformat(day)
    mon = d.strftime("%b").upper()
    name = f"cm{d.strftime('%d')}{mon}{d.year}bhav.csv.zip"
    rel = f"/content/historical/EQUITIES/{d.year}/{mon}/{name}"
    return name, rel


def fetch_bhavcopy(day: str, session: OfficialSession, *, quiet_missing: bool = False) -> tuple[bytes, dict[str, Any]] | None:
    _, rel = bhavcopy_location(day)
    errors: list[str] = []
    for host in ARCHIVE_HOSTS[:2]:
        url = host + rel
        try:
            raw = session.request(url, referer="https://www.nseindia.com/all-reports", accept="application/zip,*/*")
            if len(raw) < 100 or not raw.startswith(b"PK"):
                raise ReplayError("response is not a ZIP bhavcopy")
            return raw, {"date": day, "source_url": url, "sha256": sha256(raw), "byte_size": len(raw)}
        except ReplayError as exc:
            errors.append(str(exc))
            time.sleep(session.sleep_seconds)
    if quiet_missing:
        return None
    raise ReplayError("bhavcopy unavailable for " + day + ": " + " | ".join(errors))


def resolve_on_or_before(target: str, session: OfficialSession, days: int = 5) -> tuple[str, bytes, dict[str, Any]]:
    d = date.fromisoformat(target)
    for offset in range(days + 1):
        candidate = (d - timedelta(days=offset)).isoformat()
        hit = fetch_bhavcopy(candidate, session, quiet_missing=True)
        if hit:
            raw, meta = hit
            return candidate, raw, meta
    raise ReplayError(f"no valid trading archive on/before {target} within {days} calendar days")


def resolve_after(target: str, session: OfficialSession, days: int = 5) -> tuple[str, bytes, dict[str, Any]]:
    d = date.fromisoformat(target)
    for offset in range(1, days + 1):
        candidate = (d + timedelta(days=offset)).isoformat()
        hit = fetch_bhavcopy(candidate, session, quiet_missing=True)
        if hit:
            raw, meta = hit
            return candidate, raw, meta
    raise ReplayError(f"no valid trading archive after {target} within {days} calendar days")


def parse_bhavcopy(raw: bytes) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        members = [x for x in zf.namelist() if x.lower().endswith(".csv")]
        if not members:
            raise ReplayError("bhavcopy ZIP contains no CSV")
        text = zf.read(members[0]).decode("utf-8-sig", errors="replace")
    rows: list[dict[str, Any]] = []
    for raw_row in csv.DictReader(io.StringIO(text)):
        r = {str(k).strip().upper(): v for k, v in raw_row.items() if k is not None}
        symbol = str(r.get("SYMBOL") or "").strip().upper()
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "series": str(r.get("SERIES") or "").strip().upper(),
            "open": num(r.get("OPEN")),
            "high": num(r.get("HIGH")),
            "low": num(r.get("LOW")),
            "close": num(r.get("CLOSE")),
            "volume": num(r.get("TOTTRDQTY")),
            "turnover": num(r.get("TOTTRDVAL")),
            "isin": str(r.get("ISIN") or "").strip().upper() or None,
        })
    return rows


def company_universe(rows: Iterable[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r.get("series") != "EQ":
            continue
        isin = str(r.get("isin") or "").upper()
        if isin.startswith("INF") or not (isin.startswith("INE") or isin.startswith("IN9")):
            continue
        if not r.get("close") or not r.get("volume") or not r.get("turnover"):
            continue
        out.append({
            "canonical_id": isin,
            "isin": isin,
            "symbol": r["symbol"],
            "series": "EQ",
            "market_type": "NSE_EQ",
            "sector": "UNKNOWN_HISTORICAL_SECTOR",
            "as_of_date": as_of,
            "is_eligible": True,
        })
    return sorted(out, key=lambda x: (x["symbol"], x["isin"]))


def fetch_actions(session: OfficialSession, start: str, end: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Split long horizons into calendar-year requests to keep each official response bounded.
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    cursor = d0
    all_rows: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    while cursor <= d1:
        chunk_end = min(d1, date(cursor.year, 12, 31))
        f = cursor.strftime("%d-%m-%Y")
        t = chunk_end.strftime("%d-%m-%Y")
        query = urllib.parse.urlencode({"index": "equities", "from_date": f, "to_date": t})
        url = "https://www.nseindia.com/api/corporates-corporateActions?" + query
        payload = session.json(url)
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        rows = rows if isinstance(rows, list) else []
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        all_rows.extend(rows)
        metadata.append({"from": cursor.isoformat(), "to": chunk_end.isoformat(), "source_url": url, "sha256": sha256(raw), "rows": len(rows)})
        cursor = chunk_end + timedelta(days=1)
        time.sleep(0.08)
    return all_rows, metadata


def normalize_actions(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        symbol = str(raw.get("symbol") or raw.get("sm_symbol") or raw.get("SYMBOL") or "").strip().upper()
        purpose = str(raw.get("subject") or raw.get("purpose") or raw.get("PURPOSE") or "").strip()
        if not symbol or not purpose:
            continue
        try:
            action = parse_corporate_action(
                purpose,
                ex_date=iso_date(raw.get("exDate") or raw.get("ex_date") or raw.get("EX-DATE")),
                record_date=iso_date(raw.get("recordDate") or raw.get("record_date") or raw.get("RECORD DATE")),
                face_value=num(raw.get("faceVal") or raw.get("faceValue") or raw.get("FACE VALUE")),
            ).to_dict()
            action["symbol"] = symbol
            by_symbol[symbol].append(action)
        except Exception:
            # Unknown/unparseable rows do not silently create a price factor.
            continue
    return by_symbol


def dedupe_actions(actions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set(); out = []
    for a in actions:
        key = (a.get("action_type"), a.get("purpose"), a.get("ex_date"), a.get("record_date"), a.get("price_factor"))
        if key not in seen:
            seen.add(key); out.append(a)
    return out


def pct_rank(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    if len(ordered) <= 1:
        return {k: 50.0 for k, _ in ordered}
    return {k: round(i / (len(ordered) - 1) * 100, 2) for i, (k, _) in enumerate(ordered)}


def mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def band_fraction(value: float | None, spec: dict[str, Any]) -> float | None:
    if value is None:
        return None
    if spec["direction"] == "higher":
        for threshold, fraction in sorted(spec["bands"], key=lambda x: float(x[0]), reverse=True):
            if value >= float(threshold):
                return float(fraction)
    else:
        for threshold, fraction in sorted(spec["bands"], key=lambda x: float(x[0])):
            if value <= float(threshold):
                return float(fraction)
    return 0.0


def adjust_history(rows: list[dict[str, Any]], actions: list[dict[str, Any]], anchor: str) -> tuple[list[dict[str, Any]], int]:
    out = []; unsafe = 0
    for r in rows:
        factor = cumulative_backward_price_factor(actions, price_date=r["date"], target_date=anchor)
        if not factor["calibration_safe"]:
            unsafe += 1
            continue
        volume_factor = 1.0
        for a in factor["applied_actions"]:
            if a.get("share_factor") is not None:
                volume_factor *= float(a["share_factor"])
        out.append({
            **r,
            "adjusted_close": float(r["close"]) * float(factor["price_factor"]),
            "adjusted_high": float(r["high"]) * float(factor["price_factor"]),
            "adjusted_volume": float(r["volume"] or 0) * volume_factor,
        })
    return out, unsafe


def technical_snapshot(sample: list[dict[str, Any]], start: str, anchor: str, session: OfficialSession, config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    wanted = {r["isin"]: r for r in sample}
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_hashes: list[tuple[str, str]] = []
    missing_weekdays = 0
    d, end = date.fromisoformat(start), date.fromisoformat(anchor)
    while d <= end:
        if d.weekday() < 5:
            ds = d.isoformat()
            hit = fetch_bhavcopy(ds, session, quiet_missing=True)
            if not hit:
                missing_weekdays += 1
            else:
                raw, meta = hit
                source_hashes.append((ds, meta["sha256"]))
                for r in parse_bhavcopy(raw):
                    isin = str(r.get("isin") or "").upper()
                    if r.get("series") == "EQ" and isin in wanted and r.get("close") is not None:
                        history[isin].append({
                            "date": ds,
                            "historical_symbol": r["symbol"],
                            "close": float(r["close"]),
                            "high": float(r["high"] or r["close"]),
                            "volume": float(r["volume"] or 0),
                        })
            time.sleep(0.035)
        d += timedelta(days=1)

    action_rows, action_meta = fetch_actions(session, start, anchor)
    actions_by_symbol = normalize_actions(action_rows)
    raw_features: list[dict[str, Any]] = []
    momentum: dict[str, float] = {}
    unresolved_symbols: list[str] = []

    for isin, member in wanted.items():
        rows = sorted(history.get(isin, []), key=lambda x: x["date"])
        aliases = {member["symbol"]} | {str(x.get("historical_symbol") or "").upper() for x in rows}
        actions = dedupe_actions(a for alias in aliases for a in actions_by_symbol.get(alias, []))
        safe, unsafe = adjust_history(rows, actions, anchor)
        if unsafe:
            unresolved_symbols.append(member["symbol"])
        closes = [float(x["adjusted_close"]) for x in safe]
        latest = closes[-1] if closes else None
        dma200 = mean(closes[-200:]) if len(closes) >= 200 else None
        high52 = max((float(x["adjusted_high"]) for x in safe), default=None)
        ret12 = None if len(closes) < 200 or closes[0] <= 0 else (latest / closes[0] - 1) * 100
        if ret12 is not None:
            momentum[isin] = ret12
        up = down = 0.0
        for i in range(max(1, len(safe) - 50), len(safe)):
            if safe[i]["adjusted_close"] > safe[i-1]["adjusted_close"]:
                up += float(safe[i]["adjusted_volume"] or 0)
            elif safe[i]["adjusted_close"] < safe[i-1]["adjusted_close"]:
                down += float(safe[i]["adjusted_volume"] or 0)
        volume_acc = None if up + down <= 0 else up / (up + down) * 100
        raw_features.append({
            "isin": isin,
            "symbol": member["symbol"],
            "observations": len(safe),
            "unsafe_pre_action_rows": unsafe,
            "price_above_200dma_pct": None if dma200 in (None, 0) or latest is None else round((latest / dma200 - 1) * 100, 4),
            "distance_from_52w_high_pct": None if high52 in (None, 0) or latest is None else round((high52 - latest) / high52 * 100, 4),
            "volume_accumulation_score": None if volume_acc is None else round(volume_acc, 2),
        })

    ranks = pct_rank(momentum)
    tech_specs = {x["field"]: x for x in config["pillar_features"]["technical_accumulation"]}
    included = (
        "relative_strength_percentile",
        "volume_accumulation_score",
        "price_above_200dma_pct",
        "distance_from_52w_high_pct",
    )
    total_weight = sum(float(tech_specs[k]["weight"]) for k in included)
    snapshot: list[dict[str, Any]] = []
    for r in raw_features:
        r["relative_strength_percentile"] = ranks.get(r["isin"])
        values = {k: r.get(k) for k in included}
        grade = r["observations"] >= 200 and all(values[k] is not None for k in included)
        earned = 0.0
        if grade:
            for k in included:
                frac = band_fraction(float(values[k]), tech_specs[k])
                if frac is None:
                    grade = False
                    break
                earned += float(tech_specs[k]["weight"]) * frac
        r["technical_replay_grade"] = grade
        r["technical_partial_score"] = round(earned / total_weight * 100, 4) if grade else None
        snapshot.append(r)

    safe_payload = [
        {k: r[k] for k in ("isin", "symbol", "observations", "price_above_200dma_pct", "distance_from_52w_high_pct", "volume_accumulation_score", "relative_strength_percentile", "technical_replay_grade", "technical_partial_score")}
        for r in sorted(snapshot, key=lambda x: (x["isin"], x["symbol"]))
    ]
    freeze_bytes = json.dumps(safe_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = {
        "lookback_start": start,
        "anchor_date": anchor,
        "sample_rows": len(sample),
        "trading_archives_fetched": len(source_hashes),
        "weekday_archives_missing": missing_weekdays,
        "source_hash_chain_sha256": sha256("\n".join(f"{d}|{h}" for d, h in source_hashes).encode("utf-8")),
        "corporate_action_sources": action_meta,
        "technical_replay_grade": sum(bool(r["technical_replay_grade"]) for r in snapshot),
        "unresolved_pre_anchor_action_symbols": sorted(set(unresolved_symbols)),
        "predictor_snapshot_sha256": sha256(freeze_bytes),
        "outcomes_seen_when_snapshot_frozen": False,
    }
    return snapshot, manifest


def rank(values: list[float]) -> list[float]:
    pairs = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(pairs):
        j = i + 1
        while j < len(pairs) and pairs[j][1] == pairs[i][1]:
            j += 1
        avg = (i + j - 1) / 2 + 1
        for k in range(i, j):
            ranks[pairs[k][0]] = avg
        i = j
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    numerator = sum((x-ma)*(y-mb) for x, y in zip(ra, rb))
    da = sum((x-ma)**2 for x in ra); db = sum((y-mb)**2 for y in rb)
    return None if da <= 0 or db <= 0 else round(numerator / math.sqrt(da*db), 4)


def outcome_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [float(x["forward_multiple_3y"]) for x in rows]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean_multiple": round(sum(vals)/len(vals), 4),
        "two_x_rate_pct": round(sum(x >= 2 for x in vals)/len(vals)*100, 2),
        "five_x_rate_pct": round(sum(x >= 5 for x in vals)/len(vals)*100, 2),
        "loss_rate_pct": round(sum(x < 1 for x in vals)/len(vals)*100, 2),
    }


def acquire_outcomes(snapshot: list[dict[str, Any]], anchor_target: str, outcome_target: str, session: OfficialSession) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entry_date, entry_raw, entry_meta = resolve_after(anchor_target, session)
    exit_date, exit_raw, exit_meta = resolve_on_or_before(outcome_target, session)
    entry_rows = parse_bhavcopy(entry_raw); exit_rows = parse_bhavcopy(exit_raw)
    entry_by_isin = {str(r.get("isin") or "").upper(): r for r in entry_rows if r.get("series") == "EQ" and r.get("isin")}
    exit_by_isin = {str(r.get("isin") or "").upper(): r for r in exit_rows if r.get("series") == "EQ" and r.get("isin")}
    action_rows, action_meta = fetch_actions(session, entry_date, exit_date)
    actions_by_symbol = normalize_actions(action_rows)

    joined: list[dict[str, Any]] = []
    statuses: dict[str, int] = defaultdict(int)
    for pred in snapshot:
        if not pred.get("technical_replay_grade"):
            continue
        isin = pred["isin"]
        erow, xrow = entry_by_isin.get(isin), exit_by_isin.get(isin)
        status = "OBSERVED_CALIBRATION_SAFE"
        multiple = None
        if not erow or not erow.get("close"):
            status = "MISSING_ENTRY_PRICE"
        elif not xrow or not xrow.get("close"):
            status = "MISSING_EXIT_PRICE_OR_DELISTED"
        else:
            aliases = {pred["symbol"], erow["symbol"], xrow["symbol"]}
            actions = dedupe_actions(a for alias in aliases for a in actions_by_symbol.get(alias, []))
            factor = cumulative_backward_price_factor(actions, price_date=entry_date, target_date=exit_date)
            if not factor["calibration_safe"]:
                status = "UNRESOLVED_CORPORATE_ACTION"
            else:
                adjusted_entry = float(erow["close"]) * float(factor["price_factor"])
                if adjusted_entry > 0:
                    multiple = round(float(xrow["close"]) / adjusted_entry, 6)
                else:
                    status = "INVALID_ADJUSTED_ENTRY"
        statuses[status] += 1
        joined.append({
            "isin": isin,
            "symbol": pred["symbol"],
            "technical_partial_score": pred["technical_partial_score"],
            "forward_multiple_3y": multiple,
            "outcome_status": status,
        })

    return joined, {
        "entry_date": entry_date,
        "outcome_date": exit_date,
        "entry_source": entry_meta,
        "outcome_source": exit_meta,
        "corporate_action_sources": action_meta,
        "outcome_status_counts": dict(sorted(statuses.items())),
    }


def evaluate(joined: list[dict[str, Any]]) -> dict[str, Any]:
    safe = [r for r in joined if r["outcome_status"] == "OBSERVED_CALIBRATION_SAFE" and r["forward_multiple_3y"] is not None]
    safe.sort(key=lambda r: (float(r["technical_partial_score"]), r["symbol"]), reverse=True)
    top_n = max(1, math.ceil(len(safe)*0.25)) if safe else 0
    top = safe[:top_n]
    cohort = outcome_summary(safe); topq = outcome_summary(top)
    return {
        "ranked_calibration_safe_n": len(safe),
        "cohort": cohort,
        "top_quartile": topq,
        "top_quartile_mean_lift_x": None if not cohort.get("mean_multiple") else round(topq.get("mean_multiple", 0)/cohort["mean_multiple"], 4),
        "spearman": spearman(
            [float(r["technical_partial_score"]) for r in safe],
            [float(r["forward_multiple_3y"]) for r in safe],
        ),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Run a bounded point-in-time RK-MIS technical historical replay")
    p.add_argument("--anchor", required=True)
    p.add_argument("--lookback-start", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--sample-size", type=int, default=250)
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol.get("anchor_date") != args.anchor or protocol.get("outcome_target_date") != args.outcome:
        raise ReplayError("CLI dates do not match the frozen protocol")
    if int(protocol.get("sample_size")) != args.sample_size:
        raise ReplayError("CLI sample size does not match the frozen protocol")

    config = json.loads((ROOT / "config/rk_mie_scoring.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    session = OfficialSession()

    resolved_anchor, anchor_raw, anchor_meta = resolve_on_or_before(args.anchor, session)
    universe = company_universe(parse_bhavcopy(anchor_raw), resolved_anchor)
    sample = deterministic_market_sample(
        universe,
        sample_size=args.sample_size,
        salt=f"RK-MIS-OBJECTIVE-REPLAY|{args.anchor}",
        strata_fields=("market_type",),
    )
    if len(sample) != args.sample_size:
        raise ReplayError(f"sample underflow: expected {args.sample_size}, got {len(sample)}")

    snapshot, predictor_manifest = technical_snapshot(sample, args.lookback_start, resolved_anchor, session, config)
    # Predictor snapshot is complete here. Only now are future entry/outcome archives requested.
    joined, outcome_manifest = acquire_outcomes(snapshot, args.anchor, args.outcome, session)
    metrics = evaluate(joined)

    report = {
        "version": "rk-mis-technical-replay-v1",
        "protocol": str(args.protocol),
        "protocol_sha256": sha256(args.protocol.read_bytes()),
        "anchor_target": args.anchor,
        "anchor_resolved_trading_date": resolved_anchor,
        "outcome_target": args.outcome,
        "sample_target": args.sample_size,
        "eligible_company_universe_rows": len(universe),
        "sample_rows": len(sample),
        "technical_replay_grade_rows": predictor_manifest["technical_replay_grade"],
        "technical_score_coverage_pct": round(predictor_manifest["technical_replay_grade"]/len(sample)*100, 2),
        "calibration_safe_outcome_coverage_of_scored_pct": round(metrics["ranked_calibration_safe_n"]/predictor_manifest["technical_replay_grade"]*100, 2) if predictor_manifest["technical_replay_grade"] else 0.0,
        "metrics": metrics,
        "official_100_point_score_mutated": False,
        "alpha_claim": False,
        "no_post_result_tuning": True,
    }
    manifest = {
        "anchor_source": anchor_meta,
        "predictor": predictor_manifest,
        "outcomes": outcome_manifest,
        "official_requests_made": session.requests_made,
        "raw_exchange_files_published": False,
        "public_artifact_contains_only_derived_aggregate_results_and_source_metadata": True,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
