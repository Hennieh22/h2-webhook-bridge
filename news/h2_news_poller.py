"""
H2 News Poller v1  —  news/h2_news_poller.py
=============================================
Hybrid macro news poller. Runs as a continuous loop.

Sources
-------
1. FMP API        — treasury rates, earnings calendar (free endpoints)
2. ForexFactory   — weekly economic calendar XML (impact + currency)
3. RSS headlines  — Reuters, BBC, Al Jazeera (breaking news filter)

Output
------
outputs/H2_news_status.json   (read by /news/status endpoint + Pine Panel 2)
outputs/H2_treasury_cache.json (rolling 3-day treasury buffer for trend calc)

Env vars
--------
FMP_API_KEY   — Financial Modeling Prep API key
POLL_INTERVAL — seconds between polls (default 300)
"""

import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

# ── Config ────────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).resolve().parent.parent
OUTPUTS       = ROOT / "outputs"
OUTPUTS.mkdir(parents=True, exist_ok=True)

NEWS_OUT      = OUTPUTS / "H2_news_status.json"
TREASURY_CACHE = OUTPUTS / "H2_treasury_cache.json"

FMP_KEY       = os.environ.get("FMP_API_KEY", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 300))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [NEWS] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("h2_news_poller")

# ── Instrument → relevant currencies / keywords ───────────────────────────────
INSTRUMENT_MAP = {
    "JP225":  {"currencies": ["JPY"],             "keywords": ["BOJ", "Japan", "Nikkei", "Yen"]},
    "DE40":   {"currencies": ["EUR"],             "keywords": ["ECB", "Germany", "DAX", "Euro"]},
    "UK100":  {"currencies": ["GBP"],             "keywords": ["BOE", "UK", "Britain", "Sterling"]},
    "XAUUSD": {"currencies": ["USD"],             "keywords": ["Fed", "FOMC", "CPI", "NFP", "Gold", "inflation"]},
    "XAGUSD": {"currencies": ["USD"],             "keywords": ["Fed", "FOMC", "CPI", "Silver", "industrial"]},
    "USDJPY": {"currencies": ["USD", "JPY"],      "keywords": ["Fed", "BOJ", "FOMC", "BOJ", "Yen"]},
    "GBPUSD": {"currencies": ["GBP", "USD"],      "keywords": ["BOE", "Fed", "UK", "FOMC"]},
    "EURUSD": {"currencies": ["EUR", "USD"],      "keywords": ["ECB", "Fed", "FOMC", "Euro"]},
    "SPX":    {"currencies": ["USD"],             "keywords": ["Fed", "FOMC", "CPI", "NFP", "earnings", "S&P"]},
    "USTEC":  {"currencies": ["USD"],             "keywords": ["Fed", "FOMC", "CPI", "tech", "earnings", "Nasdaq"]},
    "US30":   {"currencies": ["USD"],             "keywords": ["Fed", "FOMC", "CPI", "NFP", "Dow"]},
    "HK50":   {"currencies": ["CNH", "HKD"],      "keywords": ["PBOC", "China", "Hong Kong", "tariff"]},
    "ASX200": {"currencies": ["AUD", "CNH"],      "keywords": ["RBA", "Australia", "China", "iron ore"]},
    "NIFTY":  {"currencies": ["INR"],             "keywords": ["RBI", "India", "Nifty"]},
}

# High-impact event titles that trigger SUPPRESS within 30 min
SUPPRESS_KEYWORDS = [
    "Non-Farm", "NFP", "FOMC", "Federal Reserve", "Fed Rate",
    "Interest Rate Decision", "CPI", "GDP", "BOJ", "Bank of Japan",
    "ECB", "BOE", "Bank of England", "RBA", "PBOC",
    "Unemployment Rate", "Retail Sales", "PMI Flash",
]

# ── HTTP helper ───────────────────────────────────────────────────────────────
def _fetch(url: str, timeout: int = 10) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "H2-NewsPoller/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except (URLError, Exception) as e:
        log.warning("fetch failed %s — %s", url, e)
        return None


