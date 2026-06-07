"""
H2 Quant v1 — Phase 4: Markov Engine
Builds transition matrices, session-conditioned forecasts,
Chapman-Kolmogorov n-step projections, Bayesian updates,
and regime clustering from classified state sequences.

Input:  states/H2_states_{instrument}_{tf}.parquet
Output: outputs/H2_transition_matrix_{instrument}_{tf}.json
        outputs/H2_dwell_times.json
"""

import json
import logging
import sys
from collections import defaultdict
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
        logging.FileHandler(LOG_DIR / "markov.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("H2.markov")

OUTPUTS_DIR = ROOT / CFG["paths"]["outputs"]
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
STATES_DIR = ROOT / "states"

MIN_SAMPLES  = CFG["statistics"]["min_samples"]       # 30
PERSIST_GATE = CFG["gates"]["markov_persistence_threshold"]  # 0.82

SESSIONS = ["tokyo", "london", "ny"]
SESSION_MAP = {
    "TOKYO":    "tokyo",
    "LONDON":   "london",
    "OVERLAP":  "london",   # OVERLAP bars count toward london matrix
    "NY":       "ny",
    "OFFHOURS": None,       # excluded from session sub-matrices
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _normalise_row(counts: dict) -> dict:
    """Normalise a count dict to a probability row."""
    total = sum(counts.values())
    if total == 0:
        return {}
    return {k: round(v / total, 6) for k, v in counts.items()}


def _matrix_to_numpy(matrix: dict) -> tuple[np.ndarray, list]:
    """Convert nested dict matrix to numpy array + ordered state list."""
    states = sorted(matrix.keys())
    n = len(states)
    idx = {s: i for i, s in enumerate(states)}
    arr = np.zeros((n, n))
    for s, row in matrix.items():
        for t, p in row.items():
            if t in idx:
                arr[idx[s], idx[t]] = p
    return arr, states


def _numpy_to_matrix(arr: np.ndarray, states: list) -> dict:
    """Convert numpy array back to nested dict matrix."""
    out = {}
    for i, s in enumerate(states):
        row = {states[j]: round(float(arr[i, j]), 6)
               for j in range(len(states)) if arr[i, j] > 0}
        if row:
            out[s] = row
    return out


# ---------------------------------------------------------------------------
# Class 1: MarkovBuilder
# ---------------------------------------------------------------------------

class MarkovBuilder:

    def __init__(self, min_samples: int = MIN_SAMPLES):
        self.min_samples = min_samples

    # ── transition counting ───────────────────────────────────────────────

    def _count_transitions(self, states: pd.Series) -> tuple[dict, dict]:
        """
        Count state→state transitions and per-state occurrence counts.
        Returns (count_matrix, state_counts).
        """
        counts  = defaultdict(lambda: defaultdict(int))
        s_count = defaultdict(int)

        prev = None
        for s in states:
            if not isinstance(s, str) or not s:
                prev = None
                continue
            s_count[s] += 1
            if prev is not None:
                counts[prev][s] += 1
            prev = s

        return dict(counts), dict(s_count)

    def _filter_min_samples(
        self,
        counts: dict,
        state_counts: dict,
    ) -> tuple[dict, set]:
        """
        Remove states with fewer than min_samples occurrences.
        Also removes transition targets that are themselves below threshold.
        Returns (filtered_counts, valid_states).
        """
        valid = {s for s, c in state_counts.items() if c >= self.min_samples}
        filtered = {}
        for src, row in counts.items():
            if src not in valid:
                continue
            filtered_row = {dst: c for dst, c in row.items() if dst in valid}
            if filtered_row:
                filtered[src] = filtered_row
        return filtered, valid

    def build_transition_matrix(
        self, state_sequence: pd.Series
    ) -> tuple[dict, dict, int]:
        """
        Build and normalise the global transition matrix.
        Excludes states below min_samples threshold.
        Returns (prob_matrix, raw_count_matrix, total_raw_transitions).
        """
        counts, s_count = self._count_transitions(state_sequence)
        filtered, valid = self._filter_min_samples(counts, s_count)

        matrix     = {}
        raw_counts = {}
        for src, row in filtered.items():
            prob_row = _normalise_row(row)
            if prob_row:
                matrix[src]     = prob_row
                raw_counts[src] = dict(row)

        total_transitions = sum(sum(r.values()) for r in filtered.values())
        log.info(
            f"Transition matrix: {len(matrix)} states, "
            f"{int(total_transitions)} transitions "
            f"(excluded {len(s_count) - len(valid)} states below {self.min_samples} samples)"
        )
        return matrix, raw_counts, int(total_transitions)

    # ── session-conditioned matrices ──────────────────────────────────────

    def build_session_matrices(
        self, df: pd.DataFrame
    ) -> tuple[dict, dict, dict]:
        """
        Build transition matrices conditioned on trading session.
        Uses state_session column; OVERLAP counts as LONDON.
        Returns (prob_matrices, raw_counts, transition_totals)
          where keys are: global, tokyo, london, ny
        """
        prob_mats  = {}
        raw_mats   = {}
        totals     = {}

        mat, raw, total = self.build_transition_matrix(df["state_id"])
        prob_mats["global"] = mat
        raw_mats["global"]  = raw
        totals["global"]    = total

        sess_col = "state_session" if "state_session" in df.columns else None
        if sess_col is None:
            log.warning("No state_session column — session matrices unavailable")
            for sess in SESSIONS:
                prob_mats[sess] = {}
                raw_mats[sess]  = {}
                totals[sess]    = 0
            return prob_mats, raw_mats, totals

        for sess in SESSIONS:
            if sess == "london":
                mask = df[sess_col].isin(["LONDON", "OVERLAP"])
            else:
                mask = df[sess_col] == sess.upper()

            sub = df.loc[mask, "state_id"]
            if len(sub) < self.min_samples * 2:
                log.warning(f"Session {sess}: only {len(sub)} bars — sparse matrix")
                prob_mats[sess] = {}
                raw_mats[sess]  = {}
                totals[sess]    = 0
            else:
                m, r, t = self.build_transition_matrix(sub)
                prob_mats[sess] = m
                raw_mats[sess]  = r
                totals[sess]    = t
                log.info(f"Session {sess}: {len(m)} states in matrix")

        return prob_mats, raw_mats, totals

    # ── stationary distribution ───────────────────────────────────────────

    def compute_stationary_distribution(self, matrix: dict) -> dict:
        """
        Solve for the long-run (stationary) probability of each state.
        Applies epsilon-smoothing (0.01) before solving to guarantee ergodicity
        — avoids absorbing-state collapse when self-transition = 1.0.
        Returns dict of {state: probability}, descending.
        """
        if not matrix:
            return {}

        arr, states = _matrix_to_numpy(matrix)
        n = len(states)
        if n == 0:
            return {}

        # Ensure rows sum to 1
        row_sums = arr.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1.0
        arr = arr / row_sums

        # Epsilon-smoothing: blend with uniform to break absorbing states
        eps = 0.01
        arr = (1 - eps) * arr + eps * np.ones((n, n)) / n

        # Power iteration — reliable for small-medium matrices
        pi = np.ones(n) / n
        for _ in range(2000):
            pi_new = pi @ arr
            if np.allclose(pi_new, pi, atol=1e-10):
                break
            pi = pi_new
        pi = np.abs(pi) / np.abs(pi).sum()

        result = {states[i]: round(float(pi[i]), 6) for i in range(n)}
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))

    # ── dwell times ───────────────────────────────────────────────────────

    def compute_dwell_times(self, state_sequence: pd.Series) -> dict:
        """
        For each state: average consecutive bars before transitioning out.
        Returns dict of {state: avg_bars}.
        """
        runs      = defaultdict(list)
        prev      = None
        run_len   = 0

        for s in state_sequence:
            if not isinstance(s, str) or not s:
                if prev is not None:
                    runs[prev].append(run_len)
                prev = None
                run_len = 0
                continue
            if s == prev:
                run_len += 1
            else:
                if prev is not None:
                    runs[prev].append(run_len)
                prev    = s
                run_len = 1

        if prev is not None:
            runs[prev].append(run_len)

        return {
            state: round(float(np.mean(lens)), 2)
            for state, lens in runs.items()
            if len(lens) >= 2   # need at least 2 runs to be meaningful
        }


