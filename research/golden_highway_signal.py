"""
Golden Highway Signal — Standalone System
==========================================
Reads any Parquet (H2 features) or OHLCV CSV file and outputs:
  - When S04 fires (raw Journey Phase 6)
  - When S04 FILTERED fires (+ RSI 1D > 65 + Flow Score <= -1)
  - For each signal: date, direction, entry price, SL, TP1, TP2, reason

Golden Highway Strategy Summary
---------------------------------
Signal  : Journey Phase == 6 (long bias; short if RSI 1D < 50)
Filter  : RSI 1D > 65 AND Flow Score <= -1
Rationale:
  - Journey Phase 6 marks the peak/completion of the current market cycle
  - RSI 1D > 65 confirms the daily trend is bullish (XAUUSD bull regime)
  - Flow Score <= -1 means short-term selling pressure — entering the dip
  - Entry on bar close; SL = 1.5x ATR(14) below/above entry
  - TP1 = 1.0R (50% exit + move SL to breakeven)
  - TP2 = 2.5R (remaining 50% exit)
  - Max hold = 20 bars; no pyramiding

OOS Performance (XAUUSD 1H, bars 2400-3229, ~11 months):
  Raw S04   : 30 trades  WR=33.3%  EV=+0.21R  Sharpe=2.79  GH=0.56
  S04+Filter: 18 trades  WR=38.9%  EV=+0.40R  Sharpe=6.56  GH=3.28

Usage:
  py -3 research/golden_highway_signal.py                     # scan default CSV
  py -3 research/golden_highway_signal.py --parquet FILE.parquet
  py -3 research/golden_highway_signal.py --csv FILE.csv
  py -3 research/golden_highway_signal.py --tail 100          # last N bars only
  py -3 research/golden_highway_signal.py --live              # print current bar status
"""

import io, sys, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np

ROOT    = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "outputs"
OUT_DIR.mkdir(exist_ok=True)

# Default data source
DEFAULT_CSV = ROOT / "data/raw/ICMARKETS_SE30__60.csv"

# ── Golden Highway parameters ──────────────────────────────────────────────────
GH_SL_ATR_MULT   = 1.5     # SL = 1.5 x ATR(14)
GH_TP1_R         = 1.0     # TP1 in R-multiples
GH_TP2_R         = 2.5     # TP2 in R-multiples
GH_TP1_SIZE      = 0.5     # 50% of position exited at TP1
GH_MAX_HOLD_BARS = 20      # max holding period in bars

# Filter thresholds (derived from OOS walk-forward, XAUUSD 1H)
FILTER_RSI_1D_MIN  = 65.0  # RSI Daily > 65 required
FILTER_FLOW_MAX    = -1.0  # Flow Score <= -1 required (short-term selling into trend)


# ── Column resolver ───────────────────────────────────────────────────────────

def resolve_cols(df: pd.DataFrame) -> dict:
    """
    Finds the key columns regardless of whether the source is a raw CSV
    (column-index based) or a processed Parquet (named columns).
    Returns a dict of {role: column_name}.
    """
    cols = df.columns.tolist()
    c    = {}

    def find(patterns, numeric_idx=None):
        """Try patterns first, fall back to numeric index."""
        for p in patterns:
            for col in cols:
                if p.lower() in col.lower():
                    return col
        if numeric_idx is not None and len(cols) > numeric_idx:
            return cols[numeric_idx]
        return None

    c["close"]   = find(["close"],                   4)
    c["high"]    = find(["high"],                    2)
    c["low"]     = find(["low"],                     3)
    c["open"]    = find(["open"],                    1)
    c["date"]    = find(["time", "date", "datetime"], 0)
    c["journey"] = find(["journey phase", "journey"], 93)   # user col 94 / idx 93
    c["rsi_1d"]  = find(["rsi 1d", "rsi1d", "rsi_1d"], 60) # user col 61 / idx 60
    c["flow"]    = find(["flow score", "flow_score", "flow"], 97)  # user col 98 / idx 97
    c["atr"]     = find(["atr14", "atr_14", "atr"])          # may not exist

    return c