def _fetch_json(url: str, timeout: int = 10) -> list | dict | None:
    raw = _fetch(url, timeout)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("JSON parse failed %s — %s", url, e)
        return None


# ── Source 1: FMP treasury rates ──────────────────────────────────────────────
def fetch_treasury_rates() -> dict:
    """Pull latest treasury rates from FMP. Returns parsed dict."""
    if not FMP_KEY:
        log.warning("FMP_API_KEY not set — skipping treasury rates")
        return {}
    url = f"https://financialmodelingprep.com/api/v4/treasury?apikey={FMP_KEY}"
    log.info("[FMP] Fetching treasury rates with key: %s...", FMP_KEY[:8])
    log.info("[FMP] URL: %s", url.replace(FMP_KEY, FMP_KEY[:8] + "****"))
    raw = _fetch(url)
    log.info("[FMP] Raw response (first 300 chars): %s", (raw or "")[:300])
    if raw is None:
        log.warning("[FMP] No response from treasury endpoint")
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("[FMP] JSON parse error: %s", e)
        return {}
    log.info("[FMP] Parsed type=%s len=%s", type(data).__name__, len(data) if isinstance(data, (list, dict)) else "n/a")
    if not data or not isinstance(data, list):
        log.warning("[FMP] Unexpected data shape: %s", str(data)[:200])
        return {}
    # Most recent entry is first
    latest = data[0] if data else {}
    return {
        "us1m":  latest.get("month1"),
        "us3m":  latest.get("month3"),
        "us6m":  latest.get("month6"),
        "us1y":  latest.get("year1"),
        "us2y":  latest.get("year2"),
        "us5y":  latest.get("year5"),
        "us10y": latest.get("year10"),
        "us30y": latest.get("year30"),
        "date":  latest.get("date"),
    }


def compute_treasury_summary(rates: dict) -> dict:
    """Compute yield curve shape and 3-day trend from rolling cache."""
    us2y  = rates.get("us2y")
    us10y = rates.get("us10y")

    # Yield curve
    if us2y is not None and us10y is not None:
        spread = round(us10y - us2y, 3)
        yield_curve = "NORMAL" if spread > 0.1 else "FLAT" if spread > -0.1 else "INVERTED"
    else:
        spread = None
        yield_curve = "UNKNOWN"

    # 3-day trend from cache
    trend = _compute_trend(us10y)

    return {
        "us10y":       us10y,
        "us2y":        us2y,
        "us5y":        rates.get("us5y"),
        "us30y":       rates.get("us30y"),
        "spread_2s10s": spread,
        "yield_curve": yield_curve,
        "trend_3day":  trend,
        "as_of":       rates.get("date"),
    }


def _compute_trend(current_10y: float | None) -> str:
    """Compare current US10Y to 3-day rolling cache. Updates cache."""
    if current_10y is None:
        return "UNKNOWN"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        cache = json.loads(TREASURY_CACHE.read_text()) if TREASURY_CACHE.exists() else []
    except Exception:
        cache = []

    # Append today if not already present
    if not cache or cache[-1].get("date") != now_str:
        cache.append({"date": now_str, "us10y": current_10y})
        cache = cache[-5:]  # keep last 5 days
        TREASURY_CACHE.write_text(json.dumps(cache, indent=2))

    if len(cache) < 2:
        return "UNKNOWN"
    oldest = cache[0]["us10y"]
    delta = current_10y - oldest
    if delta > 0.05:
        return "RISING"
    elif delta < -0.05:
        return "FALLING"
    return "FLAT"


# ── Source 1b: FMP earnings calendar ─────────────────────────────────────────
def fetch_earnings_today() -> list[str]:
    """Return list of ticker symbols reporting earnings today."""
    if not FMP_KEY:
        return []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = (
        f"https://financialmodelingprep.com/api/v3/earning_calendar"
        f"?from={today}&to={today}&apikey={FMP_KEY}"
    )
    data = _fetch_json(url)
    if not data or not isinstance(data, list):
        return []
    return [e["symbol"] for e in data if e.get("symbol")]


