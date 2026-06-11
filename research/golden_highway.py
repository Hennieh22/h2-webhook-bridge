"""
Golden Highway Backtest — XAUUSD 1H
====================================
12 signal definitions -> walk-forward backtest -> combination search -> Golden Highway score.

Column numbering: user spec uses 1-based (col 1 = time).
In pandas: pandas_index = user_col_number - 1

Backtest rules:
  Entry   : bar close when signal fires
  SL      : 1.5 x ATR(14) below/above entry
  TP1     : +1.0R  (50% exit, SL -> breakeven)
  TP2     : +2.5R  (remaining 50% exit)
  MaxHold : 20 bars
  No pyramiding (one trade at a time)

R-multiple outcomes:
  SL before TP1         -> -1.0R
  TP1 then SL at BE     -> +0.5R  (0.5x1R + 0.5x0R)
  TP1 then TP2          -> +1.75R (0.5x1R + 0.5x2.5R)
  TP1 then max hold     -> 0.5x1R + 0.5x(hold_exit_R)
  Max hold before TP1   -> hold_exit_R

OOS degradation: positive = IS better than OOS (expected), negative = OOS better.
"""

import pandas as pd
import numpy as np
from pathlib import Path
import io
import warnings
import itertools
warnings.filterwarnings('ignore')

import sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT    = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "outputs"
OUT_DIR.mkdir(exist_ok=True)

CSV_PATH = ROOT / "data/raw/ICMARKETS_SE30__60.csv"
TRAIN_END = 2400   # bars 0–2399 = IS,  2400–3229 = OOS (829 bars ~ 11 months)

# -----------------------------------------------------------------------------
# 1. LOAD + PREPARE
# -----------------------------------------------------------------------------

def load_data() -> pd.DataFrame:
    df = pd.read_csv(CSV_PATH)
    df = df.reset_index(drop=True)

    # ATR(14) — Wilder's smoothing
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            (df["high"] - df["prev_close"]).abs(),
            (df["low"]  - df["prev_close"]).abs(),
        )
    )
    df["atr14"] = df["tr"].ewm(span=14, adjust=False).mean()

    # Column name aliases (user 1-based -> pandas col name)
    # Verified by inspection:
    c = df.columns.tolist()
    df["c_ema_cross_up"]    = df[c[51]]   # col 52: EMA Cross Up
    df["c_ema_cross_dn"]    = df[c[52]]   # col 53: EMA Cross Down
    df["c_rsi_score"]       = df[c[53]]   # col 54: RSI Score
    df["c_rsi_5m"]          = df[c[54]]   # col 55: RSI 5M
    df["c_rsi_1h"]          = df[c[57]]   # col 58: RSI 1H
    df["c_rsi_1d"]          = df[c[60]]   # col 61: RSI 1D
    df["c_htf_align"]       = df[c[61]]   # col 62: HTF Alignment
    df["c_reg_bear_div"]    = df[c[68]]   # col 69: Reg Bear Div
    df["c_reg_bull_div"]    = df[c[69]]   # col 70: Reg Bull Div
    df["c_anchor_a"]        = df[c[37]]   # col 38: Anchor A
    df["c_confluence"]      = df[c[92]]   # col 93: Confluence Score
    df["c_journey"]         = df[c[93]]   # col 94: Journey Phase
    df["c_cascade"]         = df[c[94]]   # col 95: Cascade Score
    df["c_micro"]           = df[c[95]]   # col 96: Micro Completion
    df["c_mini"]            = df[c[96]]   # col 97: Mini Composite
    df["c_flow"]            = df[c[97]]   # col 98: Flow Score
    df["c_bos"]             = df[c[98]]   # col 99: BOS Importance
    df["c_bull_bo"]         = df[c[99]]   # col 100: Bullish Breakout
    df["c_bear_bo"]         = df[c[100]]  # col 101: Bearish Breakout

    df = df.fillna(0)
    return df


# -----------------------------------------------------------------------------
# 2. SIGNAL DEFINITIONS
# Returns Series of +1 (long), -1 (short), 0 (no signal)
# -----------------------------------------------------------------------------

