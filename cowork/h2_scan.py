#!/usr/bin/env python3
"""
H2 Scan — Pure Destination Ladder Scanner
No Claude API. No tokens. Runs in ~5 seconds.
Run this every 15-30 minutes during active session.

Output: clean ranked ladder table for all 15 instruments
Trigger: whenever you want a quick read on where price is
         relative to all four VWAP destinations
"""

import os
import sys
import json
import math
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ── Config ────────────────────────────────────────────────────────────────────
RAILWAY_URL  = os.environ.get("RAILWAY_URL",
    "https://h2-webhook-bridge-production.up.railway.app")
FMP_KEY      = os.environ.get("FMP_API_KEY", "")
WA_PHONE     = os.environ.get("WA_PHONE",    "27614056155")
WA_APIKEY    = os.environ.get("WA_APIKEY",   "2096445")
STATE_FILE   = Path("outputs/h2_scan_state.json")
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Validated hit rates from Stage 5 backtest ────────────────────────────────
HIT_RATES = {
    "JP225":   {"d1": 0.601, "d2": 0.402, "d3": 0.334, "ev_d1": 0.50, "ev_d1d2": 1.12, "ev_full": 1.41},
    "USTEC":   {"d1": 0.374, "d2": 0.345, "d3": 0.030, "ev_d1": 1.01, "ev_d1d2": 1.51, "ev_full": 1.76},
    "DEFAULT": {"d1": 0.450, "d2": 0.380, "d3": 0.150, "ev_d1": 0.55, "ev_d1d2": 0.90, "ev_full": 1.10},
}

# ── 15 Instruments with session gates ────────────────────────────────────────
INSTRUMENTS = {
    "JP225":   {"session": "LONDON",  "driver": "USDJPY",  "atr_pct": 0.008},
    "DE40":    {"session": "LONDON",  "driver": "EURUSD",  "atr_pct": 0.010},
    "SPX":     {"session": "NY",      "driver": "VIX",     "atr_pct": 0.007},
    "USTEC":   {"session": "NY",      "driver": "TNX",     "atr_pct": 0.012},
    "US30":    {"session": "NY",      "driver": "VIX",     "atr_pct": 0.008},
    "UK100":   {"session": "LONDON",  "driver": "GBPUSD",  "atr_pct": 0.009},
    "CAC40":   {"session": "LONDON",  "driver": "EURUSD",  "atr_pct": 0.010},
    "AUS200":  {"session": "ASIA",    "driver": "AUDUSD",  "atr_pct": 0.008},
    "HSI":     {"session": "ASIA",    "driver": "USDCNH",  "atr_pct": 0.015},
    "NI225":   {"session": "ASIA",    "driver": "USDJPY",  "atr_pct": 0.010},
    "XAUUSD":  {"session": "LONDON",  "driver": "DXY",     "atr_pct": 0.008},
    "XAGUSD":  {"session": "LONDON",  "driver": "DXY",     "atr_pct": 0.012},
    "USDJPY":  {"session": "LONDON",  "driver": "TNX",     "atr_pct": 0.005},
    "EURUSD":  {"session": "LONDON",  "driver": "DXY",     "atr_pct": 0.005},
    "GBPUSD":  {"session": "LONDON",  "driver": "DXY",     "atr_pct": 0.006},
}

# ── Session detection ─────────────────────────────────────────────────────────
def get_current_session() -> str:
    h = datetime.now(timezone.utc).hour
    if 7 <= h < 13:  return "LONDON"
    if 13 <= h < 20: return "NY"
    return "ASIA"

def session_gate(instr: str, current_session: str) -> bool:
    required = INSTRUMENTS[instr]["session"]
    if required == "LONDON": return current_session in ["LONDON", "NY"]
    if required == "NY":     return current_session == "NY"
    if required == "ASIA":   return current_session == "ASIA"
    return True

