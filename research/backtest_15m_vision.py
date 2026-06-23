"""
H2 Vision System -- 15M Backtest + Structural Stop Optimisation
Uses real 15M parquet data from data/processed/
Instruments: JP225, USTEC
"""

import numpy as np
import pandas as pd
import json, itertools
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data" / "processed"
OUT  = ROOT / "data"
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# STEP 1: LOAD DATA
# ---------------------------------------------------------------------------

def load_15m(instrument):
    path = DATA / f"H2_raw_{instrument}_15m.parquet"
    df = pd.read_parquet(path)
    df = df.rename(columns={"datetime_utc": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time").reset_index(drop=True)
    print(f"  {instrument} 15m: {len(df)} bars  "
          f"{df['time'].iloc[0].date()} to {df['time'].iloc[-1].date()}")
    return df


# ---------------------------------------------------------------------------
# STEP 2: COMPUTE FEATURES
# ---------------------------------------------------------------------------

def compute_all_components(df):
    df = df.copy()

    # ATR(14)
    prev_c = df["close"].shift(1)
    df["tr"]  = np.maximum(df["high"] - df["low"],
                np.maximum((df["high"] - prev_c).abs(),
                           (df["low"]  - prev_c).abs()))
    df["atr"] = df["tr"].rolling(14, min_periods=5).mean()

    # VWAP -- use volume-weighted if volume available, else simple mean
    df["hlc3"] = (df["high"] + df["low"] + df["close"]) / 3
    W = 96
    has_volume = df["volume"].sum() > 0
    if has_volume:
        pv     = df["hlc3"] * df["volume"].clip(lower=1)
        cum_pv = pv.rolling(W, min_periods=W // 2).sum()
        cum_v  = df["volume"].clip(lower=1).rolling(W, min_periods=W // 2).sum()
        df["vwap"] = cum_pv / cum_v
    else:
        df["vwap"] = df["hlc3"].rolling(W, min_periods=W // 2).mean()

    df["sigma"] = df["hlc3"].rolling(W, min_periods=W // 2).std()
    df["hi2"]   = df["vwap"] + 2 * df["sigma"]
    df["lo2"]   = df["vwap"] - 2 * df["sigma"]
    df["hi1"]   = df["vwap"] + df["sigma"]
    df["lo1"]   = df["vwap"] - df["sigma"]

    # Directional destinations: always hi2 for LONG, lo2 for SHORT
    df["dest_long"]  = df["hi2"]  # LONG target = upper sigma-2 band
    df["dest_short"] = df["lo2"]  # SHORT target = lower sigma-2 band

    # Multi-TF VWAP alignment: fraction of TF VWAPs below current close (bullish alignment)
    tf_aligns = []
    for bars in [4, 16, 64, W]:
        cv = df["hlc3"].rolling(bars, min_periods=bars // 2).mean()
        col = f"_vw_{bars}"
        df[col] = cv
        tf_aligns.append((df["close"] > cv).astype(int))
    df["dest_align"] = sum(tf_aligns)  # 0-4: how many TF VWAPs are below price (bull bias)
    # dest_align: 0-4. For LONG bias: want align >= 3. For SHORT: want align <= 1.

    # Macro levels
    df["pwh"] = df["high"].rolling(480, min_periods=100).max().shift(1)
    df["pwl"] = df["low"].rolling(480,  min_periods=100).min().shift(1)
    df["pdh"] = df["high"].rolling(96,  min_periods=20).max().shift(1)
    df["pdl"] = df["low"].rolling(96,   min_periods=20).min().shift(1)

    # Initial Balance (first 8 bars of each day = first 2H on 15M)
    df["date"]       = df["time"].dt.date
    df["bar_in_day"] = df.groupby("date").cumcount()
    ib_h = df[df["bar_in_day"] < 8].groupby("date")["high"].max()
    ib_l = df[df["bar_in_day"] < 8].groupby("date")["low"].min()
    df["ib_high"] = df["date"].map(ib_h)
    df["ib_low"]  = df["date"].map(ib_l)

    # FVG
    df["fvg_bull"]     = df["low"] > df["high"].shift(2)
    df["fvg_bear"]     = df["high"] < df["low"].shift(2)
    df["fvg_bull_bot"] = np.where(df["fvg_bull"], df["high"].shift(2), np.nan)
    df["fvg_bear_top"] = np.where(df["fvg_bear"], df["low"].shift(2),  np.nan)

    return df.dropna(subset=["atr", "vwap", "hi2", "lo2"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# STEP 3: STRUCTURAL STOP
# ---------------------------------------------------------------------------

def compute_structural_stop(df, i, direction, fib_lo, fib_hi,
                             lookback=16, atr_fallback=0.25):
    atr   = df["atr"].iloc[i]
    price = df["close"].iloc[i]
    start = max(0, i - lookback)

    if direction == "LONG":
        # 1. Bullish FVG inside/just below fib box
        best = np.nan
        for j in range(start, i + 1):
            if df["fvg_bull"].iloc[j]:
                bot = df["fvg_bull_bot"].iloc[j]
                if not np.isnan(bot) and bot >= fib_lo - atr * 0.5:
                    cand = bot - atr * 0.1
                    if np.isnan(best) or cand > best:
                        best = cand
        if not np.isnan(best) and best < price:
            return best, "FVG"
        # 2. Lowest swing low in fib box
        lows = df["low"].iloc[start:i]
        in_fib = lows[lows >= fib_lo]
        if len(in_fib) > 0:
            cand = in_fib.min() - atr * 0.1
            if cand < price:
                return cand, "STRUCTURE"
        return fib_lo - atr * atr_fallback, "ATR_FALLBACK"

    else:
        # 1. Bearish FVG inside/just above fib box
        best = np.nan
        for j in range(start, i + 1):
            if df["fvg_bear"].iloc[j]:
                top = df["fvg_bear_top"].iloc[j]
                if not np.isnan(top) and top <= fib_hi + atr * 0.5:
                    cand = top + atr * 0.1
                    if np.isnan(best) or cand < best:
                        best = cand
        if not np.isnan(best) and best > price:
            return best, "FVG"
        # 2. Highest swing high in fib box
        highs = df["high"].iloc[start:i]
        in_fib = highs[highs <= fib_hi]
        if len(in_fib) > 0:
            cand = in_fib.max() + atr * 0.1
            if cand > price:
                return cand, "STRUCTURE"
        return fib_hi + atr * atr_fallback, "ATR_FALLBACK"


# ---------------------------------------------------------------------------
# STEP 4: BACKTEST ENGINE
# ---------------------------------------------------------------------------

def backtest_15m_complete(df, params):
    bos_len      = params.get("bos_len",      10)
    fib_618      = params.get("fib_618",     0.50)
    fib_786      = params.get("fib_786",     0.786)
    min_align    = params.get("min_align",      0)   # min TF dests aligned (0=off)
    s2_lb        = params.get("s2_lookback",   30)   # used as qualifier feature, not gate
    require_s2   = params.get("require_s2",  False)  # set True to gate on sigma-2
    max_hold     = params.get("max_hold",      96)

    results      = []
    struct_bull  = True
    bos_up       = np.nan
    bos_dn       = np.nan
    macro_hi     = np.nan
    macro_lo     = np.nan
    new_struct_hi = np.nan
    new_struct_lo = np.nan
    level_visits  = {}
    warmup = bos_len + 100 + 1

    for i in range(warmup, len(df) - max_hold - 1):
        row  = df.iloc[i]
        atr  = row["atr"]
        if atr <= 0 or np.isnan(atr):
            continue

        price  = row["close"]
        hi2    = row["hi2"]
        lo2    = row["lo2"]
        vwap   = row["vwap"]

        # -- Update BOS state -------------------------------------------------
        recent_hi = df["high"].iloc[i - bos_len : i].max()
        recent_lo = df["low"].iloc[i  - bos_len : i].min()
        prev_c    = df["close"].iloc[i - 1]

        if price > recent_hi and prev_c <= recent_hi:
            bos_up        = recent_hi
            struct_bull   = True
            macro_lo      = recent_lo
            new_struct_hi = recent_hi

        if price < recent_lo and prev_c >= recent_lo:
            bos_dn        = recent_lo
            struct_bull   = False
            macro_hi      = recent_hi
            new_struct_lo = recent_lo

        # -- Direction from BOS state -----------------------------------------
        direction = "LONG" if struct_bull else "SHORT"

        # -- Condition 1: Fib pullback zone ------------------------------------
        if direction == "SHORT":
            if np.isnan(bos_dn) or np.isnan(macro_hi):
                continue
            swing = macro_hi - bos_dn
            if swing < atr:
                continue
            fib_hi_lvl     = macro_hi - swing * fib_618
            fib_lo_lvl     = macro_hi - swing * fib_786
            bos_ref        = bos_dn
            new_struct_ref = new_struct_lo
        else:
            if np.isnan(bos_up) or np.isnan(macro_lo):
                continue
            swing = bos_up - macro_lo
            if swing < atr:
                continue
            fib_lo_lvl     = bos_up - swing * fib_786
            fib_hi_lvl     = bos_up - swing * fib_618
            bos_ref        = bos_up
            new_struct_ref = new_struct_hi

        if not (fib_lo_lvl <= price <= fib_hi_lvl):
            continue

        # -- Sigma-2 qualifier (feature + optional gate) ----------------------
        rc = df["close"].iloc[max(0, i - s2_lb) : i]
        s2_any = bool(any(rc > hi2) or any(rc < lo2))
        if require_s2 and not s2_any:
            continue

        # -- Destination alignment (optional gate) ----------------------------
        if min_align > 0:
            align = int(row["dest_align"])
            if direction == "LONG"  and align < min_align:
                continue
            if direction == "SHORT" and align > (4 - min_align):
                continue

        # -- BOS type: REVERSAL = dest is beyond VWAP midpoint (larger move) --
        if direction == "LONG":
            bos_type = "CONTINUATION" if price < vwap else "REVERSAL"
        else:
            bos_type = "CONTINUATION" if price > vwap else "REVERSAL"

        # -- Macro alignment --------------------------------------------------
        tol   = atr * 2.0
        pwh_v = row.get("pwh", np.nan)
        pwl_v = row.get("pwl", np.nan)
        pdh_v = row.get("pdh", np.nan)
        pdl_v = row.get("pdl", np.nan)
        macro_tag = "none"
        bos_macro = dest_macro = False
        tp_dest = row["dest_long"] if direction == "LONG" else row["dest_short"]

        if direction == "SHORT":
            if not np.isnan(pwl_v) and abs(bos_ref - pwl_v) < tol:
                bos_macro = True; macro_tag = "PWL_bos"
            elif not np.isnan(pdl_v) and abs(bos_ref - pdl_v) < tol:
                bos_macro = True; macro_tag = "PDL_bos"
            if not np.isnan(pwl_v) and abs(tp_dest - pwl_v) < tol:
                dest_macro = True
                if macro_tag == "none": macro_tag = "PWL_dest"
            if not np.isnan(new_struct_ref):
                if not np.isnan(pwl_v) and abs(new_struct_ref - pwl_v) < tol:
                    if macro_tag == "none": macro_tag = "PWL_struct"
                elif not np.isnan(pdl_v) and abs(new_struct_ref - pdl_v) < tol:
                    if macro_tag == "none": macro_tag = "PDL_struct"
        else:
            if not np.isnan(pwh_v) and abs(bos_ref - pwh_v) < tol:
                bos_macro = True; macro_tag = "PWH_bos"
            elif not np.isnan(pdh_v) and abs(bos_ref - pdh_v) < tol:
                bos_macro = True; macro_tag = "PDH_bos"
            if not np.isnan(pwh_v) and abs(tp_dest - pwh_v) < tol:
                dest_macro = True
                if macro_tag == "none": macro_tag = "PWH_dest"
            if not np.isnan(new_struct_ref):
                if not np.isnan(pwh_v) and abs(new_struct_ref - pwh_v) < tol:
                    if macro_tag == "none": macro_tag = "PWH_struct"
                elif not np.isnan(pdh_v) and abs(new_struct_ref - pdh_v) < tol:
                    if macro_tag == "none": macro_tag = "PDH_struct"

        macro_align         = bos_macro or dest_macro
        new_struct_at_macro = macro_tag in \
            ["PWL_struct", "PDL_struct", "PWH_struct", "PDH_struct"]

        # -- Visit counter ----------------------------------------------------
        bucket      = round(tp_dest / max(atr * 2, 1)) * max(int(atr * 2), 1)
        visit_count = level_visits.get(bucket, 0) + 1
        level_visits[bucket] = visit_count

        # -- FVG in path ------------------------------------------------------
        ls = max(0, i - 8)
        fvg_in_path = bool((df["fvg_bear" if direction == "SHORT" else "fvg_bull"]
                            .iloc[ls:i]).any())

        # -- IB target --------------------------------------------------------
        ib_high_v = row.get("ib_high", np.nan)
        ib_low_v  = row.get("ib_low",  np.nan)
        tp_ib = np.nan
        if direction == "SHORT" and not np.isnan(ib_low_v) and ib_low_v < price:
            tp_ib = ib_low_v
        elif direction == "LONG" and not np.isnan(ib_high_v) and ib_high_v > price:
            tp_ib = ib_high_v

        # -- Structural stop --------------------------------------------------
        stop_price, stop_type = compute_structural_stop(
            df, i, direction, fib_lo_lvl, fib_hi_lvl, lookback=16)
        entry     = price
        stop_dist = abs(entry - stop_price)
        if stop_dist <= 0 or stop_dist > atr * 3:
            continue

        # Sanity check: destination must be BEYOND entry in the trade direction
        if direction == "LONG"  and tp_dest <= entry:
            continue
        if direction == "SHORT" and tp_dest >= entry:
            continue

        # -- Targets ----------------------------------------------------------
        tp_bos        = bos_ref
        tp_vwap       = vwap
        tp_new_struct = new_struct_ref
        tp_1r = entry + stop_dist if direction == "LONG" else entry - stop_dist
        tp_2r = entry + 2*stop_dist if direction == "LONG" else entry - 2*stop_dist
        tp_3r = entry + 3*stop_dist if direction == "LONG" else entry - 3*stop_dist

        # -- Forward simulation -----------------------------------------------
        hit = {k: False for k in ["dest", "bos", "vwap", "ib", "new_struct", "stop",
                                   "r1", "r2", "r3"]}
        first_hit = None
        bh = 0

        for j in range(i + 1, min(i + 1 + max_hold, len(df))):
            fj = df.iloc[j]; bh += 1
            lo_j = fj["low"]; hi_j = fj["high"]

            if direction == "SHORT":
                if hi_j >= stop_price:
                    hit["stop"] = True; break
                if not hit["dest"] and lo_j <= tp_dest:
                    hit["dest"] = True
                    if first_hit is None: first_hit = "dest"
                if not hit["bos"] and lo_j <= tp_bos:
                    hit["bos"] = True
                    if first_hit is None: first_hit = "bos"
                if not hit["vwap"] and lo_j <= tp_vwap:
                    hit["vwap"] = True
                if not hit["ib"] and not np.isnan(tp_ib) and lo_j <= tp_ib:
                    hit["ib"] = True
                    if first_hit is None: first_hit = "ib"
                if not hit["new_struct"] and not np.isnan(tp_new_struct) \
                        and lo_j <= tp_new_struct:
                    hit["new_struct"] = True
                    if first_hit is None: first_hit = "new_struct"
                if not hit["r1"] and lo_j <= tp_1r: hit["r1"] = True
                if not hit["r2"] and lo_j <= tp_2r: hit["r2"] = True
                if not hit["r3"] and lo_j <= tp_3r: hit["r3"] = True
            else:
                if lo_j <= stop_price:
                    hit["stop"] = True; break
                if not hit["dest"] and hi_j >= tp_dest:
                    hit["dest"] = True
                    if first_hit is None: first_hit = "dest"
                if not hit["bos"] and hi_j >= tp_bos:
                    hit["bos"] = True
                    if first_hit is None: first_hit = "bos"
                if not hit["vwap"] and hi_j >= tp_vwap:
                    hit["vwap"] = True
                if not hit["ib"] and not np.isnan(tp_ib) and hi_j >= tp_ib:
                    hit["ib"] = True
                    if first_hit is None: first_hit = "ib"
                if not hit["new_struct"] and not np.isnan(tp_new_struct) \
                        and hi_j >= tp_new_struct:
                    hit["new_struct"] = True
                    if first_hit is None: first_hit = "new_struct"
                if not hit["r1"] and hi_j >= tp_1r: hit["r1"] = True
                if not hit["r2"] and hi_j >= tp_2r: hit["r2"] = True
                if not hit["r3"] and hi_j >= tp_3r: hit["r3"] = True

        # -- EV (R capped at 15 to prevent extreme outliers distorting mean) --
        R_CAP = 15.0

        def ev_single(tp, hit_flag):
            if hit["stop"] and not hit_flag: return -1.0
            if not hit_flag:                 return -0.4
            return min(abs(tp - entry) / stop_dist, R_CAP)

        def ev_partial(t1, t2, h1, h2, ratio=0.5):
            if hit["stop"] and not h1: return -1.0
            r1 = min(abs(t1 - entry) / stop_dist, R_CAP)
            r2 = min(abs(t2 - entry) / stop_dist, R_CAP)
            if h1 and h2:   return r1 * ratio + r2 * (1 - ratio)
            if h1:          return r1 * ratio - (1 - ratio)
            return -0.4

        results.append({
            "i":                   i,
            "direction":           direction,
            "s2_in_window":        s2_any,
            "bos_type":            bos_type,
            "macro_align":         macro_align,
            "macro_tag":           macro_tag,
            "new_struct_at_macro": new_struct_at_macro,
            "fvg_in_path":         fvg_in_path,
            "stop_type":           stop_type,
            "visit_count":         visit_count,
            "dest_align_score":    int(row["dest_align"]),
            "hit_dest":            hit["dest"],
            "hit_bos":             hit["bos"],
            "hit_vwap":            hit["vwap"],
            "hit_ib":              hit["ib"],
            "hit_new_struct":      hit["new_struct"],
            "hit_stop":            hit["stop"],
            "first_hit":           first_hit,
            "bh":                  bh,
            "ev_dest":             ev_single(tp_dest, hit["dest"]),
            "ev_bos":              ev_single(tp_bos,  hit["bos"]),
            "ev_vwap":             ev_single(tp_vwap, hit["vwap"]),
            "ev_ib":               (ev_single(tp_ib, hit["ib"])
                                    if not np.isnan(tp_ib) else np.nan),
            "ev_new_struct":       (ev_single(tp_new_struct, hit["new_struct"])
                                    if not np.isnan(tp_new_struct) else np.nan),
            "ev_split_dest_bos":   ev_partial(tp_dest, tp_bos, hit["dest"], hit["bos"]),
            "ev_split_dest_ns":    (ev_partial(tp_dest, tp_new_struct,
                                               hit["dest"], hit["new_struct"])
                                    if not np.isnan(tp_new_struct) else np.nan),
            "hit_r1":              hit["r1"],
            "hit_r2":              hit["r2"],
            "hit_r3":              hit["r3"],
            "ev_1r":               ev_single(tp_1r, hit["r1"]),
            "ev_2r":               ev_single(tp_2r, hit["r2"]),
            "ev_3r":               ev_single(tp_3r, hit["r3"]),
            "stop_dist_atr":       stop_dist / atr,
            "r_dest":              abs(tp_dest - entry) / stop_dist,
            "r_bos":               abs(tp_bos  - entry) / stop_dist,
        })

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# STEP 5: PARAMETER SWEEP
# ---------------------------------------------------------------------------

def run_parameter_sweep(df, instrument, min_setups=30):
    print(f"\n{'='*70}")
    print(f"PARAMETER SWEEP -- {instrument}")
    print(f"{'='*70}")

    combos = list(itertools.product(
        [5, 7, 10, 14],        # bos_len
        [0.50, 0.618],         # fib_618
        [0.786, 0.85, 0.90],   # fib_786
        [0, 2, 3],             # min_align  (0 = off)
        [False, True],         # require_s2
    ))
    print(f"Testing {len(combos)} configs (min {min_setups} setups)...")

    configs = []
    for bos_len, fib_618, fib_786, min_align, require_s2 in combos:
        if fib_618 >= fib_786:
            continue
        params = dict(bos_len=bos_len, fib_618=fib_618, fib_786=fib_786,
                      min_align=min_align, s2_lookback=30,
                      require_s2=require_s2, max_hold=96)
        r = backtest_15m_complete(df, params)
        if len(r) < min_setups:
            continue

        ev_cols = ["ev_dest", "ev_bos", "ev_split_dest_bos", "ev_1r", "ev_2r", "ev_3r"]
        ev_vals = [r[c].mean() for c in ev_cols]
        ev_best = max(ev_vals)
        best_col = ev_cols[ev_vals.index(ev_best)]
        ns_ev   = r["ev_new_struct"].dropna().mean() if len(r) > 0 else np.nan

        configs.append({
            "bos_len":       bos_len,
            "fib_618":       fib_618,
            "fib_786":       fib_786,
            "min_align":     min_align,
            "require_s2":    require_s2,
            "n":             len(r),
            "stop_rate":     r["hit_stop"].mean(),
            "tp1_dest":      r["hit_dest"].mean(),
            "tp1_bos":       r["hit_bos"].mean(),
            "ev_dest":       r["ev_dest"].mean(),
            "ev_bos":        r["ev_bos"].mean(),
            "ev_split":      r["ev_split_dest_bos"].mean(),
            "ev_new_struct": ns_ev,
            "ev_best":       ev_best,
            "best_exit":     best_col,
            "fvg_stop_pct":  (r["stop_type"] == "FVG").mean(),
        })

    if not configs:
        print(f"  No config reached {min_setups} setups.")
        return pd.DataFrame()

    sweep_df = pd.DataFrame(configs).sort_values("ev_best", ascending=False)
    print(f"\nTop 10 configs by best EV:")
    print(sweep_df.head(10).to_string(index=False))
    return sweep_df


# ---------------------------------------------------------------------------
# STEP 6: DEEP ANALYSIS
# ---------------------------------------------------------------------------

def analyse_structure_targets(r, instrument):
    lines = []
    def p(s=""): lines.append(s); print(s)

    p(f"\n{'='*70}")
    p(f"STRUCTURAL TARGET ANALYSIS -- {instrument}  (n={len(r)})")
    p(f"{'='*70}")

    # BOS level
    p(f"\nBOS STRUCTURAL LEVEL:")
    p(f"  Hit rate:  {r['hit_bos'].mean()*100:.1f}%")
    p(f"  EV:        {r['ev_bos'].mean():+.3f}R")
    bm = r[r["macro_align"]]; bn = r[~r["macro_align"]]
    if len(bm):
        p(f"  + macro (n={len(bm)}):   HR={bm['hit_bos'].mean()*100:.1f}%  "
          f"EV={bm['ev_bos'].mean():+.3f}R")
    if len(bn):
        p(f"  - macro (n={len(bn)}):   HR={bn['hit_bos'].mean()*100:.1f}%  "
          f"EV={bn['ev_bos'].mean():+.3f}R")

    # New structure level
    ns = r.dropna(subset=["ev_new_struct"])
    p(f"\nNEW STRUCTURE LEVEL (n={len(ns)} with valid level):")
    if len(ns):
        p(f"  Hit rate:  {ns['hit_new_struct'].mean()*100:.1f}%")
        p(f"  EV:        {ns['ev_new_struct'].mean():+.3f}R")
        nsm = ns[ns["new_struct_at_macro"]]
        nsn = ns[~ns["new_struct_at_macro"]]
        if len(nsm):
            p(f"  + at macro (n={len(nsm)}): HR={nsm['hit_new_struct'].mean()*100:.1f}%  "
              f"EV={nsm['ev_new_struct'].mean():+.3f}R")
        if len(nsn):
            p(f"  - at macro (n={len(nsn)}): HR={nsn['hit_new_struct'].mean()*100:.1f}%  "
              f"EV={nsn['ev_new_struct'].mean():+.3f}R")

    # Visit count
    p(f"\nVISIT COUNT EFFECT:")
    for vc in [1, 2, 3]:
        sub = r[r["visit_count"] == vc]
        if len(sub):
            p(f"  Visit #{vc} (n={len(sub):3d}):  "
              f"TP_dest={sub['hit_dest'].mean()*100:.1f}%  "
              f"EV={sub['ev_dest'].mean():+.3f}R")
    sub3 = r[r["visit_count"] >= 3]
    if len(sub3):
        p(f"  Visit 3+  (n={len(sub3):3d}):  "
          f"TP_dest={sub3['hit_dest'].mean()*100:.1f}%  "
          f"EV={sub3['ev_dest'].mean():+.3f}R")

    # Stop type
    p(f"\nSTOP TYPE PERFORMANCE:")
    for st in ["FVG", "STRUCTURE", "ATR_FALLBACK"]:
        sub = r[r["stop_type"] == st]
        if len(sub):
            p(f"  {st:15s} (n={len(sub):3d}):  "
              f"StopRate={sub['hit_stop'].mean()*100:.1f}%  "
              f"Dist={sub['stop_dist_atr'].mean():.2f}x ATR  "
              f"EV={sub['ev_dest'].mean():+.3f}R")

    # IB zone
    ib = r.dropna(subset=["ev_ib"])
    p(f"\nINITIAL BALANCE ZONE (n={len(ib)}):")
    if len(ib):
        p(f"  Hit rate:  {ib['hit_ib'].mean()*100:.1f}%")
        p(f"  EV:        {ib['ev_ib'].mean():+.3f}R")

    # First hit
    p(f"\nFIRST TARGET REACHED:")
    for label, cnt in r["first_hit"].value_counts(dropna=False).items():
        p(f"  {str(label):14s}: {cnt:3d}  ({cnt/len(r)*100:.1f}%)")

    # BOS type
    p(f"\nBOS TYPE:")
    for bt in ["REVERSAL", "CONTINUATION"]:
        sub = r[r["bos_type"] == bt]
        if len(sub):
            p(f"  {bt:14s} (n={len(sub):3d}):  "
              f"TP_dest={sub['hit_dest'].mean()*100:.1f}%  "
              f"EV={sub['ev_dest'].mean():+.3f}R")

    # Dest alignment score
    p(f"\nDESTINATION ALIGNMENT SCORE (0-4):")
    for score in range(5):
        sub = r[r["dest_align_score"] == score]
        if len(sub):
            p(f"  Score {score} (n={len(sub):3d}):  "
              f"TP_dest={sub['hit_dest'].mean()*100:.1f}%  "
              f"EV={sub['ev_dest'].mean():+.3f}R")

    # FVG in path
    p(f"\nFVG IN PATH:")
    for flag in [True, False]:
        sub = r[r["fvg_in_path"] == flag]
        label = "FVG present" if flag else "No FVG    "
        if len(sub):
            p(f"  {label} (n={len(sub):3d}):  "
              f"TP_dest={sub['hit_dest'].mean()*100:.1f}%  "
              f"EV={sub['ev_dest'].mean():+.3f}R")

    # Fixed-R targets
    p(f"\nFIXED-R TARGETS (honest directional edge test):")
    for col, name, desc in [
        ("ev_1r", "1R target", "hit_r1"),
        ("ev_2r", "2R target", "hit_r2"),
        ("ev_3r", "3R target", "hit_r3"),
    ]:
        if col in r.columns:
            wr = r[desc].mean()
            ev = r[col].mean()
            p(f"  {name}: HR={wr*100:.1f}%  EV={ev:+.3f}R  "
              f"(breakeven HR = {1/(1+int(name[0])):.0%})")

    # Sigma-2 in window
    if "s2_in_window" in r.columns:
        p(f"\nSIGMA-2 IN WINDOW (last 30 bars):")
        for flag in [True, False]:
            sub = r[r["s2_in_window"] == flag]
            label = "sigma-2 present" if flag else "no sigma-2     "
            if len(sub):
                p(f"  {label} (n={len(sub):3d}):  "
                  f"TP_dest={sub['hit_dest'].mean()*100:.1f}%  "
                  f"EV={sub['ev_dest'].mean():+.3f}R")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STEP 7: WALK-FORWARD VALIDATION
# ---------------------------------------------------------------------------

def walk_forward_15m(df, best_params, instrument, n_splits=5):
    print(f"\n{'='*70}")
    print(f"WALK-FORWARD VALIDATION -- {instrument}  ({n_splits}-fold expanding)")
    print(f"{'='*70}")

    total    = len(df)
    init_pct = 0.40
    fold_pct = 0.12
    ev_col   = "ev_dest"

    in_evs, out_evs = [], []

    for fold in range(n_splits):
        train_end  = int(total * (init_pct + fold * fold_pct))
        test_start = train_end
        test_end   = min(int(test_start + total * fold_pct), total)
        if test_end <= test_start:
            continue

        r_tr = backtest_15m_complete(
            df.iloc[:train_end].reset_index(drop=True), best_params)
        r_te = backtest_15m_complete(
            df.iloc[test_start:test_end].reset_index(drop=True), best_params)

        if len(r_tr) < 5 or len(r_te) < 3:
            print(f"  Fold {fold+1}: low setup count "
                  f"(train={len(r_tr)}, test={len(r_te)}) -- skip")
            continue

        ie = r_tr[ev_col].mean()
        oe = r_te[ev_col].mean()
        in_evs.append(ie); out_evs.append(oe)
        print(f"  Fold {fold+1}:  train n={len(r_tr):3d} EV={ie:+.3f}R  |  "
              f"test n={len(r_te):3d} EV={oe:+.3f}R  |  "
              f"deg={oe-ie:+.3f}R")

    if not in_evs:
        print("  Insufficient data for walk-forward.")
        return None, None, False

    avg_in  = np.mean(in_evs)
    avg_out = np.mean(out_evs)
    avg_deg = avg_out - avg_in
    robust  = abs(avg_deg) < 0.15

    print(f"\n  AVERAGE:  in={avg_in:+.3f}R  out={avg_out:+.3f}R  "
          f"deg={avg_deg:+.3f}R  -> {'ROBUST' if robust else 'OVERFIT'}")
    return avg_in, avg_out, robust


# ---------------------------------------------------------------------------
# STEP 8: FINAL REPORT
# ---------------------------------------------------------------------------

def final_report(r, instrument, best_exit_col, params, wf_in, wf_out, wf_robust):
    n  = len(r)
    ev = r[best_exit_col].mean()
    wr = (r[best_exit_col] > 0).mean()

    ns_evs  = r["ev_new_struct"].dropna()
    nsm_evs = r[r["new_struct_at_macro"]]["ev_new_struct"].dropna()
    pw_tags = r[r["macro_tag"].str.contains("PW", na=False)]

    lines = []
    def p(s=""): lines.append(s); print(s)

    p(f"\n{'='*70}")
    p(f"H2 VISION SYSTEM -- VALIDATED SETTINGS -- {instrument}")
    p(f"{'='*70}")

    p(f"\nENTRY CONDITIONS (15M validated):")
    p(f"  BOS pivot length :  {params['bos_len']} bars ({params['bos_len']*15} min)")
    p(f"  Sigma-2 gate     :  {'REQUIRED (last 30 bars)' if params.get('require_s2') else 'feature only (not gated)'}")
    p(f"  Fib box upper    :  {params['fib_618']*100:.0f}% retracement")
    p(f"  Fib box lower    :  {params['fib_786']*100:.1f}% retracement")
    p(f"  Min TF alignment :  {params['min_align']}/4 destinations")

    p(f"\nSTOP PLACEMENT (structural priority):")
    for st in ["FVG", "STRUCTURE", "ATR_FALLBACK"]:
        sub = r[r["stop_type"] == st]
        pct = len(sub) / n * 100 if n else 0
        if len(sub):
            p(f"  {st:15s}: {pct:.0f}% of setups  "
              f"StopRate={sub['hit_stop'].mean()*100:.1f}%  "
              f"Dist={sub['stop_dist_atr'].mean():.2f}x ATR")

    p(f"\nEXIT HIERARCHY (by EV, R capped at 15):")
    for col, name in [
        ("ev_1r",             "Fixed 1R target          "),
        ("ev_2r",             "Fixed 2R target          "),
        ("ev_3r",             "Fixed 3R target          "),
        ("ev_bos",            "BOS structural level     "),
        ("ev_split_dest_bos", "50/50 split dest+BOS    "),
        ("ev_vwap",           "VWAP fair value          "),
        ("ev_new_struct",     "New structure level      "),
        ("ev_ib",             "Initial Balance boundary "),
        ("ev_dest",           "Dest VWAP sigma-2 (far)  "),
    ]:
        if col in r.columns:
            sub = r.dropna(subset=[col])
            if len(sub):
                p(f"  {name}  EV={sub[col].mean():+.4f}R  "
                  f"WR={(sub[col]>0).mean()*100:.1f}%  n={len(sub)}")

    p(f"\nNEW STRUCTURE + BOS TARGET RULES:")
    if len(ns_evs):
        p(f"  New structure (all):        HR={r['hit_new_struct'].mean()*100:.1f}%  "
          f"EV={ns_evs.mean():+.3f}R")
    if len(nsm_evs):
        p(f"  New structure + macro:      HR={r[r['new_struct_at_macro']]['hit_new_struct'].mean()*100:.1f}%  "
          f"EV={nsm_evs.mean():+.3f}R")
    if len(pw_tags):
        p(f"  BOS at PWH/PWL (weekly):    n={len(pw_tags)}  "
          f"EV_dest={pw_tags['ev_dest'].mean():+.3f}R")
    p(f"  RULE: new struct within 1.5x ATR of PWH/PWL/PDH/PDL")
    p(f"    -> TARGET PRIORITY 1, size x1.5, trail after TP1")

    p(f"\nVISIT COUNT:")
    for vc in [1, 2, 3]:
        sub = r[r["visit_count"] == vc]
        if len(sub):
            p(f"  Visit #{vc}: TP={sub['hit_dest'].mean()*100:.1f}%  "
              f"EV={sub['ev_dest'].mean():+.3f}R  (n={len(sub)})")

    p(f"\nSYSTEM FREQUENCY:")
    span_bars = r["i"].max() - r["i"].min() if n else 1
    months    = max(span_bars * 15 / 60 / 24 / 30, 0.1)
    p(f"  Total setups validated : {n}")
    p(f"  Span                   : ~{months:.1f} months")
    p(f"  Avg per month          : ~{n/months:.0f}")
    p(f"  Avg hold               : {r['bh'].mean():.1f} x 15M = {r['bh'].mean()*0.25:.1f}H")

    p(f"\nOVERALL PERFORMANCE:")
    p(f"  Best exit col  : {best_exit_col}")
    p(f"  EV per trade   : {ev:+.4f}R")
    p(f"  Win rate       : {wr*100:.1f}%")
    p(f"  Stop rate      : {r['hit_stop'].mean()*100:.1f}%")
    if wf_in is not None:
        p(f"  WF in-sample   : {wf_in:+.3f}R")
        p(f"  WF out-sample  : {wf_out:+.3f}R")
        p(f"  WF degradation : {wf_out-wf_in:+.3f}R")
        p(f"  Robustness     : {'ROBUST' if wf_robust else 'OVERFIT'}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STEP 9: VALIDATED SETTINGS DOC
# ---------------------------------------------------------------------------

def write_validated_settings(all_reports):
    lines = [
        "# H2 Vision System -- Validated Settings\n\n",
        "Generated from 15M backtest on real IC Markets / yfinance data.\n\n",
    ]
    for instrument, rep in all_reports.items():
        p = rep["params"]
        lines += [
            f"## {instrument}\n\n",
            f"**Bars:** {rep['n']} setups validated on 15M data\n\n",
            "### Entry Parameters\n\n",
            "| Parameter | Value |\n|---|---|\n",
            f"| BOS pivot length | {p['bos_len']} bars ({p['bos_len']*15} min) |\n",
            f"| sigma-2 lookback | {p['s2_lookback']} bars ({p['s2_lookback']*15} min) |\n",
            f"| Fib box upper | {p['fib_618']*100:.0f}% |\n",
            f"| Fib box lower | {p['fib_786']*100:.1f}% |\n",
            f"| Min TF alignment | {p['min_align']}/4 |\n",
            "\n### Performance\n\n",
            "| Metric | Value |\n|---|---|\n",
            f"| Best exit | {rep['best_exit']} |\n",
            f"| EV per trade | {rep['ev']:+.4f}R |\n",
            f"| Win rate | {rep['wr']*100:.1f}% |\n",
            f"| Stop rate | {rep['stop_rate']*100:.1f}% |\n",
        ]
        if rep.get("wf_in") is not None:
            lines += [
                f"| Walk-forward in | {rep['wf_in']:+.3f}R |\n",
                f"| Walk-forward out | {rep['wf_out']:+.3f}R |\n",
                f"| WF degradation | {rep['wf_out']-rep['wf_in']:+.3f}R |\n",
                f"| Robustness | {'ROBUST' if rep['wf_robust'] else 'OVERFIT'} |\n",
            ]
        lines.append("\n")

    lines += [
        "## New Structure Level as Target\n\n",
        "If new structure level is within 1.5x ATR of PWH/PWL/PDH/PDL:\n",
        "- Use new structure level as Target Priority 1\n",
        "- Size up to 1.5x normal\n",
        "- Trail stop after TP1 (destination cluster)\n\n",
        "## Oscillation Rules\n\n",
        "Price visits VWAP destinations in sequence: 15M -> 1H -> 4H -> Daily.\n",
        "Each level: collect EQH/EQL -> Fib pullback -> next leg.\n",
        "2nd/3rd visit to same level = valid entry, tighter stop.\n",
    ]

    path = DOCS / "H2_VISION_VALIDATED_SETTINGS.md"
    path.write_text("".join(lines), encoding="utf-8")
    print(f"\nValidated settings doc written to {path}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    instruments  = ["JP225", "USTEC"]
    all_reports  = {}

    for instrument in instruments:
        print(f"\n{'#'*70}")
        print(f"# {instrument}")
        print(f"{'#'*70}")

        print("Loading data...")
        df = load_15m(instrument)

        print("Computing features...")
        df = compute_all_components(df)
        print(f"  After features: {len(df)} bars")

        sweep = run_parameter_sweep(df, instrument, min_setups=30)
        sweep.to_csv(OUT / f"param_sweep_{instrument}.csv", index=False)

        if sweep.empty:
            print(f"  No config met threshold for {instrument}.")
            continue

        best_row = sweep.iloc[0]
        best_params = {
            "bos_len":     int(best_row["bos_len"]),
            "fib_618":     float(best_row["fib_618"]),
            "fib_786":     float(best_row["fib_786"]),
            "min_align":   int(best_row["min_align"]),
            "s2_lookback": 30,
            "require_s2":  bool(best_row["require_s2"]),
            "max_hold":    96,
        }
        best_exit = best_row["best_exit"]
        print(f"\nBest config: {best_params}")
        print(f"  EV={best_row['ev_best']:+.4f}R  n={int(best_row['n'])}")

        print("\nRunning full backtest with best params...")
        r = backtest_15m_complete(df, best_params)
        print(f"  Total setups: {len(r)}")

        if len(r) < 10:
            print("  Too few setups for meaningful analysis.")
            continue

        r.to_csv(OUT / f"backtest_results_15m_{instrument}.csv", index=False)

        analysis_text = analyse_structure_targets(r, instrument)
        wf_in, wf_out, wf_robust = walk_forward_15m(df, best_params, instrument)
        report_text = final_report(r, instrument, best_exit, best_params,
                                   wf_in, wf_out, wf_robust)

        all_reports[instrument] = {
            "params":     best_params,
            "best_exit":  best_exit,
            "n":          len(r),
            "ev":         float(r[best_exit].mean()),
            "wr":         float((r[best_exit] > 0).mean()),
            "stop_rate":  float(r["hit_stop"].mean()),
            "wf_in":      wf_in,
            "wf_out":     wf_out,
            "wf_robust":  wf_robust,
            "sweep_top5": sweep.head(5).to_dict("records"),
        }

    def to_py(obj):
        if isinstance(obj, (np.integer,)):  return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, (np.bool_,)):    return bool(obj)
        if isinstance(obj, dict):           return {k: to_py(v) for k, v in obj.items()}
        if isinstance(obj, list):           return [to_py(v) for v in obj]
        return obj

    with open(OUT / "backtest_results_15m.json", "w") as f:
        json.dump(to_py(all_reports), f, indent=2, default=str)

    write_validated_settings(all_reports)

    print(f"\n{'='*70}")
    print("All outputs saved.")


if __name__ == "__main__":
    main()
