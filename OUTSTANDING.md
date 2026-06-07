# H2 Quant System — Outstanding Items

_Audited 2026-06-07. All 9 phases are present and functional. Items below are gaps,
known limitations, or production-readiness steps that remain._

---

## 1. Feature Engineering (Phase 2)

| # | Item | Detail |
|---|------|--------|
| 1.1 | GARCH(1,1) is a proxy only | Uses EWM-weighted variance, not a true GARCH fit. Replace with `arch` library `arch_model(...).fit()` for production accuracy. |
| 1.2 | DXY correlation uses autocorrelation fallback | When no DXY proxy instrument is supplied, dimension 13 falls back to lag-1 autocorrelation with 0.3 confidence. Pass `--dxy USDJPY` at runtime or hardcode in `config.yaml`. |
| 1.3 | Breadth (dim 18) and Open Interest (dim 19) explicitly skipped | Correct per spec. Document in config so it is not mistaken for a bug. |

---

## 2. Live Monitor (Phase 7)

| # | Item | Detail |
|---|------|--------|
| 2.1 | `dry_run: true` in config.yaml | Webhooks and WhatsApp will not fire until this is flipped to `false`. Intentional safety default — flip only when Railway bridge and MetaAPI are verified live. |
| 2.2 | MT5 must be running on Windows | The monitor calls the MT5 Python API; if MT5 is closed, data fetch falls back silently or errors. Add a startup health-check that asserts MT5 connectivity before the loop begins. |
| 2.3 | Outcome tracking (`outcome_r`) is never written back | `update_outcome()` in `monitor.py:559` exists but is never called — there is no mechanism to record trade P&L back into `H2_signal_log.csv`. Requires a MetaAPI trade-closed webhook listener. |

---

## 3. Research / Validation (Phase 5)

| # | Item | Detail |
|---|------|--------|
| 3.1 | Walk-forward validation not yet run end-to-end | `validator.py` is complete but the full OOS report (`H2_backtest_report.html`) has not been generated for all 50 instruments across all timeframes. Run `run_batch.py` to generate. |
| 3.2 | Monte Carlo uses transition matrix only | 10,000-path MC is implemented but does not account for regime shifts (e.g. sudden volatility expansion). Consider conditioning paths on volatility regime label. |

---

## 4. TradingView Overlay (Phase 8)

| # | Item | Detail |
|---|------|--------|
| 4.1 | Pine script reads from a hardcoded URL | `H2_state_overlay.pine` fetches `H2_live_state.json` via a fixed Railway URL. If the bridge URL changes, the script must be manually updated. Consider making the URL an indicator input. |
| 4.2 | Test script `H2_state_overlay_TEST.pine` and `H2_state_overlay_EURJPY_LIVE.pine` are uncommitted variants | These can be cleaned up or merged into a single parameterised script. |

---

## 5. Production / Infrastructure

| # | Item | Detail |
|---|------|--------|
| 5.1 | No GitHub remote set | Repo is local only. Add remote and push: `git remote add origin <url> && git push -u origin master`. |
| 5.2 | No `requirements.txt` or `pyproject.toml` | Dependencies are documented in `CLAUDE.md` but not pinned. Add a `requirements.txt` so the environment is reproducible. |
| 5.3 | `logs/` directory not created on first run | `monitor.py` writes to `logs/` but there is no `mkdir -p logs` guard at startup. Add to `ensure_signal_log()` or a startup block. |
| 5.4 | `test_whatsapp.py` is a dev script in the repo root | Move to a `tests/` or `scripts/` folder or add to `.gitignore` to keep the root clean. |

---

## 6. Session Brief (Phase 9 / Cowork)

| # | Item | Detail |
|---|------|--------|
| 6.1 | News / risk flag section is not implemented | `H2_BRIEF_PROMPT.md` and `briefing/generator.py` both reference a "news events in next 2 hours" section (Section 5 of the report), but no news feed is wired up. Either integrate a free economic calendar API or mark the section as manual. |

---

## Summary

- **Blocking before going live:** items 2.1 (flip dry_run), 2.2 (MT5 health-check), 5.1 (push to GitHub)
- **Important but not blocking:** 1.1 (true GARCH), 2.3 (outcome tracking), 3.1 (run full backtest)
- **Nice to have / cleanup:** 1.2, 3.2, 4.1, 4.2, 5.2, 5.3, 5.4, 6.1
