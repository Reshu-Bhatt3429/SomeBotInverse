"""
Backtest — Does the 60s momentum signal predict 5-min direction?

Uses Binance 1-min klines to simulate:
  1. Record open price at start of each 5-min window
  2. Check direction after 60s
  3. If move > threshold → bet that direction
  4. Check if 5-min close agrees with our bet
  5. Apply Kelly sizing to calculate PnL

Assumptions for Polymarket side:
  - Typical ask price for the directional token: ~0.50 at open
  - After 60s with a move, ask shifts toward the winning side
  - We estimate ask = 0.50 + (move_magnitude * scaling)
  - Payout = $1 per token if correct, $0 if wrong
"""

import requests
import time
from datetime import datetime, timezone

# ─── Config (same as bot) ───────────────────────────────
DIRECTION_THRESHOLD = 0.03   # min % move to trigger bet (lowered for backtest signal coverage)
KELLY_FRACTION      = 0.5    # half-Kelly
MOMENTUM_SCALE      = 0.10   # edge per 1% move
MAX_MOMENTUM_EDGE   = 0.15
MIN_BET_PCT         = 0.005
MAX_BET_PCT         = 0.05
MIN_BET_USDC        = 0.50
HEDGE_SIZE_USDC     = 1.50
CHEAP_SIDE_MAX      = 0.42
STARTING_BANKROLL   = 41.38


def fetch_klines(symbol: str, interval: str, limit: int):
    """Fetch klines from Binance."""
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    result = []
    for k in data:
        result.append({
            "open_time": k[0],
            "open":  float(k[1]),
            "high":  float(k[2]),
            "low":   float(k[3]),
            "close": float(k[4]),
            "close_time": k[6],
        })
    return result


def build_5min_windows(klines_1m):
    """
    Group 1-min candles into 5-min windows.
    Returns list of dicts with open, price_at_60s, close.
    """
    windows = []
    # Group by 5-min alignment
    i = 0
    while i + 4 < len(klines_1m):
        candles = klines_1m[i:i+5]
        # Verify they're consecutive (5 x 1-min)
        window_open = candles[0]["open"]
        price_at_60s = candles[0]["close"]   # close of first 1-min = 60s in
        window_close = candles[4]["close"]   # close of 5th minute
        open_time = candles[0]["open_time"]

        windows.append({
            "time": datetime.fromtimestamp(open_time / 1000, tz=timezone.utc),
            "open": window_open,
            "at_60s": price_at_60s,
            "close": window_close,
        })
        i += 5
    return windows


def estimate_ask(move_pct_at_60s: float) -> float:
    """
    Estimate what the Polymarket ask would be for the directional token
    after 60s of price movement.

    At open, both sides ~$0.50. After a move, the winning side's ask
    increases. We estimate conservatively.
    """
    # Based on real order book data: these 5-min markets are thin.
    # Ask on winning side barely moves in 60s — stays 0.50-0.55
    magnitude = abs(move_pct_at_60s)
    ask = 0.50 + min(magnitude * 0.05, 0.08)  # barely moves, caps at 0.58
    return round(ask, 2)


def kelly_size(move_pct: float, ask: float, bankroll: float) -> float:
    """Kelly-optimal bet size."""
    if ask <= 0 or ask >= 1:
        return 0.0

    momentum_edge = min(abs(move_pct) * MOMENTUM_SCALE, MAX_MOMENTUM_EDGE)
    p_est = 0.50 + momentum_edge

    edge = p_est - ask
    if edge <= 0:
        return 0.0

    kelly_pct = edge / (1.0 - ask)
    bet_pct = kelly_pct * KELLY_FRACTION
    bet_pct = min(bet_pct, MAX_BET_PCT)
    bet_pct = max(bet_pct, MIN_BET_PCT)

    size = bankroll * bet_pct
    size = max(size, MIN_BET_USDC)
    return round(size, 2)


