"""
H2 Quant v1 — Phase 1: Data Loader
Priority: MT5 → yfinance → Stooq (daily only)
Output: data/processed/H2_raw_{instrument}_{tf}.parquet
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

LOG_DIR = ROOT / CFG["paths"]["logs"]
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, CFG["system"]["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "loader.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("H2.loader")

PROCESSED_DIR = ROOT / CFG["paths"]["data_processed"]
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

RAW_DIR = ROOT / CFG["paths"]["data_raw"]
RAW_DIR.mkdir(parents=True, exist_ok=True)

SAST_OFFSET = timedelta(hours=2)

# ---------------------------------------------------------------------------
# MT5 source
# ---------------------------------------------------------------------------

def _mt5_available() -> bool:
    try:
        import MetaTrader5 as mt5
        return mt5.initialize()
    except Exception:
        return False


def _mt5_tf_constant(tf_str: str) -> int:
    import MetaTrader5 as mt5
    mt5_map = {
        "1m":  mt5.TIMEFRAME_M1,
        "5m":  mt5.TIMEFRAME_M5,
        "15m": mt5.TIMEFRAME_M15,
        "1H":  mt5.TIMEFRAME_H1,
        "4H":  mt5.TIMEFRAME_H4,
        "D":   mt5.TIMEFRAME_D1,
    }
    tf = mt5_map.get(tf_str)
    if tf is None:
        raise ValueError(f"Unknown timeframe for MT5: {tf_str}")
    return tf


def fetch_mt5(instrument: str, timeframe: str, n_bars: Optional[int] = None) -> Optional[pd.DataFrame]:
    """Pull bars from MT5. Returns UTC-indexed OHLCV DataFrame or None."""
    import MetaTrader5 as mt5

    symbol_map = CFG["mt5"]["symbol_map"]
    symbol = symbol_map.get(instrument, instrument)
    n = n_bars or CFG["mt5"]["default_bars"]
    tf = _mt5_tf_constant(timeframe)

    if not mt5.initialize():
        log.warning("MT5 initialize() failed")
        return None

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    if rates is None or len(rates) == 0:
        log.warning(f"MT5 returned no data for {symbol} {timeframe}")
        mt5.shutdown()
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={
        "time":        "datetime_utc",
        "open":        "open",
        "high":        "high",
        "low":         "low",
        "close":       "close",
        "tick_volume": "volume",
    })
    df = df[["datetime_utc", "open", "high", "low", "close", "volume"]].copy()
    df = df.sort_values("datetime_utc").reset_index(drop=True)
    df["source"] = "mt5"

    mt5.shutdown()
    log.info(f"MT5: {instrument} {timeframe} — {len(df)} bars")
    return df


def fetch_mt5_max_history(instrument: str, timeframe: str,
                           start_year: int = 2018) -> Optional[pd.DataFrame]:
    """
    Pull maximum available MT5 history using copy_rates_range.
    Goes back to start_year (default 2018) so we get 5-8+ years of data.
    Uses date-range query instead of bar-count query to bypass the
    default_bars=5000 ceiling.
    """
    import MetaTrader5 as mt5
    from datetime import timezone as tz

    symbol_map = CFG["mt5"]["symbol_map"]
    symbol = symbol_map.get(instrument, instrument)
    tf     = _mt5_tf_constant(timeframe)

    if not mt5.initialize():
        log.warning("MT5 initialize() failed")
        return None

    date_from = datetime(start_year, 1, 1, tzinfo=tz.utc)
    date_to   = datetime.now(tz=tz.utc)

    rates = mt5.copy_rates_range(symbol, tf, date_from, date_to)
    mt5.shutdown()

    if rates is None or len(rates) == 0:
        log.warning(f"MT5 max-history returned no data for {symbol} {timeframe}")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={
        "time":        "datetime_utc",
        "open":        "open",
        "high":        "high",
        "low":         "low",
        "close":       "close",
        "tick_volume": "volume",
    })
    df = df[["datetime_utc", "open", "high", "low", "close", "volume"]].copy()
    df = df.sort_values("datetime_utc").reset_index(drop=True)
    df["source"] = "mt5_maxhist"

    log.info(f"MT5 max-history: {instrument} {timeframe} - {len(df)} bars "
             f"({str(df['datetime_utc'].iloc[0])[:10]} to "
             f"{str(df['datetime_utc'].iloc[-1])[:10]})")
    return df


# Instruments to include in the max-history pull
_MAX_HIST_INSTRUMENTS = [
    "JP225", "DE40", "UK100", "US30", "USTEC", "HK50",
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD",
    "EURJPY", "GBPJPY",
    "XAUUSD", "XAGUSD",
]
_MAX_HIST_TIMEFRAMES = ["1H", "4H", "D"]


def run_max_history_batch(start_year: int = 2018,
                           timeframes: Optional[list] = None,
                           instruments: Optional[list] = None) -> dict:
    """
    Pull maximum MT5 history for all backtest instruments.
    Falls back to yfinance (730d cap) if MT5 returns nothing.
    Saves each result to data/processed/ parquet.
    """
    insts = instruments or _MAX_HIST_INSTRUMENTS
    tfs   = timeframes   or _MAX_HIST_TIMEFRAMES
    total = len(insts) * len(tfs)
    done  = 0
    results = {}

    print(f"\n  Pulling max history (from {start_year}) for "
          f"{len(insts)} instruments × {len(tfs)} timeframes = {total} files\n")

    for inst in insts:
        for tf in tfs:
            key = f"{inst}_{tf}"
            try:
                df = fetch_mt5_max_history(inst, tf, start_year=start_year)

                if df is None or df.empty:
                    log.info(f"MT5 max-history failed for {key} — falling back to yfinance")
                    df = fetch_yfinance(inst, tf)  # 730d cap on 1H

                if df is not None and not df.empty:
                    df = _add_sast(df)
                    if _validate(df, inst, tf):
                        save_parquet(df, inst, tf)
                        n    = len(df)
                        frm  = str(df["datetime_utc"].iloc[0])[:10]
                        to   = str(df["datetime_utc"].iloc[-1])[:10]
                        results[key] = f"ok  {n:>6} bars  {frm} to {to}"
                        done += 1
                        print(f"  [{done:>3}/{total}] {key:<18}  {results[key]}")
                        continue

                results[key] = "FAIL — no data"
                print(f"  [{done:>3}/{total}] {key:<18}  {results[key]}")

            except Exception as e:
                results[key] = f"ERROR: {e}"
                log.error(f"max-history error {key}: {e}")
                print(f"  [ERR] {key:<18}  {e}")

    ok = sum(1 for v in results.values() if v.startswith("ok"))
    print(f"\n  Done: {ok}/{total} files updated.\n")
    return results


# ---------------------------------------------------------------------------
# yfinance source
# ---------------------------------------------------------------------------

def fetch_yfinance(instrument: str, timeframe: str, period: Optional[str] = None) -> Optional[pd.DataFrame]:
    """Pull bars from yfinance. Returns UTC-indexed OHLCV DataFrame or None."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed")
        return None

    ticker_map = CFG["yfinance"]["ticker_map"]
    tf_map = CFG["yfinance"]["timeframe_map"]

    ticker = ticker_map.get(instrument)
    if not ticker:
        log.warning(f"yfinance: no ticker map for {instrument}")
        return None

    yf_tf = tf_map.get(timeframe)
    if not yf_tf:
        log.warning(f"yfinance: no timeframe map for {timeframe}")
        return None

    p = period or CFG["yfinance"]["default_period"]
    # yfinance limits intraday history: 60d for ≤1H, 730d for daily
    if timeframe in ("1m", "5m", "15m"):
        p = "60d"
    elif timeframe in ("1H", "4H"):
        p = "730d"

    try:
        raw = yf.download(ticker, period=p, interval=yf_tf, auto_adjust=True, progress=False)
    except Exception as e:
        log.warning(f"yfinance download failed for {instrument}: {e}")
        return None

    if raw is None or raw.empty:
        log.warning(f"yfinance: empty data for {instrument} {timeframe}")
        return None

    # yfinance returns MultiIndex columns when downloading single ticker sometimes
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    raw = raw.rename(columns=str.lower)
    raw.index = pd.to_datetime(raw.index, utc=True)
    raw.index.name = "datetime_utc"
    raw = raw.reset_index()

    df = raw[["datetime_utc", "open", "high", "low", "close", "volume"]].copy()
    df = df.sort_values("datetime_utc").reset_index(drop=True)
    df["source"] = "yfinance"

    log.info(f"yfinance: {instrument} {timeframe} — {len(df)} bars")
    return df


