"""
╔══════════════════════════════════════════════════════════════════════╗
║          TICK ENGINE — Opposite-of-First-Tick Strategy              ║
║                                                                    ║
║  1. Wait for 5-min candle to open                                  ║
║  2. Observe first significant price move from open                 ║
║  3. Bet in the OPPOSITE direction                                  ║
║  4. Hold until end of window                                       ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import (
    BET_SIZE_USDC, DIRECTION_THRESHOLD, 
    MAX_TOTAL_USDC, MAX_ONE_SIDE_USDC,
    ENTRY_DEADLINE_SEC, PROFIT_EXIT_PCT,
    MIN_BET_USDC, DEFAULT_BANKROLL_USDC,
    FAST_LIMIT_PRICE,
    NEAR_MAX_EXIT_BID,
    LOG_DIR,
)

logger = logging.getLogger("engine")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(f"{LOG_DIR}/engine.log")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    logger.addHandler(sh)


@dataclass
class Side:
    """Tracks a single-direction (UP or DOWN) token position."""
    token_id: str
    outcome: str        # "UP" or "DOWN"
    tokens: float = 0.0
    spent: float = 0.0

    @property
    def avg_price(self) -> float:
        return self.spent / self.tokens if self.tokens > 0 else 0.0

    def add_fill(self, usdc: float, price: float):
        qty = usdc / price
        self.tokens += qty
        self.spent += usdc

    def unrealized_pct(self, current_bid: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (current_bid - self.avg_price) / self.avg_price


@dataclass
class MarketPosition:
    """Tracks the position for one market."""
    asset: str
    market: dict
    open_time: float = field(default_factory=time.time)

    up:   Optional[Side] = None
    down: Optional[Side] = None

    phase: str = "WAIT"          # WAIT → TICK → HOLD → CLOSED
    lean: Optional[str] = None   # "UP" or "DOWN" — our bet direction
    early_exit: bool = False

    def __post_init__(self):
        tokens = self.market.get("tokens", [])
        for t in tokens:
            o = t["outcome"].upper()
            if o in ("UP", "YES", "HIGHER"):
                self.up = Side(token_id=t["token_id"], outcome="UP")
            elif o in ("DOWN", "NO", "LOWER"):
                self.down = Side(token_id=t["token_id"], outcome="DOWN")

    @property
    def elapsed(self) -> float:
        return time.time() - self.open_time

    @property
    def total_spent(self) -> float:
        return (self.up.spent if self.up else 0) + (self.down.spent if self.down else 0)

    @property
    def is_closed(self) -> bool:
        return self.phase == "CLOSED"

    def side_for(self, direction: str) -> Optional[Side]:
        if direction == "UP":
            return self.up
        if direction == "DOWN":
            return self.down
        return None


class TickEngine:
    """Manages opposite-of-first-tick positions."""

    def __init__(self, executor, scanner, data_collector=None):
        self.executor = executor
        self.scanner = scanner
        self.data = data_collector
        self.positions: dict[str, MarketPosition] = {}
        self.bankroll: float = DEFAULT_BANKROLL_USDC

    def has_position(self, asset: str) -> bool:
        p = self.positions.get(asset)
        return p is not None and not p.is_closed

    def open_position(self, asset: str, market: dict):
        pos = MarketPosition(asset=asset, market=market)
        self.positions[asset] = pos
        logger.info(f"  📊 [{asset.upper()}] NEW MARKET: {market['question']}")

    def update(self, asset: str, price_feed, remaining: float):
        pos = self.positions.get(asset)
        if pos is None or pos.is_closed:
            return

        feed = price_feed.get(asset)
        move_pct = feed.move_pct()
        market = pos.market
        neg_risk = market.get("neg_risk", False)

        # ── Phase: WAIT → TICK ────────────────────────────────
        if pos.phase == "WAIT":
            if remaining is None or remaining > 300:
                return  
            pos.phase = "TICK"
            logger.info(f"  ⚡ [{asset.upper()}] WATCHING — waiting for first move")

        # ── Phase: TICK → HOLD (FAST BET) ──────────────────────
        elif pos.phase == "TICK":
            if remaining is None or remaining < ENTRY_DEADLINE_SEC:
                pos.phase = "HOLD"
                return

            if abs(move_pct) < DIRECTION_THRESHOLD:
                return  # No significant tick yet

            # OPPOSITE LOGIC:
            # If move is UP (>0), bet DOWN.
            # If move is DOWN (<0), bet UP.
            tick_dir = "UP" if move_pct > 0 else "DOWN"
            bet_dir = "DOWN" if tick_dir == "UP" else "UP"
            
            pos.lean = bet_dir
            side_obj = pos.side_for(bet_dir)
            
            if side_obj is None:
                pos.phase = "HOLD"
                return

            # Record direction detected
            window_ts = market.get("window_ts", 0)
            if self.data:
                self.data.record_direction_detected(
                    asset, window_ts, tick_dir, move_pct,
                    feed.price, pos.elapsed,
                )

            ask = FAST_LIMIT_PRICE
            budget = min(
                BET_SIZE_USDC,
                MAX_ONE_SIDE_USDC - side_obj.spent,
                MAX_TOTAL_USDC - pos.total_spent,
            )
            
            if budget < MIN_BET_USDC:
                pos.phase = "HOLD"
                return

            logger.info(
                f"  🔥 [{asset.upper()}] First tick was {tick_dir} ({move_pct:+.4f}%). "
                f"Betting OPPOSITE: {bet_dir} ${budget:.2f} @ ${ask:.2f}"
            )

            order_id = self.executor.buy(
                token_id=side_obj.token_id,
                size_usdc=budget,
                price=ask,
                neg_risk=neg_risk,
            )

            if order_id:
                side_obj.add_fill(budget, ask)
                pos.phase = "HOLD"
                if self.data:
                    self.data.record_order(
                        asset, window_ts, "BUY", bet_dir,
                        budget, ask, order_id, pos.elapsed, "MAIN",
                    )
            else:
                logger.error(f"  ❌ [{asset.upper()}] Order failed for {bet_dir}")

        # ── Phase: HOLD ───────────────────────────────────────
        elif pos.phase == "HOLD":
            # Optional: check for near-max profit exit
            self._check_near_max_exit(pos, neg_risk)
            self._check_profit_exit(pos, neg_risk)

    def resolve(self, asset: str, asset_direction: str):
        pos = self.positions.get(asset)
        if pos is None or pos.is_closed:
            return None

        up_s   = pos.up.spent   if pos.up   else 0
        down_s = pos.down.spent if pos.down else 0

        if asset_direction == "UP":
            winning_tokens = pos.up.tokens if pos.up else 0
            payout = winning_tokens * 1.0
        elif asset_direction == "DOWN":
            winning_tokens = pos.down.tokens if pos.down else 0
            payout = winning_tokens * 1.0
        else:
            payout = 0.0

        total_cost = pos.total_spent
        net_pnl = payout - total_cost

        result = {
            "asset":      asset,
            "direction":  asset_direction,
            "lean":       pos.lean,
            "up_spent":   up_s,
            "down_spent": down_s,
            "total_cost": total_cost,
            "payout":     payout,
            "net_pnl":    net_pnl,
            "correct_lean": pos.lean == asset_direction,
        }

        emoji = "✅" if net_pnl > 0 else "❌"
        logger.info(
            f"\n  {emoji} [{asset.upper()}] RESOLVED: Binance went {asset_direction}\n"
            f"  Bet was: {pos.lean or 'none'} "
            f"({'correct' if result['correct_lean'] else 'wrong'})\n"
            f"  Payout: ${payout:.2f} | Net PnL: ${net_pnl:+.2f}\n"
        )

        pos.phase = "CLOSED"
        return result

    def _check_near_max_exit(self, pos: MarketPosition, neg_risk: bool):
        if pos.is_closed: return
        for side_obj in [pos.up, pos.down]:
            if side_obj is None or side_obj.tokens <= 0: continue
            book = self.executor.get_orderbook(side_obj.token_id)
            if not book: continue
            if book["best_bid"] >= NEAR_MAX_EXIT_BID:
                sell_val = side_obj.tokens * book["best_bid"]
                logger.info(f"  🏆 [{pos.asset.upper()}] NEAR-MAX EXIT: {side_obj.outcome} @ {book['best_bid']}")
                self.executor.sell(side_obj.token_id, side_obj.tokens, book["best_bid"], neg_risk)
                pos.phase = "CLOSED"

    def _check_profit_exit(self, pos: MarketPosition, neg_risk: bool):
        if pos.is_closed: return
        for side_obj in [pos.up, pos.down]:
            if side_obj is None or side_obj.tokens <= 0: continue
            book = self.executor.get_orderbook(side_obj.token_id)
            if not book: continue
            if side_obj.unrealized_pct(book["best_bid"]) >= PROFIT_EXIT_PCT:
                logger.info(f"  💰 [{pos.asset.upper()}] PROFIT EXIT: {side_obj.outcome}")
                self.executor.sell(side_obj.token_id, side_obj.tokens, book["best_bid"], neg_risk)
                pos.phase = "CLOSED"

    def summary(self, asset: str) -> str:
        pos = self.positions.get(asset)
        if pos is None: return f"[{asset.upper()}] No position"
        return (
            f"[{asset.upper()}] {pos.phase} | "
            f"Spent ${pos.total_spent:.2f} | "
            f"Bet: {pos.lean or '—'}"
        )