# ---------------------------------------------------------------------------
# Class 2: MarkovForecaster
# ---------------------------------------------------------------------------

class MarkovForecaster:

    def forecast_next_states(
        self,
        current_state: str,
        matrices: dict,
        session: str = "global",
        top_n: int = 3,
        state_stats: Optional[dict] = None,
    ) -> Optional[list]:
        """
        Forecast top N next states from current_state.
        Tries session-conditioned matrix first, falls back to global.
        Returns list of {state, probability, ev} or None.
        """
        sess_key = SESSION_MAP.get(session.upper(), "global") or "global"

        # Try session matrix, fall back to global
        matrix = matrices.get(sess_key, {})
        if current_state not in matrix:
            matrix = matrices.get("global", {})

        if current_state not in matrix:
            log.warning(f"State '{current_state}' not found in any matrix")
            return None

        row = matrix[current_state]
        sorted_next = sorted(row.items(), key=lambda x: x[1], reverse=True)

        results = []
        for state, prob in sorted_next[:top_n]:
            ev = 0.0
            if state_stats and state in state_stats:
                ev = state_stats[state].get("ev", 0.0)
            results.append({
                "state":       state,
                "probability": round(prob, 4),
                "ev":          round(ev, 3),
            })

        return results

    def chapman_kolmogorov(
        self,
        current_state: str,
        matrix: dict,
        steps: list = None,
    ) -> dict:
        """
        Compute n-step transition probabilities via matrix exponentiation.
        P(state at t+n | state at t) for each n in steps.
        Returns {step: {state: probability}} for top states.
        """
        if steps is None:
            steps = [1, 3, 5, 10]

        if not matrix or current_state not in matrix:
            log.warning(f"CK: state '{current_state}' not in matrix")
            return {}

        arr, states = _matrix_to_numpy(matrix)
        n = len(states)
        if n == 0 or current_state not in states:
            log.warning(f"CK: state not in matrix states list")
            return {}

        # Ensure rows sum to 1
        row_sums = arr.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1.0
        arr = arr / row_sums

        start_idx = states.index(current_state)
        result    = {}
        P         = arr.copy()

        # Start: probability mass entirely in current_state
        dist = np.zeros(n)
        dist[start_idx] = 1.0

        step_done = 0
        for step in sorted(steps):
            # Advance (step - step_done) more matrix multiplications
            for _ in range(step - step_done):
                dist = dist @ arr
            step_done = step

            # Top states at this horizon (threshold > 0.1%)
            top = sorted(
                ((states[i], float(dist[i])) for i in range(n) if dist[i] > 0.001),
                key=lambda x: x[1], reverse=True,
            )[:10]
            result[step] = {s: round(p, 5) for s, p in top}

        return result

    def bayesian_update(
        self,
        prior_matrix: dict,
        from_state: str,
        to_state:   str,
        alpha: float = 0.1,
    ) -> dict:
        """
        Update the transition row for from_state after observing a
        from_state -> to_state transition.
        alpha: learning rate (0 = ignore new evidence, 1 = replace with new).
        Returns updated matrix (copy).
        """
        import copy
        updated = copy.deepcopy(prior_matrix)

        if from_state not in updated:
            log.warning(f"Bayesian update: from_state '{from_state}' not in matrix")
            return updated

        row = updated[from_state]

        # Likelihood vector: spike at to_state
        likelihood = {s: 0.0 for s in row}
        if to_state in likelihood:
            likelihood[to_state] = 1.0
        else:
            # New state observed — add it with alpha weight
            likelihood[to_state] = 1.0
            row[to_state] = 0.0

        # Posterior: (1-alpha)*prior + alpha*likelihood
        posterior = {
            s: (1 - alpha) * row.get(s, 0.0) + alpha * likelihood.get(s, 0.0)
            for s in set(list(row.keys()) + list(likelihood.keys()))
        }
        total = sum(posterior.values())
        if total > 0:
            posterior = {s: round(v / total, 6) for s, v in posterior.items() if v > 0}

        updated[from_state] = posterior
        return updated