def build_signals(df: pd.DataFrame) -> dict:
    signals = {}

    # --- S01: EMA Cross Basic
    s = pd.Series(0, index=df.index)
    s[df["c_ema_cross_up"]  == 1] =  1
    s[df["c_ema_cross_dn"]  == 1] = -1
    signals["S01_EMA_Cross"] = s

    # --- S02: JTE Golden Setup (long only)
    long = (
        (df["c_rsi_1d"]   >  65) &
        (df["c_micro"]    >= 80) &
        (df["c_mini"]     <  40) &
        (df["c_flow"]     >=  0)
    )
    s = pd.Series(0, index=df.index)
    s[long] = 1
    signals["S02_JTE_Golden"] = s

    # --- S03: RSI Stack
    long  = (df["c_rsi_score"] >= 3) & (df["c_rsi_5m"] < 30) & (df["c_rsi_1h"] < 40)
    short = (df["c_rsi_score"] >= 3) & (df["c_rsi_5m"] > 70) & (df["c_rsi_1h"] > 60)
    s = pd.Series(0, index=df.index)
    s[long]  =  1
    s[short] = -1
    signals["S03_RSI_Stack"] = s

    # --- S04: JTE Journey Phase
    long  = (df["c_journey"] == 6)
    short = (df["c_journey"] == 6) & (df["c_rsi_1d"] < 50)
    s = pd.Series(0, index=df.index)
    s[long]  =  1
    s[short] = -1  # short overrides when RSI 1D < 50
    signals["S04_Journey_Phase"] = s

    # --- S05: Confluence Score
    long  = (df["c_confluence"] >= 60)
    short = (df["c_confluence"] <= 20)
    s = pd.Series(0, index=df.index)
    s[long]  =  1
    s[short] = -1
    signals["S05_Confluence"] = s

    # --- S06: BOS + Micro Mature
    long  = (df["c_bos"] >= 4) & (df["c_micro"] >= 67) & (df["c_flow"] >=  1)
    short = (df["c_bos"] >= 4) & (df["c_micro"] >= 67) & (df["c_flow"] <= -1)
    s = pd.Series(0, index=df.index)
    s[long]  =  1
    s[short] = -1
    signals["S06_BOS_Micro"] = s

    # --- S07: CVD Divergence
    long  = (df["c_reg_bull_div"] == 1)
    short = (df["c_reg_bear_div"] == 1)
    s = pd.Series(0, index=df.index)
    s[long]  =  1
    s[short] = -1
    signals["S07_CVD_Div"] = s

    # --- S08: IB Breakout + RSI confirm
    long  = (df["c_bull_bo"] == 1) & (df["c_rsi_1h"] > 50)
    short = (df["c_bear_bo"] == 1) & (df["c_rsi_1h"] < 50)
    s = pd.Series(0, index=df.index)
    s[long]  =  1
    s[short] = -1
    signals["S08_IB_Breakout"] = s

    # --- S09: Markov Anchor Momentum
    long  = (df["c_anchor_a"] >  0) & (df["close"] >  df["c_anchor_a"]) & (df["c_flow"] >=  2)
    short = (df["c_anchor_a"] >  0) & (df["close"] <  df["c_anchor_a"]) & (df["c_flow"] <= -2)
    s = pd.Series(0, index=df.index)
    s[long]  =  1
    s[short] = -1
    signals["S09_Anchor_Mom"] = s

    # --- S10: HTF EMA Alignment
    long = (
        (df["c_htf_align"] == 2) &
        (df["c_flow"]      >=  1) &
        (df["c_rsi_1h"]    <  55)
    )
    s = pd.Series(0, index=df.index)
    s[long] = 1
    signals["S10_HTF_Align"] = s

    # --- S11: Golden Setup STRICT
    long = (
        (df["c_journey"] >= 5) &
        (df["c_micro"]   >= 80) &
        (df["c_mini"]    <  35) &
        (df["c_rsi_1d"]  >  60) &
        (df["c_flow"]    >=  0) &
        (df["c_cascade"] >=  7)
    )
    s = pd.Series(0, index=df.index)
    s[long] = 1
    signals["S11_Golden_Strict"] = s

    # --- S12: Full System Composite (4 of 6 conditions)
    cond1 = (df["c_journey"]   >= 5).astype(int)
    cond2 = (df["c_confluence"]>= 50).astype(int)
    cond3 = (df["c_htf_align"] >= 1).astype(int)
    cond4 = (df["c_flow"]      >= 1).astype(int)
    cond5 = (df["c_micro"]     >= 67).astype(int)
    cond6 = (df["c_bos"]       >= 3).astype(int)
    total = cond1 + cond2 + cond3 + cond4 + cond5 + cond6
    s = pd.Series(0, index=df.index)
    s[total >= 4] = 1
    signals["S12_Composite"] = s

    return signals


# -----------------------------------------------------------------------------
# 3. TRADE SIMULATOR
# -----------------------------------------------------------------------------

