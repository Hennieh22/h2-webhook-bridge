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
import sys
import threading
import time as _time
import urllib.request
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, Response

ROOT        = Path(__file__).resolve().parent.parent
DASHBOARD   = Path(__file__).resolve().parent
LIVE_STATE  = ROOT / "outputs" / "H2_live_state.json"
NEWS_STATE  = Path(__file__).parent.parent / "outputs" / "H2_news_status.json"

app = Flask(__name__, static_folder=str(DASHBOARD))


# -- ngrok tunnel URL helper --------------------------------------------------
def _get_ngrok_url() -> str | None:
    try:
        with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=2) as r:
            data = json.loads(r.read())
        for tunnel in data.get("tunnels", []):
            if tunnel.get("proto") == "https":
                return tunnel["public_url"]
        tunnels = data.get("tunnels", [])
        if tunnels:
            return tunnels[0]["public_url"]
    except Exception:
        pass
    return None


# -- Health check (Railway uses this) ----------------------------------------
@app.route("/health")
def health():
    ngrok_url = _get_ngrok_url()
    return jsonify({
        "status":    "ok",
        "service":   "H2 Dashboard",
        "ngrok_url": ngrok_url or "not running",
    })


# -- ngrok URL endpoint -------------------------------------------------------
@app.route("/ngrok-url")
def ngrok_url_endpoint():
    url = _get_ngrok_url()
    fmt = os.environ.get("FORMAT", "")
    if "json" in fmt or "json" in str(os.environ.get("QUERY_STRING", "")):
        resp = jsonify({"ngrok_url": url or None, "running": url is not None})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    if url:
        return Response(url, mimetype="text/plain")
    return Response("ngrok not running", mimetype="text/plain", status=503)


# -- Live state JSON endpoint -------------------------------------------------
@app.route("/api/live-state")
def live_state():
    if not LIVE_STATE.exists():
        return jsonify({"error": "H2_live_state.json not found -- run live/monitor.py first"}), 404
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


# -- Per-instrument endpoint --------------------------------------------------
@app.route("/api/live-state/<instrument>")
def live_state_instrument(instrument: str):
    instrument = instrument.upper()
    if not LIVE_STATE.exists():
        r = jsonify({"error": "H2_live_state.json not found -- run live/monitor.py first"})
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


# -- CORS preflight -----------------------------------------------------------
@app.route("/api/live-state/<instrument>", methods=["OPTIONS"])
def live_state_instrument_options(instrument: str):
    resp = Response("", status=204)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# -- News debug endpoint ------------------------------------------------------
@app.route("/news/debug")
def news_debug():
    poller_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../news/h2_news_poller.py'))
    outputs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../outputs'))
    news_file   = os.path.join(outputs_dir, 'H2_news_status.json')
    return jsonify({
        "poller_file_exists": os.path.exists(poller_path),
        "poller_path":        poller_path,
        "outputs_dir_exists": os.path.exists(outputs_dir),
        "outputs_dir":        outputs_dir,
        "news_json_exists":   os.path.exists(news_file),
        "news_json_path":     news_file,
        "thread_alive":       _poller_thread.is_alive(),
        "cwd":                os.getcwd(),
    })


# -- News status endpoint -----------------------------------------------------
@app.route("/news/status")
def news_status():
    if not NEWS_STATE.exists():
        return jsonify({"error": "News poller not yet run -- H2_news_status.json missing"}), 404
    try:
        with open(NEWS_STATE, encoding="utf-8") as f:
            data = json.load(f)
        resp = Response(json.dumps(data, indent=2), mimetype="application/json")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -- Dashboard HTML -----------------------------------------------------------
@app.route("/")
def dashboard():
    return send_from_directory(str(DASHBOARD), "h2_dashboard.html")


# -- News poller background thread --------------------------------------------
def start_news_poller_thread():
    try:
        poller_path = os.path.join(os.path.dirname(__file__), '../news/h2_news_poller.py')
        poller_path = os.path.abspath(poller_path)
        if not os.path.exists(poller_path):
            print(f"[NEWS] Poller not found at {poller_path} -- skipping")
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location("h2_news_poller", poller_path)
        poller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(poller)
        print("[NEWS] Starting news poller background thread...")
        poller.run_poller()
    except Exception as e:
        print(f"[NEWS] Poller thread error: {e}")

_poller_thread = threading.Thread(target=start_news_poller_thread, daemon=True)
_poller_thread.start()


# -- H2 Live State bridge -----------------------------------------------------
# Receives JSON POSTed by TradingView alert (H2_MTF_v1.pine) on every bar close.
# Stores VWAP destinations per instrument in memory + /tmp for persistence.

_live_state = {}
_state_file = "/tmp/h2_live_state.json"


def _ls_load():
    global _live_state
    try:
        with open(_state_file) as f:
            _live_state = json.load(f)
    except Exception:
        _live_state = {}


def _ls_save():
    try:
        with open(_state_file, "w") as f:
            json.dump(_live_state, f)
    except Exception:
        pass


_ls_load()


@app.route("/live_state", methods=["POST"])
def post_live_state():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "empty body"}), 400
        symbol = data.get("symbol", "").upper()
        if not symbol:
            return jsonify({"error": "missing symbol"}), 400
        _live_state[symbol] = {
            "dest_1h":       data.get("dest_1h"),
            "dir_1h":        data.get("dir_1h"),
            "dest_4h":       data.get("dest_4h"),
            "dir_4h":        data.get("dir_4h"),
            "dest_d":        data.get("dest_d"),
            "dir_d":         data.get("dir_d"),
            "regime":        data.get("regime"),
            "journey_state": data.get("journey_state"),
            "updated_at":    int(_time.time()),
            "timestamp":     data.get("timestamp"),
        }
        _ls_save()
        print(f"[LIVE_STATE] Updated {symbol}")
        return jsonify({"ok": True, "symbol": symbol}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/live_state", methods=["GET"])
def get_live_state():
    cutoff = int(_time.time()) - 14400
    fresh = {k: v for k, v in _live_state.items()
             if v.get("updated_at", 0) > cutoff}
    return jsonify(fresh), 200


# -- Entry point --------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"H2 Dashboard running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
