"""Central configuration for SPY Options Edge."""

# Server
import os

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))

# Refresh cadence (seconds)
QUOTE_REFRESH = 5         # SPY spot/quote
CANDLES_REFRESH = 30      # multi-timeframe candles + indicators
CHAIN_REFRESH = 20        # options chain
DEFAULT_RISK_FREE = 0.043 # 3-mo T-bill ~ 4.3%; used for Black-Scholes Greeks
DEFAULT_DIV_YIELD = 0.013 # SPY div yield ~ 1.3%

# Model
MOMENTUM_TILT = 0.30      # how much indicator bias nudges direction prob
KELLY_FRACTION = 0.25     # fractional Kelly suggestion
MAX_BANKROLL_PCT = 0.05   # cap each option position at 5% of bankroll

# Default bankroll for sizing display (UI override soon if you want)
DEFAULT_BANKROLL = 5000
