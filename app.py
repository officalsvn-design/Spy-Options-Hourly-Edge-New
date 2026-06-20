"""SPY Options Edge - Flask backend.

Run from VS Code with F5 (uses .vscode/launch.json) or from terminal:
    python app.py

Visit http://127.0.0.1:5000

Architecture:
  * background thread polls SPY quote/candles/chain on independent cadences
  * snapshot is held in memory under a lock
  * /api/snapshot returns the full current view as JSON
  * /api/strike?... returns analysis for one specific option (Greeks recomputed,
    limit-price suggestions, sizing)
  * /api/expiries returns option expiry dates
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

import config
import data_sources as ds
from indicators import compute_timeframe, last, realized_vol_annual
from options_math import score_directional_value, suggest_limit_prices, greeks

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-5s  %(name)s :: %(message)s")
log = logging.getLogger("spy-app")

TFS = ["1m", "5m", "15m", "1h", "4h"]
TF_WEIGHTS = {"1m": 0.10, "5m": 0.15, "15m": 0.20, "1h": 0.30, "4h": 0.25}

# ---------- shared state ----------
_state = {
    "quote": {},
    "candles": {tf: [] for tf in TFS},
    "indicators": {tf: None for tf in TFS},
    "bias_total": 0.0,
    "bias_by_tf": {},
    "rv_annual": 0.0,
    "chain": {"expiry": None, "calls": [], "puts": [], "tte_years": None},
    "expiries": [],
    "selected_expiry": None,
    "skew": None,
    "direction_signal": {"dir": "neu", "prob_up": 0.5, "confidence": 0,
                         "reasons": [], "verdict": "NEUTRAL"},
    "directional_picks": {"bull": [], "bear": []},
    "last_updated": {"quote": 0, "candles": 0, "chain": 0},
    "errors": {},
}
_lock = threading.Lock()


# ---------- compute helpers ----------
def _compute_indicators_and_bias():
    """Run indicator pass over all timeframes and fill bias scores."""
    inds = {}
    biases = {}
    total_w = 0.0
    total = 0.0
    for tf in TFS:
        c = _state["candles"][tf]
        ind = compute_timeframe(c)
        inds[tf] = ind
        if ind is not None:
            biases[tf] = ind["score"]
            total += ind["score"] * TF_WEIGHTS[tf]
            total_w += TF_WEIGHTS[tf]
    _state["indicators"] = inds
    _state["bias_by_tf"] = biases
    _state["bias_total"] = (total / total_w) if total_w else 0.0
    # realized vol from 1m
    closes = [x["c"] for x in _state["candles"]["1m"]]
    _state["rv_annual"] = realized_vol_annual(closes, lookback=120)


def _build_direction_signal():
    """Combine multi-TF bias + IV skew + momentum into a directional probability."""
    bias = _state["bias_total"]
    reasons = []
    confidence = 0
    if bias > 0.15:
        reasons.append(f"Multi-TF momentum bullish ({bias * 100:+.0f}%)")
        confidence += 1
    elif bias < -0.15:
        reasons.append(f"Multi-TF momentum bearish ({bias * 100:+.0f}%)")
        confidence += 1
    else:
        reasons.append(f"Multi-TF momentum near flat ({bias * 100:+.0f}%)")

    # short-term: 1m+5m alignment with 15m
    sb = sum(_state["bias_by_tf"].get(tf, 0) for tf in ("1m", "5m", "15m"))
    if sb > 1.0:
        reasons.append("Short timeframes aligned bullish")
        confidence += 1
    elif sb < -1.0:
        reasons.append("Short timeframes aligned bearish")
        confidence += 1

    # IV skew tilt
    skew = _state.get("skew")
    if skew:
        if skew["skew"] > 0.02:
            reasons.append(f"Put IV richer than call ({skew['skew'] * 100:+.1f}% skew) - protection bid")
        elif skew["skew"] < -0.01:
            reasons.append(f"Call IV richer than put ({skew['skew'] * 100:+.1f}% skew) - upside chase")
            confidence += 1

    # Translate to prob-up: 0.5 + tilt
    # Bias 1.0 -> 0.65, bias -1.0 -> 0.35 (we keep the tilt modest - this isn't a crystal ball)
    prob_up = 0.5 + bias * 0.15
    prob_up = max(0.20, min(0.80, prob_up))

    direction = ("bull" if prob_up > 0.55 else
                 "bear" if prob_up < 0.45 else "neu")
    verdict = ("LEAN BULLISH" if direction == "bull" and confidence < 2 else
               "BULLISH" if direction == "bull" else
               "LEAN BEARISH" if direction == "bear" and confidence < 2 else
               "BEARISH" if direction == "bear" else "NEUTRAL")

    _state["direction_signal"] = {
        "dir": direction, "prob_up": prob_up, "confidence": confidence,
        "reasons": reasons, "verdict": verdict, "bias": bias,
    }


def _build_directional_picks():
    """Pick the top 5 call strikes (if bull) / put strikes (if bear) by risk-reward score.

    The directional_picks panel ALWAYS shows both top calls and top puts, so users
    can compare the bull case and bear case side by side.
    """
    spot = _state["quote"].get("price")
    chain = _state["chain"]
    if not spot or not chain.get("calls"):
        _state["directional_picks"] = {"bull": [], "bear": []}
        return

    def top_for(direction, source_list):
        scored = []
        for opt in source_list:
            sc = score_directional_value(opt, spot, direction)
            if sc is None:
                continue
            row = dict(opt)
            row.update(sc)
            scored.append(row)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:5]

    _state["directional_picks"] = {
        "bull": top_for("bull", chain["calls"]),
        "bear": top_for("bear", chain["puts"]),
    }


def _choose_default_expiry():
    """Choose first expiry >= today (Yahoo's list usually starts there anyway)."""
    expiries = _state["expiries"]
    if not expiries:
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for e in expiries:
        if e >= today:
            return e
    return expiries[0]


# ---------- pollers ----------
def _poll_quote():
    try:
        q = ds.fetch_quote()
        with _lock:
            _state["quote"] = q
            _state["last_updated"]["quote"] = time.time()
            _state["errors"].pop("quote", None)
    except Exception as e:
        log.warning("quote poll failed: %s", e)
        with _lock:
            _state["errors"]["quote"] = str(e)


def _poll_candles():
    try:
        for tf in TFS:
            c = ds.fetch_candles(tf)
            with _lock:
                _state["candles"][tf] = c
        with _lock:
            _compute_indicators_and_bias()
            _build_direction_signal()
            _state["last_updated"]["candles"] = time.time()
            _state["errors"].pop("candles", None)
    except Exception as e:
        log.warning("candles poll failed: %s", e)
        with _lock:
            _state["errors"]["candles"] = str(e)


def _poll_chain():
    try:
        with _lock:
            spot = (_state["quote"] or {}).get("price")
            if not _state["expiries"]:
                _state["expiries"] = ds.fetch_expiries()
            if not _state["selected_expiry"]:
                _state["selected_expiry"] = _choose_default_expiry()
            expiry = _state["selected_expiry"]
        if not spot or not expiry:
            return
        chain = ds.fetch_chain(expiry, spot)
        skew = ds.put_call_skew(chain, spot)
        with _lock:
            _state["chain"] = chain
            _state["skew"] = skew
            _build_directional_picks()
            _build_direction_signal()
            _state["last_updated"]["chain"] = time.time()
            _state["errors"].pop("chain", None)
    except Exception as e:
        log.warning("chain poll failed: %s", e)
        with _lock:
            _state["errors"]["chain"] = str(e)


def _poller_loop():
    """Run the three pollers on independent cadences in a single thread."""
    # initial blocking pull so the dashboard has data on first load
    log.info("Initial data pull starting...")
    _poll_quote()
    log.info("Quote OK - SPY @ %s", _state["quote"].get("price"))
    _poll_candles()
    log.info("Candles + indicators OK")
    _poll_chain()
    log.info("Options chain OK - expiry %s, %d calls / %d puts",
             _state["chain"].get("expiry"),
             len(_state["chain"].get("calls", [])),
             len(_state["chain"].get("puts", [])))

    next_q = next_c = next_ch = time.time()
    while True:
        now = time.time()
        if now >= next_q:
            _poll_quote()
            next_q = now + config.QUOTE_REFRESH
        if now >= next_c:
            _poll_candles()
            next_c = now + config.CANDLES_REFRESH
        if now >= next_ch:
            _poll_chain()
            next_ch = now + config.CHAIN_REFRESH
        time.sleep(0.5)


# ---------- Flask ----------
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("dashboard.html")


def _snapshot_json():
    with _lock:
        spot = _state["quote"].get("price")
        # Limit chain to strikes near the money to keep payload small
        chain = _state["chain"]
        calls = chain.get("calls", [])
        puts = chain.get("puts", [])
        if spot and calls:
            calls = sorted(calls, key=lambda x: abs(x["strike"] - spot))[:30]
            calls.sort(key=lambda x: x["strike"])
        if spot and puts:
            puts = sorted(puts, key=lambda x: abs(x["strike"] - spot))[:30]
            puts.sort(key=lambda x: x["strike"])
        out = {
            "quote": _state["quote"],
            "indicators": _state["indicators"],
            "bias_total": _state["bias_total"],
            "bias_by_tf": _state["bias_by_tf"],
            "rv_annual": _state["rv_annual"],
            "chain": {"calls": calls, "puts": puts,
                      "expiry": chain.get("expiry"),
                      "tte_years": chain.get("tte_years")},
            "expiries": _state["expiries"],
            "selected_expiry": _state["selected_expiry"],
            "skew": _state["skew"],
            "direction_signal": _state["direction_signal"],
            "directional_picks": _state["directional_picks"],
            "last_updated": _state["last_updated"],
            "errors": _state["errors"],
            "server_time": time.time(),
        }
    return out


@app.route("/api/snapshot")
def api_snapshot():
    return jsonify(_snapshot_json())


@app.route("/api/set_expiry")
def api_set_expiry():
    """Set the active expiration and trigger a chain refresh."""
    exp = request.args.get("expiry", "").strip()
    if not exp:
        return jsonify({"ok": False, "error": "missing expiry"}), 400
    with _lock:
        if exp not in _state["expiries"]:
            return jsonify({"ok": False, "error": "unknown expiry"}), 400
        _state["selected_expiry"] = exp
    # fire a chain refresh on a worker thread so this returns fast
    threading.Thread(target=_poll_chain, daemon=True).start()
    return jsonify({"ok": True, "expiry": exp})


@app.route("/api/strike")
def api_strike():
    """Return analysis for a single strike: live Greeks + limit prices + sizing.

    Query params: strike (float), type ('call'|'put'), urgency ('patient'|'balanced'|'aggressive'),
                  bankroll (optional float)
    """
    try:
        strike = float(request.args.get("strike", ""))
        opt_type = request.args.get("type", "call")
        urgency = request.args.get("urgency", "balanced")
        bankroll = float(request.args.get("bankroll", config.DEFAULT_BANKROLL))
        if opt_type not in ("call", "put"):
            return jsonify({"ok": False, "error": "bad type"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    with _lock:
        chain = _state["chain"]
        spot = (_state["quote"] or {}).get("price")
        sig = _state["direction_signal"]
        rv = _state["rv_annual"]
        tte = chain.get("tte_years")
    if not spot:
        return jsonify({"ok": False, "error": "no spot"}), 503

    src = chain["calls"] if opt_type == "call" else chain["puts"]
    opt = next((o for o in src if abs(o["strike"] - strike) < 0.005), None)
    if not opt:
        return jsonify({"ok": False, "error": "strike not in chain"}), 404

    # Re-compute Greeks at the latest spot (chain may be a few seconds stale)
    iv = opt.get("iv") or 0.20
    fresh_g = greeks(spot, strike, tte or 1e-6, iv,
                     config.DEFAULT_RISK_FREE, config.DEFAULT_DIV_YIELD, opt_type)

    out = dict(opt)
    out.update({
        "delta": fresh_g["delta"], "gamma": fresh_g["gamma"],
        "theta": fresh_g["theta"], "vega": fresh_g["vega"],
        "rho": fresh_g["rho"], "model_price": fresh_g["model_price"],
    })

    # Limit price suggestion
    limits = suggest_limit_prices(out, urgency=urgency)

    # Directional value scoring (does this strike fit the current signal direction?)
    direction = sig.get("dir", "neu")
    scoring = None
    if direction in ("bull", "bear"):
        scoring = score_directional_value(out, spot, direction)

    # Position sizing - we cap at 1 contract = $100 * mid (one SPY contract)
    contract_cost = (out.get("mid") or 0) * 100
    max_pct = config.MAX_BANKROLL_PCT
    max_dollar = bankroll * max_pct
    suggested_contracts = int(max_dollar // contract_cost) if contract_cost > 0 else 0
    # Apply Kelly modifier: smaller size if confidence is low
    confidence_mod = max(0.25, min(1.0, sig.get("confidence", 0) / 3.0))
    suggested_contracts = max(0, int(round(suggested_contracts * confidence_mod * config.KELLY_FRACTION * 4)))
    # (4 * 0.25 = 1.0 with confidence_mod 1.0, scaled down by confidence)

    sizing = {
        "contract_cost": contract_cost,
        "max_dollar_5pct": max_dollar,
        "suggested_contracts": suggested_contracts,
        "suggested_dollar": suggested_contracts * contract_cost,
        "confidence_modifier": confidence_mod,
        "bankroll": bankroll,
    }

    return jsonify({
        "ok": True,
        "spot": spot,
        "option": out,
        "limits": limits,
        "scoring": scoring,
        "sizing": sizing,
        "direction": direction,
        "signal_verdict": sig.get("verdict"),
    })


def _start_poller():
    th = threading.Thread(target=_poller_loop, daemon=True, name="spy-poller")
    th.start()


if __name__ == "__main__":
    print(r"""
   ____  ____  __  __    ___        _   _                  ____    _
  / ___||  _ \ \ \/ /   / _ \ _ __ | |_(_) ___  _ __  ___ |  _ \  / |
  \___ \| |_) | \  /   | | | | '_ \| __| |/ _ \| '_ \/ __|| | | | | |
   ___) |  __/  /  \   | |_| | |_) | |_| | (_) | | | \__ \| |_| | | |
  |____/|_|    /_/\_\   \___/| .__/ \__|_|\___/|_| |_|___/|____/  |_|
                              |_|
   Live SPY chart + indicators + options chain + Greeks + edge model
   Open http://{host}:{port} in your browser
   First data pull may take 5-10s - please wait...
""".format(host=config.HOST, port=config.PORT))
    _start_poller()
app.run(
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 5000))
)