def simulate_trades(df: pd.DataFrame, signal: pd.Series, split: int = TRAIN_END) -> dict:
    """
    Simulate trades for a signal Series (+1 long, -1 short, 0 no signal).
    Returns dict with IS and OOS trade lists plus metrics.

    Conservative OHLC bar resolution:
      Long:  if bar.low <= sl_price -> stopped (SL); else if bar.high >= tp_price -> hit
      Short: if bar.high >= sl_price -> stopped (SL); else if bar.low <= tp_price -> hit
      (SL checked before TP within same bar = conservative)
    """
    closes  = df["close"].values
    highs   = df["high"].values
    lows    = df["low"].values
    atrs    = df["atr14"].values
    sig     = signal.values
    n       = len(df)

    trades = []
    in_trade = False
    entry_bar = 0

    for i in range(1, n):
        # -- Entry ------------------------------------------------------------
        if not in_trade and sig[i] != 0:
            direction   = int(sig[i])
            entry_price = closes[i]
            atr         = atrs[i]
            if atr <= 0:
                continue
            risk        = 1.5 * atr
            sl_init     = entry_price - direction * risk
            tp1         = entry_price + direction * 1.0 * risk
            tp2         = entry_price + direction * 2.5 * risk
            entry_bar   = i
            in_trade    = True
            tp1_hit     = False
            sl_curr     = sl_init
            continue

        # -- Manage open trade -------------------------------------------------
        if in_trade:
            bar_high = highs[i]
            bar_low  = lows[i]
            exit_r   = None
            exit_bar = i

            if direction == 1:   # long
                # SL first (conservative)
                if bar_low <= sl_curr:
                    exit_r = -1.0 if not tp1_hit else 0.0
                elif not tp1_hit and bar_high >= tp1:
                    tp1_hit = True
                    sl_curr = entry_price  # move SL to BE
                    if bar_high >= tp2:
                        exit_r = 0.5 * 1.0 + 0.5 * 2.5  # both TP1 and TP2 same bar
                    # else: TP1 hit, waiting for TP2 or BE exit
                elif tp1_hit and bar_high >= tp2:
                    exit_r = 0.5 * 1.0 + 0.5 * 2.5

            else:  # direction == -1, short
                if bar_high >= sl_curr:
                    exit_r = -1.0 if not tp1_hit else 0.0
                elif not tp1_hit and bar_low <= tp1:
                    tp1_hit = True
                    sl_curr = entry_price
                    if bar_low <= tp2:
                        exit_r = 0.5 * 1.0 + 0.5 * 2.5
                elif tp1_hit and bar_low <= tp2:
                    exit_r = 0.5 * 1.0 + 0.5 * 2.5

            # Max hold: 20 bars
            if exit_r is None and (i - entry_bar) >= 20:
                price_move = (closes[i] - entry_price) * direction
                unrealized = price_move / risk
                if tp1_hit:
                    exit_r = 0.5 * 1.0 + 0.5 * unrealized
                else:
                    exit_r = unrealized

            if exit_r is not None:
                trades.append({
                    "entry_bar":  entry_bar,
                    "exit_bar":   exit_bar,
                    "direction":  direction,
                    "entry_price": entry_price,
                    "exit_r":     exit_r,
                    "hold_bars":  exit_bar - entry_bar,
                    "tp1_hit":    tp1_hit,
                    "split":      "IS" if entry_bar < split else "OOS",
                })
                in_trade = False

    return trades


def calc_metrics(trades: list, label: str = "") -> dict:
    if not trades:
        return {
            "label": label, "n_trades": 0, "win_rate": 0, "expectancy": 0,
            "sharpe": 0, "max_dd": 0, "profit_factor": 0, "avg_hold": 0,
        }
    rs = np.array([t["exit_r"] for t in trades])
    wins  = rs[rs > 0]
    loses = rs[rs < 0]
    equity = np.cumsum(rs)
    peak   = np.maximum.accumulate(equity)
    dd     = equity - peak
    pf     = wins.sum() / abs(loses.sum()) if loses.sum() != 0 else np.inf

    # Annualised Sharpe (XAUUSD 1H: ~6500 bars/year assuming 24/5 = ~6240)
    bars_per_year = 6240
    if rs.std() > 0:
        sharpe = (rs.mean() / rs.std()) * np.sqrt(bars_per_year / max(1, len(rs)))
    else:
        sharpe = 0.0

    return {
        "label":         label,
        "n_trades":      len(rs),
        "win_rate":      round(float((rs > 0).mean() * 100), 1),
        "expectancy":    round(float(rs.mean()), 4),
        "sharpe":        round(float(sharpe), 3),
        "max_dd":        round(float(dd.min()), 3),
        "profit_factor": round(float(pf), 3),
        "avg_hold":      round(float(np.array([t["hold_bars"] for t in trades]).mean()), 1),
    }


# -----------------------------------------------------------------------------
# 4. MONTE CARLO RUIN
# -----------------------------------------------------------------------------