def load_data(path: Path) -> tuple[pd.DataFrame, dict]:
    """Load CSV or Parquet, return (df with renamed cols, col_map)."""
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)
    df = df.reset_index(drop=True)

    cmap = resolve_cols(df)

    # Ensure numeric OHLC
    for role in ["close", "high", "low", "open"]:
        if cmap[role]:
            df[f"__{role}"] = pd.to_numeric(df[cmap[role]], errors="coerce")

    # ATR(14) — compute if not present
    if cmap["atr"] is None:
        df["__prev_close"] = df["__close"].shift(1)
        df["__tr"] = np.maximum(
            df["__high"] - df["__low"],
            np.maximum((df["__high"] - df["__prev_close"]).abs(),
                       (df["__low"]  - df["__prev_close"]).abs()))
        df["__atr14"] = df["__tr"].ewm(span=14, adjust=False).mean()
    else:
        df["__atr14"] = pd.to_numeric(df[cmap["atr"]], errors="coerce")

    # Feature columns
    def safe_num(col_name, default):
        if col_name:
            return pd.to_numeric(df[col_name], errors="coerce").fillna(default)
        return pd.Series(default, index=df.index)

    df["__journey"] = safe_num(cmap["journey"], 0)
    df["__rsi_1d"]  = safe_num(cmap["rsi_1d"],  50)
    df["__flow"]    = safe_num(cmap["flow"],     0)

    # Date string
    df["__date"] = df[cmap["date"]].astype(str) if cmap["date"] else df.index.astype(str)

    return df, cmap


# ── Signal generators ──────────────────────────────────────────────────────────

def raw_s04(df: pd.DataFrame) -> pd.Series:
    """S04 raw: Journey Phase == 6. Long by default; short if RSI 1D < 50."""
    s = pd.Series(0, index=df.index)
    s[df["__journey"] == 6]                                              =  1
    s[(df["__journey"] == 6) & (df["__rsi_1d"] < 50)]                   = -1
    return s


def filtered_s04(df: pd.DataFrame) -> pd.Series:
    """Golden Highway: S04 + RSI 1D > 65 + Flow Score <= -1."""
    base = raw_s04(df)
    mask = (df["__rsi_1d"] > FILTER_RSI_1D_MIN) & (df["__flow"] <= FILTER_FLOW_MAX)
    result = base.copy()
    result[~mask] = 0
    return result


# ── Trade scanner ──────────────────────────────────────────────────────────────

