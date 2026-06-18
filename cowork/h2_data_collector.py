"""
H2 Data Collector — fetches all live market data needed for the brief
Sources: FMP API (free tier), Railway /news/status, local state files
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

FMP_KEY    = os.environ.get("FMP_API_KEY", "")
RAILWAY_URL = os.environ.get("RAILWAY_URL",
    "https://h2-webhook-bridge-production.up.railway.app")
FMP_BASE   = "https://financialmodelingprep.com/api/v3"

# ── 15 Instruments with their FX/commodity drivers ───────────────────────────
INSTRUMENTS = {
    "JP225":    {"type":"index",  "driver":"USDJPY",  "region":"Asia"},
    "DE40":     {"type":"index",  "driver":"EURUSD",  "region":"Europe"},
    "SPX":      {"type":"index",  "driver":"VIX",     "region":"US"},
    "USTEC":    {"type":"index",  "driver":"TNX",     "region":"US"},
    "US30":     {"type":"index",  "driver":"VIX",     "region":"US"},
    "UK100":    {"type":"index",  "driver":"GBPUSD",  "region":"Europe"},
    "CAC40":    {"type":"index",  "driver":"EURUSD",  "region":"Europe"},
    "AUS200":   {"type":"index",  "driver":"AUDUSD",  "region":"Asia"},
    "HSI":      {"type":"index",  "driver":"USDCNH",  "region":"Asia"},
    "NI225":    {"type":"index",  "driver":"USDJPY",  "region":"Asia"},
    "XAUUSD":   {"type":"forex",  "driver":"DXY",     "region":"Global"},
    "XAGUSD":   {"type":"forex",  "driver":"DXY",     "region":"Global"},
    "USDJPY":   {"type":"forex",  "driver":"TNX",     "region":"Global"},
    "EURUSD":   {"type":"forex",  "driver":"DXY",     "region":"Global"},
    "GBPUSD":   {"type":"forex",  "driver":"GBPUSD",  "region":"Global"},
}

def fmp_get(endpoint: str, params: dict = {}) -> Optional[list]:
    """Make a free-tier FMP API call"""
    if not FMP_KEY:
        print(f"[FMP] SKIP {endpoint}: FMP_API_KEY not set")
        return None
    try:
        params["apikey"] = FMP_KEY
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        print(f"[FMP] {endpoint}: HTTP {r.status_code} — {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[FMP] {endpoint}: {e}")
        return None

def fetch_forex_quotes() -> Dict:
    """Fetch live FX quotes for all major pairs"""
    pairs = ["USDJPY","EURUSD","GBPUSD","AUDUSD","USDCNH","USDCHF","USDCAD"]
    results = {}
    for pair in pairs:
        data = fmp_get(f"fx/{pair}")
        if data and len(data) > 0:
            d = data[0]
            results[pair] = {
                "price":  d.get("ask", d.get("price", 0)),
                "change": d.get("changes", 0),
                "change_pct": round(d.get("changes", 0) / d.get("ask", 1) * 100, 4)
                    if d.get("ask", 0) > 0 else 0
            }
    return results

def fetch_commodity_quotes() -> Dict:
    """Fetch gold, silver, oil"""
    results = {}
    for sym, name in [("GCUSD","XAUUSD"),("SIUSD","XAGUSD"),("CLUSD","USOIL")]:
        data = fmp_get(f"quote/{sym}")
        if data and len(data) > 0:
            d = data[0]
            results[name] = {
                "price":      d.get("price", 0),
                "change_pct": d.get("changesPercentage", 0),
                "prev_close": d.get("previousClose", 0),
            }
    return results

def fetch_macro_indicators() -> Dict:
    """Fetch VIX, TNX, DXY proxies via available quotes"""
    results = {}
    try:
        r = requests.get(
            "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
            "v2/accounting/od/avg_interest_rates?"
            "fields=record_date,security_desc,avg_interest_rate_amt&"
            "filter=security_type_desc:eq:Marketable&"
            "sort=-record_date&limit=10",
            timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            us10y = next((float(x["avg_interest_rate_amt"]) for x in data
                         if "Note" in x.get("security_desc", "")), None)
            us2y  = next((float(x["avg_interest_rate_amt"]) for x in data
                         if "Bill" in x.get("security_desc", "")), None)
            results["US10Y"]  = us10y
            results["US2Y"]   = us2y
            results["yield_spread"] = round(us10y - us2y, 3) if us10y and us2y else None
            results["yield_curve"]  = "INVERTED" if (us10y and us2y and us2y > us10y) else "NORMAL"
    except Exception as e:
        print(f"[TREASURY] {e}")

    # DXY direction from EURUSD (inverse proxy)
    fx = fetch_forex_quotes()
    eur = fx.get("EURUSD", {})
    if eur:
        results["DXY_proxy_direction"] = "FALLING" if eur.get("change_pct",0) > 0 else "RISING"
        results["DXY_move_pct"] = abs(eur.get("change_pct", 0))

    return results

def fetch_economic_calendar() -> list:
    """Fetch next 7 days of high-impact economic events"""
    events = []

    # Source 1: Railway ForexFactory (already running)
    try:
        r = requests.get(f"{RAILWAY_URL}/news/status", timeout=10)
        if r.status_code == 200:
            data = r.json()
            seen = set()
            for instr, info in data.get("instruments", {}).items():
                for ev in info.get("next_events", []):
                    key = ev.get("title","") + str(ev.get("mins_away",""))
                    if key not in seen:
                        seen.add(key)
                        events.append({
                            "title":     ev.get("title",""),
                            "mins_away": ev.get("mins_away", 999),
                            "impact":    ev.get("impact","Low"),
                            "source":    "ForexFactory",
                        })
    except Exception as e:
        print(f"[RAILWAY] {e}")

    # Source 2: FMP earnings calendar (free tier)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week  = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
    earnings = fmp_get("earning_calendar", {"from": today, "to": week})
    if earnings:
        major = ["AAPL","MSFT","NVDA","META","GOOGL","AMZN","TSLA",
                 "JPM","GS","MS","BAC","WFC",
                 "BABA","TCEHY","Samsung","Toyota","SoftBank"]
        for ev in earnings[:100]:
            sym = ev.get("symbol","")
            if any(m.upper() in sym.upper() for m in major):
                events.append({
                    "title":    f"Earnings: {sym}",
                    "date":     ev.get("date",""),
                    "eps_est":  ev.get("epsEstimated"),
                    "impact":   "High",
                    "source":   "FMP",
                })

    return sorted(events, key=lambda x: x.get("mins_away", 9999))

def fetch_news_status() -> Dict:
    """Get current news status from Railway"""
    try:
        r = requests.get(f"{RAILWAY_URL}/news/status", timeout=10)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {}

def fetch_all() -> Dict:
    """Main collection function — runs all fetches and returns structured brief data"""
    print("[H2] Collecting market data...")

    now_utc = datetime.now(timezone.utc)
    session = "NY" if 13 <= now_utc.hour < 20 else \
              "LONDON" if 7 <= now_utc.hour < 13 else "ASIA"

    brief = {
        "generated_at": now_utc.isoformat(),
        "session":      session,
        "fx_quotes":    fetch_forex_quotes(),
        "commodities":  fetch_commodity_quotes(),
        "macro":        fetch_macro_indicators(),
        "calendar":     fetch_economic_calendar(),
        "news_status":  fetch_news_status(),
    }

    # Load live state if available
    state_path = "outputs/H2_live_state.json"
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                brief["live_state"] = json.load(f)
        except:
            pass

    print(f"[H2] Data collected. Session: {session} | "
          f"FX pairs: {len(brief['fx_quotes'])} | "
          f"Calendar events: {len(brief['calendar'])}")

    return brief
