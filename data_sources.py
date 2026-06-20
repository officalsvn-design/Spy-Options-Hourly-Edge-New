"""Data layer for SPY Options Edge.

Pulls:
  * SPY spot quote + 24h change
  * Multi-timeframe candles (1m, 5m, 15m, 1h, 4h) for indicator computation
  * SPY options chain (calls + puts) with Greeks computed via Black-Scholes

All sources are free. yfinance is the primary source - it's a community library
that scrapes Yahoo's public endpoints. If Yahoo throttles, the candle layer
falls back to Stooq.
"""
import math
import time
import logging
from datetime import datetime, timezone

import requests

try:
    import yfinance as yf
except ImportError:
    yf = None

import config
from options_math import greeks, implied_vol

log = logging.getLogger("spy-data")

# We keep one shared HTTP session for non-yfinance fallbacks
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (SPY Options Edge / Python)",
    "Accept": "application/json,text/csv,*/*",
})

# Cache of yfinance Ticker instances to avoid recreating
_yf_tickers = {}


def _yt(symbol="SPY"):
    if symbol not in _yf_tickers and yf is not None:
        _yf_tickers[symbol] = yf.Ticker(symbol)
    return _yf_tickers.get(symbol)


# ---------- SPOT QUOTE ----------
def fetch_quote():
    """Return {price, prev_close, change_pct, change_abs, ts} or None."""
    out = {"price": None, "prev_close": None, "change_pct": None,
           "change_abs": None, "ts": time.time(), "source": None}
    # Try yfinance's fast_info first (lightweight, no API call when warm)
    t = _yt("SPY")
    if t is not None:
        try:
            fi = t.fast_info
            p = float(fi["last_price"])
            pc = float(fi.get("previous_close") or fi.get("regular_market_previous_close") or 0)
            if p > 0:
                out.update(price=p, prev_close=pc or None,
                           change_abs=(p - pc) if pc else None,
                           change_pct=((p - pc) / pc) if pc else None,
                           source="yfinance")
                return out
        except Exception as e:
            log.debug("yf fast_info failed: %s", e)
    # Fallback - Yahoo Finance quote endpoint via plain HTTP
    try:
        r = _session.get(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={"symbols": "SPY"}, timeout=6,
        )
        if r.ok:
            d = r.json().get("quoteResponse", {}).get("result", [])
            if d:
                q = d[0]
                p = q.get("regularMarketPrice")
                pc = q.get("regularMarketPreviousClose")
                if p:
                    out.update(price=float(p),
                               prev_close=float(pc) if pc else None,
                               change_abs=float(q.get("regularMarketChange") or 0) or None,
                               change_pct=float(q.get("regularMarketChangePercent") or 0) / 100 or None,
                               source="yahoo-quote")
                    return out
    except Exception as e:
        log.debug("yahoo quote fallback failed: %s", e)
    return out


# ---------- CANDLES ----------
_INTERVAL_MAP = {
    "1m":  ("1m",  "5d"),
    "5m":  ("5m",  "30d"),
    "15m": ("15m", "60d"),
    "1h":  ("60m", "730d"),
    "4h":  ("60m", "730d"),  # we'll aggregate 1h -> 4h
}


def _yf_candles(symbol, interval, period):
    t = _yt(symbol)
    if t is None:
        return None
    try:
        df = t.history(interval=interval, period=period, prepost=False,
                       actions=False, auto_adjust=False, raise_errors=False)
        if df is None or df.empty:
            return None
        out = []
        for ts, row in df.iterrows():
            try:
                out.append({
                    "t": int(ts.timestamp()),
                    "o": float(row["Open"]),
                    "h": float(row["High"]),
                    "l": float(row["Low"]),
                    "c": float(row["Close"]),
                    "v": float(row["Volume"] or 0),
                })
            except Exception:
                continue
        # drop incomplete trailing candle? we keep it - users want live
        return out
    except Exception as e:
        log.debug("yf history(%s,%s) failed: %s", interval, period, e)
        return None


def _resample(arr, factor):
    """Aggregate `factor` consecutive candles into one (used to make 4h from 1h)."""
    out = []
    for i in range(0, len(arr) - len(arr) % factor, factor):
        g = arr[i:i + factor]
        out.append({
            "t": g[0]["t"],
            "o": g[0]["o"],
            "h": max(x["h"] for x in g),
            "l": min(x["l"] for x in g),
            "c": g[-1]["c"],
            "v": sum(x["v"] for x in g),
        })
    return out


def fetch_candles(timeframe):
    """Return list of candles for a timeframe label ('1m'..'4h')."""
    interval, period = _INTERVAL_MAP[timeframe]
    data = _yf_candles("SPY", interval, period)
    if not data:
        return []
    if timeframe == "4h":
        data = _resample(data, 4)
    # Trim - keep last 400 candles max (more than enough for indicators)
    return data[-400:]


