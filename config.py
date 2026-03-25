"""
╔══════════════════════════════════════════════════════════════════════╗
║          POLYMARKET TICK BOT — CONFIGURATION                      ║
║  Opposite-of-first-tick strategy on BTC 5-min markets             ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
from dotenv import load_dotenv
load_dotenv()

# ═══════════════════════════════════════════════════════════════
#  POLYMARKET API
# ═══════════════════════════════════════════════════════════════

POLYMARKET_PRIVATE_KEY      = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
POLYMARKET_API_KEY          = os.environ.get("POLYMARKET_API_KEY", "").strip()
POLYMARKET_API_SECRET       = os.environ.get("POLYMARKET_API_SECRET", "").strip()
POLYMARKET_API_PASSPHRASE   = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
POLYMARKET_FUNDER_ADDRESS   = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
POLYMARKET_SIGNATURE_TYPE   = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

CHAIN_ID  = int(os.environ.get("CHAIN_ID", "137"))
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() in ("true", "1", "yes")

# ═══════════════════════════════════════════════════════════════
#  MARKETS
# ═══════════════════════════════════════════════════════════════

WINDOW_SEC = 300            # 5-minute markets
ASSETS = ["btc"]            # BTC only

# Slug pattern: btc-updown-5m-{ts}
SLUG_PATTERN = "{asset}-updown-5m-{ts}"

# ═══════════════════════════════════════════════════════════════
#  PRICE FEEDS (Binance)
# ═══════════════════════════════════════════════════════════════

BINANCE_WS_BTC = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_WS_ETH = "wss://stream.binance.com:9443/ws/ethusdt@aggTrade"

# ═══════════════════════════════════════════════════════════════
#  TICK STRATEGY PARAMETERS
# ═══════════════════════════════════════════════════════════════

BET_SIZE_USDC       = 5.00   # Bet size per trade
DIRECTION_THRESHOLD = 0.001  # Ultra-low: react to any micro-move (0.001%)
FAST_LIMIT_PRICE    = 0.55   # Limit price for fast execution
ENTRY_DEADLINE_SEC  = 60     # Don't enter in the last minute

# Position limits per market
MAX_TOTAL_USDC      = 10.00  # Max total spend per market
MAX_ONE_SIDE_USDC   = 10.00  # Max on one direction

# ═══════════════════════════════════════════════════════════════
#  EARLY EXIT (Safety)
# ═══════════════════════════════════════════════════════════════

PROFIT_EXIT_PCT       = 0.90   # High threshold — hold all the way
NEAR_MAX_EXIT_BID     = 0.98   # Capture 98% if it gets there

# ═══════════════════════════════════════════════════════════════
#  EXECUTION
# ═══════════════════════════════════════════════════════════════

MIN_BET_USDC        = 0.50
TICK_SIZE           = "0.01"
ORDER_TIMEOUT_SEC   = 20
FILL_TIMEOUT_SEC    = 30
API_RATE_LIMIT      = 30     # Aggressive — minimize inter-request delay
BALANCE_REFRESH_SEC = 300
DEFAULT_BANKROLL_USDC = 100.0

# ═══════════════════════════════════════════════════════════════
#  LOOP TIMING
# ═══════════════════════════════════════════════════════════════

MAIN_LOOP_INTERVAL  = 0.01  # 10ms — maximum responsiveness
DISPLAY_INTERVAL    = 30.0

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
