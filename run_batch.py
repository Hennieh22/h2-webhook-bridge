"""
H2 Quant v1 — Batch Pipeline Runner
Fetch → Engineer → Classify for all instruments × timeframes.
Skips steps where output already exists (resumable).
"""

import logging
import sys
import time
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent
with open(ROOT / "config.yaml") as f:
    CFG = yaml.safe_load(f)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(ROOT / "logs" / "batch.log", encoding="utf-8")],
)
log = logging.getLogger("H2.batch")

sys.path.insert(0, str(ROOT))
from data.loader    import fetch_and_save, load_parquet
from features.engineer  import engineer, load_raw as feat_load_raw, FEAT_DIR
from states.classifier  import classify, STATES_DIR, StateAnalyzer

INSTRUMENTS = [
    "JP225","DE40","UK100","USTEC","US30","HK50",
    "EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD",
    "EURJPY","GBPJPY","XAUUSD","XAGUSD",
]
TIMEFRAMES = ["D", "4H", "1H", "15m"]

PROC_DIR  = ROOT / CFG["paths"]["data_processed"]
FEAT_DIR  = ROOT / CFG["paths"]["features"]
STATES_DIR_P = ROOT / "states"

# yfinance bar limits by timeframe
YF_PERIOD = {"D": "5y", "4H": "730d", "1H": "730d", "15m": "60d"}

# ── helpers ──────────────────────────────────────────────────────────────────

def raw_path(inst, tf):
    return PROC_DIR / f"H2_raw_{inst}_{tf}.parquet"

def feat_path(inst, tf):
    return FEAT_DIR / f"H2_features_{inst}_{tf}.parquet"

def state_path(inst, tf):
    return STATES_DIR_P / f"H2_states_{inst}_{tf}.parquet"

def step_fetch(inst, tf, results):
    key = f"{inst}_{tf}"
    if raw_path(inst, tf).exists():
        log.info(f"[SKIP fetch] {key} — raw already exists")
        results[key]["fetch"] = "skip"
        return True
    try:
        import yfinance as yf
        ticker_map = CFG["yfinance"]["ticker_map"]
        tf_map     = CFG["yfinance"]["timeframe_map"]
        ticker = ticker_map.get(inst)
        yf_tf  = tf_map.get(tf)
        period = YF_PERIOD.get(tf, "2y")

        if ticker and yf_tf:
            raw = yf.download(ticker, period=period, interval=yf_tf,
                              auto_adjust=True, progress=False)
            if raw is not None and not raw.empty:
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = raw.columns.get_level_values(0)
                raw.columns = [c.lower() for c in raw.columns]
                raw.index = pd.to_datetime(raw.index, utc=True)
                raw.index.name = "datetime_utc"
                raw = raw.reset_index()
                raw["volume"] = pd.to_numeric(raw.get("volume", 0), errors="coerce").fillna(0)
                raw["source"] = "yfinance"
                raw["datetime_sast"] = raw["datetime_utc"] + pd.Timedelta(hours=2)
                for col in ["open","high","low","close"]:
                    raw[col] = pd.to_numeric(raw[col], errors="coerce")
                raw = raw.dropna(subset=["open","high","low","close"])
                if not raw.empty:
                    import pyarrow as pa, pyarrow.parquet as pq
                    tbl = pa.Table.from_pandas(raw, preserve_index=False)
                    pq.write_table(tbl, raw_path(inst, tf), compression="snappy")
                    log.info(f"[fetch OK] {key} — {len(raw)} bars via yfinance")
                    results[key]["fetch"] = f"ok:{len(raw)}"
                    return True

        # MT5 fallback
        from data.loader import fetch_mt5
        df = fetch_mt5(inst, tf)
        if df is not None and not df.empty:
            import pyarrow as pa, pyarrow.parquet as pq
            df["datetime_sast"] = df["datetime_utc"] + pd.Timedelta(hours=2)
            tbl = pa.Table.from_pandas(df, preserve_index=False)
            pq.write_table(tbl, raw_path(inst, tf), compression="snappy")
            log.info(f"[fetch OK] {key} — {len(df)} bars via MT5")
            results[key]["fetch"] = f"ok:{len(df)}"
            return True

        log.warning(f"[fetch FAIL] {key} — no data from any source")
        results[key]["fetch"] = "fail:no_data"
        return False

    except Exception as e:
        log.error(f"[fetch ERR] {key}: {e}")
        results[key]["fetch"] = f"error:{e}"
        return False


def step_engineer(inst, tf, results):
    key = f"{inst}_{tf}"
    if feat_path(inst, tf).exists():
        log.info(f"[SKIP engineer] {key} — features already exist")
        results[key]["engineer"] = "skip"
        return True
    if not raw_path(inst, tf).exists():
        results[key]["engineer"] = "skip:no_raw"
        return False
    try:
        df = engineer(inst, tf)
        if df is not None:
            results[key]["engineer"] = f"ok:{len(df)}"
            return True
        results[key]["engineer"] = "fail"
        return False
    except Exception as e:
        log.error(f"[engineer ERR] {key}: {e}")
        results[key]["engineer"] = f"error:{e}"
        return False


