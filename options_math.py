"""Black-Scholes greeks (no scipy needed) + option-strike scoring.

We compute analytic delta, gamma, theta, vega, and rho for European options
on a dividend-paying stock (the standard generalised Black-Scholes-Merton).
SPY is European-cash-settled-style enough for these to be a solid retail
approximation (it's an ETF with discrete dividends, but BSM with continuous
yield is the textbook fit and what every options platform displays).
"""
import math


SQRT_2 = math.sqrt(2.0)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def greeks(spot, strike, time_to_exp_years, vol, r=0.043, q=0.013, opt_type="call"):
    """Return dict with mid_price + delta, gamma, theta (per day), vega (per 1%), rho.

    All inputs are floats. `time_to_exp_years` should be >= ~1 minute; we clamp
    micro-values so divisions don't blow up.
    """
    S, K, T, sigma = float(spot), float(strike), max(float(time_to_exp_years), 1e-6), max(float(vol), 1e-4)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    df_r = math.exp(-r * T)
    df_q = math.exp(-q * T)
    Nd1, Nd2 = _norm_cdf(d1), _norm_cdf(d2)
    Nmd1, Nmd2 = _norm_cdf(-d1), _norm_cdf(-d2)
    pdf_d1 = _norm_pdf(d1)

    if opt_type == "call":
        price = S * df_q * Nd1 - K * df_r * Nd2
        delta = df_q * Nd1
        theta = (-(S * df_q * pdf_d1 * sigma) / (2 * sqrtT)
                 - r * K * df_r * Nd2
                 + q * S * df_q * Nd1)
        rho = K * T * df_r * Nd2 * 0.01  # per 1% rate
    else:
        price = K * df_r * Nmd2 - S * df_q * Nmd1
        delta = -df_q * Nmd1
        theta = (-(S * df_q * pdf_d1 * sigma) / (2 * sqrtT)
                 + r * K * df_r * Nmd2
                 - q * S * df_q * Nmd1)
        rho = -K * T * df_r * Nmd2 * 0.01

    gamma = (df_q * pdf_d1) / (S * sigma * sqrtT)
    vega = S * df_q * pdf_d1 * sqrtT * 0.01  # per 1 vol point
    theta_per_day = theta / 365.0

    return {
        "model_price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta_per_day,
        "vega": vega,
        "rho": rho,
    }


def implied_vol(market_price, spot, strike, time_to_exp_years, r=0.043, q=0.013, opt_type="call"):
    """Brent-bisection IV solve. Used as fallback when Yahoo IV is missing/zero."""
    if market_price <= 0 or time_to_exp_years <= 0:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        try:
            p = greeks(spot, strike, time_to_exp_years, mid, r, q, opt_type)["model_price"]
        except Exception:
            return None
        if abs(p - market_price) < 1e-4:
            return mid
        if p < market_price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def score_directional_value(opt, spot, direction, fee_per_contract=0.0):
    """Score how attractive an option is GIVEN a directional thesis.

    direction: 'bull' or 'bear'
    Returns a dict with cost, breakeven, delta_per_dollar, theta_drag_pct,
    and a composite 'score' (higher = better).

    Philosophy: cheapest != best. Best = most delta exposure per dollar risked,
    closest breakeven, lowest theta drag, reasonable IV (not vol-crushed).
    """
    mid = opt.get("mid")
    if not mid or mid <= 0:
        return None
    delta = opt.get("delta") or 0.0
    theta = opt.get("theta") or 0.0
    iv = opt.get("iv") or 0.0
    strike = opt["strike"]
    opt_type = opt["type"]

    # only score options that match the directional thesis
    if direction == "bull" and opt_type != "call": return None
    if direction == "bear" and opt_type != "put":  return None

    # delta-per-dollar: how much directional exposure $1 of premium buys
    abs_delta = abs(delta)
    delta_per_dollar = abs_delta / mid if mid > 0 else 0.0

    # breakeven distance as % of spot
    breakeven = (strike + mid) if opt_type == "call" else (strike - mid)
    be_dist_pct = (breakeven - spot) / spot if opt_type == "call" else (spot - breakeven) / spot
    # closer/below-zero breakeven = better; we want LOW be_dist_pct (negative = already ITM at expiry)

    # theta drag per day relative to mid
    theta_drag_pct = (theta / mid) if mid > 0 else 0.0   # typically negative for long options

    # IV penalty: prefer reasonable IV. Too low = pricing in nothing (often illiquid); too high = vol crush risk.
    # We mildly penalize IV outside [0.10, 0.40] band for SPY.
    iv_penalty = 0.0
    if iv:
        if iv < 0.08: iv_penalty = (0.08 - iv) * 2
        elif iv > 0.45: iv_penalty = (iv - 0.45) * 2

    # liquidity bonus: tight spread, decent volume
    bid, ask = opt.get("bid") or 0.0, opt.get("ask") or 0.0
    spread = (ask - bid) if (ask > 0 and bid > 0) else mid
    spread_pct = spread / mid if mid > 0 else 1.0
    vol_ = opt.get("volume") or 0
    oi = opt.get("open_interest") or 0
    liq_bonus = 0.0
    if spread_pct < 0.05: liq_bonus += 0.15
    elif spread_pct > 0.20: liq_bonus -= 0.20
    if vol_ > 500: liq_bonus += 0.05
    if oi > 1000:  liq_bonus += 0.05

    # composite: weight delta/$ heavily, penalize negative breakeven dist (far OTM = lottery)
    # we want options that give meaningful exposure without paying lottery premium
    score = (delta_per_dollar * 1.0
             - max(be_dist_pct, 0) * 1.5
             + max(theta_drag_pct, -0.20) * 0.5
             - iv_penalty
             + liq_bonus)
    return {
        "delta_per_dollar": delta_per_dollar,
        "breakeven": breakeven,
        "be_dist_pct": be_dist_pct,
        "theta_drag_pct": theta_drag_pct,
        "spread_pct": spread_pct,
        "score": score,
        "cost_one": mid * 100,   # one contract = 100 shares
    }


def suggest_limit_prices(opt, urgency="balanced"):
    """Suggest BUY and SELL limit prices for a given strike using its live bid/ask.

    urgency: 'patient' (try to capture spread), 'balanced', or 'aggressive' (fill now).
    Returns dict with buy_limit, sell_limit, and notes.
    """
    bid = opt.get("bid") or 0.0
    ask = opt.get("ask") or 0.0
    mid = opt.get("mid") or ((bid + ask) / 2 if bid and ask else 0.0)
    if bid <= 0 or ask <= 0 or mid <= 0:
        return None
    spread = ask - bid
    spread_pct = spread / mid if mid > 0 else 0.0

    if urgency == "aggressive":
        buy = ask
        sell = bid
    elif urgency == "patient":
        # try to get 1/3 of the way from your side toward mid
        buy = bid + spread * 0.33
        sell = ask - spread * 0.33
    else:  # balanced
        # split the spread - widely advised retail approach
        buy = mid - spread * 0.10
        sell = mid + spread * 0.10
        # but never worse than bid/ask
        buy = max(min(buy, ask), bid)
        sell = min(max(sell, bid), ask)

    # SPY is a Penny Pilot security: all strikes trade in $0.01 ticks regardless of premium
    def _tick_round(p):
        return round(p, 2)

    return {
        "buy_limit": _tick_round(buy),
        "sell_limit": _tick_round(sell),
        "mid": _tick_round(mid),
        "spread": _tick_round(spread),
        "spread_pct": spread_pct,
        "urgency": urgency,
    }