# ---------------------------------------------------------------------------
# Stooq source (daily only, direct HTTP)
# ---------------------------------------------------------------------------

def fetch_stooq(instrument: str) -> Optional[pd.DataFrame]:
    """Pull daily bars from Stooq via direct CSV URL. Daily only."""
    try:
        import requests
    except ImportError:
        log.warning("requests not installed")
        return None

    ticker_map = CFG["stooq"]["ticker_map"]
    base_url = CFG["stooq"]["base_url"]

    ticker = ticker_map.get(instrument)
    if not ticker:
        log.warning(f"Stooq: no ticker map for {instrument}")
        return None

    url = f"{base_url}?s={ticker}&i=d"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Stooq request failed for {instrument}: {e}")
        return None

    from io import StringIO
    try:
        raw = pd.read_csv(StringIO(resp.text))
    except Exception as e:
        log.warning(f"Stooq CSV parse error for {instrument}: {e}")
        return None

    if raw.empty or "Date" not in raw.columns:
        log.warning(f"Stooq: no usable data for {instrument}")
        return None

    raw.columns = [c.strip().lower() for c in raw.columns]
    raw["datetime_utc"] = pd.to_datetime(raw["date"], utc=True)
    raw = raw.rename(columns={"vol": "volume"})
    raw["volume"] = pd.to_numeric(raw.get("volume", 0), errors="coerce").fillna(0)

    df = raw[["datetime_utc", "open", "high", "low", "close", "volume"]].copy()
    df = df.sort_values("datetime_utc").reset_index(drop=True)
    df["source"] = "stooq"

    log.info(f"Stooq: {instrument} D — {len(df)} bars")
    return df


