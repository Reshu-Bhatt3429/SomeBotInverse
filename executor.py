"""
Executor — Order placement on Polymarket CLOB.
Handles live and dry-run modes.
"""

import time
import logging
import threading

from config import (
    POLYMARKET_PRIVATE_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE, POLYMARKET_FUNDER_ADDRESS,
    POLYMARKET_SIGNATURE_TYPE, CHAIN_ID, CLOB_HOST, LIVE_TRADING,
    TICK_SIZE, API_RATE_LIMIT, LOG_DIR,
)

logger = logging.getLogger("executor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(f"{LOG_DIR}/executor.log")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    logger.addHandler(sh)


class Executor:
    """
    Places BUY / SELL orders on the Polymarket CLOB.

    In dry-run mode (LIVE_TRADING=false) all orders are simulated.
    """

    def __init__(self):
        self._client = None
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._min_interval = 1.0 / API_RATE_LIMIT

    def setup(self) -> bool:
        if not LIVE_TRADING:
            logger.info("🔒 DRY RUN — no real orders will be placed")
            return True

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=POLYMARKET_API_KEY,
                api_secret=POLYMARKET_API_SECRET,
                api_passphrase=POLYMARKET_API_PASSPHRASE,
            )
            self._client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=POLYMARKET_PRIVATE_KEY,
                creds=creds,
                funder=POLYMARKET_FUNDER_ADDRESS or None,
                signature_type=POLYMARKET_SIGNATURE_TYPE,
            )
            logger.info("✅ CLOB client initialized")
            return True
        except ImportError:
            logger.error("⚠️  py-clob-client not installed")
            return False
        except Exception as e:
            logger.error(f"⚠️  CLOB setup failed: {e}")
            return False

    def _rate_limit(self):
        now = time.time()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def buy(self, token_id: str, size_usdc: float, price: float,
            neg_risk: bool = False) -> str | None:
        """
        Place a BUY limit order.

        Args:
            token_id:  CLOB token ID
            size_usdc: USDC amount to spend
            price:     limit price (0.01 – 0.99)
            neg_risk:  neg_risk flag for the market

        Returns:
            order_id string or None on failure.
        """
        return self._place("BUY", token_id, size_usdc, price, neg_risk)

    def sell(self, token_id: str, qty_tokens: float, price: float,
             neg_risk: bool = False) -> str | None:
        """
        Place a SELL limit order.

        Args:
            token_id:   CLOB token ID
            qty_tokens: Number of tokens to sell
            price:      limit price
            neg_risk:   neg_risk flag for the market

        Returns:
            order_id string or None on failure.
        """
        # Convert qty to usdc-equivalent for unified interface
        size_usdc = qty_tokens * price
        return self._place("SELL", token_id, size_usdc, price, neg_risk)

    def _place(self, side: str, token_id: str, size_usdc: float,
               price: float, neg_risk: bool) -> str | None:
        if price <= 0 or price >= 1.0:
            logger.error(f"Invalid price {price} for {side}")
            return None

        qty = size_usdc / price
        # Polymarket requires minimum 5 tokens per order
        if qty < 5.0:
            qty = 5.0
            size_usdc = qty * price

        if not LIVE_TRADING:
            order_id = f"DRY_{side}_{int(time.time()*1000)}"
            logger.info(
                f"🔒 [DRY] {side} {qty:.3f} tokens @ ${price:.2f} "
                f"= ${size_usdc:.2f} | {token_id[:16]}..."
            )
            return order_id

        try:
            from py_clob_client.clob_types import (
                MarketOrderArgs, PartialCreateOrderOptions, OrderType,
            )

            # Minimal locking — sign + post as fast as possible
            self._rate_limit()
            args = MarketOrderArgs(
                token_id=token_id,
                amount=size_usdc,
                side=side,
            )
            opts = PartialCreateOrderOptions(
                tick_size=TICK_SIZE,
                neg_risk=neg_risk,
            )
            signed = self._client.create_market_order(args, options=opts)
            resp = self._client.post_order(signed, orderType=OrderType.FOK)

            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id", "")
            elif hasattr(resp, "orderID"):
                order_id = resp.orderID
            else:
                order_id = str(resp) if resp else None

            if order_id:
                logger.info(
                    f"✅ MARKET {side} ${size_usdc:.2f} | ID: {order_id[:20]}..."
                )
                return order_id
            else:
                logger.error(f"⚠️  No order ID in response: {resp}")
                return None

        except Exception as e:
            logger.error(f"⚠️  {side} order failed: {e}")
            return None

    def buy_limit_gtc(self, token_id: str, size_usdc: float, price: float,
                      neg_risk: bool = False) -> str | None:
        """
        Place a resting GTC limit BUY order at a better price.
        Unlike buy() which uses FOK market orders, this sits on the book.

        Returns order_id or None.
        """
        if price <= 0 or price >= 1.0:
            logger.error(f"Invalid GTC price {price}")
            return None

        qty = size_usdc / price
        if qty < 5.0:
            qty = 5.0
            size_usdc = qty * price

        if not LIVE_TRADING:
            order_id = f"DRY_GTC_BUY_{int(time.time()*1000)}"
            logger.info(
                f"🔒 [DRY] GTC BUY {qty:.3f} tokens @ ${price:.2f} "
                f"= ${size_usdc:.2f} | {token_id[:16]}..."
            )
            return order_id

        try:
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType

            with self._lock:
                self._rate_limit()
                args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=qty,
                    side="BUY",
                )
                opts = PartialCreateOrderOptions(
                    tick_size=TICK_SIZE,
                    neg_risk=neg_risk,
                )
                signed = self._client.create_order(args, options=opts)
                resp = self._client.post_order(signed, orderType=OrderType.GTC)

            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id", "")
            elif hasattr(resp, "orderID"):
                order_id = resp.orderID
            else:
                order_id = str(resp) if resp else None

            if order_id:
                logger.info(
                    f"📋 GTC BUY ${size_usdc:.2f} @ ${price:.2f} | ID: {order_id[:20]}..."
                )
                return order_id
            else:
                logger.error(f"⚠️  No order ID in GTC response: {resp}")
                return None

        except Exception as e:
            logger.error(f"⚠️  GTC BUY order failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order. Returns True on success."""
        if not order_id:
            return False

        if not LIVE_TRADING:
            logger.info(f"🔒 [DRY] Cancel {order_id}")
            return True

        try:
            with self._lock:
                self._rate_limit()
                self._client.cancel(order_id)
            logger.info(f"🗑️  Cancelled order {order_id[:20]}...")
            return True
        except Exception as e:
            logger.warning(f"⚠️  Cancel failed ({order_id[:20]}): {e}")
            return False

    def get_order_status(self, order_id: str) -> dict | None:
        """Fetch order status to check if a GTC order has filled."""
        if not order_id:
            return None

        if not LIVE_TRADING:
            return {"status": "MATCHED", "size_matched": "0"}

        try:
            with self._lock:
                self._rate_limit()
                return self._client.get_order(order_id)
        except Exception as e:
            logger.debug(f"Order status error ({order_id[:12]}): {e}")
            return None

    def get_balance(self) -> float:
        """
        Fetch available USDC collateral balance from Polymarket.

        Uses get_balance_allowance() with AssetType.COLLATERAL.
        Balance is returned in raw units (6 decimals) → divided by 1e6.
        Returns DEFAULT_BANKROLL_USDC on failure.
        """
        from config import DEFAULT_BANKROLL_USDC, POLYMARKET_SIGNATURE_TYPE

        if not LIVE_TRADING or self._client is None:
            logger.info(f"🔒 [DRY] Balance: ${DEFAULT_BANKROLL_USDC:.2f} (simulated)")
            return DEFAULT_BANKROLL_USDC

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            self._rate_limit()
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=POLYMARKET_SIGNATURE_TYPE,
            )
            result = self._client.get_balance_allowance(params)
            # Raw balance is in 6-decimal USDC units
            raw = int(result.get("balance", 0))
            balance = raw / 1_000_000
            logger.info(f"💵 Account balance: ${balance:.2f} USDC")
            return balance
        except Exception as e:
            logger.warning(f"⚠️  Balance fetch failed ({e}) — using default ${DEFAULT_BANKROLL_USDC:.2f}")
            return DEFAULT_BANKROLL_USDC

    def get_orderbook(self, token_id: str) -> dict | None:
        """Fetch best bid/ask from CLOB."""
        try:
            if not LIVE_TRADING or self._client is None:
                return {"best_bid": 0.50, "best_ask": 0.52, "spread": 0.02}

            self._rate_limit()
            book = self._client.get_order_book(token_id)
            bids = book.bids if hasattr(book, "bids") else []
            asks = book.asks if hasattr(book, "asks") else []
            best_bid = max((float(b.price) for b in bids), default=0.0)
            best_ask = min((float(a.price) for a in asks), default=1.0)
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread":   best_ask - best_bid,
            }
        except Exception as e:
            logger.debug(f"Orderbook error ({token_id[:12]}): {e}")
            return None
