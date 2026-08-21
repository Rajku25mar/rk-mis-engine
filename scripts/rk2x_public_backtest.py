from __future__ import annotations

import io
import json
import math
import time
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "validation" / "rk2x_public"
OUT.mkdir(parents=True, exist_ok=True)

HORIZONS = {"6M": 126, "12M": 252, "24M": 504}
ROUND_TRIP_COST_PCT = 0.20
COOLDOWN = 30
MIN_SCORE = 75
VALID_STATES = {"ARMED", "TRIGGER", "SECOND_CHANCE"}

CANDIDATES = {
    "TRANSRAILL": "TRANSRAILL.NS",
    "KERNEX": "KERNEX.NS",
    "MUTHOOTFIN": "MUTHOOTFIN.NS",
    "MANAPPURAM": "MANAPPURAM.NS",
    "FINEOTEX": "FCL.NS",
    "PNCINFRA": "PNCINFRA.NS",
    "LXCHEM": "LXCHEM.NS",
    "NETWEB": "NETWEB.NS",
    "KRISHNADEF": "KRISHNADEF.NS",
    "GESHIP": "GESHIP.NS",
}

NSE_MAIN = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_SME = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RK2X-public-research/1.0)",
    "Accept": "application/json,text/csv,*/*",
    "Referer": "https://www.bseindia.com/",
}
BSE_GROUPS = ["A", "B", "M", "MT", "X", "XT", "T", "Z", "ZP"]


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift(1)
    tr = pd.concat([(df["High"] - df["Low"]), (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def normalize_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    need = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in d.columns for c in need):
        return pd.DataFrame()
    d = d[need].copy()
    for c in need:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["Open", "High", "Low", "Close"])
    d["Volume"] = d["Volume"].fillna(0)
    try:
        d.index = pd.DatetimeIndex(d.index).tz_localize(None)
    except Exception:
        pass
    return d.sort_index()


def download_one(ticker: str, period: str = "10y") -> pd.DataFrame:
    raw = yf.download(ticker, period=period, interval="1d", auto_adjust=False, progress=False, threads=False)
    return normalize_ohlcv(raw)


def download_many(tickers: list[str], period: str = "5y", chunk: int = 40) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        try:
            raw = yf.download(batch, period=period, interval="1d", auto_adjust=False, progress=False, threads=True, group_by="ticker")
        except Exception:
            raw = pd.DataFrame()
        if len(batch) == 1:
            d = normalize_ohlcv(raw)
            if not d.empty:
                out[batch[0]] = d
        elif isinstance(raw.columns, pd.MultiIndex):
            l0 = set(raw.columns.get_level_values(0))
            for t in batch:
                if t in l0:
                    d = normalize_ohlcv(raw[t])
                    if not d.empty:
                        out[t] = d
        if start and start % 400 == 0:
            time.sleep(1.0)
    return out


def score_frame(d: pd.DataFrame, bench: pd.DataFrame) -> pd.DataFrame:
    """Point-in-time RKS-like technical features; no future data used."""
    if len(d) < 210 or len(bench) < 210:
        return pd.DataFrame(index=d.index)
    x = pd.DataFrame(index=d.index)
    close, high, low, open_, volume = d["Close"], d["High"], d["Low"], d["Open"], d["Volume"]
    e20, e50, s150, s200 = ema(close, 20), ema(close, 50), sma(close, 150), sma(close, 200)
    x["value20"] = (close * volume).shift(1).rolling(20).mean()
    x["eligible"] = (close >= 50) & (x["value20"] >= 2_000_000)
    x["trend_ok"] = (close > e20) & (e20 > e50) & (e50 > s150) & (s150 > s200) & (s200 > s200.shift(20))

    bclose = bench["Close"].reindex(d.index, method="ffill")
    rs = close / bclose
    rs50 = sma(rs, 50)
    x["rs_ok"] = (rs > rs50) & (rs > rs.shift(21))

    hi52 = high.rolling(252, min_periods=210).max()
    lo52 = low.rolling(252, min_periods=210).min()
    x["dist52"] = (hi52 - close) / hi52 * 100
    x["above52low"] = (close - lo52) / lo52 * 100
    x["pos52_ok"] = (x["dist52"] <= 15) & (x["above52low"] >= 30)

    pivot = high.shift(1).rolling(20).max()
    base_low = low.shift(1).rolling(20).min()
    x["pivot"] = pivot
    x["base_range_pct"] = (pivot - base_low) / base_low * 100
    x["base_ok"] = x["base_range_pct"] <= 15

    a = atr(d, 14)
    atrpct = a / close * 100
    atrref = atrpct.shift(5).rolling(20).mean()
    x["atr_contract"] = atrpct.shift(1) < atrref

    v5 = volume.shift(1).rolling(5).mean()
    v20 = volume.shift(1).rolling(20).mean()
    v50 = volume.shift(1).rolling(50).mean()
    x["dryup_ratio"] = v5 / v20
    x["volume_dryup"] = x["dryup_ratio"] <= 0.80
    x["relvol"] = volume / v50

    st_high = high.shift(1).rolling(10).max()
    st_low = low.shift(1).rolling(10).min()
    x["bos"] = close > st_high
    x["sweep"] = (low < st_low) & (close > st_low) & (close > open_)
    x["structure_ok"] = x["bos"] | x["sweep"]

    bar = high - low
    close_location = (close - low) / bar.replace(0, np.nan)
    x["strong_bar"] = (close > open_) & (close_location >= 0.70)
    x["breakout"] = close > pivot
    x["extension_pct"] = (close - pivot) / pivot * 100
    x["distance_to_pivot_pct"] = (pivot - close) / pivot * 100

    bc = bench["Close"]
    be50, bs200 = ema(bc, 50), sma(bc, 200)
    bull = ((bc > be50) & (be50 > bs200)).reindex(d.index, method="ffill").fillna(False)
    x["market_bull"] = bull

    score = (
        x["trend_ok"].astype(int) * 25
        + x["rs_ok"].astype(int) * 15
        + x["pos52_ok"].astype(int) * 10
        + x["base_ok"].astype(int) * 10
        + x["atr_contract"].astype(int) * 10
        + x["volume_dryup"].astype(int) * 5
        + x["market_bull"].astype(int) * 10
        + x["structure_ok"].astype(int) * 10
    )
    x["score"] = score
    trigger = (
        (score >= 75) & x["trend_ok"] & x["rs_ok"] & x["market_bull"] & x["breakout"]
        & (x["relvol"] >= 1.50) & x["strong_bar"] & (x["extension_pct"] <= 5)
    )
    armed = (
        ~trigger & (score >= 65) & x["trend_ok"] & x["rs_ok"]
        & (x["distance_to_pivot_pct"] >= -5) & (x["distance_to_pivot_pct"] <= 3)
    )
    prior_roll = high.shift(1).rolling(20).max()
    prior_breakout = close > prior_roll
    recent_breakout = prior_breakout.shift(1).rolling(9).max().fillna(0).astype(bool)
    second = (
        ~trigger & recent_breakout & (close > e20) & (e20 > e50)
        & (((close - pivot).abs() / pivot * 100) <= 4) & (volume < v20) & (close > open_)
    )
    x["state"] = np.select([trigger, second, armed, score >= 50], ["TRIGGER", "SECOND_CHANCE", "ARMED", "RADAR"], default="AVOID")
    return x


def forward_stats(d: pd.DataFrame, entry_i: int, sessions: int) -> dict | None:
    end_i = entry_i + sessions - 1
    if entry_i >= len(d) or end_i >= len(d):
        return None
    entry = float(d["Open"].iloc[entry_i])
    if not np.isfinite(entry) or entry <= 0:
        return None
    w = d.iloc[entry_i:end_i + 1]
    highs = w["High"].to_numpy(float)
    lows = w["Low"].to_numpy(float)
    hit_idx = np.flatnonzero(highs >= 2 * entry)
    hit = len(hit_idx) > 0
    days = int(hit_idx[0] + 1) if hit else None
    pre = lows[: hit_idx[0] + 1] if hit else lows
    dd_before_2x = (np.nanmin(pre) / entry - 1) * 100 if len(pre) else np.nan
    return {
        "endpoint_return_pct": (float(w["Close"].iloc[-1]) / entry - 1) * 100 - ROUND_TRIP_COST_PCT,
        "mfe_pct": (np.nanmax(highs) / entry - 1) * 100,
        "mae_pct": (np.nanmin(lows) / entry - 1) * 100,
        "hit_2x": bool(hit),
        "days_to_2x": days,
        "dd_before_2x_pct": dd_before_2x,
        "hit_2x_within_10pct_dd": bool(hit and dd_before_2x >= -10),
    }


def event_study(symbol: str, ticker: str, d: pd.DataFrame, bench: pd.DataFrame) -> list[dict]:
    f = score_frame(d, bench)
    events: list[dict] = []
    last = -9999
    for i in range(252, len(d) - 1):
        if i - last < COOLDOWN or i >= len(f):
            continue
        state = str(f["state"].iloc[i])
        score = int(f["score"].iloc[i]) if pd.notna(f["score"].iloc[i]) else 0
        if not bool(f["eligible"].iloc[i]) or state not in VALID_STATES or score < MIN_SCORE:
            continue
        row = {
            "symbol": symbol,
            "ticker": ticker,
            "signal_date": str(d.index[i].date()),
            "entry_date": str(d.index[i + 1].date()),
            "entry": float(d["Open"].iloc[i + 1]),
            "state": state,
            "score": score,
            "market_bull": bool(f["market_bull"].iloc[i]),
            "dist52": float(f["dist52"].iloc[i]),
            "base_range_pct": float(f["base_range_pct"].iloc[i]),
            "dryup_ratio": float(f["dryup_ratio"].iloc[i]),
            "relvol": float(f["relvol"].iloc[i]),
        }
        complete = False
        for label, n in HORIZONS.items():
            z = forward_stats(d, i + 1, n)
            row[f"{label}_complete"] = z is not None
            if z:
                complete = True
                for k, v in z.items():
                    row[f"{label}_{k}"] = v
        if complete:
            events.append(row)
            last = i
    return events


def summarize(events: pd.DataFrame, label: str) -> dict:
    if events.empty or f"{label}_complete" not in events:
        return {"samples": 0}
    x = events[events[f"{label}_complete"].fillna(False)].copy()
    if x.empty:
        return {"samples": 0}
    ret = pd.to_numeric(x[f"{label}_endpoint_return_pct"], errors="coerce")
    mfe = pd.to_numeric(x[f"{label}_mfe_pct"], errors="coerce")
    mae = pd.to_numeric(x[f"{label}_mae_pct"], errors="coerce")
    dd = pd.to_numeric(x[f"{label}_dd_before_2x_pct"], errors="coerce")
    hit = x[f"{label}_hit_2x"].fillna(False).astype(bool)
    clean = x[f"{label}_hit_2x_within_10pct_dd"].fillna(False).astype(bool)
    days = pd.to_numeric(x.loc[hit, f"{label}_days_to_2x"], errors="coerce")
    return {
        "samples": int(len(x)),
        "positive_endpoint_pct": round(float((ret > 0).mean() * 100), 2),
        "hit_2x_pct": round(float(hit.mean() * 100), 2),
        "clean_2x_le10dd_pct": round(float(clean.mean() * 100), 2),
        "median_endpoint_return_pct": round(float(ret.median()), 2),
        "avg_endpoint_return_pct": round(float(ret.mean()), 2),
        "median_mfe_pct": round(float(mfe.median()), 2),
        "median_mae_pct": round(float(mae.median()), 2),
        "median_dd_before_2x_pct": round(float(dd.median()), 2),
        "median_days_to_2x": round(float(days.median()), 1) if days.notna().any() else None,
        "best_endpoint_return_pct": round(float(ret.max()), 2),
        "worst_endpoint_return_pct": round(float(ret.min()), 2),
    }


def fetch_nse_universe() -> pd.DataFrame:
    frames = []
    for board, url in [("NSE_MAIN", NSE_MAIN), ("NSE_SME", NSE_SME)]:
        try:
            r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [str(c).strip().upper() for c in df.columns]
            sym = next((c for c in ["SYMBOL", "SYMBOL_NAME", "TCKRSYMB"] if c in df), None)
            isin = next((c for c in ["ISIN NUMBER", "ISIN", "ISIN_CODE"] if c in df), None)
            series = next((c for c in ["SERIES", "SCTYSRS"] if c in df), None)
            if sym is None:
                continue
            z = pd.DataFrame({"ticker": df[sym].astype(str).str.strip() + ".NS", "board": board})
            z["isin"] = df[isin].astype(str).str.strip() if isin else ""
            if series:
                ser = df[series].astype(str).str.strip()
                z = z[ser.isin(["EQ", "BE", "BZ", "SM", "ST"]) | ser.eq("")]
            frames.append(z)
        except Exception as exc:
            print(f"NSE universe source failed {url}: {exc}")
    if not frames:
        return pd.DataFrame(columns=["ticker", "board", "isin"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("ticker")


def _pick_key(d: dict, keys: Iterable[str]):
    lower = {str(k).lower(): k for k in d}
    for key in keys:
        if key.lower() in lower:
            return d[lower[key.lower()]]
    return None


def fetch_bse_universe(nse_isins: set[str]) -> pd.DataFrame:
    rows = []
    s = requests.Session()
    for group in BSE_GROUPS:
        try:
            r = s.get(BSE_API, params={"Group": group, "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"}, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                vals = next((v for v in data.values() if isinstance(v, list)), [])
            else:
                vals = data if isinstance(data, list) else []
            for item in vals:
                if not isinstance(item, dict):
                    continue
                code = _pick_key(item, ["SCRIP_CD", "ScripCode", "scrip_cd", "SCRIPCODE"])
                isin = _pick_key(item, ["ISIN_NUMBER", "ISIN", "isin_no", "ISIN_CODE"])
                if code is None:
                    continue
                code = str(code).strip()
                isin = "" if isin is None else str(isin).strip()
                if not code.isdigit():
                    continue
                if isin and isin in nse_isins:
                    continue
                rows.append({"ticker": f"{code}.BO", "board": "BSE_ONLY", "isin": isin})
        except Exception as exc:
            print(f"BSE group {group} failed: {exc}")
    return pd.DataFrame(rows).drop_duplicates("ticker") if rows else pd.DataFrame(columns=["ticker", "board", "isin"])


def reverse_samples(ticker: str, d: pd.DataFrame, bench: pd.DataFrame) -> list[dict]:
    """Monthly anchor reverse study: what features preceded future 2x outcomes?"""
    f = score_frame(d, bench)
    rows = []
    for i in range(252, len(d) - 1, 21):
        if i >= len(f) or not bool(f["eligible"].iloc[i]):
            continue
        row = {
            "ticker": ticker,
            "anchor_date": str(d.index[i].date()),
            "score": float(f["score"].iloc[i]),
            "state": str(f["state"].iloc[i]),
            "trend_ok": bool(f["trend_ok"].iloc[i]),
            "rs_ok": bool(f["rs_ok"].iloc[i]),
            "pos52_ok": bool(f["pos52_ok"].iloc[i]),
            "base_ok": bool(f["base_ok"].iloc[i]),
            "atr_contract": bool(f["atr_contract"].iloc[i]),
            "volume_dryup": bool(f["volume_dryup"].iloc[i]),
            "market_bull": bool(f["market_bull"].iloc[i]),
            "structure_ok": bool(f["structure_ok"].iloc[i]),
            "dist52": float(f["dist52"].iloc[i]),
            "base_range_pct": float(f["base_range_pct"].iloc[i]),
            "dryup_ratio": float(f["dryup_ratio"].iloc[i]),
            "relvol": float(f["relvol"].iloc[i]),
        }
        complete = False
        for label, n in HORIZONS.items():
            z = forward_stats(d, i + 1, n)
            if z:
                complete = True
                row[f"{label}_hit_2x"] = z["hit_2x"]
                row[f"{label}_clean_2x"] = z["hit_2x_within_10pct_dd"]
                row[f"{label}_return_pct"] = z["endpoint_return_pct"]
            else:
                row[f"{label}_hit_2x"] = None
                row[f"{label}_clean_2x"] = None
                row[f"{label}_return_pct"] = None
        if complete:
            rows.append(row)
    return rows


def fingerprint(samples: pd.DataFrame, label: str) -> dict:
    col = f"{label}_hit_2x"
    if samples.empty or col not in samples:
        return {"samples": 0}
    x = samples[samples[col].notna()].copy()
    if x.empty:
        return {"samples": 0}
    y = x[col].astype(bool)
    out = {"samples": int(len(x)), "winner_rate_pct": round(float(y.mean() * 100), 2), "features": {}}
    bools = ["trend_ok", "rs_ok", "pos52_ok", "base_ok", "atr_contract", "volume_dryup", "market_bull", "structure_ok"]
    for b in bools:
        yes = x[x[b].astype(bool)]
        no = x[~x[b].astype(bool)]
        out["features"][b] = {
            "true_n": int(len(yes)),
            "true_hit_pct": round(float(yes[col].astype(bool).mean() * 100), 2) if len(yes) else None,
            "false_n": int(len(no)),
            "false_hit_pct": round(float(no[col].astype(bool).mean() * 100), 2) if len(no) else None,
        }
    for state in ["TRIGGER", "SECOND_CHANCE", "ARMED", "RADAR", "AVOID"]:
        z = x[x["state"].eq(state)]
        out["features"][f"state_{state}"] = {"n": int(len(z)), "hit_pct": round(float(z[col].astype(bool).mean() * 100), 2) if len(z) else None}
    for num in ["score", "dist52", "base_range_pct", "dryup_ratio", "relvol"]:
        out["features"][num] = {
            "winner_median": round(float(pd.to_numeric(x.loc[y, num], errors="coerce").median()), 3) if y.any() else None,
            "nonwinner_median": round(float(pd.to_numeric(x.loc[~y, num], errors="coerce").median()), 3) if (~y).any() else None,
        }
    return out


def main():
    print("Downloading benchmark...")
    bench = download_one("^NSEI", "10y")
    if len(bench) < 600:
        raise RuntimeError(f"benchmark history insufficient: {len(bench)}")

    # A: current ten candidates.
    a_events = []
    a_symbols = {}
    for symbol, ticker in CANDIDATES.items():
        print(f"A {symbol}")
        d = download_one(ticker, "10y")
        ev = event_study(symbol, ticker, d, bench) if len(d) >= 210 else []
        a_events.extend(ev)
        edf = pd.DataFrame(ev)
        a_symbols[symbol] = {
            "sessions": int(len(d)),
            "history_start": str(d.index.min().date()) if len(d) else None,
            "history_end": str(d.index.max().date()) if len(d) else None,
            "event_count": int(len(ev)),
            **{label: summarize(edf, label) for label in HORIZONS},
        }
    a_df = pd.DataFrame(a_events)
    a_summary = {"symbols": a_symbols, "aggregate": {label: summarize(a_df, label) for label in HORIZONS}}
    a_df.to_csv(OUT / "A_candidate_events.csv", index=False)
    (OUT / "A_candidate_summary.json").write_text(json.dumps(a_summary, indent=2), encoding="utf-8")

    # B/C: current full investable listed universe (NSE + BSE-exclusive), using 5y public OHLCV.
    print("Loading NSE/BSE universe...")
    nse = fetch_nse_universe()
    nse_isins = set(nse["isin"].dropna().astype(str)) - {"", "nan"}
    bse = fetch_bse_universe(nse_isins)
    uni = pd.concat([nse, bse], ignore_index=True).drop_duplicates("ticker")
    uni.to_csv(OUT / "universe_used.csv", index=False)
    tickers = uni["ticker"].tolist()
    print(f"Universe tickers={len(tickers)} NSE={len(nse)} BSE-only={len(bse)}")

    price_map = download_many(tickers, "5y", chunk=35)
    print(f"Downloaded usable histories={len(price_map)}")

    b_events = []
    c_rows = []
    for j, ticker in enumerate(tickers, 1):
        d = price_map.get(ticker)
        if d is None or len(d) < 300:
            continue
        symbol = ticker.replace(".NS", "").replace(".BO", "")
        b_events.extend(event_study(symbol, ticker, d, bench))
        c_rows.extend(reverse_samples(ticker, d, bench))
        if j % 250 == 0:
            print(f"processed {j}/{len(tickers)}")

    b_df = pd.DataFrame(b_events)
    c_df = pd.DataFrame(c_rows)
    b_df.to_csv(OUT / "B_universe_events.csv", index=False)
    c_df.to_csv(OUT / "C_reverse_samples.csv", index=False)
    b_summary = {
        "universe_requested": int(len(tickers)),
        "usable_histories": int(len(price_map)),
        "events": int(len(b_df)),
        "horizons": {label: summarize(b_df, label) for label in HORIZONS},
        "methodology_note": "Current active NSE + BSE-exclusive universe; survivorship bias remains because delisted historical constituents are not reconstructed.",
    }
    c_summary = {label: fingerprint(c_df, label) for label in HORIZONS}
    (OUT / "B_universe_summary.json").write_text(json.dumps(b_summary, indent=2), encoding="utf-8")
    (OUT / "C_reverse_fingerprint.json").write_text(json.dumps(c_summary, indent=2), encoding="utf-8")

    master = {
        "generated_at_utc": pd.Timestamp.utcnow().isoformat(),
        "candidate_A": a_summary,
        "universe_B": b_summary,
        "reverse_C": c_summary,
        "limitations": [
            "Price/volume backtest only; historical point-in-time fundamentals/news/ownership are not injected.",
            "Universe B/C uses current active listed securities, so survivorship bias is not eliminated.",
            "Yahoo Finance is the historical OHLCV source; exchange masters are used for current universe membership.",
            "2x means intraday high reached >=2x next-session-open entry within the horizon; endpoint return is reported separately.",
        ],
    }
    (OUT / "RK2X_PUBLIC_MASTER.json").write_text(json.dumps(master, indent=2), encoding="utf-8")
    print(json.dumps({"A": a_summary["aggregate"], "B": b_summary, "C": c_summary}, indent=2))


if __name__ == "__main__":
    main()
