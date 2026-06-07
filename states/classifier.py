"""
H2 Quant v1 — Phase 3: State Classifier
Reads feature Parquet files and classifies every bar into a composite state ID.
Format: {TREND}_{MOMENTUM}_{VOLATILITY}_{LIQUIDITY}_{SESSION}_{STRUCTURE}_{RSI}
Input:  features/H2_features_{instrument}_{tf}.parquet
Output: states/H2_states_{instrument}_{tf}.parquet
"""

import logging
import sys
import textwrap
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
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
        logging.FileHandler(LOG_DIR / "classifier.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("H2.classifier")

STATES_DIR = ROOT / CFG["paths"].get("states", "states")
STATES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR = ROOT / CFG["paths"]["outputs"]
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
FEAT_DIR = ROOT / CFG["paths"]["features"]

# ---------------------------------------------------------------------------
# Valid label sets
# ---------------------------------------------------------------------------

TREND_LABELS      = {"BULL", "BEAR", "NEUTRAL"}
MOMENTUM_LABELS   = {"STRONG", "WEAK", "NEUTRAL"}
VOLATILITY_LABELS = {"HIGH", "NORMAL", "LOW"}
LIQUIDITY_LABELS  = {"ABOVE", "BELOW", "INSIDE"}
SESSION_LABELS    = {"TOKYO", "LONDON", "NY", "OVERLAP", "OFFHOURS"}
STRUCTURE_LABELS  = {"MSS", "CHOCH", "BOS", "PULLBACK", "RANGE", "TREND_CONT"}
RSI_LABELS        = {"OB", "OS", "NEUTRAL"}

# ---------------------------------------------------------------------------
# StateClassifier
# ---------------------------------------------------------------------------

class StateClassifier:
    """
    Classifies every bar of a feature DataFrame into a composite state ID.
    All logic is purely vectorised — no row-by-row loops except validate_state.
    """

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _col(name: str) -> str:
        """Return the __value column name for a feature."""
        return f"{name}__value"

    @staticmethod
    def _safe(df: pd.DataFrame, col: str, default=np.nan) -> pd.Series:
        """Return column if present, else a Series of default."""
        if col in df.columns:
            return df[col]
        return pd.Series(default, index=df.index)

    # ── dimension classifiers ─────────────────────────────────────────────

    def classify_trend(self, df: pd.DataFrame) -> pd.Series:
        """
        BULL  = EMA21 > EMA50 > EMA200
        BEAR  = EMA21 < EMA50 < EMA200
        NEUTRAL = anything else
        """
        e21  = self._safe(df, self._col("d07_ema21"),  df["close"] if "close" in df.columns else np.nan)
        e50  = self._safe(df, self._col("d07_ema50"),  df["close"] if "close" in df.columns else np.nan)
        e200 = self._safe(df, self._col("d07_ema200"), df["close"] if "close" in df.columns else np.nan)

        bull = (e21 > e50) & (e50 > e200)
        bear = (e21 < e50) & (e50 < e200)

        trend = pd.Series("NEUTRAL", index=df.index, dtype=str)
        trend[bull] = "BULL"
        trend[bear] = "BEAR"
        return trend

    def classify_momentum(self, df: pd.DataFrame) -> pd.Series:
        """
        STRONG  = RSI > 55 and MACD histogram > 0
        WEAK    = RSI < 45 and MACD histogram < 0
        NEUTRAL = anything else
        """
        rsi  = pd.to_numeric(self._safe(df, self._col("d04_rsi"),       50), errors="coerce").fillna(50)
        macd = pd.to_numeric(self._safe(df, self._col("d04_macd_hist"),  0),  errors="coerce").fillna(0)

        strong = (rsi > 55) & (macd > 0)
        weak   = (rsi < 45) & (macd < 0)

        mom = pd.Series("NEUTRAL", index=df.index, dtype=str)
        mom[strong] = "STRONG"
        mom[weak]   = "WEAK"
        return mom

    def classify_volatility(self, df: pd.DataFrame) -> pd.Series:
        """
        HIGH   = ATR rank > 0.66
        LOW    = ATR rank < 0.33
        NORMAL = between 0.33 and 0.66
        Uses d11_vol_rank (ATR percentile rank over 50 bars).
        """
        rank = pd.to_numeric(self._safe(df, self._col("d11_vol_rank"), 0.5), errors="coerce").fillna(0.5)

        vol = pd.Series("NORMAL", index=df.index, dtype=str)
        vol[rank > 0.66] = "HIGH"
        vol[rank < 0.33] = "LOW"
        return vol

    def classify_liquidity(self, df: pd.DataFrame) -> pd.Series:
        """
        ABOVE  = close above most recent swing high (dist_swing_high < 0 — price > swing H)
        BELOW  = close below most recent swing low  (dist_swing_low  < 0)
        INSIDE = between swing high and swing low
        d05_dist_swing_high = (swingH - close) / close  → negative when close > swingH
        d05_dist_swing_low  = (close - swingL) / close  → negative when close < swingL
        """
        dh = pd.to_numeric(self._safe(df, self._col("d05_dist_swing_high"), 0.01), errors="coerce").fillna(0.01)
        dl = pd.to_numeric(self._safe(df, self._col("d05_dist_swing_low"),  0.01), errors="coerce").fillna(0.01)

        above = dh < 0          # close > swing high
        below = dl < 0          # close < swing low

        liq = pd.Series("INSIDE", index=df.index, dtype=str)
        liq[above] = "ABOVE"
        liq[below] = "BELOW"
        # ABOVE takes priority if somehow both fire (shouldn't happen)
        return liq

    def classify_session(self, df: pd.DataFrame) -> pd.Series:
        """
        Read d06_session__value if available, else derive from UTC hour.
        OVERLAP takes priority (13:00–16:00 UTC).
        """
        if self._col("d06_session") in df.columns:
            sess = self._safe(df, self._col("d06_session"), "OFFHOURS").astype(str)
            # Ensure only valid labels pass through
            valid = sess.isin(SESSION_LABELS)
            sess[~valid] = "OFFHOURS"
            return sess

        # Fallback: derive from datetime_utc
        if "datetime_utc" in df.columns:
            h = pd.to_datetime(df["datetime_utc"], utc=True).dt.hour
        else:
            return pd.Series("OFFHOURS", index=df.index, dtype=str)

        sess = pd.Series("OFFHOURS", index=df.index, dtype=str)
        sess[(h >= 0)  & (h < 7)]  = "TOKYO"
        sess[(h >= 7)  & (h < 13)] = "LONDON"
        sess[(h >= 13) & (h < 16)] = "OVERLAP"
        sess[(h >= 16) & (h < 22)] = "NY"
        return sess

    def classify_structure(self, df: pd.DataFrame) -> pd.Series:
        """
        Hierarchy: MSS > CHOCH > BOS > PULLBACK > RANGE > TREND_CONT

        MSS     = d08_mss  flag True on this bar
        CHOCH   = d08_choch flag True on this bar
        BOS     = d08_bos   flag True on this bar
        PULLBACK = price retracing (MACD hist < 0 in bull trend or > 0 in bear),
                   no BOS/CHoCH in last 5 bars, HL structure holding
        RANGE   = no BOS/CHoCH for 15+ bars, price between swing levels
        TREND_CONT = none of the above
        """
        mss   = pd.to_numeric(self._safe(df, self._col("d08_mss"),   0), errors="coerce").fillna(0)
        choch = pd.to_numeric(self._safe(df, self._col("d08_choch"), 0), errors="coerce").fillna(0)
        bos   = pd.to_numeric(self._safe(df, self._col("d08_bos"),   0), errors="coerce").fillna(0)
        macd  = pd.to_numeric(self._safe(df, self._col("d04_macd_hist"), 0), errors="coerce").fillna(0)

        # Rolling count of BOS/CHoCH events in last N bars
        event = ((bos > 0) | (choch > 0)).astype(int)
        events_5  = event.rolling(5,  min_periods=1).sum()
        events_15 = event.rolling(15, min_periods=1).sum()

        # PULLBACK proxy: MACD turning, no recent structure event
        pullback_bull = (macd < 0) & (events_5 == 0)
        pullback_bear = (macd > 0) & (events_5 == 0)
        pullback = pullback_bull | pullback_bear

        # RANGE: no structure events in 15 bars, LIQUIDITY = INSIDE
        liq = self.classify_liquidity(df)
        in_range = (events_15 == 0) & (liq == "INSIDE")

        struct = pd.Series("TREND_CONT", index=df.index, dtype=str)
        struct[in_range]    = "RANGE"
        struct[pullback]    = "PULLBACK"
        struct[bos  > 0]    = "BOS"
        struct[choch > 0]   = "CHOCH"
        struct[mss   > 0]   = "MSS"
        return struct

    def classify_rsi(self, df: pd.DataFrame) -> pd.Series:
        """
        OB      = RSI >= 65
        OS      = RSI <= 35
        NEUTRAL = 35 < RSI < 65
        """
        rsi = pd.to_numeric(self._safe(df, self._col("d04_rsi"), 50), errors="coerce").fillna(50)
        r = pd.Series("NEUTRAL", index=df.index, dtype=str)
        r[rsi >= 65] = "OB"
        r[rsi <= 35] = "OS"
        return r

    # ── main interface ────────────────────────────────────────────────────

    def classify_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Adds state_id column and all seven component columns to df.
        Returns a copy.
        """
        out = df.copy()

        out["state_trend"]      = self.classify_trend(df)
        out["state_momentum"]   = self.classify_momentum(df)
        out["state_volatility"] = self.classify_volatility(df)
        out["state_liquidity"]  = self.classify_liquidity(df)
        out["state_session"]    = self.classify_session(df)
        out["state_structure"]  = self.classify_structure(df)
        out["state_rsi"]        = self.classify_rsi(df)

        out["state_id"] = (
            out["state_trend"]      + "_" +
            out["state_momentum"]   + "_" +
            out["state_volatility"] + "_" +
            out["state_liquidity"]  + "_" +
            out["state_session"]    + "_" +
            out["state_structure"]  + "_" +
            out["state_rsi"]
        )

        log.info(f"Classified {len(out)} bars -> {out['state_id'].nunique()} unique states")
        return out

    def classify_bar(self, row: pd.Series) -> str:
        """Classify a single bar (row as Series). Convenience wrapper."""
        df_single = row.to_frame().T.reset_index(drop=True)
        result = self.classify_dataframe(df_single)
        return result["state_id"].iloc[0]

    @staticmethod
    def validate_state(state_id: str) -> bool:
        """
        Return True if state_id encodes valid 7-dimension labels.
        TREND_CONT is a two-token structure label so the full state has 8 underscored tokens.
        We parse left-to-right against known label sets to handle this.
        """
        if not isinstance(state_id, str):
            return False
        parts = state_id.split("_")
        # Minimum: 7 tokens (no TREND_CONT), maximum 8 (with TREND_CONT)
        if len(parts) not in (7, 8):
            return False
        # First 4 fields are always single-token and unambiguous
        if parts[0] not in TREND_LABELS:      return False
        if parts[1] not in MOMENTUM_LABELS:   return False
        if parts[2] not in VOLATILITY_LABELS: return False
        if parts[3] not in LIQUIDITY_LABELS:  return False
        # SESSION can be OFFHOURS (1 token) — always single-token
        if parts[4] not in SESSION_LABELS:    return False
        # STRUCTURE: try single token first, then two-token (TREND_CONT)
        if len(parts) == 8:
            struct = parts[5] + "_" + parts[6]
            rsi    = parts[7]
        else:
            struct = parts[5]
            rsi    = parts[6]
        if struct not in STRUCTURE_LABELS: return False
        if rsi    not in RSI_LABELS:       return False
        return True


# ---------------------------------------------------------------------------
# StateAnalyzer
# ---------------------------------------------------------------------------

class StateAnalyzer:

    def __init__(self, min_samples: int = 30):
        self.min_samples = min_samples

    def state_distribution(self, df: pd.DataFrame) -> pd.DataFrame:
        """Count and % frequency of each state_id."""
        counts = df["state_id"].value_counts()
        pct    = (counts / len(df) * 100).round(2)
        result = pd.DataFrame({"count": counts, "pct": pct})
        result.index.name = "state_id"
        return result

    def min_sample_check(self, df: pd.DataFrame) -> pd.DataFrame:
        """Flag states with fewer than min_samples occurrences."""
        dist = self.state_distribution(df)
        flagged = dist[dist["count"] < self.min_samples].copy()
        flagged["status"] = "BELOW_MIN"
        return flagged

    def state_timeline(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compact state timeline: each row = one state run.
        Columns: start_dt, end_dt, state_id, bars, session
        Used by Markov engine.
        """
        if "state_id" not in df.columns:
            raise ValueError("df must have state_id column")

        dt_col = "datetime_utc" if "datetime_utc" in df.columns else df.index
        sid = df["state_id"].values
        dts = pd.to_datetime(df[dt_col] if isinstance(dt_col, str) else df.index)
        sess = df["state_session"].values if "state_session" in df.columns else \
               df["d06_session__value"].values if "d06_session__value" in df.columns else \
               ["UNKNOWN"] * len(df)

        runs = []
        i = 0
        while i < len(sid):
            j = i + 1
            while j < len(sid) and sid[j] == sid[i]:
                j += 1
            runs.append({
                "start_dt": dts.iloc[i],
                "end_dt":   dts.iloc[j - 1],
                "state_id": sid[i],
                "bars":     j - i,
                "session":  sess[i],
            })
            i = j

        return pd.DataFrame(runs)

    def avg_dwell(self, df: pd.DataFrame) -> pd.DataFrame:
        """Average bar duration per state."""
        tl = self.state_timeline(df)
        avg = tl.groupby("state_id")["bars"].mean().round(2)
        return avg.to_frame("avg_bars")

    def session_distribution(self, df: pd.DataFrame) -> pd.DataFrame:
        """State count broken down by session."""
        sess_col = "state_session" if "state_session" in df.columns else None
        if sess_col is None:
            return pd.DataFrame()
        return df.groupby([sess_col, "state_id"]).size().rename("count").reset_index()

    def print_summary(
        self,
        df: pd.DataFrame,
        instrument: str = "",
        timeframe: str = "",
        top_n: int = 10,
    ) -> str:
        """
        Human-readable summary report.
        Returns the report string (also prints it).
        """
        clf = StateClassifier()
        dist    = self.state_distribution(df)
        flagged = self.min_sample_check(df)
        dwell   = self.avg_dwell(df)
        sess_df = self.session_distribution(df)

        valid_count   = (dist["count"] >= self.min_samples).sum()
        invalid_count = (dist["count"] <  self.min_samples).sum()
        pct_valid_bars = dist[dist["count"] >= self.min_samples]["count"].sum() / len(df) * 100

        # Validate all state IDs
        bad_ids = [s for s in dist.index if not clf.validate_state(s)]

        lines = []
        sep = "=" * 70

        lines += [
            sep,
            f"  STATE CLASSIFIER REPORT — {instrument} {timeframe}",
            sep,
            f"  Total bars classified  : {len(df):,}",
            f"  Unique states found    : {dist.shape[0]}",
            f"  States >= {self.min_samples} samples   : {valid_count} "
            f"({pct_valid_bars:.1f}% of bars covered)",
            f"  States below minimum   : {invalid_count} (excluded from Markov)",
            f"  Invalid state format   : {len(bad_ids)}",
            "",
        ]

        # Top N states
        lines += [f"  TOP {top_n} STATES BY FREQUENCY:", "-" * 70]
        for state, row in dist.head(top_n).iterrows():
            dw  = dwell.loc[state, "avg_bars"] if state in dwell.index else "?"
            bar_w = int(row["pct"] / 2)
            bar   = "#" * bar_w + "-" * (25 - bar_w)
            flag  = " [<30]" if row["count"] < self.min_samples else ""
            lines.append(
                f"  {state:<55} {row['pct']:5.2f}%  dwell={dw}{flag}"
            )
        lines.append("")

        # Session breakdown for top 5
        if not sess_df.empty:
            lines += ["  SESSION BREAKDOWN (top 5 states per session):", "-" * 70]
            for sess in ["TOKYO", "LONDON", "OVERLAP", "NY", "OFFHOURS"]:
                sub = sess_df[sess_df["state_session"] == sess].sort_values("count", ascending=False).head(5)
                if sub.empty:
                    continue
                lines.append(f"  {sess}:")
                for _, r in sub.iterrows():
                    lines.append(f"    {r['state_id']:<55} {r['count']:>5} bars")
            lines.append("")

        # Below-minimum states
        if not flagged.empty:
            lines += [
                f"  STATES BELOW {self.min_samples}-SAMPLE MINIMUM ({len(flagged)}):",
                "-" * 70,
            ]
            for state, row in flagged.head(30).iterrows():
                lines.append(f"  {state:<55} {row['count']:>4} bars")
            if len(flagged) > 30:
                lines.append(f"  ... and {len(flagged) - 30} more")
            lines.append("")

        if bad_ids:
            lines += ["  INVALID STATE IDs (format error):", "-" * 70]
            for b in bad_ids[:10]:
                lines.append(f"    {b}")
            lines.append("")

        lines.append(sep)
        report = "\n".join(lines)
        print(report)
        return report


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_features(instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
    path = FEAT_DIR / f"H2_features_{instrument}_{timeframe}.parquet"
    if not path.exists():
        log.error(f"Feature parquet not found: {path}")
        return None
    df = pd.read_parquet(path)
    log.info(f"Loaded features: {instrument} {timeframe} — {len(df)} rows, {len(df.columns)} cols")
    return df


def save_states(df: pd.DataFrame, instrument: str, timeframe: str) -> Path:
    out = STATES_DIR / f"H2_states_{instrument}_{timeframe}.parquet"
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, out, compression="snappy")
    log.info(f"Saved states: {out} ({len(df)} rows)")
    return out


def save_summary(report: str, instrument: str, timeframe: str) -> Path:
    out = OUTPUTS_DIR / f"H2_state_summary_{instrument}_{timeframe}.txt"
    out.write_text(report, encoding="utf-8")
    log.info(f"Saved summary: {out}")
    return out


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def classify(instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
    """Full pipeline: load features → classify → analyze → save."""
    feat_df = load_features(instrument, timeframe)
    if feat_df is None:
        return None

    clf = StateClassifier()
    df  = clf.classify_dataframe(feat_df)

    analyzer = StateAnalyzer(min_samples=CFG["statistics"]["min_samples"])
    report   = analyzer.print_summary(df, instrument=instrument, timeframe=timeframe)

    save_states(df, instrument, timeframe)
    save_summary(report, instrument, timeframe)

    return df


def classify_batch(
    instruments: Optional[list] = None,
    timeframes: Optional[list] = None,
) -> dict:
    insts = instruments or (
        CFG["instruments"]["primary"] + CFG["instruments"].get("forex", [])
    )
    tfs = timeframes or CFG["timeframes"]["research"]

    results = {}
    for inst in insts:
        for tf in tfs:
            key = f"{inst}_{tf}"
            try:
                df = classify(inst, tf)
                results[key] = "ok" if df is not None else "no_features"
            except Exception as e:
                log.error(f"Batch error {key}: {e}")
                results[key] = f"error: {e}"
    ok = sum(1 for v in results.values() if v == "ok")
    log.info(f"Batch complete: {ok}/{len(results)} succeeded")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="H2 Quant — State Classifier (Phase 3)")
    parser.add_argument("--instrument", "-i")
    parser.add_argument("--timeframe",  "-t")
    parser.add_argument("--batch", action="store_true", help="Classify all instruments")
    args = parser.parse_args()

    if args.batch:
        results = classify_batch()
        ok = sum(1 for v in results.values() if v == "ok")
        print(f"\nBatch: {ok}/{len(results)} succeeded")
    elif args.instrument and args.timeframe:
        df = classify(args.instrument, args.timeframe)
        if df is None:
            print("Failed — check logs/classifier.log")
            sys.exit(1)
        print(f"\nDone. {len(df)} bars classified.")
        # Show last 10 state transitions
        tl = StateAnalyzer().state_timeline(df)
        print(f"\nLast 10 state transitions:")
        print(tl.tail(10).to_string(index=False))
    else:
        parser.print_help()