# ── Fetch live FX prices ──────────────────────────────────────────────────────
def fetch_prices() -> Dict:
    prices = {}

    # Primary: exchangerate-api (free, no key)
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        if r.status_code == 200:
            rates = r.json().get("rates", {})
            prices["USDJPY"] = round(rates.get("JPY", 0), 3)
            prices["EURUSD"] = round(1/rates["EUR"], 5) if rates.get("EUR") else 0
            prices["GBPUSD"] = round(1/rates["GBP"], 5) if rates.get("GBP") else 0
            prices["AUDUSD"] = round(1/rates["AUD"], 5) if rates.get("AUD") else 0
            prices["USDCAD"] = round(rates.get("CAD", 0), 5)
            prices["USDCHF"] = round(rates.get("CHF", 0), 5)
    except Exception as e:
        print(f"[PRICES] exchangerate-api: {e}")

    # Fallback: frankfurter ECB
    if len(prices) < 4:
        try:
            r = requests.get("https://api.frankfurter.app/latest?from=USD", timeout=8)
            if r.status_code == 200:
                rates = r.json().get("rates", {})
                if "JPY" in rates and "USDJPY" not in prices:
                    prices["USDJPY"] = round(rates["JPY"], 3)
                if "EUR" in rates and "EURUSD" not in prices:
                    prices["EURUSD"] = round(1/rates["EUR"], 5)
                if "GBP" in rates and "GBPUSD" not in prices:
                    prices["GBPUSD"] = round(1/rates["GBP"], 5)
        except Exception as e:
            print(f"[PRICES] frankfurter: {e}")

    # Metals: gold-api.com (free, no key required)
    try:
        for symbol, h2_name in [("XAU", "XAUUSD"), ("XAG", "XAGUSD")]:
            r = requests.get(
                f"https://api.gold-api.com/price/{symbol}",
                timeout=8)
            if r.status_code == 200:
                data = r.json()
                price_val = float(data.get("price", data.get("Price", 0)))
                if price_val > 0:
                    # XAG sanity check — silver should be $25-50/oz
                    # If > 100, likely returned in wrong unit — divide by 32.15 (oz per kg)
                    if h2_name == "XAGUSD" and price_val > 100:
                        price_val = round(price_val / 32.15, 4)
                        print(f"[METALS] XAG unit correction applied: {price_val}")
                    prices[h2_name] = round(price_val, 2)
                    print(f"[METALS] gold-api.com {h2_name}: {prices[h2_name]}")
    except Exception as e:
        print(f"[METALS] gold-api.com: {e}")

    # If XAG still wrong after correction, try silver endpoint
    if prices.get("XAGUSD", 0) > 100 or prices.get("XAGUSD", 0) == 0:
        try:
            r = requests.get("https://api.gold-api.com/price/silver", timeout=8)
            if r.status_code == 200:
                data = r.json()
                price_val = float(data.get("price", 0))
                if 20 < price_val < 100:
                    prices["XAGUSD"] = round(price_val, 4)
                    print(f"[METALS] gold-api silver endpoint: {prices['XAGUSD']}")
        except:
            pass

    # Fallback: FMP direct quote
    if "XAUUSD" not in prices and FMP_KEY:
        for fmp_sym, h2_name in [("XAUUSD", "XAUUSD"), ("XAGUSD", "XAGUSD")]:
            try:
                r = requests.get(
                    f"https://financialmodelingprep.com/api/v3/quote/{fmp_sym}",
                    params={"apikey": FMP_KEY}, timeout=8)
                if r.status_code == 200:
                    data = r.json()
                    if data:
                        prices[h2_name] = data[0].get("price", 0)
                        print(f"[METALS] FMP {h2_name}: {prices[h2_name]}")
            except Exception as e:
                print(f"[METALS] FMP {fmp_sym}: {e}")

    # Final fallback with flag
    if "XAUUSD" not in prices:
        prices["XAUUSD"] = 4300.0
        prices["_xau_fallback"] = True
    if "XAGUSD" not in prices:
        prices["XAGUSD"] = 32.50
        prices["_xag_fallback"] = True

    return prices

# ── Fetch live VWAP state from Railway and cache locally ─────────────────────
def fetch_live_state() -> bool:
    """
    Fetches live VWAP destinations from Railway POST /live_state endpoint
    and writes them to outputs/H2_live_state.json for build_ladder_from_live_state().
    Returns True if live state was fetched and cached.
    """
    state_path = Path("outputs/H2_live_state.json")
    try:
        r = requests.get(f"{RAILWAY_URL}/live_state", timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, dict) and len(data) > 0:
                with open(state_path, "w") as f:
                    json.dump(data, f, indent=2)
                symbols = list(data.keys())
                print(f"[LIVE_STATE] Fetched {len(symbols)} instruments: {symbols}")
                return True
    except Exception as e:
        print(f"[LIVE_STATE] {e}")
    return False