def monte_carlo_ruin(trades: list, n_paths: int = 1000,
                     start_equity: float = 100.0,
                     ruin_level: float = 20.0) -> float:
    """
    Bootstrap 1000 paths of len=len(trades) from OOS trade returns.
    Ruin = equity falls below ruin_level at any point.
    Returns ruin probability [0, 1].
    """
    if len(trades) < 5:
        return 1.0
    rs = np.array([t["exit_r"] for t in trades])
    ruin_count = 0
    rng = np.random.default_rng(42)
    for _ in range(n_paths):
        sample   = rng.choice(rs, size=len(rs), replace=True)
        equity   = start_equity + np.cumsum(sample)
        if np.any(equity < ruin_level):
            ruin_count += 1
    return ruin_count / n_paths


# -----------------------------------------------------------------------------
# 5. COMBINATION SIGNAL
# -----------------------------------------------------------------------------

def combine_signals(sig_a: pd.Series, sig_b: pd.Series) -> pd.Series:
    """Both signals must agree on the same bar (same direction)."""
    combined = pd.Series(0, index=sig_a.index)
    combined[(sig_a == 1)  & (sig_b == 1)]  =  1
    combined[(sig_a == -1) & (sig_b == -1)] = -1
    return combined


# -----------------------------------------------------------------------------
# 6. GOLDEN HIGHWAY SCORE
# -----------------------------------------------------------------------------

def golden_highway_score(sharpe: float, max_dd: float, ruin_prob: float) -> float:
    if max_dd == 0 or sharpe <= 0:
        return 0.0
    return sharpe * (1.0 / abs(max_dd)) * (1.0 - ruin_prob)


# -----------------------------------------------------------------------------
# 7. DESCRIPTION HELPER
# -----------------------------------------------------------------------------

SIGNAL_DESCRIPTIONS = {
    "S01_EMA_Cross":    "EMA cross signal — basic trend direction change",
    "S02_JTE_Golden":   "JTE Golden Setup — RSI 1D > 65, Micro >= 80, Mini < 40, Flow >= 0 (long bias)",
    "S03_RSI_Stack":    "RSI Stack — multi-TF RSI alignment at extreme levels",
    "S04_Journey_Phase":"Journey Phase 6 — specific market cycle stage, short if RSI 1D < 50",
    "S05_Confluence":   "Confluence Score threshold — >= 60 for long, <= 20 for short",
    "S06_BOS_Micro":    "BOS Importance >= 4 + Micro Completion >= 67, directional by Flow Score",
    "S07_CVD_Div":      "CVD Regular Divergence — bull div long, bear div short",
    "S08_IB_Breakout":  "IB Breakout (Bullish/Bearish signal) + RSI 1H confirmation",
    "S09_Anchor_Mom":   "Markov Anchor momentum — price vs Anchor A with Flow Score >= ±2",
    "S10_HTF_Align":    "HTF EMA Alignment = 2 (fully bullish) + Flow >= 1 + RSI 1H < 55",
    "S11_Golden_Strict":"Golden Setup STRICT — 6-condition long filter including Cascade >= 7",
    "S12_Composite":    "Full System Composite — 4 of 6 conditions met (long only)",
}


# -----------------------------------------------------------------------------
# 8. MAIN RUNNER
# -----------------------------------------------------------------------------

