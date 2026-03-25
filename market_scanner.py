"""
Market Scanner — Finds active BTC and ETH 5-min markets on Polymarket.
Returns token IDs, prices, and market metadata for both assets.
"""

import json
import time
import logging
import requests

from config import GAMMA_HOST, CLOB_HOST, WINDOW_SEC, SLUG_PATTERN, LOG_DIR

logger = logging.getLogger("scanner")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(f"{LOG_DIR}/scanner.log")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    logger.addHandler(sh)


class MarketScanner:
    """
    Finds current 5-minute BTC and ETH markets via slug prediction.

    Slug pattern: {asset}-updown-5m-{unix_timestamp}
    where timestamp is aligned to 5-minute boundaries.
    """

    def __init__(self):
        self._session = requests.Session()
        self._cache = {}   # slug -> market data

    def current_window(self):
        """Return the current 5-min window start timestamp."""
        now = int(time.time())
        return (now // WINDOW_SEC) * WINDOW_SEC

    def get_market(self, asset: str, ts: int = None):
        """
        Fetch market data for a given asset and window timestamp.

        Args:
            asset: "btc" or "eth"
            ts:    window start timestamp (defaults to current window)

        Returns:
            dict with keys: question, condition_id, tokens (list of
            {token_id, outcome}), neg_risk, end_ts, slug
            or None if not found / already closed.
        """
        if ts is None:
            ts = self.current_window()

        slug = SLUG_PATTERN.format(asset=asset, ts=ts)

        # Cache hit
        if slug in self._cache:
            cached = self._cache[slug]
            if not cached.get("closed"):
                return cached

        try:
            r = self._session.get(
                f"{GAMMA_HOST}/events",
                params={"slug": slug},
                timeout=10,
            )
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            logger.error(f"Gamma fetch error ({slug}): {e}")
            return None

        if not events:
            return None

        event = events[0]
        for mkt in event.get("markets", []):
            if mkt.get("closed"):
                continue

            # Parse token IDs and outcomes
            try:
                token_ids = json.loads(mkt["clobTokenIds"]) if isinstance(mkt["clobTokenIds"], str) else mkt["clobTokenIds"]
                outcomes  = json.loads(mkt["outcomes"])     if isinstance(mkt["outcomes"], str)     else mkt["outcomes"]
            except Exception:
                continue

            tokens = [
                {"token_id": tid, "outcome": o}
                for tid, o in zip(token_ids, outcomes)
            ]
            if not tokens:
                continue

            end_str = mkt.get("endDate") or mkt.get("end_date_iso", "")

            market = {
                "slug":         slug,
                "asset":        asset,
                "question":     mkt.get("question", event.get("title", "")),
                "condition_id": mkt.get("conditionId") or mkt.get("condition_id", ""),
                "tokens":       tokens,
                "neg_risk":     mkt.get("negRisk", False),
                "end_str":      end_str,
                "window_ts":    ts,
            }
            self._cache[slug] = market
            logger.info(f"📋 [{asset.upper()}] {market['question']}")
            return market

        return None

    def get_both_markets(self):
        """
        Fetch current BTC and ETH markets simultaneously.

        Returns:
            dict: {"btc": market_or_None, "eth": market_or_None}
        """
        ts = self.current_window()
        result = {}
        for asset in ["btc", "eth"]:
            mkt = self.get_market(asset, ts)
            if mkt is None:
                # Try next window (market might have started early)
                mkt = self.get_market(asset, ts + WINDOW_SEC)
            result[asset] = mkt
        return result

    def get_orderbook(self, token_id: str):
        """
        Fetch best bid/ask for a token from the CLOB.

        Returns:
            {"best_bid": float, "best_ask": float, "spread": float}
            or None on error.
        """
        try:
            r = self._session.get(
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
                "spread":   best_ask - best_bid,
            }
        except Exception as e:
            logger.warning(f"Orderbook fetch error ({token_id[:12]}): {e}")
            return None

    def get_token_prices(self, market: dict):
        """
        Return {outcome: {best_bid, best_ask, spread}} for all tokens.
        """
        prices = {}
        for token in market.get("tokens", []):
            book = self.get_orderbook(token["token_id"])
            if book:
                prices[token["outcome"].upper()] = book
        return prices

    def seconds_remaining(self, market: dict):
        """Calculate seconds left in a market."""
        end_str = market.get("end_str", "")
        if not end_str:
            return None
        try:
            from datetime import datetime, timezone
            # Handle both "Z" and "+00:00" suffixes
            end_str_clean = end_str.replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(end_str_clean)
            now = datetime.now(timezone.utc)
            return (end_dt - now).total_seconds()
        except Exception:
            return None