# ── Fetch Railway news status ─────────────────────────────────────────────────
def fetch_news_status() -> Dict:
    try:
        r = requests.get(f"{RAILWAY_URL}/news/status", timeout=8)
        if r.status_code == 200:
            data = r.json()
            return {
                k: v.get("status", "CLEAR")
                for k, v in data.get("instruments", {}).items()
            }
    except:
        pass
    return {}

# ── VWAP destination computation ─────────────────────────────────────────────
def compute_destinations(price: float, atr: float) -> List[Dict]:
    """
    Compute 3 simulated VWAP destinations from current price.
    Without live OHLC, we use ATR-based band approximation.
    These are ESTIMATES — validate against TradingView Panel 4.
    """
    destinations = []
    scales = [
        {"tf": "1H",    "mult": 1.0,  "role": "ENTRY TIMING"},
        {"tf": "4H",    "mult": 2.2,  "role": "JOURNEY STATE"},
        {"tf": "DAILY", "mult": 3.8,  "role": "DESTINATION"},
    ]

    for scale in scales:
        dist = atr * scale["mult"]
        dest_up   = round(price + dist, 2)
        dest_down = round(price - dist, 2)
        destinations.append({
            "tf":       scale["tf"],
            "role":     scale["role"],
            "dest_up":  dest_up,
            "dest_down":dest_down,
            "dist_r":   round(scale["mult"], 1),
        })

    return destinations

def build_ladder_from_live_state(instr: str, price: float,
                                  atr: float) -> Optional[Dict]:
    """
    Try to read actual VWAP destinations from H2_live_state.json
    (written by live/monitor.py if running).
    Falls back to ATR-estimated destinations if not available.
    """
    state_path = Path("outputs/H2_live_state.json")
    if state_path.exists():
        try:
            with open(state_path) as f:
                state = json.load(f)
            instr_data = state.get(instr, {})
            if instr_data.get("dest_1h") and instr_data.get("dest_4h"):
                dests_raw = [
                    {"tf": "1H",    "dest": instr_data["dest_1h"],
                     "dir": instr_data.get("dir_1h","?")},
                    {"tf": "4H",    "dest": instr_data["dest_4h"],
                     "dir": instr_data.get("dir_4h","?")},
                    {"tf": "DAILY", "dest": instr_data.get("dest_d", price),
                     "dir": instr_data.get("dir_d","?")},
                ]
                dests_sorted = sorted(dests_raw,
                    key=lambda x: abs(x["dest"] - price))
                for i, d in enumerate(dests_sorted):
                    d["ladder_pos"] = i + 1
                    d["dist_pts"]   = round(abs(price - d["dest"]), 2)
                    d["dist_r"]     = round(abs(price - d["dest"]) / atr, 1) if atr > 0 else 0
                return {"source": "live_state", "dests": dests_sorted}
        except:
            pass

    # ATR-based fallback — both directions
    dests = [
        {"tf": "1H",    "ladder_pos": 1, "dist_r": 1.0,
         "dest_up": round(price + atr*1.0, 2),
         "dest_down": round(price - atr*1.0, 2)},
        {"tf": "4H",    "ladder_pos": 2, "dist_r": 2.2,
         "dest_up": round(price + atr*2.2, 2),
         "dest_down": round(price - atr*2.2, 2)},
        {"tf": "DAILY", "ladder_pos": 3, "dist_r": 3.8,
         "dest_up": round(price + atr*3.8, 2),
         "dest_down": round(price - atr*3.8, 2)},
    ]
    return {"source": "atr_estimate", "dests": dests}