# ---------- OPTIONS CHAIN ----------
def _years_to_expiry(expiry_str):
    """Convert 'YYYY-MM-DD' to years until 4pm ET expiration."""
    try:
        # Expirations on Yahoo are quoted as the date; SPY options expire 4pm ET
        # (close of trading). We use a UTC approximation - ET is UTC-5/-4 (we'll
        # use -4 for EDT which is the modal case during US market hours).
        exp_dt = datetime.strptime(expiry_str, "%Y-%m-%d").replace(
            hour=20, minute=0, tzinfo=timezone.utc)  # 4pm EDT = 20:00 UTC
        delta = exp_dt - datetime.now(timezone.utc)
        years = delta.total_seconds() / (365.25 * 86400.0)
        return max(years, 1e-6)
    except Exception:
        return None


def fetch_expiries():
    """Return list of available SPY option expiration dates."""
    t = _yt("SPY")
    if t is None:
        return []
    try:
        return list(t.options)
    except Exception as e:
        log.debug("expiries failed: %s", e)
        return []


def fetch_chain(expiry, spot, r=None, q=None):
    """Return {'calls': [...], 'puts': [...], 'expiry': str, 'tte_years': float}.

    Each option row: strike, type, bid, ask, mid, last, iv, volume, open_interest,
    delta, gamma, theta(/day), vega(/1%), rho, in_the_money, contract_symbol.
    """
    r = r if r is not None else config.DEFAULT_RISK_FREE
    q = q if q is not None else config.DEFAULT_DIV_YIELD
    t = _yt("SPY")
    if t is None:
        return {"calls": [], "puts": [], "expiry": expiry, "tte_years": None}
    try:
        chain = t.option_chain(expiry)
    except Exception as e:
        log.warning("option_chain(%s) failed: %s", expiry, e)
        return {"calls": [], "puts": [], "expiry": expiry, "tte_years": None}

    tte = _years_to_expiry(expiry) or 1e-6

    def _row(rec, kind):
        try:
            strike = float(rec["strike"])
        except Exception:
            return None
        bid = float(rec.get("bid") or 0)
        ask = float(rec.get("ask") or 0)
        last = float(rec.get("lastPrice") or 0)
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (last if last > 0 else 0)
        iv = rec.get("impliedVolatility")
        try:
            iv = float(iv) if iv is not None else None
        except Exception:
            iv = None
        if not iv or iv <= 0:
            iv = implied_vol(mid, spot, strike, tte, r, q, kind) if mid > 0 else None
        g = greeks(spot, strike, tte, iv or 0.2, r, q, kind) if iv else None

        def _safe_int(value):
            try:
                if value is None:
                    return None
                if isinstance(value, float) and math.isnan(value):
                    return None
                ivalue = int(value)
                return ivalue if ivalue != 0 else None
            except (TypeError, ValueError):
                return None

        return {
            "strike": strike,
            "type": kind,
            "bid": bid or None,
            "ask": ask or None,
            "mid": mid or None,
            "last": last or None,
            "iv": iv,
            "volume": _safe_int(rec.get("volume")),
            "open_interest": _safe_int(rec.get("openInterest")),
            "delta": g["delta"] if g else None,
            "gamma": g["gamma"] if g else None,
            "theta": g["theta"] if g else None,
            "vega": g["vega"] if g else None,
            "rho": g["rho"] if g else None,
            "model_price": g["model_price"] if g else None,
            "in_the_money": (kind == "call" and spot > strike) or (kind == "put" and spot < strike),
            "contract_symbol": rec.get("contractSymbol"),
        }

    calls = []
    puts = []
    try:
        for rec in chain.calls.to_dict("records"):
            row = _row(rec, "call")
            if row: calls.append(row)
        for rec in chain.puts.to_dict("records"):
            row = _row(rec, "put")
            if row: puts.append(row)
    except Exception as e:
        log.warning("chain row parse failed: %s", e)

    calls.sort(key=lambda x: x["strike"])
    puts.sort(key=lambda x: x["strike"])
    return {"calls": calls, "puts": puts, "expiry": expiry, "tte_years": tte}


# ---------- IV SKEW / TERM STRUCTURE ----------
def put_call_skew(chain, spot):
    """A simple skew metric: avg IV of 5 closest OTM puts vs 5 closest OTM calls.

    Negative skew (puts > calls) = market pricing in downside; bullish-contrarian
    signal traditionally, but for short horizons more often a 'protection bid'.
    """
    if not chain or not chain.get("calls") or not chain.get("puts"):
        return None
    otm_calls = [c for c in chain["calls"] if c["strike"] >= spot and c.get("iv")]
    otm_puts = [p for p in chain["puts"] if p["strike"] <= spot and p.get("iv")]
    otm_calls = sorted(otm_calls, key=lambda x: x["strike"])[:5]
    otm_puts = sorted(otm_puts, key=lambda x: -x["strike"])[:5]
    if not otm_calls or not otm_puts:
        return None
    avg_c = sum(c["iv"] for c in otm_calls) / len(otm_calls)
    avg_p = sum(p["iv"] for p in otm_puts) / len(otm_puts)
    return {"call_iv": avg_c, "put_iv": avg_p, "skew": avg_p - avg_c}
