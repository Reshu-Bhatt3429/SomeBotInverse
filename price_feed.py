"""
Price Feed — Real-time BTC and ETH prices from Binance WebSocket.
Tracks price, move % from market open, and momentum direction.
"""

import json
import time
import logging
import threading
import websocket

from config import BINANCE_WS_BTC, BINANCE_WS_ETH, LOG_DIR

logger = logging.getLogger("price_feed")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(f"{LOG_DIR}/price_feed.log")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    logger.addHandler(sh)


class AssetFeed:
    """Single-asset price feed from Binance aggTrade stream."""

    def __init__(self, ws_url: str, name: str):
        self.name = name
        self._ws_url = ws_url
        self.price = 0.0
        self.open_price = 0.0     # Set at market open
        self.open_time = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._ws_thread = None
        self._reconnect_delay = 5

    def start(self):
        self._running = True
        self._ws_thread = threading.Thread(target=self._loop, daemon=True)
        self._ws_thread.start()

    def stop(self):
        self._running = False

    def set_open(self, price=None):
        """Mark the start of a new market window."""
        with self._lock:
            self.open_price = price if price else self.price
            self.open_time = time.time()

    def move_pct(self):
        """Return % price change since market open. 0.0 if no open set."""
        if self.open_price <= 0:
            return 0.0
        return (self.price - self.open_price) / self.open_price * 100

    def direction(self):
        """'UP', 'DOWN', or 'FLAT' based on move from open."""
        move = self.move_pct()
        if move > 0.05:
            return "UP"
        if move < -0.05:
            return "DOWN"
        return "FLAT"

    @property
    def is_ready(self):
        return self._running and self.price > 0

    def _loop(self):
        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_message=self._on_message,
                    on_open=lambda ws: logger.info(f"📡 {self.name} WS connected"),
                    on_error=lambda ws, e: logger.error(f"⚠️  {self.name} WS error: {e}"),
                    on_close=lambda ws, c, m: logger.info(f"📡 {self.name} WS closed"),
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"⚠️  {self.name} WS loop error: {e}")
            if self._running:
                time.sleep(self._reconnect_delay)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            with self._lock:
                self.price = float(data["p"])
        except Exception:
            pass


class PriceFeed:
    """Combined BTC + ETH price feed."""

    def __init__(self):
        self.btc = AssetFeed(BINANCE_WS_BTC, "BTC")
        self.eth = AssetFeed(BINANCE_WS_ETH, "ETH")

    def start(self):
        self.btc.start()
        self.eth.start()
        logger.info("📡 Price feeds starting (BTC + ETH)...")

    def stop(self):
        self.btc.stop()
        self.eth.stop()

    def wait_ready(self, timeout=15):
        """Block until both feeds have prices, or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.btc.is_ready and self.eth.is_ready:
                logger.info(
                    f"✅ Feeds ready: BTC=${self.btc.price:,.2f}  "
                    f"ETH=${self.eth.price:,.2f}"
                )
                return True
            time.sleep(0.5)
        logger.warning("⚠️  Price feed timeout — proceeding anyway")
        return False

    def get(self, asset: str) -> AssetFeed:
        return self.btc if asset == "btc" else self.eth
