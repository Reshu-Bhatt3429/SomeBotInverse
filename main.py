"""
╔══════════════════════════════════════════════════════════════════════╗
║       POLYMARKET TICK BOT — MAIN LOOP                             ║
║  Opposite-of-first-tick strategy on BTC 5-min markets             ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import time
import signal
import sys
import csv
import os
import logging
from datetime import datetime

from config import (
    LIVE_TRADING, MAIN_LOOP_INTERVAL, DISPLAY_INTERVAL,
    ASSETS, WINDOW_SEC, ENTRY_DEADLINE_SEC,
    BALANCE_REFRESH_SEC,
    LOG_DIR,
)
from price_feed import PriceFeed
from market_scanner import MarketScanner
from executor import Executor
from tick_engine import TickEngine
from data_collector import DataCollector

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

logger = logging.getLogger("main")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    fh = logging.FileHandler(f"{LOG_DIR}/bot_{ts}.log")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(sh)


class TickBot:
    def __init__(self):
        self.feed    = PriceFeed()
        self.scanner = MarketScanner()
        self.executor = Executor()
        self.data    = DataCollector()
        self.engine  = TickEngine(self.executor, self.scanner, self.data)

        # Session stats
        self.daily_pnl         = 0.0
        self.session_pnl       = 0.0
        self.total_markets     = 0
        self.profitable_markets = 0

        # Display
        self._last_display  = 0.0
        self._last_window   = (int(time.time()) // WINDOW_SEC) * WINDOW_SEC
        self._last_balance  = 0.0
        self._running       = True

        # Trade log
        self._csv_path = os.path.join(LOG_DIR, "trades.csv")
        self._init_csv()

    def run(self):
        signal.signal(signal.SIGINT,  self._handle_stop)
        signal.signal(signal.SIGTERM, self._handle_stop)

        mode = "🔴 LIVE" if LIVE_TRADING else "🔵 DRY RUN"
        logger.info(f"\n{'═'*60}")
        logger.info(f"  POLYMARKET TICK BOT | {mode}")
        logger.info(f"  Assets: {', '.join(a.upper() for a in ASSETS)}")
        logger.info(f"{'═'*60}\n")

        if not self.executor.setup():
            logger.error("❌ Executor setup failed")
            return

        self.feed.start()
        self.feed.wait_ready(timeout=15)

        # Start data collection
        self.data.start(self.feed, self.scanner, self.engine)

        self._refresh_balance()

        next_window = self._last_window + WINDOW_SEC
        wait_sec = int(next_window - time.time())
        logger.info(f"🚀 Bot running — waiting for next window in ~{wait_sec}s\n")

        while self._running:
            try:
                self._tick()
                time.sleep(MAIN_LOOP_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"⚠️  Loop error: {e}", exc_info=True)
                time.sleep(5)

        self._shutdown()

    def _tick(self):
        now = time.time()
        current_window = (int(now) // WINDOW_SEC) * WINDOW_SEC

        if now - self._last_balance >= BALANCE_REFRESH_SEC:
            self._refresh_balance()

        # Hard stop: $25 session loss limit
        if self.session_pnl <= -25.0:
            logger.warning(f"🛑 Session loss limit hit (${self.session_pnl:+.2f}) — stopping bot")
            self._running = False
            return

        if current_window != self._last_window:
            self._last_window = current_window
            self._on_new_window(current_window)

        for asset in ASSETS:
            if not self.engine.has_position(asset):
                continue

            pos = self.engine.positions[asset]
            if pos.is_closed:
                continue

            remaining = self.scanner.seconds_remaining(pos.market)

            if remaining is not None and remaining <= 0:
                self._resolve_market(asset)
                continue

            self.engine.update(asset, self.feed, remaining)

        self._maybe_display()

    def _on_new_window(self, window_ts: int):
        logger.info(f"\n⏰ New window: {datetime.utcfromtimestamp(window_ts).strftime('%H:%M UTC')}")

        for asset in ASSETS:
            if self.engine.has_position(asset):
                self._resolve_market(asset)

            market = self.scanner.get_market(asset, window_ts)
            if market is None:
                logger.info(f"  [{asset.upper()}] No market found for this window")
                continue

            remaining = self.scanner.seconds_remaining(market)
            if remaining is not None and remaining < ENTRY_DEADLINE_SEC:
                logger.info(f"  [{asset.upper()}] Market too close to expiry ({remaining:.0f}s) — skipping")
                continue

            feed = self.feed.get(asset)
            feed.set_open()

            self.data.record_window_open(asset, window_ts, market, feed.price)
            self.engine.open_position(asset, market)

    def _resolve_market(self, asset: str):
        feed = self.feed.get(asset)
        move = feed.move_pct()
        outcome = "UP" if move > 0 else "DOWN" if move < 0 else "FLAT"

        result = self.engine.resolve(asset, outcome)
        if result is None:
            return

        pos = self.engine.positions.get(asset)
        window_ts = pos.market.get("window_ts", 0) if pos else 0
        self.data.record_window_resolve(
            asset, window_ts, result,
            binance_open=feed.open_price,
            binance_close=feed.price,
        )

        pnl = result["net_pnl"]
        self.daily_pnl   += pnl
        self.session_pnl += pnl
        self.total_markets += 1

        if pnl > 0:
            self.profitable_markets += 1

        self._log_csv(result)

    def _maybe_display(self):
        now = time.time()
        if now - self._last_display < DISPLAY_INTERVAL:
            return
        self._last_display = now

        mkt_wr = (self.profitable_markets / self.total_markets * 100 if self.total_markets > 0 else 0)
        lines = [
            f"\n{'─'*60}",
            f"  BTC ${self.feed.btc.price:>10,.2f}  "
            f"move {self.feed.btc.move_pct():+.3f}%  "
            f"{self.feed.btc.direction()}",
            f"  Bankroll: ${self.engine.bankroll:.2f}",
            f"  PnL: Daily ${self.daily_pnl:+.2f} | Session ${self.session_pnl:+.2f}",
            f"  Win Rate: {mkt_wr:.0f}% ({self.profitable_markets}W/{self.total_markets} total)",
        ]
        for asset in ASSETS:
            lines.append(f"  {self.engine.summary(asset)}")
        lines.append(f"{'─'*60}")
        logger.info("\n".join(lines))

    def _refresh_balance(self):
        balance = self.executor.get_balance()
        if balance > 0:
            self.engine.bankroll = balance
            logger.info(f"💵 Bankroll: ${balance:.2f} USDC")
        self._last_balance = time.time()

    def _shutdown(self):
        logger.info("\n🛑 Shutting down...")
        self.data.stop()
        self.feed.stop()
        mkt_wr = (self.profitable_markets / self.total_markets * 100
                  if self.total_markets > 0 else 0)
        logger.info(
            f"\n{'═'*60}\n"
            f"  Session complete\n"
            f"  Markets: {self.profitable_markets}W / "
            f"{self.total_markets - self.profitable_markets}L "
            f"({mkt_wr:.0f}%)\n"
            f"  Session PnL: ${self.session_pnl:+.2f}\n"
            f"{'═'*60}\n"
        )

    def _handle_stop(self, *_):
        logger.info("\n🛑 Stop signal received")
        self._running = False

    def _init_csv(self):
        if not os.path.exists(self._csv_path):
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "asset", "direction", "lean",
                    "total_cost", "payout", "net_pnl", "correct_lean",
                    "daily_pnl", "session_pnl",
                ])

    def _log_csv(self, result: dict):
        try:
            with open(self._csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.utcnow().isoformat(),
                    result["asset"],
                    result["direction"],
                    result["lean"],
                    f"{result['total_cost']:.4f}",
                    f"{result['payout']:.4f}",
                    f"{result['net_pnl']:.4f}",
                    result["correct_lean"],
                    f"{self.daily_pnl:.4f}",
                    f"{self.session_pnl:.4f}",
                ])
        except Exception as e:
            logger.warning(f"CSV log failed: {e}")


if __name__ == "__main__":
    bot = TickBot()
    bot.run()
