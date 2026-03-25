"""
╔══════════════════════════════════════════════════════════════════════╗
║          HEDGE ENGINE — Both-Sides Position Manager                ║
║                                                                    ║
║  Reverse-engineered from @Hcrystallash's trading pattern:         ║
║  1. Open a small hedge on the CHEAP side immediately              ║
║  2. After 60s, load up on the direction BTC/ETH is actually moving║
║  3. Average in with small additional buys as conviction grows     ║
║  4. Exit early if position hits 70%+ unrealized profit            ║
║  5. Otherwise hold to expiry (binary resolution)                  ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import (
    HEDGE_SIZE_USDC, CHEAP_SIDE_MAX,
    MAIN_BET_SIZE_USDC, DIRECTION_THRESHOLD, MAIN_BET_DELAY_SEC,
    FAST_BET_SIZE_USDC, FOLLOWUP_LIMIT_PRICE, FOLLOWUP_LIMIT_SIZE_USDC,
    ADDON_SIZE_USDC, ADDON_DELAY_SEC, ADDON_THRESHOLD,
    MAX_TOTAL_USDC, MAX_ONE_SIDE_USDC,
    ENTRY_DEADLINE_SEC, PROFIT_EXIT_PCT,
    MIN_BET_USDC, DEFAULT_BANKROLL_USDC,
    FAST_LIMIT_PRICE,
    NEAR_MAX_EXIT_BID,
    LOG_DIR,
)

logger = logging.getLogger("hedge")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(f"{LOG_DIR}/hedge.log")
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

    def current_value(self, current_bid: float) -> float:
        return self.tokens * current_bid


@dataclass
class MarketPosition:
    """
    Tracks the full both-sides position for one market (one asset).

    Phases:
        WAIT    — market found, waiting for open
        HEDGE   — small hedge placed on cheap side, watching direction
        MAIN    — main directional bet placed, may add-on
        HOLD    — fully positioned, holding to expiry or profit exit
        CLOSED  — position resolved or exited
    """
    asset: str
    market: dict
    open_time: float = field(default_factory=time.time)

    up:   Optional[Side] = None
    down: Optional[Side] = None

    phase: str = "WAIT"          # WAIT → HEDGE → MAIN → HOLD → CLOSED
    lean: Optional[str] = None   # "UP" or "DOWN" — which direction we favor
    addon_done: bool = False
    early_exit: bool = False

    # Follow-up GTC limit order tracking
    followup_order_id: Optional[str] = None
    followup_token_id: Optional[str] = None
    followup_size_usdc: float = 0.0
    followup_price: float = 0.0

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

    def opposite_side(self, direction: str) -> Optional[Side]:
        if direction == "UP":
            return self.down
        if direction == "DOWN":
            return self.up
        return None


class HedgeEngine:
    """
    Manages both-sides positions across BTC and ETH markets.

    Call update() on each loop iteration — it decides what to buy/sell.
    """

    def __init__(self, executor, scanner, data_collector=None):
        self.executor = executor
        self.scanner = scanner
        self.data = data_collector
        self.positions: dict[str, MarketPosition] = {}  # asset -> position
        self.bankroll: float = DEFAULT_BANKROLL_USDC     # updated from live balance

        # Streak tracking — controls cooldown and win scaling
        self.consecutive_losses: int = 0    # reset on any win
        self.skip_next_window: bool = False # set after 2 losses, cleared after 1 skip
        self.boost_next_trade: bool = False # set after a win, cleared after 1 trade

    def report_result(self, net_pnl: float):
        """
        Call after each resolved window to update streak state.
        Drives 2-loss cooldown and win-boost logic.
        """
        if net_pnl > 0:
            self.consecutive_losses = 0
            self.skip_next_window = False
            self.boost_next_trade = True
            logger.info(f"  🔥 WIN — next trade boosted 50%")
        elif net_pnl < 0:
            self.consecutive_losses += 1
            self.boost_next_trade = False
            if self.consecutive_losses >= 2:
                self.skip_next_window = True
                logger.info(
                    f"  ⏸️  {self.consecutive_losses} consecutive losses — "
                    f"skipping next window"
                )
        # net_pnl == 0 (no bet) doesn't change streak

    def should_skip(self) -> bool:
        """Check if we should skip this window due to loss cooldown."""
        if self.skip_next_window:
            self.skip_next_window = False
            self.consecutive_losses = 0  # reset after serving the skip
            logger.info(f"  ⏭️  Skipping window (2-loss cooldown)")
            return True
        return False

    def get_bet_size(self) -> float:
        """
        Returns the current bet size, applying win-streak boost if active.
        Boost is 1.5x for one trade after a win, then resets.
        """
        base = FAST_BET_SIZE_USDC
        if self.boost_next_trade:
            self.boost_next_trade = False
            boosted = round(base * 1.5, 2)
            logger.info(f"  🔥 Boosted bet: ${base:.2f} → ${boosted:.2f}")
            return boosted
        return base

    def has_position(self, asset: str) -> bool:
        p = self.positions.get(asset)
        return p is not None and not p.is_closed

    def open_position(self, asset: str, market: dict):
        """Register a new market for an asset."""
        pos = MarketPosition(asset=asset, market=market)
        self.positions[asset] = pos
        logger.info(
            f"\n{'═'*60}\n"
            f"  📊 [{asset.upper()}] NEW MARKET: {market['question']}\n"
            f"{'═'*60}"
        )

    def update(self, asset: str, price_feed, remaining: float):
        """
        Main decision loop for one asset.

        Called every second. Decides whether to:
        - Place initial hedge
        - Place main directional bet
        - Add to winning side
        - Exit early for profit
        """
        pos = self.positions.get(asset)
        if pos is None or pos.is_closed:
            return

        feed = price_feed.get(asset)
        move_pct = feed.move_pct()
        direction = feed.direction()
        market = pos.market
        neg_risk = market.get("neg_risk", False)

        # ── Phase: WAIT → HEDGE (skip straight to watching) ───
        if pos.phase == "WAIT":
            if remaining is None or remaining > 300:
                return  # Market hasn't started yet
            # Skip hedge order book fetch — go straight to watching
            # for directional signal to fire the fast bet
            pos.phase = "HEDGE"
            logger.info(f"  ⚡ [{asset.upper()}] ARMED — waiting for first tick")

        # ── Phase: HEDGE → MAIN (FAST PATH) ────────────────────
        elif pos.phase == "HEDGE":
            if remaining is None or remaining < ENTRY_DEADLINE_SEC:
                pos.phase = "HOLD"
                return

            if pos.elapsed < MAIN_BET_DELAY_SEC:
                return

            if abs(move_pct) < DIRECTION_THRESHOLD:
                return  # No tick yet

            # FAST: direction detected → submit immediately at fixed limit
            # Skip order book fetch (~300ms) — use FAST_LIMIT_PRICE instead
            main_dir = "UP" if move_pct > 0 else "DOWN"
            pos.lean = main_dir

            main_side_obj = pos.side_for(main_dir)
            if main_side_obj is None:
                return

            # Record direction detection
            window_ts = market.get("window_ts", 0)
            if self.data:
                self.data.record_direction_detected(
                    asset, window_ts, main_dir, move_pct,
                    feed.price, pos.elapsed,
                )

            ask = FAST_LIMIT_PRICE
            bet_size = self.get_bet_size()
            budget = min(
                bet_size,
                MAX_ONE_SIDE_USDC - main_side_obj.spent,
                MAX_TOTAL_USDC - pos.total_spent,
            )
            if budget < MIN_BET_USDC:
                return

            order_id = self.executor.buy(
                token_id=main_side_obj.token_id,
                size_usdc=budget,
                price=ask,
                neg_risk=neg_risk,
            )

            # Record order attempt (success or failure)
            if self.data:
                self.data.record_order(
                    asset, window_ts, "BUY", main_dir,
                    budget, ask, order_id, pos.elapsed, "MAIN",
                )

            if order_id:
                main_side_obj.add_fill(budget, ask)
                pos.phase = "MAIN"
                logger.info(
                    f"  ⚡ [{asset.upper()}] FAST BET: {main_dir} "
                    f"${budget:.2f} @ ${ask:.2f} "
                    f"(move={move_pct:+.4f}%, {pos.elapsed:.1f}s in)"
                )

                # Place follow-up GTC limit at better price
                followup_budget = min(
                    FOLLOWUP_LIMIT_SIZE_USDC,
                    MAX_ONE_SIDE_USDC - main_side_obj.spent,
                    MAX_TOTAL_USDC - pos.total_spent,
                )
                if followup_budget >= MIN_BET_USDC:
                    followup_id = self.executor.buy_limit_gtc(
                        token_id=main_side_obj.token_id,
                        size_usdc=followup_budget,
                        price=FOLLOWUP_LIMIT_PRICE,
                        neg_risk=neg_risk,
                    )
                    if self.data:
                        self.data.record_order(
                            asset, window_ts, "BUY", main_dir,
                            followup_budget, FOLLOWUP_LIMIT_PRICE,
                            followup_id, pos.elapsed, "FOLLOWUP_GTC",
                        )
                    if followup_id:
                        pos.followup_order_id = followup_id
                        pos.followup_token_id = main_side_obj.token_id
                        pos.followup_size_usdc = followup_budget
                        pos.followup_price = FOLLOWUP_LIMIT_PRICE
                        logger.info(
                            f"  📋 [{asset.upper()}] FOLLOWUP GTC: {main_dir} "
                            f"${followup_budget:.2f} @ ${FOLLOWUP_LIMIT_PRICE:.2f}"
                        )

        # ── Phase: MAIN → ADD-ON ──────────────────────────────
        elif pos.phase == "MAIN":
            if remaining is None or remaining < ENTRY_DEADLINE_SEC:
                pos.phase = "HOLD"
                return

            # Check for near-max exit first (bid >= $0.97)
            if self._check_near_max_exit(pos, neg_risk):
                return

            # Check for early profit exit first
            if self._check_profit_exit(pos, neg_risk):
                return

            # Add-on: double down if direction is very clear
            if (not pos.addon_done
                    and pos.elapsed >= ADDON_DELAY_SEC
                    and abs(move_pct) >= ADDON_THRESHOLD
                    and pos.lean is not None):

                main_side_obj = pos.side_for(pos.lean)
                if main_side_obj is None:
                    pos.phase = "HOLD"
                    return

                prices_addon = self.scanner.get_token_prices(market)
                p_addon = prices_addon.get(pos.lean, {})
                ask_addon = p_addon.get("best_ask", 0)
                budget = min(
                    ADDON_SIZE_USDC,
                    MAX_ONE_SIDE_USDC - main_side_obj.spent,
                    MAX_TOTAL_USDC - pos.total_spent,
                )
                if budget >= MIN_BET_USDC:
                    ask = ask_addon

                    if 0 < ask < 0.90:
                        order_id = self.executor.buy(
                            token_id=main_side_obj.token_id,
                            size_usdc=budget,
                            price=ask,
                            neg_risk=neg_risk,
                        )
                        if self.data:
                            self.data.record_order(
                                asset, market.get("window_ts", 0),
                                "BUY", pos.lean, budget, ask,
                                order_id, pos.elapsed, "ADDON",
                            )
                        if order_id:
                            main_side_obj.add_fill(budget, ask)
                            pos.addon_done = True
                            logger.info(
                                f"  ➕ [{asset.upper()}] ADD-ON: {pos.lean} "
                                f"${budget:.2f} @ ${ask:.2f} "
                                f"(move={move_pct:+.3f}%)"
                            )

            # Transition to HOLD after add-on window
            if pos.elapsed >= ADDON_DELAY_SEC + 30:
                pos.phase = "HOLD"

        # ── Phase: HOLD ───────────────────────────────────────
        elif pos.phase == "HOLD":
            if self._check_near_max_exit(pos, neg_risk):
                return
            self._check_profit_exit(pos, neg_risk)

    def resolve(self, asset: str, asset_direction: str):
        """
        Resolve position when market ends.

        Args:
            asset:           "btc" or "eth"
            asset_direction: "UP" or "DOWN" — how asset actually moved
        """
        pos = self.positions.get(asset)
        if pos is None or pos.is_closed:
            return None

        # Handle follow-up GTC order: check fill status, cancel if unfilled
        self._settle_followup(pos)

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
            f"\n  {emoji} [{asset.upper()}] RESOLVED: BTC/ETH went {asset_direction}\n"
            f"  Lean was: {pos.lean or 'none'} "
            f"({'correct' if result['correct_lean'] else 'wrong'})\n"
            f"  Up spent: ${up_s:.2f} | Down spent: ${down_s:.2f}\n"
            f"  Payout: ${payout:.2f} | Net PnL: ${net_pnl:+.2f}\n"
        )

        pos.phase = "CLOSED"
        return result

    # ─────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────

    def _settle_followup(self, pos: MarketPosition):
        """
        Check if the follow-up GTC limit order filled.
        If filled, credit the tokens to the position.
        Always cancel whatever remains.
        """
        if not pos.followup_order_id:
            return

        order_id = pos.followup_order_id
        side_obj = pos.side_for(pos.lean) if pos.lean else None

        # Check fill status
        status = self.executor.get_order_status(order_id)
        filled_size = 0.0
        if status:
            # Polymarket returns size_matched as string of USDC filled
            try:
                filled_size = float(status.get("size_matched", 0))
            except (ValueError, TypeError):
                filled_size = 0.0

        if filled_size > 0 and side_obj:
            # size_matched is in tokens; cost = tokens * price
            fill_cost = filled_size * pos.followup_price
            side_obj.add_fill(fill_cost, pos.followup_price)
            logger.info(
                f"  📋 [{pos.asset.upper()}] FOLLOWUP FILLED: "
                f"{filled_size:.2f} tokens @ ${pos.followup_price:.2f} "
                f"(${fill_cost:.2f})"
            )

        # Cancel any unfilled remainder
        self.executor.cancel_order(order_id)
        pos.followup_order_id = None

    def _find_cheap_side(self, prices: dict):
        """Return (direction, ask_price) for the cheaper side, or (None, None)."""
        best_dir, best_price = None, 1.0
        for direction, p in prices.items():
            ask = p.get("best_ask", 1.0)
            if ask < best_price:
                best_price = ask
                best_dir = direction
        return best_dir, best_price if best_dir else None

    def _check_near_max_exit(self, pos: MarketPosition, neg_risk: bool) -> bool:
        """
        If any side's bid >= NEAR_MAX_EXIT_BID ($0.97), sell immediately.
        Captures near-certain wins early to free up capital for the next window.
        """
        if pos.is_closed:
            return False

        for side_obj in [pos.up, pos.down]:
            if side_obj is None or side_obj.tokens <= 0:
                continue

            book = self.executor.get_orderbook(side_obj.token_id)
            if not book:
                continue

            current_bid = book["best_bid"]
            if current_bid >= NEAR_MAX_EXIT_BID:
                # Cancel followup before exiting
                self._settle_followup(pos)
                sell_value = side_obj.tokens * current_bid
                logger.info(
                    f"\n  🏆 [{pos.asset.upper()}] NEAR-MAX EXIT: "
                    f"{side_obj.outcome} bid=${current_bid:.2f}\n"
                    f"  Selling {side_obj.tokens:.4f} tokens @ ${current_bid:.2f} "
                    f"→ ${sell_value:.2f} (entry avg ${side_obj.avg_price:.2f})"
                )
                order_id = self.executor.sell(
                    token_id=side_obj.token_id,
                    qty_tokens=side_obj.tokens,
                    price=current_bid,
                    neg_risk=neg_risk,
                )
                if self.data:
                    self.data.record_order(
                        pos.asset, pos.market.get("window_ts", 0),
                        "SELL", side_obj.outcome,
                        sell_value, current_bid,
                        order_id, pos.elapsed, "NEAR_MAX_EXIT",
                    )
                    self.data.record_book_snapshot(
                        pos.asset, pos.market.get("window_ts", 0),
                        side_obj.outcome, side_obj.token_id,
                        book, "near_max_exit",
                    )
                if order_id:
                    pos.early_exit = True
                    pos.phase = "CLOSED"
                    return True

        return False

    def _check_profit_exit(self, pos: MarketPosition, neg_risk: bool) -> bool:
        """
        If either side has 70%+ unrealized profit, sell it back.
        Returns True if an exit order was placed.
        """
        for side_obj in [pos.up, pos.down]:
            if side_obj is None or side_obj.tokens <= 0:
                continue

            book = self.executor.get_orderbook(side_obj.token_id)
            if not book:
                continue

            current_bid = book["best_bid"]
            if current_bid <= 0:
                continue

            unreal_pct = side_obj.unrealized_pct(current_bid)

            if unreal_pct >= PROFIT_EXIT_PCT:
                # Cancel followup before exiting
                self._settle_followup(pos)
                sell_value = side_obj.tokens * current_bid
                logger.info(
                    f"\n  💰 [{pos.asset.upper()}] PROFIT EXIT: "
                    f"{side_obj.outcome} {unreal_pct:+.1%}\n"
                    f"  Selling {side_obj.tokens:.4f} tokens @ ${current_bid:.2f} "
                    f"→ ${sell_value:.2f} (entry avg ${side_obj.avg_price:.2f})"
                )
                order_id = self.executor.sell(
                    token_id=side_obj.token_id,
                    qty_tokens=side_obj.tokens,
                    price=current_bid,
                    neg_risk=neg_risk,
                )
                if order_id:
                    pos.early_exit = True
                    pos.phase = "CLOSED"
                    return True

        return False

    def summary(self, asset: str) -> str:
        pos = self.positions.get(asset)
        if pos is None:
            return f"[{asset.upper()}] No position"
        up_s   = pos.up.spent   if pos.up   else 0
        down_s = pos.down.spent if pos.down else 0
        return (
            f"[{asset.upper()}] {pos.phase} | "
            f"UP ${up_s:.2f} | DOWN ${down_s:.2f} | "
            f"Total ${pos.total_spent:.2f} | "
            f"Lean: {pos.lean or '—'}"
        )
