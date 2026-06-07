"""
H2 Quant v1 — Phase 5: Research Validator
Per-state statistical edge, walk-forward validation, Monte Carlo simulation.
Strict no-lookahead. Minimum 30 samples per state.

Input:  states/H2_states_{instrument}_{tf}.parquet
        outputs/H2_transition_matrix_{instrument}_{tf}.json
Output: outputs/H2_state_stats.json          (per-instrument)
        outputs/H2_backtest_report.html       (per-instrument)
"""

import json
import logging
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
with open(ROOT / "config.yaml") as f:
    CFG = yaml.safe_load(f)

LOG_DIR = ROOT / CFG["paths"]["logs"]
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, CFG["system"]["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "validator.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("H2.validator")

OUTPUTS_DIR = ROOT / CFG["paths"]["outputs"]
STATES_DIR  = ROOT / "states"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

MIN_SAMPLES    = CFG["statistics"]["min_samples"]       # 30
OOS_FRACTION   = CFG["statistics"]["oos_fraction"]      # 0.20
MC_PATHS       = CFG["statistics"]["monte_carlo_paths"] # 10000
HOLD_BARS      = 5          # forward look window for WR/EV
ATR_PERIOD     = CFG["features"]["atr_period"]          # 14
OOS_DEGRADE_LIMIT = 0.15    # flag state if OOS WR drops > 15pp


# ── helpers ──────────────────────────────────────────────────────────────────

def _wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half   = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return (max(0.0, round(centre - half, 4)),
            min(1.0, round(centre + half, 4)))


def _atr_series(df: pd.DataFrame) -> pd.Series:
    """Recompute ATR(14) from OHLCV columns."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=ATR_PERIOD - 1, adjust=False).mean()


def _trend_from_state(state_id: str) -> str:
    """Extract the TREND dimension from a composite state_id."""
    parts = state_id.split("_")
    return parts[0] if parts else "NEUTRAL"


def _session_from_row(row: pd.Series) -> str:
    for col in ("state_session", "d06_session__value"):
        if col in row.index and isinstance(row[col], str):
            return row[col].upper()
    return "UNKNOWN"


# ── Class 1: StatePerformanceCalculator ──────────────────────────────────────

class StatePerformanceCalculator:

    def __init__(self, hold_bars: int = HOLD_BARS, min_samples: int = MIN_SAMPLES):
        self.hold_bars   = hold_bars
        self.min_samples = min_samples

    # ── core trade outcome computation ────────────────────────────────────

    def _compute_outcomes(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        For every bar, compute forward-looking trade outcomes.
        Direction determined by TREND dimension of state_id.
        R unit = ATR(14) at signal bar.
        Returns df with added columns: direction, r_outcome, mae_r, mfe_r, is_win
        """
        close  = df["close"].values
        high   = df["high"].values
        low    = df["low"].values
        atr    = _atr_series(df).values
        states = df["state_id"].values
        n      = len(df)
        h      = self.hold_bars

        direction  = np.array([_trend_from_state(s) for s in states])
        r_outcome  = np.full(n, np.nan)
        mae_r      = np.full(n, np.nan)
        mfe_r      = np.full(n, np.nan)

        for i in range(n - h):
            if atr[i] <= 0 or np.isnan(atr[i]):
                continue
            d = direction[i]
            if d not in ("BULL", "BEAR"):
                continue

            entry    = close[i]
            fwd_h    = high[i + 1: i + h + 1]
            fwd_l    = low[i  + 1: i + h + 1]
            fwd_c    = close[i + h]
            r        = atr[i]

            if d == "BULL":
                r_outcome[i] = (fwd_c  - entry) / r
                mfe_r[i]     = (np.max(fwd_h) - entry) / r
                mae_r[i]     = (np.min(fwd_l) - entry) / r   # negative = adverse
            else:  # BEAR
                r_outcome[i] = (entry  - fwd_c)  / r
                mfe_r[i]     = (entry  - np.min(fwd_l)) / r
                mae_r[i]     = (entry  - np.max(fwd_h)) / r  # negative = adverse

        out = df.copy()
        out["_direction"] = direction
        out["_r_outcome"] = r_outcome
        out["_mfe_r"]     = mfe_r
        out["_mae_r"]     = mae_r
        out["_is_win"]    = r_outcome > 0
        return out

    # ── per-state stats ───────────────────────────────────────────────────

    def _state_stats_from_outcomes(self, grp: pd.DataFrame) -> dict:
        """Compute stats for one state's group of outcome rows."""
        valid = grp.dropna(subset=["_r_outcome"])
        n     = len(valid)
        if n < self.min_samples:
            return {}

        wins     = valid[valid["_is_win"]]
        losses   = valid[~valid["_is_win"]]
        n_wins   = len(wins)
        n_losses = len(losses)
        wr       = n_wins / n

        avg_win_r  = wins["_r_outcome"].mean()   if n_wins   > 0 else 0.0
        avg_loss_r = losses["_r_outcome"].abs().mean() if n_losses > 0 else 0.0

        ev = (wr * avg_win_r) - ((1 - wr) * avg_loss_r)

        total_win_r  = wins["_r_outcome"].sum()
        total_loss_r = losses["_r_outcome"].abs().sum()
        pf = (total_win_r / total_loss_r) if total_loss_r > 0 else float("inf")

        avg_r = valid["_r_outcome"].mean()
        mae   = valid["_mae_r"].mean()
        mfe   = valid["_mfe_r"].mean()

        ci_lo, ci_hi = _wilson_ci(n_wins, n)

        return {
            "win_rate":         round(wr,       4),
            "ev":               round(ev,       4),
            "profit_factor":    round(min(pf, 99.0), 3),
            "avg_r":            round(avg_r,    4),
            "mae":              round(mae,      4),
            "mfe":              round(mfe,      4),
            "sample_count":     n,
            "ci_95_low":        ci_lo,
            "ci_95_high":       ci_hi,
            "avg_win_r":        round(avg_win_r,  4),
            "avg_loss_r":       round(avg_loss_r, 4),
        }

    # ── session breakdown ─────────────────────────────────────────────────

    def _session_breakdown(self, grp: pd.DataFrame) -> dict:
        sess_col = "state_session" if "state_session" in grp.columns else None
        if sess_col is None:
            return {}
        breakdown = {}
        for sess in ("TOKYO", "LONDON", "OVERLAP", "NY"):
            sub = grp[grp[sess_col] == sess]
            valid = sub.dropna(subset=["_r_outcome"])
            n = len(valid)
            if n < 5:
                continue
            n_wins = (valid["_is_win"]).sum()
            wr = n_wins / n
            avg_win  = valid.loc[valid["_is_win"],  "_r_outcome"].mean() if n_wins > 0 else 0.0
            avg_loss = valid.loc[~valid["_is_win"], "_r_outcome"].abs().mean() if (n - n_wins) > 0 else 0.0
            ev = wr * avg_win - (1 - wr) * avg_loss
            breakdown[sess.lower()] = {
                "wr":      round(wr,  4),
                "ev":      round(ev,  4),
                "samples": n,
            }
        return breakdown

    # ── transition pair stats ─────────────────────────────────────────────

    def _transition_pair_stats(self, df: pd.DataFrame) -> dict:
        """For each (from_state → to_state) pair, compute EV of that sequence."""
        pairs  = {}
        states = df["state_id"].values
        for i in range(len(df) - 1):
            if np.isnan(df["_r_outcome"].iloc[i + 1]):
                continue
            pair_key = f"{states[i]}|{states[i+1]}"
            if pair_key not in pairs:
                pairs[pair_key] = []
            pairs[pair_key].append(df["_r_outcome"].iloc[i + 1])

        results = {}
        for pair, outcomes in pairs.items():
            if len(outcomes) < 10:
                continue
            outcomes = np.array(outcomes)
            results[pair] = {
                "ev":      round(float(np.mean(outcomes)), 4),
                "samples": len(outcomes),
            }
        return results

    # ── main interface ────────────────────────────────────────────────────

    def compute(self, df: pd.DataFrame) -> tuple[dict, dict]:
        """
        Compute all per-state stats and transition pair stats.
        Returns (state_stats_dict, transition_pair_stats_dict).
        """
        df_out = self._compute_outcomes(df)

        state_stats   = {}
        valid_states  = df_out["state_id"].value_counts()
        valid_states  = valid_states[valid_states >= self.min_samples].index

        for state in valid_states:
            grp  = df_out[df_out["state_id"] == state]
            stats = self._state_stats_from_outcomes(grp)
            if stats:
                stats["session_breakdown"] = self._session_breakdown(grp)
                state_stats[state]         = stats

        pair_stats = self._transition_pair_stats(df_out)
        return state_stats, pair_stats


# ── Class 2: WalkForwardValidator ────────────────────────────────────────────

class WalkForwardValidator:

    def __init__(self, train_pct: float = 1 - OOS_FRACTION, n_windows: int = 5):
        self.train_pct  = train_pct
        self.n_windows  = n_windows

    def split_data(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Strict time-based split. No shuffle."""
        df = df.sort_values("datetime_utc").reset_index(drop=True)
        cut = int(len(df) * self.train_pct)
        return df.iloc[:cut].copy(), df.iloc[cut:].copy()

    def _date_range(self, df: pd.DataFrame) -> tuple[str, str]:
        dt_col = "datetime_utc"
        if dt_col not in df.columns or df.empty:
            return ("?", "?")
        start = pd.to_datetime(df[dt_col].iloc[0]).strftime("%Y-%m-%d")
        end   = pd.to_datetime(df[dt_col].iloc[-1]).strftime("%Y-%m-%d")
        return start, end

    def validate_in_sample(self, train_df: pd.DataFrame) -> dict:
        calc  = StatePerformanceCalculator()
        stats, _ = calc.compute(train_df)
        return stats

    def validate_out_of_sample(
        self, oos_df: pd.DataFrame, trained_stats: dict
    ) -> tuple[dict, list]:
        """
        Apply trained stats to OOS data.
        Returns (oos_stats, flagged_states).
        Flags states where OOS WR drops > 15pp vs in-sample WR.
        """
        calc = StatePerformanceCalculator(min_samples=5)   # lower bar for OOS
        oos_stats, _ = calc.compute(oos_df)

        flagged = []
        for state, is_stats in trained_stats.items():
            if state not in oos_stats:
                continue
            drop = is_stats["win_rate"] - oos_stats[state]["win_rate"]
            if drop > OOS_DEGRADE_LIMIT:
                flagged.append(state)
                oos_stats[state]["oos_flagged"] = True
            else:
                oos_stats[state]["oos_flagged"] = False

        return oos_stats, flagged

    def walk_forward_windows(self, df: pd.DataFrame) -> dict:
        """
        5 expanding windows. Each: train on all prior, test on next slice.
        Returns stability_score per state: fraction of windows where the
        state had positive EV in OOS. Range 0.0 – 1.0.
        """
        df = df.sort_values("datetime_utc").reset_index(drop=True)
        n  = len(df)
        # Use the last n_windows+1 equal slices
        slice_size = n // (self.n_windows + 1)
        if slice_size < MIN_SAMPLES * 2:
            log.warning("Not enough data for walk-forward windows")
            return {}

        window_results = {}   # state -> list of (ev, oos_n)

        for w in range(self.n_windows):
            # Training: bars 0 … (w+1)*slice_size
            # OOS:      bars (w+1)*slice_size … (w+2)*slice_size
            train_end = (w + 1) * slice_size
            oos_end   = min((w + 2) * slice_size, n)

            train_df  = df.iloc[:train_end]
            oos_df    = df.iloc[train_end:oos_end]

            calc     = StatePerformanceCalculator(min_samples=MIN_SAMPLES)
            is_stats, _ = calc.compute(train_df)

            oos_calc = StatePerformanceCalculator(min_samples=5)
            oos_stats, _ = oos_calc.compute(oos_df)

            for state in is_stats:
                if state not in oos_stats:
                    continue
                ev = oos_stats[state]["ev"]
                if state not in window_results:
                    window_results[state] = []
                window_results[state].append(ev)

        # Stability = fraction of windows with positive OOS EV
        stability = {}
        for state, evs in window_results.items():
            if not evs:
                continue
            score = sum(1 for e in evs if e > 0) / len(evs)
            stability[state] = round(score, 3)

        return stability


# ── Class 3: MonteCarloSimulator ─────────────────────────────────────────────

class MonteCarloSimulator:

    def __init__(self, n_paths: int = MC_PATHS, n_steps: int = 100):
        self.n_paths = n_paths
        self.n_steps = n_steps

    def simulate_paths(
        self,
        transition_matrix: dict,
        state_ev: dict,
        start_state: Optional[str] = None,
    ) -> np.ndarray:
        """
        Generate n_paths random walks through state space.
        Returns equity_curves array of shape (n_paths, n_steps+1).
        """
        states = list(transition_matrix.keys())
        if not states:
            return np.zeros((self.n_paths, self.n_steps + 1))

        # Precompute transition arrays for fast sampling
        trans_probs = {}
        trans_states = {}
        for s, row in transition_matrix.items():
            targets = list(row.keys())
            probs   = np.array([row[t] for t in targets], dtype=float)
            total   = probs.sum()
            if total > 0:
                probs /= total
            trans_states[s] = targets
            trans_probs[s]  = probs

        # Use most common state as start, or specified
        if start_state and start_state in transition_matrix:
            initial = start_state
        else:
            initial = states[0]

        rng = np.random.default_rng(42)
        equity = np.zeros((self.n_paths, self.n_steps + 1))

        for path_i in range(self.n_paths):
            state  = initial
            cum_r  = 0.0
            for step in range(self.n_steps):
                # Sample next state
                if state in trans_states and len(trans_states[state]) > 0:
                    probs  = trans_probs[state]
                    idx    = rng.choice(len(trans_states[state]), p=probs)
                    state  = trans_states[state][idx]

                # Add EV of landing state (with noise to simulate real outcomes)
                ev        = state_ev.get(state, 0.0)
                noise     = rng.normal(0, abs(ev) * 0.5 + 0.2)
                cum_r    += ev + noise
                equity[path_i, step + 1] = cum_r

        return equity

    def compute_equity_percentiles(self, equity: np.ndarray) -> dict:
        """Compute percentile equity curves and risk metrics."""
        pcts = {
            "p5":  np.percentile(equity, 5,  axis=0).tolist(),
            "p25": np.percentile(equity, 25, axis=0).tolist(),
            "p50": np.percentile(equity, 50, axis=0).tolist(),
            "p75": np.percentile(equity, 75, axis=0).tolist(),
            "p95": np.percentile(equity, 95, axis=0).tolist(),
        }

        # Max drawdown per path
        max_drawdowns = []
        for path in equity:
            peak = np.maximum.accumulate(path)
            dd   = (path - peak)
            max_drawdowns.append(float(dd.min()))

        # Probability of ruin: equity < -20R at any point
        ruin = np.mean([np.any(path < -20) for path in equity])

        return {
            "percentiles":     pcts,
            "p5_max_drawdown": round(float(np.percentile(max_drawdowns, 5)),  2),
            "p25_max_drawdown":round(float(np.percentile(max_drawdowns, 25)), 2),
            "p50_max_drawdown":round(float(np.percentile(max_drawdowns, 50)), 2),
            "p75_max_drawdown":round(float(np.percentile(max_drawdowns, 75)), 2),
            "p95_max_drawdown":round(float(np.percentile(max_drawdowns, 95)), 2),
            "probability_of_ruin":      round(float(ruin),                    4),
            "expected_r_per_n_steps":   round(float(np.median(equity[:, -1])),2),
            "all_max_drawdowns":        [round(d, 2) for d in max_drawdowns[:500]],
        }

    def sharpe_ratio(self, equity: np.ndarray, trades_per_year: int = 250) -> float:
        """Compute annualised Sharpe ratio from step-by-step returns."""
        step_returns = np.diff(equity, axis=1)
        mean_ret  = step_returns.mean(axis=1)
        std_ret   = step_returns.std(axis=1)
        std_ret[std_ret == 0] = 1e-9
        sharpes   = (mean_ret / std_ret) * np.sqrt(trades_per_year)
        return round(float(np.median(sharpes)), 3)


# ── Class 4: ReportGenerator ─────────────────────────────────────────────────

class ReportGenerator:

    def _traffic_light(self, ev: float, oos_flagged: bool) -> str:
        if oos_flagged:
            return "#ff4444"
        if ev >= 0.5:
            return "#22c55e"
        if ev >= 0.0:
            return "#facc15"
        return "#ef4444"

    def _style(self) -> str:
        return """
<style>
  body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0;
         margin: 0; padding: 20px; }
  h1   { color: #38bdf8; font-size: 1.6rem; border-bottom: 1px solid #334155;
         padding-bottom: 10px; }
  h2   { color: #7dd3fc; font-size: 1.2rem; margin-top: 32px; }
  h3   { color: #94a3b8; font-size: 1rem; }
  table{ border-collapse: collapse; width: 100%; font-size: 0.78rem;
         margin-bottom: 24px; }
  th   { background: #1e293b; color: #94a3b8; padding: 6px 10px;
         text-align: left; border-bottom: 1px solid #334155; }
  td   { padding: 5px 10px; border-bottom: 1px solid #1e293b; }
  tr:hover td { background: #1e293b; }
  .ev-pos  { color: #22c55e; }
  .ev-neg  { color: #ef4444; }
  .ev-neut { color: #facc15; }
  .oos-flag{ background: #3f0f0f; }
  .pill    { display: inline-block; padding: 1px 6px; border-radius: 9px;
             font-size: 0.7rem; }
  .pill-g  { background: #14532d; color: #86efac; }
  .pill-y  { background: #713f12; color: #fde68a; }
  .pill-r  { background: #450a0a; color: #fca5a5; }
  .card    { background: #1e293b; border-radius: 8px; padding: 16px;
             margin-bottom: 16px; }
  .grid2   { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid3   { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
  .stat    { font-size: 1.4rem; font-weight: bold; color: #38bdf8; }
  .label   { font-size: 0.75rem; color: #64748b; }
  .bar-bg  { background: #334155; border-radius: 4px; height: 8px; }
  .bar-fill{ background: #38bdf8; border-radius: 4px; height: 8px; }
  .oos-warn{ color: #fbbf24; font-size: 0.75rem; }
</style>
"""

    def _ev_class(self, ev: float) -> str:
        if ev >= 0.3:
            return "ev-pos"
        if ev < 0:
            return "ev-neg"
        return "ev-neut"

    def _pill(self, score: float) -> str:
        if score >= 0.7:
            return f'<span class="pill pill-g">{score:.2f}</span>'
        if score >= 0.4:
            return f'<span class="pill pill-y">{score:.2f}</span>'
        return f'<span class="pill pill-r">{score:.2f}</span>'

    def _mc_chart(self, percentiles: dict) -> str:
        """Inline SVG equity curve fan chart."""
        steps = len(percentiles["p50"])
        if steps == 0:
            return ""
        w, h, pad = 800, 260, 40
        data_min = min(min(percentiles["p5"]),  -1)
        data_max = max(max(percentiles["p95"]),  1)
        data_range = data_max - data_min or 1

        def sx(i):
            return pad + (i / (steps - 1)) * (w - 2 * pad)

        def sy(v):
            return h - pad - ((v - data_min) / data_range) * (h - 2 * pad)

        def polyline(vals, color, opacity, width=1):
            pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(vals))
            return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-opacity="{opacity}" stroke-width="{width}"/>'

        def polygon(vals_hi, vals_lo, color, opacity):
            pts_hi = [(sx(i), sy(v)) for i, v in enumerate(vals_hi)]
            pts_lo = [(sx(i), sy(v)) for i, v in enumerate(reversed(vals_lo))]
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts_hi + pts_lo)
            return f'<polygon points="{pts}" fill="{color}" fill-opacity="{opacity}"/>'

        # Zero line
        zy = sy(0)
        svg = [
            f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="background:#0f172a;border-radius:8px">',
            f'<line x1="{pad}" y1="{zy:.1f}" x2="{w-pad}" y2="{zy:.1f}" '
            f'stroke="#334155" stroke-width="1" stroke-dasharray="4"/>',
            polygon(percentiles["p95"], percentiles["p5"],  "#38bdf8", 0.10),
            polygon(percentiles["p75"], percentiles["p25"], "#38bdf8", 0.18),
            polyline(percentiles["p5"],  "#ef4444", 0.6),
            polyline(percentiles["p95"], "#22c55e", 0.6),
            polyline(percentiles["p25"], "#7dd3fc", 0.7),
            polyline(percentiles["p75"], "#7dd3fc", 0.7),
            polyline(percentiles["p50"], "#ffffff", 1.0, 2),
            f'<text x="{pad}" y="16" fill="#64748b" font-size="11">Monte Carlo — Equity Curve Fan (n={MC_PATHS:,})</text>',
            f'<text x="{w//2}" y="{h-8}" fill="#64748b" font-size="10" text-anchor="middle">Steps</text>',
            "</svg>",
        ]
        return "\n".join(svg)

    def _dd_histogram(self, drawdowns: list) -> str:
        """Mini inline SVG histogram of max drawdowns."""
        if not drawdowns:
            return ""
        arr = np.array(drawdowns)
        bins = np.linspace(arr.min(), arr.max() or -0.1, 25)
        counts, edges = np.histogram(arr, bins=bins)
        max_c = counts.max() or 1
        w, h, pad = 600, 160, 30
        bin_w = (w - 2 * pad) / len(counts)

        bars = []
        for i, c in enumerate(counts):
            bh = int((c / max_c) * (h - 2 * pad))
            x  = pad + i * bin_w
            y  = h - pad - bh
            bars.append(
                f'<rect x="{x:.1f}" y="{y}" width="{bin_w-1:.1f}" height="{bh}" '
                f'fill="#38bdf8" fill-opacity="0.7"/>'
            )

        svg = [
            f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="background:#0f172a;border-radius:8px">',
            f'<text x="{pad}" y="16" fill="#64748b" font-size="11">Max Drawdown Distribution (R)</text>',
            *bars,
            "</svg>",
        ]
        return "\n".join(svg)

    def _session_heatmap(self, state_stats: dict) -> str:
        """Session EV mini-heatmap as HTML table."""
        rows = []
        for state, s in sorted(state_stats.items(), key=lambda x: x[1]["ev"], reverse=True)[:20]:
            sb = s.get("session_breakdown", {})
            toks = state.split("_")
            short = "_".join(toks[:3]) + "..." if len(toks) > 3 else state

            def cell(sess_key):
                data = sb.get(sess_key)
                if not data:
                    return "<td style='color:#475569'>—</td>"
                ev   = data["ev"]
                col  = "#22c55e" if ev > 0.3 else ("#facc15" if ev >= 0 else "#ef4444")
                return f"<td style='color:{col}'>{ev:+.2f}<br><span style='color:#475569;font-size:0.65rem'>{data['samples']}s</span></td>"

            rows.append(
                f"<tr><td title='{state}' style='font-size:0.7rem'>{short}</td>"
                f"{cell('tokyo')}{cell('london')}{cell('ny')}</tr>"
            )

        return f"""
<table>
  <thead><tr><th>State (top 20 by EV)</th><th>TOKYO</th><th>LONDON</th><th>NY</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""

    def generate(
        self,
        instrument:    str,
        timeframe:     str,
        state_stats:   dict,
        oos_stats:     dict,
        flagged_states:list,
        stability:     dict,
        mc_results:    dict,
        train_period:  tuple,
        oos_period:    tuple,
    ) -> str:
        """Generate full HTML report. Returns HTML string."""

        # Merge OOS data into state_stats for display
        merged = {}
        for state, s in state_stats.items():
            entry = dict(s)
            if state in oos_stats:
                entry["oos_win_rate"] = oos_stats[state]["win_rate"]
                entry["oos_ev"]       = oos_stats[state]["ev"]
                entry["oos_flagged"]  = oos_stats[state].get("oos_flagged", False)
            else:
                entry["oos_win_rate"] = None
                entry["oos_ev"]       = None
                entry["oos_flagged"]  = False
            entry["stability_score"] = stability.get(state, 0.0)
            merged[state] = entry

        sorted_by_ev = sorted(merged.items(), key=lambda x: x[1]["ev"], reverse=True)
        top10         = sorted_by_ev[:10]
        bottom10      = sorted_by_ev[-10:]
        mc_pcts       = mc_results.get("percentiles", {})
        mc_dds        = mc_results.get("all_max_drawdowns", [])
        mc_sharpe     = mc_results.get("median_sharpe", 0)

        ts_ok   = sum(1 for s in merged.values() if not s.get("oos_flagged"))
        ts_flag = len(flagged_states)
        ts_stab = sum(1 for s in stability.values() if s >= 0.7)

        # ── Executive summary cards ────────────────────────────────────────
        best_state = top10[0][0][:50] if top10 else "—"
        best_ev    = top10[0][1]["ev"] if top10 else 0

        exec_summary = f"""
<div class="grid3">
  <div class="card">
    <div class="label">Total states analysed</div>
    <div class="stat">{len(merged)}</div>
    <div class="label">with &ge;30 samples</div>
  </div>
  <div class="card">
    <div class="label">OOS validated / flagged</div>
    <div class="stat">{ts_ok} / <span style="color:#ef4444">{ts_flag}</span></div>
    <div class="label">overfit threshold &gt;15pp drop</div>
  </div>
  <div class="card">
    <div class="label">Stable states (&ge;0.7 score)</div>
    <div class="stat">{ts_stab}</div>
    <div class="label">walk-forward consistency</div>
  </div>
</div>
<div class="grid3">
  <div class="card">
    <div class="label">Median Monte Carlo Sharpe</div>
    <div class="stat">{mc_sharpe:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Median max drawdown (R)</div>
    <div class="stat" style="color:#fbbf24">{mc_results.get('p50_max_drawdown', 0):.1f}R</div>
  </div>
  <div class="card">
    <div class="label">Probability of ruin (&lt;-20R)</div>
    <div class="stat" style="color:{'#22c55e' if mc_results.get('probability_of_ruin',1)<0.01 else '#ef4444'}">
      {mc_results.get('probability_of_ruin', 0):.1%}</div>
  </div>
</div>
<div class="card">
  <div class="label">Best state by EV</div>
  <div style="font-size:0.9rem;color:#38bdf8;margin-top:4px">{best_state}</div>
  <div style="font-size:0.8rem;color:#22c55e">EV = {best_ev:+.3f}R</div>
</div>"""

        # ── Top/Bottom state tables ────────────────────────────────────────
        def state_row(state, s, rank_col=""):
            ev    = s["ev"]
            ec    = self._ev_class(ev)
            oos_f = s.get("oos_flagged", False)
            row_cls = ' class="oos-flag"' if oos_f else ""
            oos_str = f"{s['oos_win_rate']:.1%}" if s.get("oos_win_rate") is not None else "—"
            oos_flag_icon = " ⚠" if oos_f else ""
            stab  = self._pill(s.get("stability_score", 0))
            return (
                f"<tr{row_cls}>"
                f"<td style='font-size:0.7rem'>{state}</td>"
                f"<td>{s['win_rate']:.1%}</td>"
                f"<td class='{ec}'>{ev:+.3f}</td>"
                f"<td>{s['profit_factor']:.2f}</td>"
                f"<td>{s['avg_r']:+.3f}</td>"
                f"<td>{s['mae']:.3f}</td>"
                f"<td>{s['mfe']:.3f}</td>"
                f"<td>{s['sample_count']}</td>"
                f"<td>{s['ci_95_low']:.2f}–{s['ci_95_high']:.2f}</td>"
                f"<td>{oos_str}{oos_flag_icon}</td>"
                f"<td>{stab}</td>"
                f"</tr>"
            )

        th_row = ("<tr><th>State</th><th>WR</th><th>EV</th><th>PF</th><th>Avg R</th>"
                  "<th>MAE</th><th>MFE</th><th>Samples</th><th>CI 95%</th>"
                  "<th>OOS WR</th><th>Stab</th></tr>")

        top10_rows    = "".join(state_row(s, d) for s, d in top10)
        bottom10_rows = "".join(state_row(s, d) for s, d in bottom10)
        all_rows      = "".join(state_row(s, d) for s, d in sorted_by_ev)

        mc_chart_svg = self._mc_chart(mc_pcts)
        dd_hist_svg  = self._dd_histogram(mc_dds)
        sess_heatmap = self._session_heatmap(merged)

        # ── WF stability table ────────────────────────────────────────────
        stab_rows = "".join(
            f"<tr><td style='font-size:0.7rem'>{s}</td>"
            f"<td>{self._pill(v)}</td>"
            f"<td class='{'ev-pos' if v>=0.7 else 'ev-neut' if v>=0.4 else 'ev-neg'}'>"
            f"{'TRADEABLE' if v>=0.7 else 'MARGINAL' if v>=0.4 else 'AVOID'}</td></tr>"
            for s, v in sorted(stability.items(), key=lambda x: x[1], reverse=True)
        )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>H2 Quant — Backtest Report: {instrument} {timeframe}</title>
{self._style()}
</head>
<body>
<h1>H2 Quant v1 — Backtest Report</h1>
<p style="color:#64748b">
  {instrument} {timeframe} &nbsp;|&nbsp;
  Train: {train_period[0]} to {train_period[1]} &nbsp;|&nbsp;
  OOS: {oos_period[0]} to {oos_period[1]} &nbsp;|&nbsp;
  Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
</p>

<h2>Section 1 — Executive Summary</h2>
{exec_summary}

<h2>Section 2 — Top 10 States by EV</h2>
<table>{th_row}{top10_rows}</table>

<h2>Section 2b — Bottom 10 States (Worst EV — Avoid)</h2>
<table>{th_row}{bottom10_rows}</table>

<h2>Section 2c — All States (sorted by EV)</h2>
<table>{th_row}{all_rows}</table>

<h2>Section 3 — Session Breakdown (EV Heatmap)</h2>
{sess_heatmap}

<h2>Section 4 — Walk-Forward Stability</h2>
<p style="color:#64748b">Score = fraction of OOS windows with positive EV.
  &ge;0.7 = Tradeable &nbsp; 0.4–0.7 = Marginal &nbsp; &lt;0.4 = Avoid</p>
<table>
  <thead><tr><th>State</th><th>Stability Score</th><th>Status</th></tr></thead>
  <tbody>{stab_rows}</tbody>
</table>

<h2>Section 5 — Monte Carlo Simulation ({MC_PATHS:,} paths, {self._mc_steps(mc_pcts)} steps)</h2>
<div class="grid2">
  <div class="card">
    <div class="label">Median Sharpe (annualised)</div>
    <div class="stat">{mc_sharpe:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Expected R after {self._mc_steps(mc_pcts)} trades (median path)</div>
    <div class="stat">{mc_results.get('expected_r_per_n_steps', 0):+.1f}R</div>
  </div>
</div>
<div class="card">
  <b>Max Drawdown Distribution (R):</b>
  p5={mc_results.get('p5_max_drawdown', 0):.1f}R &nbsp;
  p25={mc_results.get('p25_max_drawdown', 0):.1f}R &nbsp;
  p50={mc_results.get('p50_max_drawdown', 0):.1f}R &nbsp;
  p75={mc_results.get('p75_max_drawdown', 0):.1f}R &nbsp;
  p95={mc_results.get('p95_max_drawdown', 0):.1f}R &nbsp;
  <span class="oos-warn">Prob. of Ruin: {mc_results.get('probability_of_ruin', 0):.2%}</span>
</div>
{mc_chart_svg}
<br>
{dd_hist_svg}
</body>
</html>"""
        return html

    def _mc_steps(self, pcts: dict) -> int:
        return len(pcts.get("p50", [])) - 1


# ── Main pipeline ─────────────────────────────────────────────────────────────

def validate(
    instrument: str,
    timeframe:  str,
    verbose:    bool = True,
) -> Optional[dict]:
    """Full validation pipeline for one instrument+timeframe."""

    # ── Load states parquet ───────────────────────────────────────────────
    states_path = STATES_DIR / f"H2_states_{instrument}_{timeframe}.parquet"
    if not states_path.exists():
        log.error(f"States not found: {states_path}")
        return None
    df = pd.read_parquet(states_path)
    log.info(f"Loaded: {instrument} {timeframe} — {len(df)} bars")

    # ── Load transition matrix (for MC) ───────────────────────────────────
    tm_path = OUTPUTS_DIR / f"H2_transition_matrix_{instrument}_{timeframe}.json"
    transition_matrix = {}
    if tm_path.exists():
        with open(tm_path, encoding="utf-8") as f:
            tm_data = json.load(f)
        transition_matrix = tm_data.get("matrices", {}).get("global", {})

    # ── Walk-forward split ────────────────────────────────────────────────
    wfv          = WalkForwardValidator()
    train_df, oos_df = wfv.split_data(df)
    train_period = wfv._date_range(train_df)
    oos_period   = wfv._date_range(oos_df)
    log.info(f"Train: {train_period[0]}–{train_period[1]} ({len(train_df)} bars) | "
             f"OOS: {oos_period[0]}–{oos_period[1]} ({len(oos_df)} bars)")

    # ── In-sample stats ───────────────────────────────────────────────────
    log.info("Computing in-sample state stats...")
    is_stats, pair_stats = StatePerformanceCalculator().compute(train_df)
    log.info(f"In-sample: {len(is_stats)} states with stats")

    # ── OOS validation ────────────────────────────────────────────────────
    log.info("Computing OOS validation...")
    oos_stats, flagged = wfv.validate_out_of_sample(oos_df, is_stats)
    log.info(f"OOS: {len(oos_stats)} states | flagged: {len(flagged)}")

    # ── Walk-forward stability ────────────────────────────────────────────
    log.info("Running walk-forward windows...")
    stability = wfv.walk_forward_windows(df)
    log.info(f"Stability scores computed for {len(stability)} states")

    # ── Monte Carlo ───────────────────────────────────────────────────────
    log.info(f"Running Monte Carlo ({MC_PATHS:,} paths)...")
    state_ev = {s: d["ev"] for s, d in is_stats.items()}
    mc_sim   = MonteCarloSimulator(n_paths=MC_PATHS, n_steps=100)
    equity   = mc_sim.simulate_paths(transition_matrix, state_ev)
    mc_pcts  = mc_sim.compute_equity_percentiles(equity)
    mc_sharpe = mc_sim.sharpe_ratio(equity)
    mc_results = {**mc_pcts, "median_sharpe": mc_sharpe}
    log.info(f"MC: Sharpe={mc_sharpe:.2f} | "
             f"median DD={mc_results.get('p50_max_drawdown',0):.1f}R | "
             f"ruin={mc_results.get('probability_of_ruin',0):.2%}")

    # ── Assemble H2_state_stats.json ─────────────────────────────────────
    output_states = {}
    for state, s in is_stats.items():
        entry = dict(s)
        oos   = oos_stats.get(state, {})
        entry["oos_win_rate"]     = oos.get("win_rate")
        entry["oos_ev"]           = oos.get("ev")
        entry["oos_flagged"]      = state in flagged
        entry["stability_score"]  = stability.get(state, 0.0)
        output_states[state]      = entry

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instrument":   instrument,
        "timeframe":    timeframe,
        "train_period": f"{train_period[0]} to {train_period[1]}",
        "oos_period":   f"{oos_period[0]} to {oos_period[1]}",
        "states":       output_states,
        "transition_pair_stats": pair_stats,
        "monte_carlo":  {
            "median_sharpe":          mc_sharpe,
            "p5_max_drawdown":        mc_results.get("p5_max_drawdown",  0),
            "p25_max_drawdown":       mc_results.get("p25_max_drawdown", 0),
            "p50_max_drawdown":       mc_results.get("p50_max_drawdown", 0),
            "p95_max_drawdown":       mc_results.get("p95_max_drawdown", 0),
            "probability_of_ruin":    mc_results.get("probability_of_ruin", 0),
            "expected_r_per_100_trades": mc_results.get("expected_r_per_n_steps", 0),
        },
    }

    # Save state_stats JSON
    stats_path = OUTPUTS_DIR / f"H2_state_stats_{instrument}_{timeframe}.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    log.info(f"Saved: {stats_path}")

    # ── HTML report ───────────────────────────────────────────────────────
    log.info("Generating HTML report...")
    rg   = ReportGenerator()
    html = rg.generate(
        instrument    = instrument,
        timeframe     = timeframe,
        state_stats   = is_stats,
        oos_stats     = oos_stats,
        flagged_states= flagged,
        stability     = stability,
        mc_results    = mc_results,
        train_period  = train_period,
        oos_period    = oos_period,
    )
    report_path = OUTPUTS_DIR / f"H2_backtest_report_{instrument}_{timeframe}.html"
    report_path.write_text(html, encoding="utf-8")
    log.info(f"Saved: {report_path}")

    # ── Console summary ───────────────────────────────────────────────────
    if verbose:
        _print_validation_summary(
            instrument, timeframe, is_stats, oos_stats,
            flagged, stability, mc_results, train_period, oos_period,
        )

    return output


def _print_validation_summary(
    instrument, timeframe, is_stats, oos_stats,
    flagged, stability, mc_results, train_period, oos_period,
):
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  VALIDATOR REPORT — {instrument} {timeframe}")
    print(sep)
    print(f"  Train  : {train_period[0]} -> {train_period[1]}")
    print(f"  OOS    : {oos_period[0]} -> {oos_period[1]}")
    print(f"  States : {len(is_stats)} in-sample | {len(oos_stats)} OOS | {len(flagged)} overfit-flagged")
    print()

    # Top 10 by EV
    top10 = sorted(is_stats.items(), key=lambda x: x[1]["ev"], reverse=True)[:10]
    print(f"  TOP 10 STATES BY EV:")
    print(f"  {'STATE':<55} {'WR':>6} {'EV':>7} {'PF':>6} {'N':>5}  CI-95%")
    print(f"  {'-'*70}")
    for state, s in top10:
        ci = f"{s['ci_95_low']:.2f}-{s['ci_95_high']:.2f}"
        flag = " [OOS-FAIL]" if state in flagged else ""
        print(f"  {state:<55} {s['win_rate']:>6.1%} {s['ev']:>+7.3f} "
              f"{min(s['profit_factor'],9.9):>6.2f} {s['sample_count']:>5}  {ci}{flag}")

    # OOS summary
    oos_ok   = sum(1 for s in is_stats if s not in flagged)
    stab_ok  = sum(1 for v in stability.values() if v >= 0.7)
    stab_bad = sum(1 for v in stability.values() if v < 0.4)
    print(f"\n  OOS VALIDATION:")
    print(f"    Passed (degradation < 15pp) : {oos_ok}")
    print(f"    Flagged overfit             : {len(flagged)}")
    if flagged:
        for s in flagged[:5]:
            print(f"      - {s}")

    print(f"\n  WALK-FORWARD STABILITY:")
    print(f"    Score >= 0.7 (tradeable)    : {stab_ok}")
    print(f"    Score <  0.4 (avoid)        : {stab_bad}")

    # Session best
    sess_ev = {"tokyo": [], "london": [], "ny": []}
    for s in is_stats.values():
        for sess, data in s.get("session_breakdown", {}).items():
            if sess in sess_ev:
                sess_ev[sess].append(data["ev"])
    print(f"\n  SESSION PERFORMANCE (avg EV across states):")
    for sess, evs in sess_ev.items():
        avg = np.mean(evs) if evs else 0
        print(f"    {sess.upper():<8}: {avg:+.4f}R avg EV  ({len(evs)} state-sessions)")

    # MC
    mc = mc_results
    print(f"\n  MONTE CARLO ({MC_PATHS:,} paths, 100 steps):")
    print(f"    Median Sharpe               : {mc.get('median_sharpe', 0):.2f}")
    print(f"    Max DD (p5/p50/p95)         : {mc.get('p5_max_drawdown', 0):.1f}R / "
          f"{mc.get('p50_max_drawdown', 0):.1f}R / {mc.get('p95_max_drawdown', 0):.1f}R")
    print(f"    Expected R per 100 trades   : {mc.get('expected_r_per_n_steps', 0):+.1f}R")
    print(f"    Probability of ruin (<-20R) : {mc.get('probability_of_ruin', 0):.2%}")
    print(sep)


def validate_batch(
    instruments: Optional[list] = None,
    timeframes:  Optional[list] = None,
) -> dict:
    INSTS = instruments or [
        "JP225","DE40","UK100","USTEC","US30","HK50",
        "EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD",
        "EURJPY","GBPJPY","XAUUSD","XAGUSD",
    ]
    TFS = timeframes or ["D", "4H", "1H", "15m"]

    results = {}
    for inst in INSTS:
        for tf in TFS:
            key = f"{inst}_{tf}"
            try:
                out = validate(inst, tf, verbose=False)
                if out:
                    n_states = len(out["states"])
                    sharpe   = out["monte_carlo"]["median_sharpe"]
                    results[key] = f"ok: {n_states} states | Sharpe={sharpe:.2f}"
                else:
                    results[key] = "fail: no data"
            except Exception as e:
                log.error(f"Batch error {key}: {e}", exc_info=True)
                results[key] = f"error: {str(e)[:60]}"

    # Print consolidated table
    print(f"\n{'='*80}")
    print("  VALIDATOR BATCH SUMMARY")
    print(f"{'='*80}")
    print(f"  {'KEY':<20} {'RESULT'}")
    print(f"  {'-'*78}")
    for k, v in results.items():
        print(f"  {k:<20} {v}")
    ok = sum(1 for v in results.values() if v.startswith("ok"))
    print(f"\n  {ok}/{len(results)} completed successfully.")
    print(f"{'='*80}")
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="H2 Quant — Research Validator (Phase 5)")
    parser.add_argument("--instrument", "-i")
    parser.add_argument("--timeframe",  "-t")
    parser.add_argument("--batch", action="store_true")
    args = parser.parse_args()

    if args.batch:
        validate_batch()
        print("\nPhase 5 complete. Ready for Phase 6 — Briefing Generator.")
    elif args.instrument and args.timeframe:
        out = validate(args.instrument, args.timeframe, verbose=True)
        if out is None:
            sys.exit(1)
    else:
        parser.print_help()