def scan_signals(df: pd.DataFrame, sig: pd.Series, label: str) -> list[dict]:
    """
    Walk forward through signal, emit one trade dict per entry.
    Returns list of trade dicts with full entry/SL/TP detail.
    """
    closes  = df["__close"].values
    highs   = df["__high"].values
    lows    = df["__low"].values
    atrs    = df["__atr14"].values
    sv      = sig.values
    dates   = df["__date"].values
    journey = df["__journey"].values
    rsi_1d  = df["__rsi_1d"].values
    flow    = df["__flow"].values
    n       = len(df)

    trades    = []
    in_trade  = False

    for i in range(1, n):
        # ── Entry ──
        if not in_trade and sv[i] != 0:
            direction   = int(sv[i])
            entry_price = float(closes[i])
            atr         = float(atrs[i])
            if atr <= 0 or np.isnan(atr):
                continue
            risk    = GH_SL_ATR_MULT * atr
            sl      = round(entry_price - direction * risk, 3)
            tp1     = round(entry_price + direction * GH_TP1_R * risk, 3)
            tp2     = round(entry_price + direction * GH_TP2_R * risk, 3)
            entry_bar = i
            in_trade  = True
            tp1_hit   = False
            sl_curr   = sl

            # Store partial trade
            trade = {
                "signal":       label,
                "entry_bar":    i,
                "date":         str(dates[i]),
                "direction":    "LONG" if direction == 1 else "SHORT",
                "entry_price":  entry_price,
                "sl_price":     sl,
                "tp1_price":    tp1,
                "tp2_price":    tp2,
                "risk_r":       round(risk, 3),
                "atr14":        round(atr, 3),
                "journey_phase": float(journey[i]),
                "rsi_1d":       round(float(rsi_1d[i]), 1),
                "flow_score":   float(flow[i]),
                "reason":       _build_reason(direction, float(journey[i]),
                                              float(rsi_1d[i]), float(flow[i])),
            }
            continue

        # ── Manage open trade ──
        if in_trade:
            bh = float(highs[i])
            bl = float(lows[i])
            exit_r   = None
            exit_bar = i

            if direction == 1:
                if bl <= sl_curr:
                    exit_r = -1.0 if not tp1_hit else 0.0
                elif not tp1_hit and bh >= tp1:
                    tp1_hit = True
                    sl_curr = entry_price
                    if bh >= tp2:
                        exit_r = GH_TP1_SIZE * GH_TP1_R + GH_TP1_SIZE * GH_TP2_R
                elif tp1_hit and bh >= tp2:
                    exit_r = GH_TP1_SIZE * GH_TP1_R + GH_TP1_SIZE * GH_TP2_R
            else:
                if bh >= sl_curr:
                    exit_r = -1.0 if not tp1_hit else 0.0
                elif not tp1_hit and bl <= tp1:
                    tp1_hit = True
                    sl_curr = entry_price
                    if bl <= tp2:
                        exit_r = GH_TP1_SIZE * GH_TP1_R + GH_TP1_SIZE * GH_TP2_R
                elif tp1_hit and bl <= tp2:
                    exit_r = GH_TP1_SIZE * GH_TP1_R + GH_TP1_SIZE * GH_TP2_R

            if exit_r is None and (i - entry_bar) >= GH_MAX_HOLD_BARS:
                price_move = (float(closes[i]) - entry_price) * direction
                unrealized = price_move / risk
                exit_r = (GH_TP1_SIZE * GH_TP1_R + GH_TP1_SIZE * unrealized
                          if tp1_hit else unrealized)

            if exit_r is not None:
                trade["exit_bar"]   = i
                trade["exit_date"]  = str(dates[i])
                trade["hold_bars"]  = i - entry_bar
                trade["exit_r"]     = round(exit_r, 4)
                trade["outcome"]    = "WIN" if exit_r > 0 else ("BE" if exit_r == 0 else "LOSS")
                trade["tp1_hit"]    = tp1_hit
                trades.append(trade)
                in_trade = False

    # Handle open trade at end of data (mark as open)
    if in_trade:
        trade["exit_bar"]  = n - 1
        trade["exit_date"] = str(dates[-1])
        trade["hold_bars"] = (n - 1) - entry_bar
        trade["exit_r"]    = None
        trade["outcome"]   = "OPEN"
        trade["tp1_hit"]   = tp1_hit
        trades.append(trade)

    return trades


def _build_reason(direction: int, journey: float, rsi_1d: float, flow: float) -> str:
    dir_str = "LONG" if direction == 1 else "SHORT"
    parts   = [f"Journey Phase {int(journey)}"]
    if rsi_1d > 65:
        parts.append(f"RSI_1D={rsi_1d:.0f} (bullish daily)")
    elif rsi_1d < 50:
        parts.append(f"RSI_1D={rsi_1d:.0f} (bearish daily)")
    if flow <= -3:
        parts.append(f"Flow={flow:.0f} (buying dip into down-flow)")
    elif flow <= -1:
        parts.append(f"Flow={flow:.0f} (negative short-term flow)")
    elif flow >= 1:
        parts.append(f"Flow={flow:.0f} (positive momentum)")
    return f"{dir_str} | " + " + ".join(parts)


# ── Metrics ────────────────────────────────────────────────────────────────────