def main():
    print("=" * 72)
    print("  GOLDEN HIGHWAY BACKTEST — XAUUSD 1H")
    print("=" * 72)
    print(f"  Data: {CSV_PATH.name}")

    df = load_data()
    print(f"  Bars: {len(df)}  |  IS: 0–{TRAIN_END-1}  |  OOS: {TRAIN_END}–{len(df)-1}")
    print(f"  ATR(14) current: {df['atr14'].iloc[-1]:.2f}")
    print()

    signals = build_signals(df)

    # -- STEP 1: Signal fire counts -------------------------------------------
    print("-" * 72)
    print("  STEP 1 — SIGNAL FIRE COUNTS")
    print("-" * 72)
    print(f"  {'Signal':<22} {'IS Long':>8} {'IS Short':>9} {'OOS Long':>9} {'OOS Short':>10} {'OOS Total':>10}  Status")
    print("  " + "-" * 70)

    is_mask  = df.index < TRAIN_END
    oos_mask = df.index >= TRAIN_END
    flagged  = []

    for name, sig in signals.items():
        is_long   = int((sig[is_mask]  ==  1).sum())
        is_short  = int((sig[is_mask]  == -1).sum())
        oos_long  = int((sig[oos_mask] ==  1).sum())
        oos_short = int((sig[oos_mask] == -1).sum())
        oos_total = oos_long + oos_short
        status    = "OK" if oos_total >= 20 else "⚠ LOW (<20)"
        if oos_total < 20:
            flagged.append(name)
        print(f"  {name:<22} {is_long:>8} {is_short:>9} {oos_long:>9} {oos_short:>10} {oos_total:>10}  {status}")

    if flagged:
        print()
        print(f"  FLAGGED (< 20 OOS fires): {', '.join(flagged)}")
        print("  These signals will be included with a LOW-SAMPLE warning.")

    # -- STEP 2: Backtest each signal -----------------------------------------
    print()
    print("-" * 72)
    print("  STEP 2 — INDIVIDUAL SIGNAL BACKTEST RESULTS")
    print("-" * 72)

    results_is  = {}
    results_oos = {}
    all_trades  = {}

    for name, sig in signals.items():
        trades = simulate_trades(df, sig)
        is_trades  = [t for t in trades if t["split"] == "IS"]
        oos_trades = [t for t in trades if t["split"] == "OOS"]
        all_trades[name] = {"is": is_trades, "oos": oos_trades}

        m_is  = calc_metrics(is_trades,  label=f"{name}_IS")
        m_oos = calc_metrics(oos_trades, label=f"{name}_OOS")
        results_is[name]  = m_is
        results_oos[name] = m_oos

    # Print OOS table
    hdr = f"  {'Signal':<22} {'Trades':>7} {'WR%':>6} {'Exp_R':>7} {'Sharpe':>7} {'MaxDD':>7} {'PF':>7} {'AvgHold':>8}  {'IS WR%':>7}  {'OOS Deg':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    rows_for_csv = []
    for name in signals:
        m   = results_oos[name]
        mis = results_is[name]
        deg = round(mis["win_rate"] - m["win_rate"], 1)
        flag = " ⚠" if name in flagged else ""
        print(
            f"  {name:<22} {m['n_trades']:>7} {m['win_rate']:>6.1f} "
            f"{m['expectancy']:>7.4f} {m['sharpe']:>7.3f} {m['max_dd']:>7.3f} "
            f"{m['profit_factor']:>7.3f} {m['avg_hold']:>8.1f}  "
            f"{mis['win_rate']:>7.1f}  {deg:>8.1f}{flag}"
        )
        rows_for_csv.append({
            "signal": name,
            "description": SIGNAL_DESCRIPTIONS.get(name, ""),
            "oos_trades": m["n_trades"],
            "oos_win_rate": m["win_rate"],
            "oos_expectancy": m["expectancy"],
            "oos_sharpe": m["sharpe"],
            "oos_max_dd": m["max_dd"],
            "oos_profit_factor": m["profit_factor"],
            "oos_avg_hold": m["avg_hold"],
            "is_win_rate": mis["win_rate"],
            "oos_degradation": deg,
            "flagged": name in flagged,
        })

    # -- STEP 3: Combinations -------------------------------------------------
    print()
    print("-" * 72)
    print("  STEP 3 — COMBINATION TEST (signals with OOS Sharpe > 0.5)")
    print("-" * 72)

    qualifying = [n for n in signals if results_oos[n]["sharpe"] > 0.5]
    print(f"  Qualifying signals: {qualifying}")
    print(f"  Testing {len(qualifying) * (len(qualifying)-1) // 2} combinations...")
    print()

    combo_results = []
    for a, b in itertools.combinations(qualifying, 2):
        combo_sig  = combine_signals(signals[a], signals[b])
        trades     = simulate_trades(df, combo_sig)
        oos_trades = [t for t in trades if t["split"] == "OOS"]
        m          = calc_metrics(oos_trades, label=f"{a} + {b}")
        if m["n_trades"] >= 5:
            ruin = monte_carlo_ruin(oos_trades)
            gh   = golden_highway_score(m["sharpe"], m["max_dd"], ruin)
            combo_results.append({
                "combo":       f"{a} & {b}",
                "sig_a":       a,
                "sig_b":       b,
                "oos_trades":  m["n_trades"],
                "win_rate":    m["win_rate"],
                "expectancy":  m["expectancy"],
                "sharpe":      m["sharpe"],
                "max_dd":      m["max_dd"],
                "profit_factor": m["profit_factor"],
                "avg_hold":    m["avg_hold"],
                "ruin_prob":   round(ruin, 4),
                "gh_score":    round(gh, 4),
            })

    combo_results.sort(key=lambda x: x["sharpe"], reverse=True)
    top10 = combo_results[:10]

    if top10:
        print(f"  {'Combination':<48} {'Trades':>7} {'WR%':>6} {'Sharpe':>7} {'MaxDD':>7} {'Ruin%':>7} {'GH Score':>9}")
        print("  " + "-" * 95)
        for r in top10:
            print(
                f"  {r['combo']:<48} {r['oos_trades']:>7} {r['win_rate']:>6.1f} "
                f"{r['sharpe']:>7.3f} {r['max_dd']:>7.3f} {r['ruin_prob']*100:>7.1f} "
                f"{r['gh_score']:>9.4f}"
            )
    else:
        print("  No qualifying combinations with >= 5 OOS trades.")

    # -- STEP 4: Golden Highway ------------------------------------------------
    print()
    print("-" * 72)
    print("  STEP 4 — GOLDEN HIGHWAY TOP 3")
    print("-" * 72)

    # Build full universe for GH scoring: all singles + all combos
    gh_universe = []

    for name in signals:
        m      = results_oos[name]
        trades = all_trades[name]["oos"]
        if m["n_trades"] < 5:
            continue
        ruin = monte_carlo_ruin(trades)
        gh   = golden_highway_score(m["sharpe"], m["max_dd"], ruin)
        gh_universe.append({
            "name":       name,
            "type":       "single",
            "n_trades":   m["n_trades"],
            "win_rate":   m["win_rate"],
            "expectancy": m["expectancy"],
            "sharpe":     m["sharpe"],
            "max_dd":     m["max_dd"],
            "profit_factor": m["profit_factor"],
            "avg_hold":   m["avg_hold"],
            "ruin_prob":  round(ruin, 4),
            "gh_score":   round(gh, 4),
            "description": SIGNAL_DESCRIPTIONS.get(name, ""),
        })

    for r in combo_results:
        gh_universe.append({
            "name":       r["combo"],
            "type":       "combination",
            "n_trades":   r["oos_trades"],
            "win_rate":   r["win_rate"],
            "expectancy": r["expectancy"],
            "sharpe":     r["sharpe"],
            "max_dd":     r["max_dd"],
            "profit_factor": r["profit_factor"],
            "avg_hold":   r["avg_hold"],
            "ruin_prob":  r["ruin_prob"],
            "gh_score":   r["gh_score"],
            "description": f"AND combination of {r['sig_a']} and {r['sig_b']}",
        })

    gh_universe.sort(key=lambda x: x["gh_score"], reverse=True)
    top3_gh = gh_universe[:3]

    for rank, g in enumerate(top3_gh, 1):
        fires_per_month = round(g["n_trades"] / (829 / (6240 / 12)), 1)
        print()
        print(f"  {'='*68}")
        print(f"  #{rank}  {g['name']}")
        print(f"  {'='*68}")
        print(f"  Type          : {g['type']}")
        print(f"  Description   : {g['description']}")
        print(f"  OOS Trades    : {g['n_trades']}")
        print(f"  Win Rate      : {g['win_rate']}%")
        print(f"  Expectancy    : {g['expectancy']:+.4f}R per trade")
        print(f"  Sharpe        : {g['sharpe']:.3f}")
        print(f"  Max Drawdown  : {g['max_dd']:.3f}R")
        print(f"  Profit Factor : {g['profit_factor']:.3f}")
        print(f"  Avg Hold      : {g['avg_hold']:.1f} bars (~{g['avg_hold']:.0f}h)")
        print(f"  Ruin Prob     : {g['ruin_prob']*100:.1f}%  (MC 1000 paths, ruin<20R)")
        print(f"  GH Score      : {g['gh_score']:.4f}  [Sharpe x (1/|MaxDD|) x (1-Ruin)]")
        print(f"  Fires/month   : ~{fires_per_month:.1f}")

    # -- STEP 5: Save outputs --------------------------------------------------
    print()
    print("-" * 72)
    print("  STEP 5 — SAVING OUTPUTS")
    print("-" * 72)

    # 5a: individual signals CSV
    df_signals = pd.DataFrame(rows_for_csv)
    p1 = OUT_DIR / "golden_highway_backtest.csv"
    df_signals.to_csv(p1, index=False)
    print(f"  Saved: {p1.name}")

    # 5b: combinations CSV
    if top10:
        df_combos = pd.DataFrame(top10)
        p2 = OUT_DIR / "golden_highway_combinations.csv"
        df_combos.to_csv(p2, index=False)
        print(f"  Saved: {p2.name}")

    # 5c: Markdown report
    _save_markdown_report(df_signals, top10, top3_gh, df)
    print(f"  Saved: golden_highway_report.md")

    # 5d: HTML report
    _save_html_report(df_signals, top10, top3_gh)
    print(f"  Saved: golden_highway_report.html")

    print()
    print("  Done.")
    print("=" * 72)