# ── Source 2: ForexFactory RSS ────────────────────────────────────────────────
def fetch_ff_calendar() -> list[dict]:
    """
    Parse ForexFactory weekly XML calendar.
    Returns list of event dicts: {title, currency, impact, datetime_utc}
    """
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
    raw = _fetch(url, timeout=15)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log.warning("FF XML parse error: %s", e)
        return []

    events = []
    now_utc = datetime.now(timezone.utc)
    year = now_utc.year

    for ev in root.findall(".//event"):
        def _t(tag: str) -> str:
            el = ev.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        title    = _t("title")
        currency = _t("country")   # FF uses 'country' for currency code
        impact   = _t("impact")    # "High", "Medium", "Low", "Holiday"
        date_str = _t("date")      # e.g. "06-17-2025" or "Jun 17, 2025"
        time_str = _t("time")      # e.g. "8:30am" or "All Day"

        dt_utc = _parse_ff_datetime(date_str, time_str, year)
        events.append({
            "title":    title,
            "currency": currency.upper(),
            "impact":   impact,
            "dt_utc":   dt_utc,
            "raw_date": date_str,
            "raw_time": time_str,
        })

    return events


def _parse_ff_datetime(date_str: str, time_str: str, year: int) -> datetime | None:
    """Best-effort parse of ForexFactory date+time strings."""
    import re
    # Try MM-DD-YYYY
    for fmt in ("%m-%d-%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            d = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            pass
    else:
        return None

    if not time_str or time_str.lower() in ("all day", "tentative", ""):
        return d.replace(hour=0, minute=0)

    # Parse time like "8:30am", "2:00pm"
    match = re.match(r"(\d{1,2}):(\d{2})(am|pm)", time_str.lower())
    if match:
        h, m, ampm = int(match.group(1)), int(match.group(2)), match.group(3)
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        d = d.replace(hour=h, minute=m)
    return d


# ── Source 3: RSS headlines ───────────────────────────────────────────────────
RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
]

HEADLINE_SHOCK_KEYWORDS = [
    "war", "invasion", "nuclear", "emergency", "crash", "collapse",
    "crisis", "pandemic", "default", "sanctions", "tariff", "trade war",
    "Fed surprise", "rate hike", "rate cut surprise", "black swan",
]

def fetch_rss_headlines() -> list[dict]:
    """Return recent macro-relevant headlines from RSS sources."""
    headlines = []
    if not HAS_FEEDPARSER:
        return headlines

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                link  = entry.get("link", "")
                published = entry.get("published", "")
                headlines.append({
                    "title":     title,
                    "link":      link,
                    "published": published,
                    "source":    feed.feed.get("title", feed_url),
                })
        except Exception as e:
            log.warning("RSS feed failed %s: %s", feed_url, e)

    return headlines


def has_shock_headline(headlines: list[dict]) -> tuple[bool, str]:
    """Check headlines for market-shock keywords."""
    for h in headlines:
        title_lower = h["title"].lower()
        for kw in HEADLINE_SHOCK_KEYWORDS:
            if kw.lower() in title_lower:
                return True, h["title"]
    return False, ""