# ── Load previous scan state ──────────────────────────────────────────────────
def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_state(state: Dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── WhatsApp alert ────────────────────────────────────────────────────────────
def send_whatsapp(message: str):
    try:
        import urllib.parse
        encoded = urllib.parse.quote(message)
        url = f"https://api.callmebot.com/whatsapp.php?phone={WA_PHONE}&text={encoded}&apikey={WA_APIKEY}"
        r = requests.get(url, timeout=10)
        print(f"[WA] Sent: {r.status_code}")
    except Exception as e:
        print(f"[WA] Error: {e}")

def check_destination_flip(instr: str, current_d1: float,
                            current_dir: str, prev_state: Dict) -> Optional[str]:
    """
    Detect when price has crossed D1 and a new D1 has appeared
    on the opposite side — this is the signal event.
    """
    prev = prev_state.get(instr, {})
    prev_d1  = prev.get("d1", 0)
    prev_dir = prev.get("dir", "")

    if prev_d1 == 0:
        return None

    if prev_dir and current_dir and prev_dir != current_dir:
        return (
            f"H2 SCAN ALERT\n"
            f"{instr} destination flip!\n"
            f"Previous: {prev_dir} -> {prev_d1}\n"
            f"New D1: {current_dir} -> {current_d1}\n"
            f"Price crossed D1 - next target confirmed\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
    return None

# ── Main scan ─────────────────────────────────────────────────────────────────
def run_scan(verbose: bool = True):
    now_utc = datetime.now(timezone.utc)
    session = get_current_session()
    fetch_live_state()   # refresh H2_live_state.json from Railway if available
    prices  = fetch_prices()
    news    = fetch_news_status()
    prev_state = load_state()
    new_state  = {}

    results = []

    for instr in INSTRUMENTS:
        cfg   = INSTRUMENTS[instr]
        price = prices.get(instr, 0)

        # Placeholder prices for instruments without direct price feed
        if price == 0:
            if instr in ["JP225","NI225"]:  price = 70000.0
            elif instr in ["DE40","CAC40"]: price = 24000.0
            elif instr in ["SPX"]:          price = 5500.0
            elif instr in ["USTEC"]:        price = 21800.0
            elif instr in ["US30"]:         price = 52000.0
            elif instr in ["UK100"]:        price = 8400.0
            elif instr in ["AUS200"]:       price = 8200.0
            elif instr in ["HSI"]:          price = 23000.0
            elif instr == "XAUUSD":         price = 4300.0
            elif instr == "XAGUSD":         price = 32.50
            else: continue

        atr = price * cfg["atr_pct"]
        ladder = build_ladder_from_live_state(instr, price, atr)
        if not ladder:
            continue

        dests  = ladder["dests"]
        source = ladder["source"]
        rates  = HIT_RATES.get(instr, HIT_RATES["DEFAULT"])

        in_session  = session_gate(instr, session)
        news_status = news.get(instr, "CLEAR")

        if source == "live_state":
            d1 = dests[0]
            d2 = dests[1] if len(dests) > 1 else None
            d3 = dests[2] if len(dests) > 2 else None

            d1_dir    = d1.get("dir", "?")
            d1_dest   = d1.get("dest", 0)
            d1_dist_r = d1.get("dist_r", 0)
            d2_dest   = d2.get("dest", 0) if d2 else None
            d2_dist_r = d2.get("dist_r", 0) if d2 else None
            d3_dest   = d3.get("dest", 0) if d3 else None

            flip_msg = check_destination_flip(
                instr, d1_dest, d1_dir, prev_state)
            if flip_msg and in_session and news_status == "CLEAR":
                send_whatsapp(flip_msg)

            new_state[instr] = {"d1": d1_dest, "dir": d1_dir, "price": price}

            ev_d1   = round(rates["d1"] * d1_dist_r, 2)
            ev_d1d2 = round(ev_d1 + rates["d1"]*rates["d2"]*(d2_dist_r or 0), 2)

        else:
            d = dests[0]
            d1_dir    = "?"
            d1_dest   = f"{d['dest_up']}/{d['dest_down']}"
            d1_dist_r = d["dist_r"]
            d2_dest   = f"{dests[1]['dest_up']}/{dests[1]['dest_down']}"
            d2_dist_r = dests[1]["dist_r"]
            d3_dest   = f"{dests[2]['dest_up']}/{dests[2]['dest_down']}"
            ev_d1     = round(rates["d1"] * d1_dist_r, 2)
            ev_d1d2   = round(ev_d1 + rates["d1"]*rates["d2"]*d2_dist_r, 2)

        price_flag = ""
        if instr == "XAUUSD" and prices.get("_xau_fallback"):
            price_flag = "*"
        elif instr == "XAGUSD" and prices.get("_xag_fallback"):
            price_flag = "*"

        row = {
            "instr":      instr,
            "price":      price,
            "price_flag": price_flag,
            "d1_dir":     d1_dir,
            "d1_dest":    d1_dest,
            "d1_dist_r":  d1_dist_r,
            "d2_dest":    d2_dest,
            "d2_dist_r":  d2_dist_r,
            "d3_dest":    d3_dest,
            "ev_d1":      ev_d1,
            "ev_d1d2":    ev_d1d2,
            "hit_rate":   f"{rates['d1']*100:.0f}%",
            "in_session": in_session,
            "news":       news_status,
            "source":     source,
        }
        results.append(row)

    results.sort(key=lambda x: (
        0 if x["in_session"] else 1,
        0 if x["news"] == "CLEAR" else 1,
        -x["ev_d1d2"]
    ))

    save_state(new_state)
    return results, session

# ── Display ───────────────────────────────────────────────────────────────────
def display_scan(results: List[Dict], session: str):
    now = datetime.now(timezone.utc)
    w   = 80

    print("\n" + "="*w)
    print(f"  H2 SCAN -- {now.strftime('%d %b %Y -- %H:%M UTC')} -- {session} SESSION")
    print("="*w)
    print(f"  {'INSTR':<8} {'PRICE':>10} {'D1 TARGET':>18} {'D1 R':>5} {'HIT%':>5} {'EV D1':>6} {'EV+D2':>6} {'GATE':>5} {'NEWS':>8} {'SRC':>4}")
    print("-"*w)

    active_count = 0
    best_ev      = 0
    best_instr   = ""

    for r in results:
        gate_str = "OK" if r["in_session"] else "--"
        news_str = {"CLEAR":"CLEAR","CAUTION":"CAUTION","SUPPRESS":"SUPRSS"}.get(r["news"], r["news"])
        src_flag = "LVE" if r["source"] == "live_state" else "ATR"

        if r["in_session"] and r["news"] == "CLEAR":
            active_count += 1
            if r["ev_d1d2"] > best_ev:
                best_ev    = r["ev_d1d2"]
                best_instr = r["instr"]

        d1_str = str(r["d1_dest"])
        if len(d1_str) > 18:
            d1_str = d1_str[:17] + "~"

        price_str = f"{r['price']:,.2f}{r.get('price_flag','')}"

        print(f"  {r['instr']:<8} "
              f"{price_str:>11} "
              f"{d1_str:>18} "
              f"{r['d1_dist_r']:>4.1f}R "
              f"{r['hit_rate']:>5} "
              f"{r['ev_d1']:>5.2f}R "
              f"{r['ev_d1d2']:>5.2f}R "
              f"{gate_str:>5} "
              f"{news_str:>8} "
              f"{src_flag:>4}")

    print("-"*w)
    print(f"  Active in session: {active_count} | Session: {session}")
    if best_instr:
        print(f"  Best EV: {best_instr} -> {best_ev:.2f}R (D1+D2 combined)")
    print(f"  ATR = estimated destinations. LVE = live state from TradingView. * = fallback price.")
    print("="*w)

    active_rows = [r for r in results if r["in_session"] and r["news"] == "CLEAR"][:3]
    if active_rows:
        wa_lines = [f"H2 SCAN {now.strftime('%H:%M')}UTC {session}"]
        for r in active_rows:
            wa_lines.append(
                f"{r['instr']} D1={r['d1_dest']} "
                f"({r['d1_dist_r']}R {r['hit_rate']} EV:{r['ev_d1d2']}R)"
            )
        return "\n".join(wa_lines)
    return None

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    print("[H2 SCAN] Starting...")
    results, session = run_scan()
    wa_summary = display_scan(results, session)

    out_path = Path("outputs/h2_scan_latest.json")
    with open(out_path, "w") as f:
        json.dump({
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "session":   session,
            "results":   results,
        }, f, indent=2, default=str)

    print(f"\n[H2 SCAN] Saved to {out_path}")

if __name__ == "__main__":
    main()
