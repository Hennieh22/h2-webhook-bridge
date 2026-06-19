"""
H2 Quant v1 — Live Dashboard Server  (dashboard/app.py)
"""

import json
import os
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


# ---------------------------------------------------------------------------
# H2 Live State — file-based persistence
# ---------------------------------------------------------------------------
# Stored at /tmp/h2_live_state.json (Railway ephemeral disk).
# SURVIVES:  dyno sleep/wake cycles, process restarts.
# WIPED BY:  fresh Railway deploy (new container).
# Acceptable: TradingView alerts refresh every bar close (≤15 min staleness).
# ---------------------------------------------------------------------------
_LIVE_FILE = Path("/tmp/h2_live_state.json")
_LAST_RAW  = {"body": None, "parsed": None, "symbol": None,
               "error": None, "received_at": None}


def _ls_load() -> dict:
    """Read live state from disk. Returns {} if file missing or corrupt."""
    try:
        if _LIVE_FILE.exists():
            with open(_LIVE_FILE) as f:
                return json.load(f)
    except Exception as e:
        print(f"[LIVE_STATE] Load error: {e}")
    return {}


def _ls_save(state: dict):
    """Write live state to disk."""
    try:
        _LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LIVE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[LIVE_STATE] Save error: {e}")


# ---------------------------------------------------------------------------
# ngrok helper
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "H2 Dashboard",
                    "ngrok_url": _get_ngrok_url() or "not running"})


@app.route("/ngrok-url")
def ngrok_url_endpoint():
    url = _get_ngrok_url()
    if url:
        return Response(url, mimetype="text/plain")
    return Response("ngrok not running", mimetype="text/plain", status=503)


@app.route("/api/live-state")
def live_state():
    if not LIVE_STATE.exists():
        return jsonify({"error": "H2_live_state.json not found"}), 404
    try:
        with open(LIVE_STATE, encoding="utf-8") as f:
            data = json.load(f)
        resp = Response(json.dumps(data, indent=2), mimetype="application/json")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/live-state/<instrument>")
def live_state_instrument(instrument: str):
    instrument = instrument.upper()
    if not LIVE_STATE.exists():
        r = jsonify({"error": "H2_live_state.json not found"})
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
            "session": full.get("session", inst_data.get("session", "OFFHOURS")),
            **inst_data,
        }
        resp = Response(json.dumps(payload, indent=2), mimetype="application/json")
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp
    except Exception as e:
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500


@app.route("/api/live-state/<instrument>", methods=["OPTIONS"])
def live_state_instrument_options(instrument: str):
    resp = Response("", status=204)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/news/debug")
def news_debug():
    poller_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '../news/h2_news_poller.py'))
    outputs_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '../outputs'))
    news_file = os.path.join(outputs_dir, 'H2_news_status.json')
    return jsonify({
        "poller_file_exists": os.path.exists(poller_path),
        "outputs_dir_exists": os.path.exists(outputs_dir),
        "news_json_exists":   os.path.exists(news_file),
        "thread_alive":       _poller_thread.is_alive(),
        "cwd":                os.getcwd(),
    })


@app.route("/news/status")
def news_status():
    if not NEWS_STATE.exists():
        return jsonify({"error": "H2_news_status.json missing"}), 404
    try:
        with open(NEWS_STATE, encoding="utf-8") as f:
            data = json.load(f)
        resp = Response(json.dumps(data, indent=2), mimetype="application/json")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def dashboard():
    return send_from_directory(str(DASHBOARD), "h2_dashboard.html")


# ---------------------------------------------------------------------------
# Live State endpoints — file-backed, survives dyno restarts
# ---------------------------------------------------------------------------

@app.route("/live_state", methods=["POST"])
def post_live_state():
    raw_body = request.get_data(as_text=True)
    print(f"[LIVE_STATE] POST raw: {raw_body[:500]}")

    _LAST_RAW["body"]        = raw_body[:2000]
    _LAST_RAW["received_at"] = int(_time.time())
    _LAST_RAW["error"]       = None
    _LAST_RAW["symbol"]      = None

    try:
        raw_data = json.loads(raw_body) if raw_body else None
        if not raw_data:
            _LAST_RAW["error"] = "empty body"
            return jsonify({"error": "empty body"}), 400

        _LAST_RAW["parsed"] = raw_data

        # Handle TradingView {{message}} wrapper if present:
        # {"message": "{\"type\":\"live_state\",...}"} -> unwrap inner JSON
        data = raw_data
        if "symbol" not in raw_data and "message" in raw_data:
            print("[LIVE_STATE] Unwrapping TV message field")
            try:
                data = json.loads(raw_data["message"])
            except Exception as e:
                _LAST_RAW["error"] = f"unwrap failed: {e}"
                return jsonify({"error": "could not parse nested message",
                                "raw": raw_body[:500]}), 400

        symbol = data.get("symbol", "").upper()
        # Strip broker prefix e.g. "ICMARKETS:JP225" -> "JP225"
        if ":" in symbol:
            symbol = symbol.split(":")[-1]

        _LAST_RAW["symbol"] = symbol

        if not symbol:
            _LAST_RAW["error"] = "missing symbol"
            return jsonify({"error": "missing symbol",
                            "received_keys": list(data.keys())}), 400

        # Read current state from disk, update, write back
        state = _ls_load()
        state[symbol] = {
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
        _ls_save(state)

        print(f"[LIVE_STATE] Stored {symbol} | "
              f"dest_1h={data.get('dest_1h')} dir_1h={data.get('dir_1h')} | "
              f"file={_LIVE_FILE}")
        return jsonify({"ok": True, "symbol": symbol}), 200

    except Exception as e:
        _LAST_RAW["error"] = str(e)
        print(f"[LIVE_STATE] Exception: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/live_state", methods=["GET"])
def get_live_state():
    """Read from disk every time — survives dyno sleep/restart."""
    state  = _ls_load()
    cutoff = int(_time.time()) - 14400  # 4-hour freshness window
    fresh  = {k: v for k, v in state.items()
              if v.get("updated_at", 0) > cutoff}
    return jsonify(fresh), 200


@app.route("/live_state/debug", methods=["GET"])
def debug_live_state():
    """Diagnostic: shows last raw POST body and full stored state."""
    state  = _ls_load()
    cutoff = int(_time.time()) - 14400
    return jsonify({
        "last_post":      _LAST_RAW,
        "stored_keys":    list(state.keys()),
        "current_time":   int(_time.time()),
        "cutoff":         cutoff,
        "live_file":      str(_LIVE_FILE),
        "file_exists":    _LIVE_FILE.exists(),
        "all_state":      state,
    }), 200


# ---------------------------------------------------------------------------
# News poller background thread
# ---------------------------------------------------------------------------
def start_news_poller_thread():
    try:
        poller_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '../news/h2_news_poller.py'))
        if not os.path.exists(poller_path):
            print(f"[NEWS] Poller not found at {poller_path} -- skipping")
            return
        import importlib.util
        spec   = importlib.util.spec_from_file_location("h2_news_poller", poller_path)
        poller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(poller)
        print("[NEWS] Starting news poller background thread...")
        poller.run_poller()
    except Exception as e:
        print(f"[NEWS] Poller thread error: {e}")

_poller_thread = threading.Thread(target=start_news_poller_thread, daemon=True)
_poller_thread.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"H2 Dashboard running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