# ── Status logic ──────────────────────────────────────────────────────────────
def compute_instrument_status(
    instrument: str,
    ff_events: list[dict],
    headlines: list[dict],
    earnings: list[str],
) -> dict:
    cfg = INSTRUMENT_MAP.get(instrument, {"currencies": ["USD"], "keywords": []})
    currencies = cfg["currencies"]
    keywords   = cfg["keywords"]

    now_utc = datetime.now(timezone.utc)
    triggered_events = []
    status = "CLEAR"
    reason = "No high-impact events"

    # Check ForexFactory events for this instrument's currencies
    for ev in ff_events:
        if ev["impact"] not in ("High", "Medium"):
            continue
        if ev["currency"] not in currencies:
            continue
        dt = ev.get("dt_utc")
        if dt is None:
            continue
        minutes_away = (dt - now_utc).total_seconds() / 60

        event_info = {
            "title":    ev["title"],
            "currency": ev["currency"],
            "impact":   ev["impact"],
            "minutes_away": round(minutes_away),
        }

        if -60 <= minutes_away <= 30 and ev["impact"] == "High":
            # High-impact within 30 min ahead or just passed
            if any(sk.lower() in ev["title"].lower() for sk in SUPPRESS_KEYWORDS):
                status = "SUPPRESS"
                reason = f"SUPPRESS: {ev['title']} ({ev['currency']}) in {round(minutes_away)}min"
                triggered_events.append(event_info)
                break
            else:
                if status != "SUPPRESS":
                    status = "CAUTION"
                    reason = f"High impact: {ev['title']} in {round(minutes_away)}min"
                triggered_events.append(event_info)

        elif 30 < minutes_away <= 120 and ev["impact"] == "High":
            if status == "CLEAR":
                status = "CAUTION"
                reason = f"Upcoming: {ev['title']} ({ev['currency']}) in {round(minutes_away)}min"
            triggered_events.append(event_info)

        elif 0 < minutes_away <= 30 and ev["impact"] == "Medium":
            if status == "CLEAR":
                status = "CAUTION"
                reason = f"Medium impact: {ev['title']} in {round(minutes_away)}min"
            triggered_events.append(event_info)

    # Headline shock check
    shock, shock_title = has_shock_headline(headlines)
    if shock:
        for kw in keywords:
            if kw.lower() in shock_title.lower():
                if status != "SUPPRESS":
                    status = "CAUTION"
                    reason = f"Breaking: {shock_title[:80]}"
                break

    # Earnings check for equity indices
    if instrument in ("SPX", "USTEC", "US30") and earnings:
        major = [s for s in earnings if s in ("AAPL", "NVDA", "MSFT", "GOOGL", "META", "AMZN", "TSLA")]
        if major:
            if status == "CLEAR":
                status = "CAUTION"
                reason = f"Major earnings today: {', '.join(major)}"

    return {
        "status":   status,
        "reason":   reason,
        "events":   triggered_events[:5],  # cap at 5
        "checked_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── Main poll cycle ───────────────────────────────────────────────────────────
def poll() -> None:
    now_utc = datetime.now(timezone.utc)
    log.info("Poll cycle starting — %s", now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Fetch all sources
    rates        = fetch_treasury_rates()
    treasury     = compute_treasury_summary(rates)
    earnings     = fetch_earnings_today()
    ff_events    = fetch_ff_calendar()
    headlines    = fetch_rss_headlines()

    log.info(
        "Sources: treasury=%s, ff_events=%d, headlines=%d, earnings=%d",
        "ok" if treasury.get("us10y") else "missing",
        len(ff_events), len(headlines), len(earnings),
    )

    # Per-instrument status
    instruments = {}
    for inst in INSTRUMENT_MAP:
        instruments[inst] = compute_instrument_status(
            inst, ff_events, headlines, earnings
        )

    # Build output
    output = {
        "generated_at_utc":    now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_sast":   (now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+02:00"),
        "poll_interval_sec":   POLL_INTERVAL,
        "treasury_rates":      treasury,
        "major_earnings_today": earnings[:20],
        "ff_events_loaded":    len(ff_events),
        "instruments":         instruments,
    }

    NEWS_OUT.write_text(json.dumps(output, indent=2))
    log.info("Written -> %s", NEWS_OUT)


def main() -> None:
    log.info("H2 News Poller starting (interval=%ds)", POLL_INTERVAL)
    if not FMP_KEY:
        log.warning("FMP_API_KEY not set — treasury and earnings will be empty")
    if not HAS_FEEDPARSER:
        log.warning("feedparser not installed — RSS headlines disabled")

    while True:
        try:
            poll()
        except Exception as e:
            log.error("Poll cycle error: %s", e, exc_info=True)
        time.sleep(POLL_INTERVAL)


run_poller = main  # alias used by Flask background thread launcher

if __name__ == "__main__":
    main()
