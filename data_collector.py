"""
Data Collector — Records everything needed to analyze and improve the bot's edge.

Writes to logs/data/ as JSON-lines files (one JSON object per line).
Three data streams:
  1. windows.jsonl  — one record per 5-min window per asset (entry, outcome, timing)
  2. ticks.jsonl    — price snapshots every ~10s during each window
  3. books.jsonl    — order book snapshots at key moments
"""

import json
import os
import time
import threading
import logging
import requests

from config import LOG_DIR, WINDOW_SEC, CLOB_HOST

DATA_DIR = os.path.join(LOG_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

logger = logging.getLogger("data_collector")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = logging.FileHandler(f"{LOG_DIR}/data_collector.log")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)

TICK_SAMPLE_INTERVAL = 10  # seconds between price snapshots


class DataCollector:
    """
    Non-blocking data collection that runs alongside the bot.
    Call record_* methods from the bot's hot path — they just append to buffers.
    A background thread flushes to disk.
    """

    def __init__(self):
        self._windows_path = os.path.join(DATA_DIR, "windows.jsonl")
        self._ticks_path = os.path.join(DATA_DIR, "ticks.jsonl")
        self._books_path = os.path.join(DATA_DIR, "books.jsonl")
        self._orders_path = os.path.join(DATA_DIR, "orders.jsonl")

        self._buffer_lock = threading.Lock()
        self._buffer = []  # list of (filepath, dict) to flush

        self._tick_thread = None
        self._running = False
        self._price_feed = None
        self._scanner = None
        self._engine = None

        # Own HTTP session for book sampling — does NOT share the executor's rate limiter
        self._http = requests.Session()

        # Track active windows for tick sampling
        self._active_windows = {}  # asset -> {window_ts, open_price, ...}

    def start(self, price_feed, scanner, engine):
        """Start background tick sampling."""
        self._price_feed = price_feed
        self._scanner = scanner
        self._engine = engine
        self._running = True
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()
        logger.info("Data collector started")

    def stop(self):
        self._running = False
        self._flush()
        logger.info("Data collector stopped")

    # ─── Window lifecycle events ─────────────────────────────

    def record_window_open(self, asset: str, window_ts: int, market: dict,
                           binance_price: float):
        """Called when a new 5-min window starts for an asset."""
        self._active_windows[asset] = {
            "window_ts": window_ts,
            "open_price": binance_price,
        }
        self._append(self._windows_path, {
            "event": "window_open",
            "ts": time.time(),
            "asset": asset,
            "window_ts": window_ts,
            "slug": market.get("slug", ""),
            "binance_price": binance_price,
            "market_question": market.get("question", ""),
        })

    def record_direction_detected(self, asset: str, window_ts: int,
                                  direction: str, move_pct: float,
                                  binance_price: float, elapsed_sec: float):
        """Called when the direction threshold is crossed and a bet is about to fire."""
        self._append(self._windows_path, {
            "event": "direction_detected",
            "ts": time.time(),
            "asset": asset,
            "window_ts": window_ts,
            "direction": direction,
            "move_pct": move_pct,
            "binance_price": binance_price,
            "elapsed_sec": elapsed_sec,
        })

    def record_order(self, asset: str, window_ts: int, side: str,
                     direction: str, size_usdc: float, price_sent: float,
                     order_id: str | None, elapsed_sec: float,
                     order_type: str = "MAIN"):
        """Called after every order attempt (success or failure)."""
        self._append(self._orders_path, {
            "event": "order",
            "ts": time.time(),
            "asset": asset,
            "window_ts": window_ts,
            "side": side,
            "direction": direction,
            "size_usdc": size_usdc,
            "price_sent": price_sent,
            "order_id": order_id,
            "filled": order_id is not None,
            "elapsed_sec": elapsed_sec,
            "order_type": order_type,
        })

    def record_window_resolve(self, asset: str, window_ts: int,
                              result: dict, binance_open: float,
                              binance_close: float):
        """Called when a window resolves."""
        self._append(self._windows_path, {
            "event": "window_resolve",
            "ts": time.time(),
            "asset": asset,
            "window_ts": window_ts,
            "binance_open": binance_open,
            "binance_close": binance_close,
            "move_pct": ((binance_close - binance_open) / binance_open * 100)
                        if binance_open > 0 else 0,
            "actual_direction": result.get("direction", ""),
            "lean": result.get("lean", ""),
            "correct_lean": result.get("correct_lean", False),
            "total_cost": result.get("total_cost", 0),
            "payout": result.get("payout", 0),
            "net_pnl": result.get("net_pnl", 0),
            "early_exit": result.get("early_exit", False),
        })

    def record_book_snapshot(self, asset: str, window_ts: int,
                             direction: str, token_id: str,
                             book: dict, reason: str):
        """Record an order book snapshot at a specific moment."""
        self._append(self._books_path, {
            "ts": time.time(),
            "asset": asset,
            "window_ts": window_ts,
            "direction": direction,
            "token_id": token_id[:20],
            "best_bid": book.get("best_bid", 0),
            "best_ask": book.get("best_ask", 0),
            "spread": book.get("spread", 0),
            "reason": reason,
        })

    # ─── Background tick sampling ────────────────────────────

    def _tick_loop(self):
        """Sample Binance prices every TICK_SAMPLE_INTERVAL seconds."""
        while self._running:
            try:
                if self._price_feed:
                    now = time.time()
                    current_window = (int(now) // WINDOW_SEC) * WINDOW_SEC
                    window_elapsed = now - current_window

                    for asset in ["btc", "eth"]:
                        feed = self._price_feed.get(asset)
                        if feed and feed.price > 0:
                            self._append(self._ticks_path, {
                                "ts": now,
                                "asset": asset,
                                "window_ts": current_window,
                                "window_elapsed": round(window_elapsed, 2),
                                "price": feed.price,
                                "open_price": feed.open_price,
                                "move_pct": feed.move_pct(),
                                "direction": feed.direction(),
                            })

                    # Sample order books for active positions every 30s
                    # Skip during first 30s of window — that's the entry hot path
                    if (int(now) % 30 < TICK_SAMPLE_INTERVAL
                            and window_elapsed > 30
                            and self._engine):
                        for asset in ["btc", "eth"]:
                            if not self._engine.has_position(asset):
                                continue
                            pos = self._engine.positions[asset]
                            if pos.is_closed:
                                continue
                            wts = pos.market.get("window_ts", 0)
                            for side_obj in [pos.up, pos.down]:
                                if side_obj is None:
                                    continue
                                try:
                                    book = self._fetch_book(side_obj.token_id)
                                    if book:
                                        self.record_book_snapshot(
                                            asset, wts, side_obj.outcome,
                                            side_obj.token_id, book,
                                            "periodic_sample",
                                        )
                                except Exception:
                                    pass

                self._flush()
            except Exception as e:
                logger.error(f"Tick loop error: {e}")

            time.sleep(TICK_SAMPLE_INTERVAL)

    def _fetch_book(self, token_id: str) -> dict | None:
        """Fetch order book using our own HTTP session (not the executor's)."""
        try:
            r = self._http.get(
                f"{CLOB_HOST}/book",
                params={"token_id": token_id},
                timeout=5,
            )
            r.raise_for_status()
            book = r.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = max((float(b["price"]) for b in bids), default=0.0)
            best_ask = min((float(a["price"]) for a in asks), default=1.0)
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": best_ask - best_bid,
            }
        except Exception:
            return None

    # ─── Buffer management ───────────────────────────────────

    def _append(self, filepath: str, record: dict):
        with self._buffer_lock:
            self._buffer.append((filepath, record))

    def _flush(self):
        with self._buffer_lock:
            items = self._buffer[:]
            self._buffer.clear()

        # Group by file
        by_file = {}
        for filepath, record in items:
            by_file.setdefault(filepath, []).append(record)

        for filepath, records in by_file.items():
            try:
                with open(filepath, "a") as f:
                    for r in records:
                        f.write(json.dumps(r) + "\n")
            except Exception as e:
                logger.error(f"Flush error ({filepath}): {e}")