# ---------------------------------------------------------------------------
# CSV import (MT5 manual export)
# ---------------------------------------------------------------------------

def load_csv(path: str, instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
    """
    Load an MT5-exported CSV.
    Expected columns: Date, Time, Open, High, Low, Close, Volume
    """
    try:
        raw = pd.read_csv(path)
    except Exception as e:
        log.error(f"CSV load failed: {e}")
        return None

    raw.columns = [c.strip() for c in raw.columns]

    if "Date" in raw.columns and "Time" in raw.columns:
        raw["datetime_utc"] = pd.to_datetime(
            raw["Date"].astype(str) + " " + raw["Time"].astype(str),
            utc=True,
        )
    elif "Datetime" in raw.columns:
        raw["datetime_utc"] = pd.to_datetime(raw["Datetime"], utc=True)
    elif "<DATE>" in raw.columns and "<TIME>" in raw.columns:
        raw["datetime_utc"] = pd.to_datetime(
            raw["<DATE>"].astype(str) + " " + raw["<TIME>"].astype(str),
            utc=True,
        )
    else:
        log.error(f"Cannot parse datetime from CSV columns: {list(raw.columns)}")
        return None

    col_map = {
        "Open":   "open",  "High":   "high",  "Low":    "low",
        "Close":  "close", "Volume": "volume",
        "<OPEN>": "open",  "<HIGH>": "high",  "<LOW>":  "low",
        "<CLOSE>":"close", "<VOL>":  "volume", "<TICKVOL>": "volume",
    }
    raw = raw.rename(columns=col_map)
    for col in ["open", "high", "low", "close"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw["volume"] = pd.to_numeric(raw.get("volume", 0), errors="coerce").fillna(0)

    df = raw[["datetime_utc", "open", "high", "low", "close", "volume"]].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("datetime_utc").reset_index(drop=True)
    df["source"] = "csv"

    log.info(f"CSV: {instrument} {timeframe} — {len(df)} bars from {path}")
    return df


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def _add_sast(df: pd.DataFrame) -> pd.DataFrame:
    """Add SAST column (UTC+2) and normalise types."""
    df["datetime_sast"] = df["datetime_utc"] + SAST_OFFSET
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def _validate(df: pd.DataFrame, instrument: str, timeframe: str) -> bool:
    required = {"datetime_utc", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        log.error(f"Missing columns for {instrument} {timeframe}: {missing}")
        return False
    if df.empty:
        log.error(f"Empty DataFrame for {instrument} {timeframe}")
        return False
    if (df["high"] < df["low"]).any():
        n = (df["high"] < df["low"]).sum()
        log.warning(f"{instrument} {timeframe}: {n} bars where high < low — dropping")
        df.drop(df[df["high"] < df["low"]].index, inplace=True)
    return True


def fetch(
    instrument: str,
    timeframe: str,
    csv_path: Optional[str] = None,
    n_bars: Optional[int] = None,
    force_source: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV data for instrument/timeframe via priority chain.
    Returns clean DataFrame with datetime_utc + datetime_sast index columns.
    """
    df = None

    if force_source == "csv" or csv_path:
        df = load_csv(csv_path, instrument, timeframe)

    elif force_source == "yfinance":
        df = fetch_yfinance(instrument, timeframe)

    elif force_source == "stooq":
        df = fetch_stooq(instrument)

    else:
        # Auto priority: MT5 → yfinance → stooq
        try:
            df = fetch_mt5(instrument, timeframe, n_bars)
        except Exception as e:
            log.warning(f"MT5 fetch error for {instrument} {timeframe}: {e}")
            df = None

        if df is None or df.empty:
            log.info(f"Falling back to yfinance for {instrument} {timeframe}")
            try:
                df = fetch_yfinance(instrument, timeframe)
            except Exception as e:
                log.warning(f"yfinance error for {instrument} {timeframe}: {e}")
                df = None

        if (df is None or df.empty) and timeframe == "D":
            log.info(f"Falling back to Stooq for {instrument} D")
            try:
                df = fetch_stooq(instrument)
            except Exception as e:
                log.warning(f"Stooq error for {instrument}: {e}")
                df = None

    if df is None or df.empty:
        log.error(f"All sources failed for {instrument} {timeframe}")
        return None

    df = _add_sast(df)

    if not _validate(df, instrument, timeframe):
        return None

    return df


def save_parquet(df: pd.DataFrame, instrument: str, timeframe: str) -> Path:
    """Save processed DataFrame to Parquet."""
    fname = f"H2_raw_{instrument}_{timeframe}.parquet"
    out = PROCESSED_DIR / fname
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, out, compression="snappy")
    log.info(f"Saved: {out} ({len(df)} rows)")
    return out


def load_parquet(instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
    """Load a previously saved Parquet file."""
    fname = f"H2_raw_{instrument}_{timeframe}.parquet"
    path = PROCESSED_DIR / fname
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    log.info(f"Loaded: {path} ({len(df)} rows)")
    return df


def fetch_and_save(
    instrument: str,
    timeframe: str,
    csv_path: Optional[str] = None,
    n_bars: Optional[int] = None,
    force_source: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """Fetch, validate, and save. Returns DataFrame or None."""
    df = fetch(instrument, timeframe, csv_path=csv_path, n_bars=n_bars, force_source=force_source)
    if df is not None:
        save_parquet(df, instrument, timeframe)
    return df


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_batch(
    instruments: Optional[list] = None,
    timeframes: Optional[list] = None,
    delay_seconds: float = 0.5,
) -> dict:
    """
    Fetch + save all instrument/timeframe combos.
    Returns summary dict {instrument_tf: ok/fail}.
    """
    insts = instruments or (
        CFG["instruments"]["primary"] + CFG["instruments"].get("forex", [])
    )
    tfs = timeframes or CFG["timeframes"]["research"]

    results = {}
    total = len(insts) * len(tfs)
    done = 0

    for inst in insts:
        for tf in tfs:
            key = f"{inst}_{tf}"
            try:
                df = fetch_and_save(inst, tf)
                results[key] = "ok" if df is not None else "fail"
            except Exception as e:
                log.error(f"Batch error {key}: {e}")
                results[key] = f"error: {e}"
            done += 1
            log.info(f"Progress: {done}/{total} — {key}: {results[key]}")
            if delay_seconds > 0:
                time.sleep(delay_seconds)

    ok = sum(1 for v in results.values() if v == "ok")
    log.info(f"Batch complete: {ok}/{total} succeeded")
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="H2 Quant — Data Loader (Phase 1)")
    parser.add_argument("--instrument", "-i", help="Single instrument (e.g. JP225)")
    parser.add_argument("--timeframe", "-t", help="Timeframe (e.g. 1H, D, 5m)")
    parser.add_argument("--csv", help="Path to MT5 CSV export")
    parser.add_argument("--source", choices=["mt5", "yfinance", "stooq", "csv"], help="Force data source")
    parser.add_argument("--batch", action="store_true", help="Fetch all primary instruments")
    parser.add_argument("--bars", type=int, help="Number of bars (MT5 only)")
    parser.add_argument("--max-history", action="store_true",
                        help="Pull maximum available MT5 history (from 2018) for all backtest instruments")
    parser.add_argument("--start-year", type=int, default=2018,
                        help="Start year for --max-history (default: 2018)")
    args = parser.parse_args()

    if args.max_history:
        insts = [args.instrument] if args.instrument else None
        tfs   = [args.timeframe]  if args.timeframe  else None
        results = run_max_history_batch(
            start_year  = args.start_year,
            timeframes  = tfs,
            instruments = insts,
        )
        ok = sum(1 for v in results.values() if v.startswith("ok"))
        print(f"Max-history pull complete: {ok}/{len(results)} files updated.")
    elif args.batch:
        results = run_batch()
        ok = sum(1 for v in results.values() if v == "ok")
        print(f"\nBatch result: {ok}/{len(results)} successful")
    elif args.instrument and args.timeframe:
        df = fetch_and_save(
            args.instrument,
            args.timeframe,
            csv_path=args.csv,
            n_bars=args.bars,
            force_source=args.source,
        )
        if df is not None:
            print(f"\n{args.instrument} {args.timeframe}: {len(df)} bars")
            print(df.tail(5).to_string())
        else:
            print("Failed — check logs/loader.log")
            sys.exit(1)
    else:
        parser.print_help()