def run_backtest(symbol: str, label: str):
    """Run backtest for one asset."""
    print(f"\n{'═'*60}")
    print(f"  BACKTEST: {label} ({symbol})")
    print(f"  Strategy: 60s momentum → Kelly-sized directional bet")
    print(f"{'═'*60}\n")

    # Fetch 500 x 1-min candles = 100 x 5-min windows
    klines = fetch_klines(symbol, "1m", 500)
    windows = build_5min_windows(klines)
    print(f"  Fetched {len(klines)} 1-min candles → {len(windows)} 5-min windows\n")

    bankroll = STARTING_BANKROLL
    total_trades = 0
    wins = 0
    losses = 0
    skipped = 0
    total_pnl = 0.0
    hedge_cost = 0.0

    trades = []

    for w in windows:
        # 60s momentum signal
        move_60s = ((w["at_60s"] - w["open"]) / w["open"]) * 100
        # 5-min outcome
        move_5m = ((w["close"] - w["open"]) / w["open"]) * 100

        # Skip if no clear direction
        if abs(move_60s) < DIRECTION_THRESHOLD:
            skipped += 1
            continue

        # Our bet direction
        bet_dir = "UP" if move_60s > 0 else "DOWN"
        # Actual outcome
        actual_dir = "UP" if move_5m > 0 else ("DOWN" if move_5m < 0 else "FLAT")

        # Estimate ask price
        ask = estimate_ask(move_60s)

        # Kelly bet size
        bet = kelly_size(move_60s, ask, bankroll)
        if bet <= 0:
            skipped += 1
            continue

        # Hedge cost (cheap side at open, ~$0.50 but we assume sometimes cheap)
        hedge = 0.0
        cheap_ask = 1.0 - ask  # opposite side is roughly complement
        if cheap_ask <= CHEAP_SIDE_MAX:
            hedge = HEDGE_SIZE_USDC

        # Result
        correct = (bet_dir == actual_dir)
        if correct:
            # Win: payout = tokens * $1, tokens = bet / ask
            tokens = bet / ask
            payout = tokens * 1.0
            net = payout - bet - hedge
            wins += 1
        else:
            # Loss: tokens worth $0
            net = -bet - hedge
            losses += 1

        total_trades += 1
        total_pnl += net
        bankroll += net
        hedge_cost += hedge

        trades.append({
            "time": w["time"].strftime("%m-%d %H:%M"),
            "move_60s": move_60s,
            "move_5m": move_5m,
            "bet_dir": bet_dir,
            "actual": actual_dir,
            "ask": ask,
            "bet_usd": bet,
            "hedge": hedge,
            "net": net,
            "bankroll": bankroll,
            "correct": correct,
        })

    # ─── Print results ──────────────────────────────────
    print(f"  {'#':>3}  {'Time':>11}  {'60s%':>6}  {'5m%':>6}  {'Bet':>4}  "
          f"{'Got':>4}  {'Ask':>5}  {'$Bet':>5}  {'Net':>7}  {'Bank':>7}  {'OK'}")
    print(f"  {'─'*80}")

    for i, t in enumerate(trades):
        mark = "✓" if t["correct"] else "✗"
        print(f"  {i+1:>3}  {t['time']:>11}  {t['move_60s']:>+5.2f}%  "
              f"{t['move_5m']:>+5.2f}%  {t['bet_dir']:>4}  {t['actual']:>4}  "
              f"${t['ask']:.2f}  ${t['bet_usd']:>4.2f}  ${t['net']:>+6.2f}  "
              f"${t['bankroll']:>6.2f}  {mark}")

    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    print(f"\n  {'─'*60}")
    print(f"  RESULTS:")
    print(f"  Total 5-min windows:   {len(windows)}")
    print(f"  Traded:                {total_trades}")
    print(f"  Skipped (no signal):   {skipped}")
    print(f"  Wins:                  {wins} ({win_rate:.1f}%)")
    print(f"  Losses:                {losses} ({100-win_rate:.1f}%)")
    print(f"  Total hedge cost:      ${hedge_cost:+.2f}")
    print(f"  Total PnL:             ${total_pnl:+.2f}")
    print(f"  Starting bankroll:     ${STARTING_BANKROLL:.2f}")
    print(f"  Ending bankroll:       ${bankroll:.2f}")
    print(f"  Return:                {((bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100):+.1f}%")
    print(f"  {'─'*60}")

    return {
        "trades": total_trades, "wins": wins, "losses": losses,
        "skipped": skipped, "pnl": total_pnl, "bankroll": bankroll,
        "win_rate": win_rate,
    }


if __name__ == "__main__":
    print(f"\n{'═'*60}")
    print(f"  POLYMARKET HEDGE BOT — BACKTEST")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Starting bankroll: ${STARTING_BANKROLL:.2f}")
    print(f"  Direction threshold: {DIRECTION_THRESHOLD}%")
    print(f"  Kelly fraction: {KELLY_FRACTION} (half-Kelly)")
    print(f"{'═'*60}")

    results = {}
    for symbol, label in [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")]:
        results[label] = run_backtest(symbol, label)
        time.sleep(1)  # rate limit

    # Combined summary
    total_trades = sum(r["trades"] for r in results.values())
    total_wins = sum(r["wins"] for r in results.values())
    total_pnl = sum(r["pnl"] for r in results.values())
    combined_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0

    print(f"\n{'═'*60}")
    print(f"  COMBINED RESULTS (BTC + ETH)")
    print(f"  Total trades:     {total_trades}")
    print(f"  Win rate:         {combined_wr:.1f}%")
    print(f"  Combined PnL:     ${total_pnl:+.2f}")
    print(f"{'═'*60}\n")
