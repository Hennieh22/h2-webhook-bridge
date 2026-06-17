"""
H2 Quant v1 — Live Dashboard Server  (dashboard/app.py)
========================================================
Minimal Flask server that:
  GET /                          -> serves h2_dashboard.html
  GET /api/live-state            -> full H2_live_state.json (all instruments)
  GET /api/live-state/<inst>     -> single instrument data (Pine request.json target)
  GET /health                    -> {"status": "ok"}

The per-instrument endpoint is the Pine Script data source.
It returns a flat JSON object with CORS headers so Pine's request.json()
can reach every field via RFC 6901 JSON pointers:
  /current_state
  /next_states/0/probability
  /gates/markov_gap/value
  etc.

Local:   py -3 dashboard/app.py          -> http://localhost:5050
Railway: set PORT env var (Railway injects it automatically)

No external dependencies beyond Flask.
"""

import json
import os
import urllib.request
from pathlib import Path
from flask import Flask, jsonify, send_from_directory, Response

ROOT        = Path(__file__).resolve().parent.parent
DASHBOARD   = Path(__file__).resolve().parent
LIVE_STATE  = ROOT / "outputs" / "H2_live_state.json"
NEWS_STATE  = ROOT / "outputs" / "H2_news_status.json"

app = Flask(__name__, static_folder=str(DASHBOARD))


# ── ngrok tunnel URL helper ───────────────────────────────────────────────────
def _get_ngrok_url() -> str | None:
    """
    Query ngrok's local API to find the current public HTTPS tunnel URL.
    Returns the URL string or None if ngrok is not running.
    ngrok exposes its API on http://localhost:4040/api/tunnels by default.
    """
    try:
        with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=2) as r:
            data = json.loads(r.read())
        for tunnel in data.get("tunnels", []):
            if tunnel.get("proto") == "https":
                return tunnel["public_url"]
        # Fallback: return first tunnel regardless of proto
        tunnels = data.get("tunnels", [])
        if tunnels:
            return tunnels[0]["public_url"]
    except Exception:
        pass
    return None


# ── Health check (Railway uses this) ────────────────────────────────────────
@app.route("/health")
def health():
    ngrok_url = _get_ngrok_url()
    return jsonify({
        "status":    "ok",
        "service":   "H2 Dashboard",
        "ngrok_url": ngrok_url or "not running",
    })


# ── ngrok URL endpoint — lets Pine / tv_bridge / monitor autodiscover URL ────
@app.route("/ngrok-url")
def ngrok_url_endpoint():
    """
    Returns the current ngrok public URL as plain text.
    Useful for scripts that need to discover the URL programmatically.
    Also accepts ?format=json for JSON response.
    """
    url = _get_ngrok_url()
    fmt = os.environ.get("FORMAT", "")
    if "json" in fmt or "json" in str(os.environ.get("QUERY_STRING", "")):
        resp = jsonify({"ngrok_url": url or None, "running": url is not None})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    if url:
        return Response(url, mimetype="text/plain")
    return Response("ngrok not running", mimetype="text/plain", status=503)


# ── Live state JSON endpoint ─────────────────────────────────────────────────
@app.route("/api/live-state")
def live_state():
    if not LIVE_STATE.exists():
        return jsonify({"error": "H2_live_state.json not found — run live/monitor.py first"}), 404
    try:
        with open(LIVE_STATE, encoding="utf-8") as f:
            data = json.load(f)
        resp = Response(
            json.dumps(data, indent=2),
            mimetype="application/json",
        )
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Per-instrument endpoint — Pine Script data source ────────────────────────
# GET /api/live-state/US30  →  flat JSON for one instrument
# CORS * so Pine's request.json() can reach it from TradingView servers.
# Structure mirrors H2_live_state.json instruments[name], with
# generated_at_sast and session injected at top level for easy JSON pointers.
@app.route("/api/live-state/<instrument>")
def live_state_instrument(instrument: str):
    instrument = instrument.upper()
    if not LIVE_STATE.exists():
        r = jsonify({"error": "H2_live_state.json not found — run live/monitor.py first"})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 404
    try:
        with open(LIVE_STATE, encoding="utf-8") as f:
            full = json.load(f)

        inst_data = full.get("instruments", {}).get(instrument)
        if inst_data is None:
            r = jsonify({"error": f"{instrument} not found in live state"})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 404

        # Inject top-level context fields so Pine can reach them at root level
        payload = {
            "generated_at_sast": full.get("generated_at_sast", ""),
            "generated_at_utc":  full.get("generated_at_utc",  ""),
            "session":           full.get("session", inst_data.get("session", "OFFHOURS")),
            **inst_data,
        }

        resp = Response(
            json.dumps(payload, indent=2),
            mimetype="application/json",
        )
        resp.headers["Access-Control-Allow-Origin"]   = "*"
        resp.headers["Access-Control-Allow-Methods"]  = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"]  = "Content-Type"
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

    except Exception as e:
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500


# ── CORS preflight for the per-instrument endpoint ───────────────────────────
@app.route("/api/live-state/<instrument>", methods=["OPTIONS"])
def live_state_instrument_options(instrument: str):
    resp = Response("", status=204)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ── News status endpoint ─────────────────────────────────────────────────────
@app.route("/news/status")
def news_status():
    if not NEWS_STATE.exists():
        return jsonify({"error": "News poller not yet run — H2_news_status.json missing"}), 404
    try:
        with open(NEWS_STATE, encoding="utf-8") as f:
            data = json.load(f)
        resp = Response(json.dumps(data, indent=2), mimetype="application/json")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Dashboard HTML ────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return send_from_directory(str(DASHBOARD), "h2_dashboard.html")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"H2 Dashboard running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
