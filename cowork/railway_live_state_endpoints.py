"""
H2 Live State Bridge — add these routes to the existing Railway webhook app.

The existing app already has /news/status and the dashboard.
These two routes add live VWAP state from Pine Script alerts.

Pine fires:  POST /live_state  (JSON body — see payload format below)
Scan reads:  GET  /live_state  (returns merged state for all instruments)

Payload format (from Pine alert):
{
  "type": "live_state",
  "symbol": "JP225",
  "dest_1h": 70179.14,
  "dir_1h": "UP",
  "dest_4h": 70075.27,
  "dir_4h": "UP",
  "dest_d": 69612.67,
  "dir_d": "UP",
  "regime": "WITHIN",
  "journey_state": 1,
  "timestamp": 1718705400
}

GET /live_state response (merged across all instruments):
{
  "JP225": { "dest_1h": 70179.14, "dir_1h": "UP", ... },
  "XAUUSD": { "dest_1h": ..., ... },
  ...
}
"""

import json
import time

# ── In-memory state store (survives restarts via file if you add persistence) ──
_live_state: dict = {}
_state_file = "/tmp/h2_live_state.json"

def _load_state():
    global _live_state
    try:
        with open(_state_file) as f:
            _live_state = json.load(f)
    except Exception:
        _live_state = {}

def _save_state():
    try:
        with open(_state_file, "w") as f:
            json.dump(_live_state, f)
    except Exception:
        pass

_load_state()

# ── Flask routes ──────────────────────────────────────────────────────────────
# Add to your existing Flask app:

# from flask import Flask, request, jsonify
# app = Flask(__name__)  # already exists in your bridge

# @app.route("/live_state", methods=["POST"])
def post_live_state():
    """Receive Pine alert and store instrument state."""
    from flask import request, jsonify
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "empty body"}), 400

        symbol = data.get("symbol", "").upper()
        if not symbol:
            return jsonify({"error": "missing symbol"}), 400

        _live_state[symbol] = {
            "dest_1h":      data.get("dest_1h"),
            "dir_1h":       data.get("dir_1h"),
            "dest_4h":      data.get("dest_4h"),
            "dir_4h":       data.get("dir_4h"),
            "dest_d":       data.get("dest_d"),
            "dir_d":        data.get("dir_d"),
            "regime":       data.get("regime"),
            "journey_state":data.get("journey_state"),
            "updated_at":   int(time.time()),
            "timestamp":    data.get("timestamp"),
        }
        _save_state()
        print(f"[LIVE_STATE] Updated {symbol}: {_live_state[symbol]}")
        return jsonify({"ok": True, "symbol": symbol}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# @app.route("/live_state", methods=["GET"])
def get_live_state():
    """Return merged live state for all instruments."""
    from flask import jsonify
    # Prune stale entries (older than 4 hours)
    cutoff = int(time.time()) - 14400
    fresh = {k: v for k, v in _live_state.items()
             if v.get("updated_at", 0) > cutoff}
    return jsonify(fresh), 200
