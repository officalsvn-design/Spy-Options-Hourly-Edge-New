"""Pure-Python technical indicators - no numpy/pandas required.

All functions take plain lists of floats and return same-length lists,
None-padded where values cannot be computed yet.
"""


def ema(values, period):
    out, prev, k = [], None, 2.0 / (period + 1)
    for i, v in enumerate(values):
        prev = v if i == 0 else v * k + prev * (1 - k)
        out.append(prev)
    return out


def sma(values, period):
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain /= period
    loss /= period
    out[period] = 100 - 100 / (1 + gain / (loss or 1e-9))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (period - 1) + max(d, 0.0)) / period
        loss = (loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100 - 100 / (1 + gain / (loss or 1e-9))
    return out


def macd(closes, fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [ef[i] - es[i] for i in range(len(closes))]
    sig = ema(line, signal)
    hist = [line[i] - sig[i] for i in range(len(closes))]
    return {"line": line, "signal": sig, "hist": hist}


def wave_trend(highs, lows, closes, n1=10, n2=21):
    ap = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    esa = ema(ap, n1)
    d = ema([abs(ap[i] - esa[i]) for i in range(len(ap))], n1)
    ci = [(ap[i] - esa[i]) / (0.015 * (d[i] or 1e-9)) for i in range(len(ap))]
    wt1 = ema(ci, n2)
    wt2 = sma(wt1, 4)
    return {"wt1": wt1, "wt2": wt2}


def bollinger(closes, period=20, mult=2.0):
    mid = sma(closes, period)
    upper, lower, pctb = [], [], []
    for i in range(len(closes)):
        if i < period - 1:
            upper.append(None); lower.append(None); pctb.append(None)
            continue
        m = mid[i]
        var = sum((closes[j] - m) ** 2 for j in range(i - period + 1, i + 1)) / period
        sd = var ** 0.5
        u, l = m + mult * sd, m - mult * sd
        upper.append(u); lower.append(l)
        pctb.append((closes[i] - l) / ((u - l) or 1e-9))
    return {"mid": mid, "upper": upper, "lower": lower, "pctb": pctb}


def stochastic(highs, lows, closes, k_period=14, d_period=3):
    k = [None] * len(closes)
    for i in range(k_period - 1, len(closes)):
        hh = max(highs[i - k_period + 1:i + 1])
        ll = min(lows[i - k_period + 1:i + 1])
        k[i] = 100 * (closes[i] - ll) / ((hh - ll) or 1e-9)
    d = sma([x if x is not None else 0.0 for x in k], d_period)
    return {"k": k, "d": d}


def last(arr):
    for v in reversed(arr):
        if v is not None:
            return v
    return None


def realized_vol_annual(closes, lookback=60):
    """Annualized realized volatility from intraday minute closes."""
    if len(closes) < 5:
        return 0.0
    import math
    sample = closes[-min(lookback, len(closes)):]
    rets = []
    for i in range(1, len(sample)):
        rets.append(math.log(sample[i] / sample[i - 1]))
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    # Annualize: 252 trading days * 390 minutes/day = 98280 minutes
    return math.sqrt(var * 98280)


def _classify(v, bull, bear):
    return "bull" if v > bull else "bear" if v < bear else "neu"


def compute_timeframe(candles):
    """Return per-timeframe indicator cells + a net bias score in [-1, 1]."""
    if not candles or len(candles) < 30:
        return None
    c = [x["c"] for x in candles]
    h = [x["h"] for x in candles]
    l = [x["l"] for x in candles]
    R = rsi(c)
    M = macd(c)
    W = wave_trend(h, l, c)
    B = bollinger(c)
    ST = stochastic(h, l, c)
    e20, e50 = last(ema(c, 20)), last(ema(c, 50))
    price = c[-1]
    rv, mh = last(R), last(M["hist"])
    w1, w2 = last(W["wt1"]), last(W["wt2"])
    pb = last(B["pctb"])
    kv, dv = last(ST["k"]), last(ST["d"])

    cells = {
        "rsi":   {"v": rv, "cls": "neu" if rv is None else _classify(rv, 55, 45),
                  "txt": "\u2014" if rv is None else f"{rv:.0f}"},
        "macd":  {"v": mh, "cls": "neu" if mh is None else ("bull" if mh > 0 else "bear"),
                  "txt": "\u2014" if mh is None else ("\u25B2" if mh > 0 else "\u25BC")},
        "wt":    {"v": w1, "cls": "neu" if (w1 is None or w2 is None) else
                  ("bull" if w1 > w2 else "bear"),
                  "txt": "\u2014" if w1 is None else f"{w1:.0f}"},
        "stoch": {"v": kv, "cls": "neu" if (kv is None or dv is None) else
                  ("bull" if kv > dv else "bear"),
                  "txt": "\u2014" if kv is None else f"{kv:.0f}"},
        "trend": {"v": price, "cls": ("neu" if e50 is None else
                                       ("bull" if price > e50 and e20 > e50 else
                                        "bear" if price < e50 and e20 < e50 else "neu")),
                  "txt": "\u2014" if e50 is None else ("\u25B2" if price > e50 else "\u25BC")},
        "bb":    {"v": pb, "cls": "neu" if pb is None else _classify(pb, 0.55, 0.45),
                  "txt": "\u2014" if pb is None else f"{pb:.2f}"},
    }
    score = 0
    for cell in cells.values():
        if cell["cls"] == "bull":
            score += 1
        elif cell["cls"] == "bear":
            score -= 1
    return {"cells": cells, "score": score / len(cells)}