# ---------------------------------------------------------------------------
# Class 3: MarkovAnalyzer
# ---------------------------------------------------------------------------

class MarkovAnalyzer:

    def highest_ev_transitions(
        self,
        matrix: dict,
        state_stats: dict,
        top_n: int = 20,
    ) -> list:
        """
        Find state→state transitions with best historical EV.
        Only includes transitions where both states have known EV stats.
        """
        candidates = []
        for from_state, row in matrix.items():
            for to_state, prob in row.items():
                if to_state not in state_stats:
                    continue
                ev = state_stats[to_state].get("ev", 0.0)
                wr = state_stats[to_state].get("win_rate", 0.0)
                n  = state_stats[to_state].get("sample_count", 0)
                candidates.append({
                    "from_state":   from_state,
                    "to_state":     to_state,
                    "probability":  round(prob, 4),
                    "ev":           round(ev, 3),
                    "win_rate":     round(wr, 3),
                    "sample_count": n,
                    "score":        round(prob * ev, 4),
                })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:top_n]

    def most_persistent_states(
        self,
        matrix: dict,
        dwell_times: dict,
        threshold: float = PERSIST_GATE,
    ) -> list:
        """
        Find states where self-transition probability >= threshold.
        These are stable / trending states — Markov gate 2.
        """
        persistent = []
        for state, row in matrix.items():
            self_prob = row.get(state, 0.0)
            if self_prob >= threshold:
                persistent.append({
                    "state":              state,
                    "self_transition":    round(self_prob, 4),
                    "avg_dwell_bars":     dwell_times.get(state, None),
                    "outgoing_states":    len(row) - (1 if state in row else 0),
                })

        persistent.sort(key=lambda x: x["self_transition"], reverse=True)
        return persistent

    def regime_clusters(self, matrix: dict) -> dict:
        """
        Group states into macro regime clusters based on transition similarity.
        Uses cosine similarity of transition rows, then k-means style labelling.
        Labels: TRENDING / RANGING / VOLATILE / COMPRESSING
        """
        if len(matrix) < 4:
            return {s: "UNKNOWN" for s in matrix}

        arr, states = _matrix_to_numpy(matrix)

        # Pad rows to sum to 1 for states with partial coverage
        row_sums = arr.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1.0
        arr_norm = arr / row_sums

        # Heuristic cluster assignment using state name tokens
        clusters = {}
        for state in states:
            parts = state.split("_")
            # Extract key tokens (handle TREND_CONT 8-part IDs)
            trend     = parts[0] if len(parts) >= 1 else "NEUTRAL"
            momentum  = parts[1] if len(parts) >= 2 else "NEUTRAL"
            vol       = parts[2] if len(parts) >= 3 else "NORMAL"
            structure = parts[5] if len(parts) >= 6 else "RANGE"
            # Handle TREND_CONT (8 parts)
            if len(parts) == 8:
                structure = parts[5] + "_" + parts[6]

            # Assign regime label from token combinations
            if structure in ("BOS", "MSS", "CHOCH") and trend in ("BULL", "BEAR"):
                regime = "TRENDING"
            elif structure == "PULLBACK" and vol == "LOW":
                regime = "COMPRESSING"
            elif structure in ("RANGE", "TREND_CONT") and trend == "NEUTRAL":
                regime = "RANGING"
            elif vol == "HIGH" and structure in ("BOS", "CHOCH", "MSS"):
                regime = "VOLATILE"
            elif trend == "NEUTRAL" and momentum == "NEUTRAL":
                regime = "RANGING"
            elif vol == "HIGH":
                regime = "VOLATILE"
            else:
                regime = "COMPRESSING"

            clusters[state] = regime

        return clusters

    def print_analysis(
        self,
        matrices:       dict,
        dwell_times:    dict,
        stationary:     dict,
        instrument:     str,
        timeframe:      str,
        top_ev:         list = None,
        persistent:     list = None,
        clusters:       dict = None,
        ck_example:     dict = None,
        ck_state:       str  = "",
    ):
        sep = "=" * 72
        print(f"\n{sep}")
        print(f"  MARKOV ENGINE REPORT — {instrument} {timeframe}")
        print(sep)

        global_matrix = matrices.get("global", {})
        total_states  = len(global_matrix)
        total_trans   = sum(len(r) for r in global_matrix.values())

        print(f"  Global matrix  : {total_states} states, {total_trans} transition pairs")
        for sess in SESSIONS:
            sm = matrices.get(sess, {})
            print(f"  {sess.upper():<8} matrix : {len(sm)} states")

        # Most frequent state transitions — use empirical frequency
        print(f"\n  TOP 10 NEXT-STATE PROBABILITIES (from most frequent state):")
        print(f"  {'-'*70}")
        if global_matrix:
            # Pick state that appears most in the matrix AND has highest empirical freq
            top_state = max(
                global_matrix,
                key=lambda s: len(global_matrix[s]),   # most outgoing transitions = richest row
            )
            row = sorted(global_matrix[top_state].items(), key=lambda x: x[1], reverse=True)
            print(f"  From: {top_state}")
            for to_s, prob in row[:10]:
                bar = "#" * int(prob * 40)
                print(f"    {to_s:<55} {prob:.3f}  {bar}")

        # Persistent states
        print(f"\n  PERSISTENT STATES (self-transition >= {PERSIST_GATE}):")
        print(f"  {'-'*70}")
        if persistent:
            for p in persistent[:10]:
                dw = f"{p['avg_dwell_bars']:.1f}" if p["avg_dwell_bars"] else "?"
                print(f"    {p['state']:<55} self={p['self_transition']:.3f}  dwell={dw}b")
        else:
            print(f"    None found at threshold {PERSIST_GATE}")

        # Top EV transitions
        print(f"\n  TOP 5 EV TRANSITIONS (probability × ev score):")
        print(f"  {'-'*70}")
        if top_ev:
            for t in top_ev[:5]:
                print(f"    {t['from_state']:<40} -> {t['to_state']:<40}")
                print(f"      prob={t['probability']:.3f}  ev={t['ev']:.2f}  "
                      f"wr={t['win_rate']:.1%}  score={t['score']:.3f}")
        else:
            print("    No state_stats provided — run Phase 5 first for EV data")

        # Regime clusters
        if clusters:
            from collections import Counter
            counts = Counter(clusters.values())
            print(f"\n  REGIME CLUSTER DISTRIBUTION:")
            print(f"  {'-'*70}")
            for regime, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                bar = "#" * int(cnt / max(counts.values()) * 30)
                print(f"    {regime:<15} {cnt:>4} states  {bar}")

        # Chapman-Kolmogorov example
        if ck_example:
            print(f"\n  CHAPMAN-KOLMOGOROV FORECAST:")
            print(f"  Current state: {ck_state}")
            print(f"  {'-'*70}")
            for step, dist in ck_example.items():
                top3 = list(dist.items())[:3]
                print(f"  t+{step:>2}: ", end="")
                for s, p in top3:
                    print(f"{s} ({p:.3f})  ", end="")
                print()

        # Stationary distribution top 5
        if stationary:
            top_stat = sorted(stationary.items(), key=lambda x: x[1], reverse=True)[:5]
            print(f"\n  STATIONARY DISTRIBUTION (long-run tendencies, top 5):")
            print(f"  {'-'*70}")
            for s, p in top_stat:
                print(f"    {s:<55} {p:.4f}")

        print(f"{sep}\n")


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_states(instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
    path = STATES_DIR / f"H2_states_{instrument}_{timeframe}.parquet"
    if not path.exists():
        log.error(f"States parquet not found: {path}")
        return None
    df = pd.read_parquet(path)
    log.info(f"Loaded states: {instrument} {timeframe} — {len(df)} rows")
    return df


def save_transition_matrix(
    data:       dict,
    instrument: str,
    timeframe:  str,
) -> Path:
    fname = f"H2_transition_matrix_{instrument}_{timeframe}.json"
    out   = OUTPUTS_DIR / fname
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info(f"Saved: {out}")
    return out


def load_transition_matrix(instrument: str, timeframe: str) -> Optional[dict]:
    fname = f"H2_transition_matrix_{instrument}_{timeframe}.json"
    path  = OUTPUTS_DIR / fname
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_dwell_times(all_dwell: dict) -> Path:
    out = OUTPUTS_DIR / "H2_dwell_times.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_dwell, f, indent=2)
    log.info(f"Saved dwell times: {out}")
    return out


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build(
    instrument:  str,
    timeframe:   str,
    state_stats: Optional[dict] = None,
    verbose:     bool = True,
) -> Optional[dict]:
    """
    Full Markov pipeline for one instrument+timeframe.
    Returns the complete output dict (also saved to JSON).
    """
    df = load_states(instrument, timeframe)
    if df is None:
        return None

    builder    = MarkovBuilder(min_samples=MIN_SAMPLES)
    forecaster = MarkovForecaster()
    analyzer   = MarkovAnalyzer()

    # ── build matrices ────────────────────────────────────────────────────
    log.info(f"Building session matrices: {instrument} {timeframe}")
    matrices, raw_counts, trans_totals = builder.build_session_matrices(df)

    # ── derived quantities ────────────────────────────────────────────────
    global_matrix = matrices.get("global", {})
    stationary    = builder.compute_stationary_distribution(global_matrix)
    dwell_times   = builder.compute_dwell_times(df["state_id"])
    persistent    = analyzer.most_persistent_states(global_matrix, dwell_times)
    clusters      = analyzer.regime_clusters(global_matrix)
    top_ev        = analyzer.highest_ev_transitions(global_matrix, state_stats or {})

    # ── Chapman-Kolmogorov on most recent live state ──────────────────────
    live_state = df["state_id"].iloc[-1] if len(df) > 0 else None
    ck_example = {}
    if live_state and live_state in global_matrix:
        ck_example = forecaster.chapman_kolmogorov(
            live_state, global_matrix, steps=[1, 3, 5, 10]
        )

    # ── total transition count (from raw counts, not probabilities) ───────
    total_transitions     = trans_totals.get("global", 0)
    total_raw_transitions = len(df) - 1

    # ── empirical state frequencies (for "most frequent" lookup) ─────────
    state_freq = df["state_id"].value_counts().to_dict()

    # ── assemble output JSON ──────────────────────────────────────────────
    output = {
        "instrument":    instrument,
        "timeframe":     timeframe,
        "generated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_states":  len(global_matrix),
        "total_transitions": total_transitions,
        "total_raw_transitions": total_raw_transitions,
        "matrices": {
            sess: mat for sess, mat in matrices.items()
        },
        "raw_counts": {
            sess: rc for sess, rc in raw_counts.items()
        },
        "stationary_distribution": stationary,
        "dwell_times":   dwell_times,
        "state_freq":    {k: int(v) for k, v in state_freq.items()
                          if k in global_matrix},
        "top_ev_transitions": top_ev,
        "persistent_states": persistent,
        "regime_clusters":   clusters,
        "live_state":        live_state,
        "ck_forecast":       ck_example,
    }

    save_transition_matrix(output, instrument, timeframe)

    # ── print validation report ───────────────────────────────────────────
    if verbose:
        analyzer.print_analysis(
            matrices     = matrices,
            dwell_times  = dwell_times,
            stationary   = stationary,
            instrument   = instrument,
            timeframe    = timeframe,
            top_ev       = top_ev,
            persistent   = persistent,
            clusters     = clusters,
            ck_example   = ck_example,
            ck_state     = live_state or "",
        )

    return output


def build_batch(
    instruments: Optional[list] = None,
    timeframes:  Optional[list] = None,
    verbose:     bool = False,
) -> dict:
    """
    Run Markov engine on all instrument×timeframe combinations.
    Aggregates dwell times across all combinations and saves H2_dwell_times.json.
    """
    insts = instruments or (
        CFG["instruments"]["primary"] + CFG["instruments"].get("forex", [])
    )
    tfs = timeframes or CFG["timeframes"]["research"]

    results    = {}
    all_dwell  = {}

    for inst in insts:
        for tf in tfs:
            key = f"{inst}_{tf}"
            try:
                out = build(inst, tf, verbose=verbose)
                if out is not None:
                    results[key] = f"ok: {out['total_states']} states, {out['total_transitions']} transitions"
                    all_dwell[key] = out.get("dwell_times", {})
                else:
                    results[key] = "fail: no states file"
            except Exception as e:
                log.error(f"Markov batch error {key}: {e}", exc_info=True)
                results[key] = f"error: {e}"

    save_dwell_times(all_dwell)

    ok  = sum(1 for v in results.values() if v.startswith("ok"))
    log.info(f"Markov batch complete: {ok}/{len(results)} succeeded")
    return results


# ---------------------------------------------------------------------------
# Consolidated summary table
# ---------------------------------------------------------------------------

def print_batch_summary(instruments: list, timeframes: list):
    sep = "=" * 100
    print(f"\n{sep}")
    print("  MARKOV ENGINE — CONSOLIDATED SUMMARY")
    print(sep)
    print(f"  {'INSTRUMENT':<10} {'TF':<5} {'STATES':>7} {'TRANS':>8} "
          f"{'PERSISTENT':>10} {'CLUSTERS':>30}  LIVE STATE")
    print(f"  {'-'*98}")

    for inst in instruments:
        for tf in timeframes:
            path = OUTPUTS_DIR / f"H2_transition_matrix_{inst}_{tf}.json"
            if not path.exists():
                print(f"  {inst:<10} {tf:<5} {'NO DATA':>7}")
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                n_states     = d.get("total_states", 0)
                n_trans      = d.get("total_transitions", 0)
                n_persist    = len(d.get("persistent_states", []))
                clusters_raw = d.get("regime_clusters", {})
                from collections import Counter
                c = Counter(clusters_raw.values())
                cluster_str  = " ".join(f"{k[0]}:{v}" for k, v in sorted(c.items()))
                live         = (d.get("live_state") or "")[:45]
                print(f"  {inst:<10} {tf:<5} {n_states:>7} {n_trans:>8} "
                      f"{n_persist:>10} {cluster_str:>30}  {live}")
            except Exception as e:
                print(f"  {inst:<10} {tf:<5} ERR: {e}")

    print(sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    INSTRUMENTS = [
        "JP225","DE40","UK100","USTEC","US30","HK50",
        "EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD",
        "EURJPY","GBPJPY","XAUUSD","XAGUSD",
    ]
    TIMEFRAMES = ["D", "4H", "1H", "15m"]

    parser = argparse.ArgumentParser(description="H2 Quant — Markov Engine (Phase 4)")
    parser.add_argument("--instrument", "-i")
    parser.add_argument("--timeframe",  "-t")
    parser.add_argument("--batch",  action="store_true")
    parser.add_argument("--summary", action="store_true", help="Print summary table only")
    args = parser.parse_args()

    if args.summary:
        print_batch_summary(INSTRUMENTS, TIMEFRAMES)

    elif args.batch:
        results = build_batch(INSTRUMENTS, TIMEFRAMES, verbose=False)
        ok = sum(1 for v in results.values() if v.startswith("ok"))
        print(f"\nBatch: {ok}/{len(results)} succeeded")
        print_batch_summary(INSTRUMENTS, TIMEFRAMES)
        print("\nPhase 4 complete. Ready for Phase 5 — Research Validator.")

    elif args.instrument and args.timeframe:
        out = build(args.instrument, args.timeframe, verbose=True)
        if out is None:
            print("Failed — check logs/markov.log")
            sys.exit(1)
        print(f"\nDone. {out['total_states']} states | {out['total_transitions']} transitions")
    else:
        parser.print_help()