# -----------------------------------------------------------------------------
# 9. REPORT WRITERS
# -----------------------------------------------------------------------------

def _save_markdown_report(df_sigs, top10, top3_gh, df_data):
    lines = []
    lines.append("# Golden Highway Backtest — XAUUSD 1H")
    lines.append("")
    lines.append(f"**Data:** ICMARKETS_SE30, 60 — {len(df_data)} bars  ")
    lines.append(f"**Period:** Dec 2024 – Jun 2026  ")
    lines.append(f"**IS window:** bars 0–2399 | **OOS window:** bars 2400–3229 (829 bars)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Individual Signal Results (OOS)")
    lines.append("")
    lines.append("| Signal | Trades | WR% | Exp R | Sharpe | MaxDD | PF | AvgHold | IS WR% | Deg |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in df_sigs.iterrows():
        flag = " ⚠" if r["flagged"] else ""
        lines.append(
            f"| {r['signal']}{flag} | {r['oos_trades']} | {r['oos_win_rate']:.1f} "
            f"| {r['oos_expectancy']:+.4f} | {r['oos_sharpe']:.3f} | {r['oos_max_dd']:.3f} "
            f"| {r['oos_profit_factor']:.3f} | {r['oos_avg_hold']:.1f} "
            f"| {r['is_win_rate']:.1f} | {r['oos_degradation']:+.1f} |"
        )
    lines.append("")
    lines.append("> ⚠ = fewer than 20 OOS trades — statistics unreliable")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Top 10 Combinations (OOS Sharpe-ranked)")
    lines.append("")
    if top10:
        lines.append("| Combination | Trades | WR% | Sharpe | MaxDD | Ruin% | GH Score |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in top10:
            lines.append(
                f"| {r['combo']} | {r['oos_trades']} | {r['win_rate']:.1f} "
                f"| {r['sharpe']:.3f} | {r['max_dd']:.3f} "
                f"| {r['ruin_prob']*100:.1f} | {r['gh_score']:.4f} |"
            )
    else:
        lines.append("*No qualifying combinations.*")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Golden Highway Top 3")
    lines.append("")
    lines.append("**Score formula:** `GH = Sharpe x (1 / |MaxDD|) x (1 - Ruin_Probability)`")
    lines.append("")
    for rank, g in enumerate(top3_gh, 1):
        fires_per_month = round(g["n_trades"] / (829 / (6240 / 12)), 1)
        lines.append(f"### #{rank} — {g['name']}")
        lines.append("")
        lines.append(f"**What it detects:** {g['description']}")
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|---|---|")
        lines.append(f"| OOS Trades | {g['n_trades']} |")
        lines.append(f"| Win Rate | {g['win_rate']}% |")
        lines.append(f"| Expectancy | {g['expectancy']:+.4f}R |")
        lines.append(f"| Sharpe | {g['sharpe']:.3f} |")
        lines.append(f"| Max Drawdown | {g['max_dd']:.3f}R |")
        lines.append(f"| Profit Factor | {g['profit_factor']:.3f} |")
        lines.append(f"| Avg Hold | {g['avg_hold']:.1f} bars |")
        lines.append(f"| Ruin Probability | {g['ruin_prob']*100:.1f}% |")
        lines.append(f"| **GH Score** | **{g['gh_score']:.4f}** |")
        lines.append(f"| Fires/month | ~{fires_per_month:.1f} |")
        lines.append("")

    lines.append("---")
    lines.append("*Generated by research/golden_highway.py — H2 Quant v1*")

    (OUT_DIR / "golden_highway_report.md").write_text("\n".join(lines), encoding="utf-8")


def _save_html_report(df_sigs, top10, top3_gh):
    def row_color(sharpe):
        if sharpe >= 1.5: return "#1a3a1a"
        if sharpe >= 0.5: return "#1a2a1a"
        if sharpe > 0:    return "#2a2a1a"
        return "#3a1a1a"

    rows_html = ""
    for _, r in df_sigs.iterrows():
        bg   = row_color(r["oos_sharpe"])
        flag = " ⚠" if r["flagged"] else ""
        rows_html += f"""
        <tr style="background:{bg}">
          <td>{r['signal']}{flag}</td>
          <td>{r['oos_trades']}</td>
          <td>{r['oos_win_rate']:.1f}%</td>
          <td>{r['oos_expectancy']:+.4f}</td>
          <td>{r['oos_sharpe']:.3f}</td>
          <td>{r['oos_max_dd']:.3f}</td>
          <td>{r['oos_profit_factor']:.3f}</td>
          <td>{r['oos_avg_hold']:.1f}</td>
          <td>{r['is_win_rate']:.1f}%</td>
          <td>{r['oos_degradation']:+.1f}</td>
        </tr>"""

    combo_rows = ""
    for r in top10:
        bg = row_color(r["sharpe"])
        combo_rows += f"""
        <tr style="background:{bg}">
          <td>{r['combo']}</td>
          <td>{r['oos_trades']}</td>
          <td>{r['win_rate']:.1f}%</td>
          <td>{r['sharpe']:.3f}</td>
          <td>{r['max_dd']:.3f}</td>
          <td>{r['ruin_prob']*100:.1f}%</td>
          <td>{r['gh_score']:.4f}</td>
        </tr>"""

    gh_cards = ""
    medals = ["🥇", "🥈", "🥉"]
    for rank, g in enumerate(top3_gh, 1):
        fires_pm = round(g["n_trades"] / (829 / (6240 / 12)), 1)
        gh_cards += f"""
        <div style="background:#0d1f0d;border:1px solid #26a69a;border-radius:8px;padding:20px;margin:12px 0">
          <h3 style="color:#ffd700;margin:0 0 12px">{medals[rank-1]} #{rank} — {g['name']}</h3>
          <p style="color:#8b949e;margin:0 0 12px;font-style:italic">{g['description']}</p>
          <table style="width:100%;border-collapse:collapse">
            <tr><td style="color:#8b949e;padding:3px 8px">Win Rate</td><td style="color:#e6edf3;text-align:right">{g['win_rate']}%</td>
                <td style="color:#8b949e;padding:3px 8px">Sharpe</td><td style="color:#e6edf3;text-align:right">{g['sharpe']:.3f}</td></tr>
            <tr><td style="color:#8b949e;padding:3px 8px">Expectancy</td><td style="color:#3fb950;text-align:right">{g['expectancy']:+.4f}R</td>
                <td style="color:#8b949e;padding:3px 8px">Max DD</td><td style="color:#f85149;text-align:right">{g['max_dd']:.3f}R</td></tr>
            <tr><td style="color:#8b949e;padding:3px 8px">OOS Trades</td><td style="color:#e6edf3;text-align:right">{g['n_trades']}</td>
                <td style="color:#8b949e;padding:3px 8px">Ruin Prob</td><td style="color:#e6edf3;text-align:right">{g['ruin_prob']*100:.1f}%</td></tr>
            <tr><td style="color:#8b949e;padding:3px 8px">Fires/month</td><td style="color:#e6edf3;text-align:right">~{fires_pm}</td>
                <td style="color:#8b949e;padding:3px 8px"><b>GH Score</b></td><td style="color:#ffd700;text-align:right"><b>{g['gh_score']:.4f}</b></td></tr>
          </table>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Golden Highway Backtest — XAUUSD 1H</title>
<style>
  body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',system-ui,sans-serif;margin:0;padding:20px}}
  h1{{color:#ffd700;border-bottom:1px solid #30363d;padding-bottom:12px}}
  h2{{color:#58a6ff;margin-top:32px}}
  table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
  th{{background:#161b22;color:#ffd700;padding:8px 10px;text-align:left;border-bottom:1px solid #30363d}}
  td{{padding:6px 10px;border-bottom:1px solid #21262d}}
  .meta{{color:#8b949e;font-size:13px;margin:8px 0 24px}}
  .note{{color:#e3b341;font-size:12px;margin-top:8px}}
</style>
</head>
<body>
<h1>Golden Highway Backtest — XAUUSD 1H</h1>
<p class="meta">Data: ICMARKETS_SE30, 60 &nbsp;|&nbsp; 3230 bars Dec 2024 – Jun 2026 &nbsp;|&nbsp;
IS: bars 0–2399 &nbsp;|&nbsp; OOS: bars 2400–3229 (829 bars)</p>

<h2>Individual Signals — OOS Results</h2>
<table>
<thead><tr>
  <th>Signal</th><th>Trades</th><th>WR%</th><th>Exp R</th>
  <th>Sharpe</th><th>MaxDD</th><th>PF</th><th>AvgHold</th>
  <th>IS WR%</th><th>OOS Deg</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
<p class="note">⚠ = fewer than 20 OOS trades — statistics unreliable</p>

<h2>Top 10 Combinations — OOS Sharpe Ranked</h2>
<table>
<thead><tr>
  <th>Combination</th><th>Trades</th><th>WR%</th>
  <th>Sharpe</th><th>MaxDD</th><th>Ruin%</th><th>GH Score</th>
</tr></thead>
<tbody>{combo_rows if combo_rows else '<tr><td colspan="7" style="color:#8b949e">No qualifying combinations</td></tr>'}</tbody>
</table>

<h2>Golden Highway Top 3</h2>
<p style="color:#8b949e;font-size:13px">Score = Sharpe x (1 / |MaxDD|) x (1 − Ruin_Probability)</p>
{gh_cards}

<p style="color:#484f58;font-size:11px;margin-top:32px">Generated by research/golden_highway.py — H2 Quant v1</p>
</body>
</html>"""

    (OUT_DIR / "golden_highway_report.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