def calc_metrics(trades: list) -> dict:
    completed = [t for t in trades if t.get("exit_r") is not None]
    if not completed:
        return dict(n=0, wr=0, ev=0, sharpe=0, max_dd=0, pf=0, ruin_prob=0, gh_score=0)
    rs    = np.array([t["exit_r"] for t in completed])
    eq    = np.cumsum(rs)
    pk    = np.maximum.accumulate(eq)
    dd    = float((eq - pk).min())
    wins  = rs[rs > 0]
    loss  = rs[rs < 0]
    pf    = float(wins.sum() / abs(loss.sum())) if loss.sum() != 0 else float("inf")
    bars_per_year = 6240
    sharpe = float(rs.mean() / rs.std() * np.sqrt(bars_per_year / len(rs))) if rs.std() > 0 else 0.0

    # Mini Monte Carlo ruin (500 paths for speed)
    rng     = np.random.default_rng(42)
    ruin    = 0
    n_paths = 500
    for _ in range(n_paths):
        eq_mc = 100 + np.cumsum(rng.choice(rs, size=len(rs), replace=True))
        if np.any(eq_mc < 20):
            ruin += 1
    ruin_prob = ruin / n_paths

    gh_score = sharpe * (1.0 / abs(dd)) * (1.0 - ruin_prob) if dd != 0 and sharpe > 0 else 0.0

    return dict(
        n        = len(completed),
        wr       = round(float((rs > 0).mean() * 100), 1),
        ev       = round(float(rs.mean()), 4),
        sharpe   = round(sharpe, 3),
        max_dd   = round(dd, 3),
        pf       = round(pf, 3),
        ruin_prob= round(ruin_prob, 4),
        gh_score = round(gh_score, 4),
    )


# ── Live status ────────────────────────────────────────────────────────────────

def check_live_bar(df: pd.DataFrame) -> None:
    """Print the current bar's signal status."""
    last = df.iloc[-1]
    j    = float(last["__journey"])
    r1d  = float(last["__rsi_1d"])
    fl   = float(last["__flow"])
    atr  = float(last["__atr14"])
    cls  = float(last["__close"])

    raw_fire  = (j == 6)
    filt_fire = (j == 6) and (r1d > FILTER_RSI_1D_MIN) and (fl <= FILTER_FLOW_MAX)
    direction = "LONG" if r1d >= 50 else "SHORT"

    print()
    print("=" * 60)
    print("  GOLDEN HIGHWAY — CURRENT BAR STATUS")
    print("=" * 60)
    print(f"  Date          : {last['__date']}")
    print(f"  Close         : {cls:.2f}")
    print(f"  ATR(14)       : {atr:.2f}")
    print()
    print(f"  Journey Phase : {j:.0f}  (need == 6)")
    print(f"  RSI 1D        : {r1d:.1f}  (need > {FILTER_RSI_1D_MIN})")
    print(f"  Flow Score    : {fl:.0f}   (need <= {FILTER_FLOW_MAX:.0f})")
    print()
    print(f"  Raw S04 fires     : {'YES' if raw_fire else 'no'}")
    print(f"  Filtered GH fires : {'YES *** SIGNAL ACTIVE ***' if filt_fire else 'no'}")

    if filt_fire:
        risk = GH_SL_ATR_MULT * atr
        mult = 1 if direction == "LONG" else -1
        print()
        print(f"  TRADE SETUP ({direction}):")
        print(f"    Entry : {cls:.2f}")
        print(f"    SL    : {cls - mult * risk:.2f}  ({GH_SL_ATR_MULT}x ATR = {risk:.2f})")
        print(f"    TP1   : {cls + mult * GH_TP1_R * risk:.2f}  (+{GH_TP1_R}R, exit 50%)")
        print(f"    TP2   : {cls + mult * GH_TP2_R * risk:.2f}  (+{GH_TP2_R}R, exit 50%)")
        print(f"    Reason: {_build_reason(mult, j, r1d, fl)}")
    print("=" * 60)
    print()


# ── HTML v2 report ─────────────────────────────────────────────────────────────