def step_classify(inst, tf, results):
    key = f"{inst}_{tf}"
    if state_path(inst, tf).exists():
        log.info(f"[SKIP classify] {key} — states already exist")
        results[key]["classify"] = "skip"
        return True
    if not feat_path(inst, tf).exists():
        results[key]["classify"] = "skip:no_features"
        return False
    try:
        df = classify(inst, tf)
        if df is not None:
            results[key]["classify"] = f"ok:{df['state_id'].nunique()}_states"
            return True
        results[key]["classify"] = "fail"
        return False
    except Exception as e:
        log.error(f"[classify ERR] {key}: {e}")
        results[key]["classify"] = f"error:{e}"
        return False


# ── consolidated summary ─────────────────────────────────────────────────────

def print_consolidated_summary():
    rows = []
    analyzer = StateAnalyzer(min_samples=CFG["statistics"]["min_samples"])

    for inst in INSTRUMENTS:
        for tf in TIMEFRAMES:
            sp = state_path(inst, tf)
            if not sp.exists():
                rows.append({
                    "instrument": inst, "timeframe": tf,
                    "bars": "-", "unique_states": "-",
                    "states_ge30": "-", "top_state": "NO DATA",
                })
                continue
            try:
                df   = pd.read_parquet(sp)
                dist = analyzer.state_distribution(df)
                ge30 = (dist["count"] >= 30).sum()
                top  = dist.index[0] if len(dist) > 0 else "?"
                rows.append({
                    "instrument":   inst,
                    "timeframe":    tf,
                    "bars":         len(df),
                    "unique_states":dist.shape[0],
                    "states_ge30":  ge30,
                    "top_state":    top,
                })
            except Exception as e:
                rows.append({
                    "instrument": inst, "timeframe": tf,
                    "bars": "ERR", "unique_states": "ERR",
                    "states_ge30": "ERR", "top_state": str(e)[:40],
                })

    summary = pd.DataFrame(rows)

    hdr = f"{'INSTRUMENT':<10} {'TF':<5} {'BARS':>7} {'STATES':>7} {'>= 30':>7}  TOP STATE"
    sep = "=" * 110
    print(f"\n{sep}")
    print("  CONSOLIDATED STATE SUMMARY — ALL INSTRUMENTS x TIMEFRAMES")
    print(sep)
    print(f"  {hdr}")
    print(f"  {'-'*108}")
    for _, r in summary.iterrows():
        print(f"  {r['instrument']:<10} {r['timeframe']:<5} {str(r['bars']):>7} "
              f"{str(r['unique_states']):>7} {str(r['states_ge30']):>7}  {r['top_state']}")
    print(sep)

    # Aggregate stats
    ok_rows = summary[summary["bars"] != "-"]
    if not ok_rows.empty:
        total_bars   = pd.to_numeric(ok_rows["bars"], errors="coerce").sum()
        total_states = pd.to_numeric(ok_rows["unique_states"], errors="coerce").sum()
        print(f"\n  Total bars processed  : {int(total_bars):,}")
        print(f"  Total unique states   : {int(total_states):,}")
        print(f"  Combinations complete : {len(ok_rows)}/{len(summary)}")
    print(sep)
    return summary


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    total     = len(INSTRUMENTS) * len(TIMEFRAMES)
    done      = 0
    results   = {f"{i}_{t}": {"fetch":"?","engineer":"?","classify":"?"}
                 for i in INSTRUMENTS for t in TIMEFRAMES}

    t0 = time.time()
    print(f"\nBatch pipeline: {len(INSTRUMENTS)} instruments x {len(TIMEFRAMES)} timeframes = {total} jobs\n")

    for inst in INSTRUMENTS:
        for tf in TIMEFRAMES:
            key = f"{inst}_{tf}"
            done += 1
            elapsed = time.time() - t0
            print(f"\n[{done:02d}/{total}] {key}  (elapsed {elapsed:.0f}s)")

            ok1 = step_fetch(inst, tf, results)
            ok2 = step_engineer(inst, tf, results) if ok1 else False
            ok3 = step_classify(inst, tf, results) if ok2 else False

            status = "OK" if ok3 else ("PARTIAL" if ok2 else ("RAW_ONLY" if ok1 else "FAIL"))
            print(f"  fetch={results[key]['fetch']}  "
                  f"engineer={results[key]['engineer']}  "
                  f"classify={results[key]['classify']}  => {status}")

            time.sleep(0.3)   # polite pause between yfinance calls

    elapsed_total = time.time() - t0
    print(f"\nAll jobs finished in {elapsed_total:.1f}s\n")

    print_consolidated_summary()
    print("\nPhase 3 batch complete. Ready for Phase 4 — Markov Engine.")
