# SPY Options Edge

A live, browser-based SPY options trading dashboard you run locally from VS Code. Pulls SPY quote, multi-timeframe candles, full options chain with computed Greeks, and embeds a live TradingView SPY chart. Built to help you size and time SPY option trades — not predict the market.

## What it shows

- **Live TradingView SPY chart** (AMEX:SPY) with RSI + MACD overlays, multi-timeframe selector (1m / 5m / 15m / 1h / 4h)
- **Multi-timeframe momentum gauge** — weighted bias from RSI, MACD, WaveTrend, Stochastic, EMA trend, Bollinger %B
- **Signal Matrix** — per-timeframe color-coded indicator readings
- **Direction Signal** — BULLISH / BEARISH / NEUTRAL verdict with probability bar (calibrated 20–80% so you don't get false confidence)
- **Best Risk-Reward Strikes** — top 5 calls and top 5 puts scored by:
  - delta-per-dollar (most exposure per $ risked)
  - breakeven distance (how far SPY actually has to move)
  - theta drag (daily premium decay)
  - IV penalty (avoid vol-crushed or vol-pumped strikes)
  - liquidity bonus (tight spread + decent volume / OI)
- **Live Options Chain** — calls + puts merged on strike, sorted around the money, ATM highlighted, with live bid / ask / IV / delta / theta / volume / OI
- **Strike Analyzer** — click any strike for:
  - Fresh Black-Scholes Greeks recomputed at the current spot
  - Suggested **BUY LIMIT** and **SELL LIMIT** prices across three urgency tiers (patient / balanced / aggressive)
  - Directional fit score vs. the current signal
  - Position sizing (¼-Kelly, capped at 5% of bankroll, adjusted by signal confidence)

## Quickstart

```bash
# 1. Open the project in VS Code
cd spy-options-edge
code .

# 2. Install dependencies (Python 3.9+ recommended)
pip install -r requirements.txt

# 3. Run it
#    Option A (VS Code):  press F5
#    Option B (terminal): python app.py

# 4. Open the dashboard
#    http://127.0.0.1:5000
```

First load takes 5–10 seconds while it pulls SPY quote, all 5 timeframes of candles, and the full options chain. After that, snapshots refresh every 5 seconds in your browser.

## Refresh cadence

| Data | Refresh interval |
|---|---|
| SPY spot quote | every 5 s |
| OHLC candles + indicators | every 30 s |
| Options chain + Greeks | every 20 s |
| Browser snapshot pull | every 5 s |

Configurable in `config.py`.

## Honest caveats — read these

- **Yahoo Finance is the data source.** During US market hours, Yahoo's SPY quote and options chain can be delayed by up to 15 minutes for some IPs. The TradingView chart is independent and stays live. **Always cross-check fills in your broker before submitting orders.**
- **Greeks are model values** computed with Black-Scholes assuming continuous dividend yield (`q = 1.3%`) and a constant risk-free rate (`r = 4.3%`). SPY is technically an American-style option on an ETF, but BSM is what every retail platform displays and the difference is negligible for short-dated trades.
- **No real-time Level-2 order book.** Free options data shows only best bid / best ask — not full DOM depth. For true L2 you'd need a paid feed (Polygon, Tradier, IBKR). The limit-price recommender works on bid/ask/spread/mid, which is what most retail traders actually have access to anyway.
- **The Direction Signal is a statistical bias estimate, not a prediction.** No tool can reliably forecast SPY's next move. The signal is calibrated to stay between 20% and 80% probability to keep you honest.
- **Position-sizing suggestions are starting points.** They assume the bankroll you pass in (default $5,000) and a fractional Kelly with a hard 5% cap. Adjust for your own risk tolerance, account size, and tax situation. **This is not financial advice.**

## Project structure

```
spy-options-edge/
├── app.py                  # Flask backend + background poller + JSON API
├── config.py               # cadences, model params, defaults
├── data_sources.py         # SPY quote / candles / options chain (via yfinance + Yahoo)
├── indicators.py           # pure-Python RSI, MACD, WaveTrend, Stoch, Bollinger, EMA
├── options_math.py         # Black-Scholes Greeks, IV solver, scoring, limit prices
├── templates/
│   └── dashboard.html      # the entire frontend (HTML + CSS + vanilla JS)
├── requirements.txt        # Flask, requests, yfinance
└── .vscode/
    └── launch.json         # F5 in VS Code just works
```

No frontend framework, no build step, no database — single-process Python app that streams its in-memory snapshot over a JSON endpoint.

## How the directional pick scoring works

For each option matching the directional thesis (calls for bull, puts for bear), we compute:

```
score = delta_per_dollar               (most directional bang per buck)
      - max(breakeven_distance_pct, 0) * 1.5    (penalize far-OTM lottery tickets)
      + max(theta_drag_pct, -0.20) * 0.5        (cap the theta penalty)
      - iv_penalty                              (vol-crushed or vol-pumped)
      + liquidity_bonus                         (tight spread + decent vol/OI)
```

Top 5 by score for each side. The point is **risk-adjusted exposure**, not lowest premium. A $0.02 far-OTM lottery ticket has cheap premium but terrible delta-per-dollar and effectively zero chance of paying off, so it scores poorly here.

## Position sizing formula

```
suggested_contracts = floor(max_dollar / contract_cost)
                    * confidence_modifier      (0.25 — 1.00 based on signal confidence)
                    * kelly_fraction (0.25)
                    * 4                        (4 × 0.25 = 1.0 at full confidence)

max_dollar = bankroll * 0.05                   (hard 5% cap per position)
```

At full signal confidence (3/3), you get the full 5% cap allocation. At low confidence, sizing drops to ~25% of that. Override `bankroll` via the `?bankroll=` query param on `/api/strike` (UI input field is on the roadmap).

## API endpoints

- `GET /` — dashboard HTML
- `GET /api/snapshot` — full current state (quote, indicators, chain, picks, signal)
- `GET /api/expiries` — implicit via snapshot
- `GET /api/set_expiry?expiry=YYYY-MM-DD` — switch active expiration
- `GET /api/strike?strike=X&type=call|put&urgency=patient|balanced|aggressive&bankroll=N` — single-strike analysis

## Troubleshooting

**"No data loading" / empty quote:** Yahoo throttles aggressive IPs. Wait a minute, refresh. If persistent, your IP may be rate-limited — try from a different network or restart your router.

**"options chain empty":** Yahoo's options endpoint occasionally returns empty for the front-week expiry right before/after open. Switch to a different expiry in the dropdown.

**"4h candles look like 1h":** They're resampled from 1h candles client-side (Yahoo doesn't natively serve 4h). Working as designed.

**"can't see all the strikes":** The snapshot trims to the 30 strikes closest to spot (per side) to keep the JSON payload small. Use the expiry dropdown to flip to a different week if you need strikes farther OTM.

## License

Personal-use research tool. No warranty. You are responsible for every trade you make.