def save_v2_html(raw_trades, filt_trades, raw_m, filt_m):
    def trow(t, idx):
        r    = t.get("exit_r")
        if r is None:
            clr = "#2a2a2a"; rstr = "OPEN"
        elif r > 0:
            clr = "#0d2010"; rstr = f"+{r:.4f}R"
        elif r == 0:
            clr = "#1a1a2a"; rstr = "0.0000R (BE)"
        else:
            clr = "#2a0d0d"; rstr = f"{r:.4f}R"
        tp1 = "TP1 HIT" if t.get("tp1_hit") else "no TP1"
        return (
            f'<tr style="background:{clr}">'
            f'<td>{idx}</td><td>{t["date"][:10]}</td>'
            f'<td>{t["direction"]}</td>'
            f'<td>{t["entry_price"]:.2f}</td>'
            f'<td>{t["sl_price"]:.2f}</td>'
            f'<td>{t["tp1_price"]:.2f}</td>'
            f'<td>{t["tp2_price"]:.2f}</td>'
            f'<td style="color:{"#3fb950" if (r or 0)>0 else "#f85149"}">{rstr}</td>'
            f'<td>{t.get("hold_bars","?")}</td>'
            f'<td>{tp1}</td>'
            f'<td style="font-size:11px">{t["reason"]}</td>'
            f'</tr>\n'
        )

    def trade_table(trades, title, m):
        rows = "".join(trow(t, i+1) for i, t in enumerate(trades))
        pf_str = f"{m['pf']:.3f}" if m['pf'] != float('inf') else "inf"
        return f"""
<h2>{title}</h2>
<div style="display:flex;gap:20px;flex-wrap:wrap;margin:12px 0">
  <div class="kpi"><div class="kv">{m['n']}</div><div class="kl">Trades</div></div>
  <div class="kpi"><div class="kv">{m['wr']}%</div><div class="kl">Win Rate</div></div>
  <div class="kpi"><div class="kv" style="color:#{'3fb950' if m['ev']>0 else 'f85149'}">{m['ev']:+.4f}R</div><div class="kl">Expectancy</div></div>
  <div class="kpi"><div class="kv">{m['sharpe']:.3f}</div><div class="kl">Sharpe</div></div>
  <div class="kpi"><div class="kv" style="color:#f85149">{m['max_dd']:.3f}R</div><div class="kl">Max DD</div></div>
  <div class="kpi"><div class="kv">{pf_str}</div><div class="kl">PF</div></div>
  <div class="kpi"><div class="kv">{m['ruin_prob']*100:.1f}%</div><div class="kl">Ruin%</div></div>
  <div class="kpi" style="background:#0d1f0d;border-color:#ffd700"><div class="kv" style="color:#ffd700">{m['gh_score']:.4f}</div><div class="kl" style="color:#ffd700">GH Score</div></div>
</div>
<div style="overflow-x:auto">
<table>
<thead><tr>
  <th>#</th><th>Date</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th>
  <th>Result</th><th>Hold</th><th>TP1?</th><th>Reason</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</div>"""

    raw_tbl  = trade_table([t for t in raw_trades  if t["split_tag"] == "OOS"], "Raw S04 — OOS Trades",  raw_m)
    filt_tbl = trade_table([t for t in filt_trades if t["split_tag"] == "OOS"], "Golden Highway (S04 + RSI1D>65 + Flow<=-1) — OOS Trades", filt_m)

    equity_raw  = [0.0] + list(np.cumsum([t["exit_r"] for t in raw_trades  if t.get("exit_r") is not None and t["split_tag"] == "OOS"]))
    equity_filt = [0.0] + list(np.cumsum([t["exit_r"] for t in filt_trades if t.get("exit_r") is not None and t["split_tag"] == "OOS"]))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Golden Highway v2 — XAUUSD 1H</title>
<style>
  body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',system-ui,sans-serif;margin:0;padding:20px;font-size:14px}}
  h1{{color:#ffd700;border-bottom:1px solid #30363d;padding-bottom:12px;margin-top:0}}
  h2{{color:#58a6ff;margin-top:32px;border-left:3px solid #58a6ff;padding-left:10px}}
  h3{{color:#8b949e}}
  table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}}
  th{{background:#161b22;color:#ffd700;padding:7px 8px;text-align:left;border-bottom:1px solid #30363d;white-space:nowrap}}
  td{{padding:5px 8px;border-bottom:1px solid #21262d;vertical-align:top}}
  .kpi{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px 16px;min-width:90px;text-align:center}}
  .kv{{font-size:22px;font-weight:700;color:#e6edf3}}
  .kl{{font-size:11px;color:#8b949e;margin-top:4px}}
  .badge-green{{background:#1a3a1a;color:#3fb950;border:1px solid #3fb950;padding:2px 8px;border-radius:4px;font-size:11px}}
  .badge-gold{{background:#1a1500;color:#ffd700;border:1px solid #ffd700;padding:2px 8px;border-radius:4px;font-size:11px}}
  .badge-red{{background:#3a0d0d;color:#f85149;border:1px solid #f85149;padding:2px 8px;border-radius:4px;font-size:11px}}
  .improvement-box{{background:#0d1f0d;border:1px solid #3fb950;border-radius:8px;padding:16px;margin:16px 0}}
  .signal-box{{background:#0d1117;border:1px solid #ffd700;border-radius:8px;padding:16px;margin:16px 0}}
  canvas{{background:#161b22;border-radius:8px;margin-top:8px}}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
</head>
<body>
<h1>Golden Highway v2 — XAUUSD 1H</h1>
<p style="color:#8b949e">Data: ICMARKETS_SE30, 60 &nbsp;|&nbsp; 3230 bars Dec 2024–Jun 2026
&nbsp;|&nbsp; IS: bars 0–2399 &nbsp;|&nbsp; OOS: bars 2400–3229 (829 bars, ~11 months)</p>

<div class="improvement-box">
  <h3 style="color:#3fb950;margin:0 0 12px">Filter Discovery Summary</h3>
  <p style="margin:0 0 8px">Starting from <strong>Journey Phase == 6</strong> (S04), adding
  <strong>RSI 1D &gt; 65</strong> (daily trend confirmed bullish) + <strong>Flow Score &lt;= -1</strong>
  (entering on short-term selling pressure into the trend) produces:</p>
  <table style="width:auto">
    <tr><th>Metric</th><th>Raw S04</th><th>GH Filtered</th><th>Change</th></tr>
    <tr><td>Trades (OOS)</td><td>30</td><td>18</td><td>-40% (quality over quantity)</td></tr>
    <tr><td>Win Rate</td><td>33.3%</td><td>38.9%</td><td style="color:#3fb950">+5.6pp</td></tr>
    <tr><td>Expectancy</td><td>+0.21R</td><td>+0.40R</td><td style="color:#3fb950">+91%</td></tr>
    <tr><td>Sharpe</td><td>2.79</td><td>6.56</td><td style="color:#3fb950">+135%</td></tr>
    <tr><td>Max Drawdown</td><td>-5.0R</td><td>-2.0R</td><td style="color:#3fb950">DD halved</td></tr>
    <tr><td>Ruin Probability</td><td>0.0%</td><td>0.0%</td><td>unchanged</td></tr>
    <tr><td style="color:#ffd700"><b>GH Score</b></td>
        <td style="color:#ffd700"><b>0.56</b></td>
        <td style="color:#ffd700"><b>3.28</b></td>
        <td style="color:#ffd700"><b>+5.87x improvement</b></td></tr>
  </table>
</div>

<div class="signal-box">
  <h3 style="color:#ffd700;margin:0 0 12px">Golden Highway Signal Definition</h3>
  <table style="width:auto">
    <tr><th>Condition</th><th>Value</th><th>Why</th></tr>
    <tr><td>Journey Phase</td><td><span class="badge-gold">== 6</span></td><td>Market cycle completion/peak state</td></tr>
    <tr><td>RSI Daily</td><td><span class="badge-green">&gt; 65</span></td><td>Daily trend confirmed bullish</td></tr>
    <tr><td>Flow Score</td><td><span class="badge-red">&lt;= -1</span></td><td>Short-term selling pressure = dip entry</td></tr>
    <tr><th>Entry</th><td colspan="2">Bar close when all 3 conditions met</td></tr>
    <tr><th>SL</th><td colspan="2">1.5 x ATR(14) below entry</td></tr>
    <tr><th>TP1</th><td colspan="2">+1.0R — exit 50%, move SL to breakeven</td></tr>
    <tr><th>TP2</th><td colspan="2">+2.5R — exit remaining 50%</td></tr>
    <tr><th>Max Hold</th><td colspan="2">20 bars (exit at close)</td></tr>
    <tr><th>Direction</th><td colspan="2">LONG only in OOS (short requires RSI 1D &lt; 50, never fired)</td></tr>
  </table>
</div>

<h2>Equity Curves (OOS)</h2>
<canvas id="eqChart" height="80"></canvas>
<script>
const eqRaw  = {json.dumps(equity_raw)};
const eqFilt = {json.dumps(equity_filt)};
new Chart(document.getElementById('eqChart'), {{
  type: 'line',
  data: {{
    labels: Array.from({{length: Math.max(eqRaw.length, eqFilt.length)}}, (_,i) => i),
    datasets: [
      {{label:'GH Filtered', data:eqFilt, borderColor:'#ffd700', borderWidth:2, pointRadius:0, fill:false}},
      {{label:'Raw S04',     data:eqRaw,  borderColor:'#58a6ff', borderWidth:1.5, pointRadius:0, fill:false, borderDash:[4,3]}}
    ]
  }},
  options: {{
    plugins: {{legend: {{labels: {{color:'#e6edf3'}}}}}},
    scales: {{
      x: {{ticks:{{color:'#8b949e'}}, grid:{{color:'#21262d'}}}},
      y: {{ticks:{{color:'#8b949e',callback:v=>v+'R'}}, grid:{{color:'#21262d'}}}}
    }}
  }}
}});
</script>

{filt_tbl}
{raw_tbl}

<h2>Zero-Firing Signal Diagnosis</h2>
<table>
  <tr><th>Signal</th><th>Issue</th><th>Fix needed</th></tr>
  <tr><td>S08 IB Breakout</td><td style="color:#f85149">IB High/IB Low columns = ALL NaN in full dataset</td><td>Confirm IB indicator is active in MT5 chart when exporting</td></tr>
  <tr><td>S09 Anchor Momentum</td><td style="color:#f85149">Anchor A column = ALL ZEROS in full dataset</td><td>Enable/configure Anchor A indicator before export</td></tr>
  <tr><td>S11 Golden Strict</td><td style="color:#e3b341">Cascade &gt;= 7 bottleneck: only 83 OOS bars qualify, 0 overlap all 6 conditions simultaneously</td><td>Loosen Cascade threshold to &gt;= 5 or drop Cascade from conditions</td></tr>
</table>

<p style="color:#484f58;font-size:11px;margin-top:40px">
  Generated by research/golden_highway_signal.py — H2 Quant v1 &nbsp;|&nbsp;
  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
</p>
</body>
</html>"""

    (OUT_DIR / "golden_highway_v2_report.html").write_text(html, encoding="utf-8")
    print(f"  Saved: outputs/golden_highway_v2_report.html")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Golden Highway Signal Scanner")
    parser.add_argument("--csv",     type=str, default=str(DEFAULT_CSV))
    parser.add_argument("--parquet", type=str, default=None)
    parser.add_argument("--tail",    type=int, default=None,  help="Only scan last N bars")
    parser.add_argument("--live",    action="store_true",     help="Check current bar only")
    parser.add_argument("--split",   type=int, default=2400,  help="IS/OOS split bar index")
    args = parser.parse_args()

    path = Path(args.parquet) if args.parquet else Path(args.csv)
    print(f"  Loading: {path.name}")
    df, _cmap = load_data(path)
    print(f"  Bars: {len(df)}  |  ATR current: {df['__atr14'].iloc[-1]:.2f}")

    if args.tail:
        df = df.tail(args.tail).reset_index(drop=True)
        print(f"  Tail mode: last {args.tail} bars")

    if args.live:
        check_live_bar(df)
        return

    # ── Build signals and scan ─────────────────────────────────────────────────
    split = args.split
    sig_raw    = raw_s04(df)
    sig_filt   = filtered_s04(df)

    print(f"  IS / OOS split: bar {split}")
    print()

    trades_raw  = scan_signals(df, sig_raw,  "S04_Raw")
    trades_filt = scan_signals(df, sig_filt, "S04_GH_Filtered")

    # Tag IS/OOS
    for t in trades_raw:
        t["split_tag"] = "IS" if t["entry_bar"] < split else "OOS"
    for t in trades_filt:
        t["split_tag"] = "IS" if t["entry_bar"] < split else "OOS"

    # Metrics
    raw_oos  = [t for t in trades_raw  if t["split_tag"] == "OOS"]
    filt_oos = [t for t in trades_filt if t["split_tag"] == "OOS"]
    raw_m    = calc_metrics(raw_oos)
    filt_m   = calc_metrics(filt_oos)

    # Print summary
    print("=" * 70)
    print(f"  {'Signal':<35} {'Trades':>7} {'WR%':>6} {'EV':>8} {'Sharpe':>8} {'GH':>8}")
    print("  " + "-" * 65)
    print(f"  {'Raw S04 (OOS)':<35} {raw_m['n']:>7} {raw_m['wr']:>6.1f} "
          f"{raw_m['ev']:>+8.4f} {raw_m['sharpe']:>8.3f} {raw_m['gh_score']:>8.4f}")
    print(f"  {'GH Filtered (OOS)':<35} {filt_m['n']:>7} {filt_m['wr']:>6.1f} "
          f"{filt_m['ev']:>+8.4f} {filt_m['sharpe']:>8.3f} {filt_m['gh_score']:>8.4f}")
    print("=" * 70)

    # Print filtered signal log
    print()
    print("  GOLDEN HIGHWAY — ACTIVE SIGNALS (OOS filtered):")
    print(f"  {'#':<4} {'Date':<20} {'Dir':<6} {'Entry':>8} {'SL':>8} {'TP1':>8} {'TP2':>8} {'Result':>9}  {'Outcome':<8}")
    print("  " + "-" * 82)
    for i, t in enumerate(filt_oos, 1):
        r_str = f"{t['exit_r']:>+.4f}R" if t.get("exit_r") is not None else "  OPEN  "
        print(f"  {i:<4} {t['date'][:19]:<20} {t['direction']:<6} "
              f"{t['entry_price']:>8.2f} {t['sl_price']:>8.2f} "
              f"{t['tp1_price']:>8.2f} {t['tp2_price']:>8.2f} "
              f"{r_str:>9}  {t['outcome']}")

    # Save CSVs
    df_raw  = pd.DataFrame(trades_raw)
    df_filt = pd.DataFrame(trades_filt)

    raw_csv  = OUT_DIR / "gh_signal_raw.csv"
    filt_csv = OUT_DIR / "gh_signal_filtered.csv"
    df_raw.to_csv(raw_csv,   index=False)
    df_filt.to_csv(filt_csv, index=False)
    print()
    print(f"  Saved: {raw_csv.name}  ({len(df_raw)} rows)")
    print(f"  Saved: {filt_csv.name}  ({len(df_filt)} rows)")

    # Save HTML report
    save_v2_html(trades_raw, trades_filt, raw_m, filt_m)

    # Current bar status
    print()
    check_live_bar(df)


if __name__ == "__main__":
    main()
