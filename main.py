"""RansReplay — a pump.fun replay trading bot for the make.fun
"Winner Takes All — $5 Pump.fun Replay Sprint" (harness pumpfun-replay/v2).

Strategy, in one paragraph
--------------------------
The bot adapts the screening logic of the Rans Farming Telegram bot
(token-safety filters first, momentum second) to the only data a replay
offers: bonding-curve Create/Trade/Complete events. Every candidate coin
must pass a set of HARD safety filters that are never relaxed (minimum
real liquidity, no creator dumping, no single-buyer dominance, no
creator self-buy dominance, sell pressure must stay below buy pressure,
no buying into a crash). Coins that pass are scored on momentum
(net flow, unique buyers, price appreciation inside the window,
flow acceleration, distance from the window high). Entries run under a
tiered fallback: strict thresholds early in the 6-hour window, looser
momentum/quality thresholds as time passes without enough round trips,
and small endgame positions in the best available hard-safe candidate —
so the bot cannot finish the window with zero trades (a 0 PnL can never
take a place), while the anti-rug filters stay intact at every tier.
Exits are active: stop-loss, take-profit, trailing stop, momentum-fade
exit, dump exit and a time stop rotate capital through at least 20
distinct coins (the competition's minimum round-trip bar) with round
trips of at least 0.01 SOL each, and a drawdown brake protects the
50% maximum-drawdown guardrail.

Determinism: no clock, no environment, no randomness; iteration is only
over insertion-ordered dicts, sets are used for membership tests only.
Standard library only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import makefun_bot  # noqa: E402

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL

# ---------------------------------------------------------------------------
# Tunables (all amounts in lamports unless the name says otherwise)
# ---------------------------------------------------------------------------

WINDOW_SLOTS_EST = 54_000          # ~6 hours at ~0.4 s/slot: the final window
EVAL_WINDOW = 450                  # momentum window, in slots (~6 minutes)
FADE_WINDOW = 300                  # short window for exit fade checks
TAPE_MAX = 320                     # trades kept per coin (ring, oldest dropped)
MIN_ROUND_TRIP = 10_000_000        # 0.01 SOL: the guardrail's minimum buy
CURVE_FEE = 0.0125                 # 95 bps protocol + 30 bps creator

# Hard safety filters — NEVER relaxed by the fallback tiers.
HARD_MIN_REAL_QUOTE = 8 * LAMPORTS     # real SOL reserves (liquidity floor)
HARD_MAX_TOP_BUYER_SHARE = 0.62        # one wallet dominating buys = bundle risk
HARD_MAX_CREATOR_BUY_SHARE = 0.45      # creator buying their own launch too hard
HARD_MAX_SELL_RATIO = 0.85             # sell SOL may not exceed 85% of buy SOL
HARD_MIN_AGE_SLOTS = 90                # let the first bundle blocks pass
HARD_CRASH_FLOOR = 0.50                # last price must be >= 50% of window high
HARD_MIN_WINDOW_TRADES = 6

# Fallback tiers: (name, until_elapsed_fraction, min_buys, min_unique_buyers,
#                 min_net_sol, min_price_up, min_near_high, stake_lamports)
TIERS = (
    ("strict",   0.40, 10, 6, 2.00,  0.05, 0.92, 80_000_000),
    ("standard", 0.70,  8, 5, 1.20,  0.03, 0.90, 55_000_000),
    ("relaxed",  0.88,  6, 4, 0.60,  0.01, 0.86, 35_000_000),
    ("endgame",  1.01,  5, 3, 0.25, -0.02, 0.82, 18_000_000),
)
TARGET_ROUND_TRIPS = 24            # guardrail is 20; aim above it for margin
MAX_SLIPPAGE_BPS = 2_000

# Exit rules
STOP_LOSS = -0.10
TAKE_PROFIT = 0.60
TRAIL_ARM = 0.18                   # trailing stop arms at +20%
TRAIL_GAP = 0.12                   # ...and fires 12 points below the peak
HOLD_SOFT_SLOTS = 900            # time stop when not clearly winning
HOLD_HARD_SLOTS = 1_800            # never hold longer than this
REENTRY_COOLDOWN = 900
DRAWDOWN_BRAKE = 0.35              # pause new entries beyond this drawdown


# ---------------------------------------------------------------------------
# Per-coin tape: the replay equivalent of the Rans Farming /scan inputs
# ---------------------------------------------------------------------------

class Tape:
    __slots__ = ("creator", "created_slot", "trades", "creator_sold",
                 "total_buys", "total_sells")

    def __init__(self):
        self.creator = None
        self.created_slot = None
        # each trade: (slot, is_buy, sol_lamports, user, price) — price is
        # virtual_quote / virtual_token as a float ratio (deterministic).
        self.trades = []
        self.creator_sold = False
        self.total_buys = 0
        self.total_sells = 0


tapes = {}            # mint -> Tape, insertion ordered
held = {}             # mint -> {"entry_slot": int, "peak_pnl": float}
closed_mints = {}     # mint -> slot of our completed round trip (count once)
cooldown_until = {}   # mint -> slot after which re-entry is allowed again
dead_mints = set()    # stop-lossed or creator-dumped: membership only
first_slot = None
peak_equity = None
entries_paused = False


def tape_of(mint):
    t = tapes.get(mint)
    if t is None:
        t = Tape()
        tapes[mint] = t
    return t


def elapsed_fraction(state):
    if first_slot is None:
        return 0.0
    frac = (state.slot - first_slot) / WINDOW_SLOTS_EST
    return 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)


def tier_for(state):
    """Current fallback tier, also driven by round-trip pace: if completed
    round trips lag the pace the 20-coin bar needs, drop a tier early."""
    frac = elapsed_fraction(state)
    pace_needed = TARGET_ROUND_TRIPS * frac
    lag = pace_needed - (len(closed_mints) + len(held))
    idx = 0
    while idx < len(TIERS) - 1 and frac > TIERS[idx][1]:
        idx += 1
    if lag > 3 and idx < len(TIERS) - 1:
        idx += 1
    if lag > 7 and idx < len(TIERS) - 1:
        idx += 1
    return TIERS[idx]


def window_stats(t, now, window):
    """Aggregate the tape over the last `window` slots. Returns a dict or
    None when there is too little data. Pure function of the tape."""
    lo = now - window
    buys = sells = 0
    buy_sol = sell_sol = 0
    net_old = net_new = 0
    mid = now - window // 2
    start_price = -1.0
    last_price = 0.0
    high = 0.0
    by_user = {}
    creator_buy = 0
    seen = 0
    for (slot, is_buy, sol, user, price) in t.trades:
        if slot <= lo:
            continue
        seen += 1
        if start_price < 0.0:
            start_price = price
        last_price = price
        if price > high:
            high = price
        signed = sol if is_buy else -sol
        if slot <= mid:
            net_old += signed
        else:
            net_new += signed
        if is_buy:
            buys += 1
            buy_sol += sol
            by_user[user] = by_user.get(user, 0) + sol
            if t.creator is not None and user == t.creator:
                creator_buy += sol
        else:
            sells += 1
            sell_sol += sol
    if seen == 0 or start_price <= 0.0:
        return None
    top_buyer = 0
    for v in by_user.values():
        if v > top_buyer:
            top_buyer = v
    return {
        "trades": seen,
        "buys": buys,
        "sells": sells,
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "net": buy_sol - sell_sol,
        "net_new": net_new,
        "net_old": net_old,
        "unique_buyers": len(by_user),
        "top_share": (top_buyer / buy_sol) if buy_sol else 1.0,
        "creator_share": (creator_buy / buy_sol) if buy_sol else 0.0,
        "up": (last_price / start_price - 1.0) if start_price else 0.0,
        "near_high": (last_price / high) if high else 0.0,
    }


def hard_safe(state, mint, t, st):
    """The anti-rug filters. These are identical at every fallback tier."""
    curve = state.curves.get(mint)
    if curve is None or curve.complete:
        return False
    if curve.real_quote_reserves < HARD_MIN_REAL_QUOTE:
        return False
    if t.creator_sold:
        return False
    if t.created_slot is not None and state.slot - t.created_slot < HARD_MIN_AGE_SLOTS:
        return False
    if st["trades"] < HARD_MIN_WINDOW_TRADES:
        return False
    if st["top_share"] > HARD_MAX_TOP_BUYER_SHARE:
        return False
    if st["creator_share"] > HARD_MAX_CREATOR_BUY_SHARE:
        return False
    if st["sell_sol"] > st["buy_sol"] * HARD_MAX_SELL_RATIO:
        return False
    if st["near_high"] < HARD_CRASH_FLOOR:
        return False
    return True


def momentum_score(st):
    """One comparable number for ranking candidates inside a tier."""
    net_sol = st["net"] / LAMPORTS
    score = net_sol * 2.0
    score += st["unique_buyers"] * 0.35
    score += st["buys"] * 0.08
    score += st["up"] * 3.0
    score += st["near_high"] * 1.0
    if st["net_new"] > st["net_old"]:
        score += 0.8            # flow is accelerating, not fading
    score -= st["top_share"] * 1.2
    return score


def position_value_lamports(state, mint, tokens):
    """Mark a position to the curve, net of the curve fee, as the exit
    rules see it (tx fee subtracted by the caller when it matters)."""
    curve = state.curves.get(mint)
    if curve is None or tokens <= 0:
        return 0
    vt = curve.virtual_token_reserves
    vq = curve.virtual_quote_reserves
    if vt + tokens <= 0:
        return 0
    gross = tokens * vq // (vt + tokens)
    return int(gross * (1.0 - CURVE_FEE))


# ---------------------------------------------------------------------------
# Main per-batch logic
# ---------------------------------------------------------------------------

def on_batch(state, batch):
    global first_slot, peak_equity, entries_paused

    if first_slot is None:
        first_slot = state.slot
    now = state.slot
    caps = state.hello["caps"]
    tx_fee = state.tx_fee_lamports()
    max_orders = caps["max_orders_per_batch"]

    # --- fold this batch's events into the tapes ---------------------------
    traded = {}       # mint -> True, insertion ordered by first touch
    big_sell = {}     # mint -> True when a large sell lands this batch
    for e in batch["events"]:
        mint = e["mint"]
        kind = e["event"]
        if kind == "CreateEvent":
            t = tape_of(mint)
            if t.created_slot is None:
                t.created_slot = e["slot"]
            if t.creator is None:
                t.creator = e.get("creator") or e.get("user")
            traded[mint] = True
        elif kind == "TradeEvent":
            t = tape_of(mint)
            if t.creator is None and e.get("creator"):
                t.creator = e["creator"]
            vt = int(e["virtual_token_reserves"])
            vq = int(e["virtual_quote_reserves"])
            price = (vq / vt) if vt else 0.0
            sol = int(e["sol_amount"])
            t.trades.append((e["slot"], bool(e["is_buy"]), sol, e["user"], price))
            if len(t.trades) > TAPE_MAX:
                del t.trades[: len(t.trades) - TAPE_MAX]
            if e["is_buy"]:
                t.total_buys += 1
            else:
                t.total_sells += 1
                if t.creator is not None and e["user"] == t.creator:
                    t.creator_sold = True
                if sol >= LAMPORTS // 2:
                    big_sell[mint] = True
            traded[mint] = True
        else:  # CompleteEvent
            traded[mint] = True

    # --- sync our own book with the account --------------------------------
    for mint in list(held.keys()):
        if mint not in state.positions:
            # Position gone: sold by us, completed, or flattened.
            closed_mints[mint] = now
            del held[mint]
    for mint in state.positions:
        if mint not in held and mint not in closed_mints:
            held[mint] = {"entry_slot": now, "peak_pnl": 0.0}
        elif mint in held and mint in closed_mints and mint not in state.positions:
            pass

    pending_mints = {}
    for p in state.pending.values():
        pending_mints[p.order["mint"]] = True

    orders = []

    # --- exits first: they free slots and lock in round trips --------------
    for mint in list(held.keys()):
        if len(orders) >= max_orders:
            break
        pos = state.positions.get(mint)
        if pos is None or mint in pending_mints:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue  # the harness closes completed curves itself
        h = held[mint]
        value = position_value_lamports(state, mint, pos.token_amount) - tx_fee
        pnl = (value / pos.cost_lamports - 1.0) if pos.cost_lamports else 0.0
        if pnl > h["peak_pnl"]:
            h["peak_pnl"] = pnl
        age = now - h["entry_slot"]
        t = tapes.get(mint)
        fade = window_stats(t, now, FADE_WINDOW) if t is not None else None

        sell = False
        if pnl <= STOP_LOSS:
            sell = True
            dead_mints.add(mint)
        elif pnl >= TAKE_PROFIT:
            sell = True
        elif h["peak_pnl"] >= TRAIL_ARM and pnl <= h["peak_pnl"] - TRAIL_GAP:
            sell = True
        elif mint in big_sell or (t is not None and t.creator_sold):
            sell = True
            dead_mints.add(mint)
        elif fade is not None and fade["net"] < 0 and pnl < 0.02:
            sell = True
        elif age >= HOLD_HARD_SLOTS:
            sell = True
        elif age >= HOLD_SOFT_SLOTS and pnl < 0.15:
            sell = True

        if sell:
            orders.append({
                "side": "sell",
                "mint": mint,
                "token_amount": pos.token_amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            pending_mints[mint] = True
            cooldown_until[mint] = now + REENTRY_COOLDOWN

    # --- drawdown brake ------------------------------------------------------
    equity = state.cash
    for mint, pos in state.positions.items():
        equity += position_value_lamports(state, mint, pos.token_amount)
    if peak_equity is None or equity > peak_equity:
        peak_equity = equity
    if peak_equity and peak_equity > 0:
        dd = (peak_equity - equity) / peak_equity
        entries_paused = dd > DRAWDOWN_BRAKE

    # --- entries -------------------------------------------------------------
    tier = tier_for(state)
    (_, _, min_buys, min_uniq, min_net_sol, min_up, min_near, stake) = tier
    min_net = int(min_net_sol * LAMPORTS)

    if not entries_paused:
        free = state.free_cash_lamports()
        open_slots = caps["max_open_positions"] - state.committed_position_count()
        # Sells sent this batch free their slot only on fill; stay conservative
        # and let committed_position_count (which counts pending buys) rule.
        candidates = []
        for mint in traded:
            if len(candidates) >= 64:
                break
            if mint in held or mint in pending_mints or mint in dead_mints:
                continue
            if mint in closed_mints:
                continue  # one round trip per coin is what the bar rewards
            if cooldown_until.get(mint, -1) > now:
                continue
            t = tapes.get(mint)
            if t is None:
                continue
            st = window_stats(t, now, EVAL_WINDOW)
            if st is None:
                continue
            if not hard_safe(state, mint, t, st):
                continue
            if st["buys"] < min_buys or st["unique_buyers"] < min_uniq:
                continue
            if st["net"] < min_net or st["up"] < min_up or st["near_high"] < min_near:
                continue
            if st["net_new"] <= st["net_old"]:
                continue  # flow must still be accelerating, at every tier
            if st["up"] > 0.75:
                continue  # do not chase a coin that already ran too far
            candidates.append((momentum_score(st), mint))
        # Highest score first; ties keep first-touch order (stable sort).
        candidates.sort(key=lambda c: -c[0])
        for _, mint in candidates:
            if len(orders) >= max_orders or open_slots <= 0:
                break
            cap = state.max_buy_lamports(mint)
            amount = min(stake, cap)
            if amount < MIN_ROUND_TRIP + 2_000_000:
                continue  # must clear the 0.01 SOL round-trip bar with margin
            if free < amount + tx_fee:
                continue
            orders.append({
                "side": "buy",
                "mint": mint,
                "quote_lamports": amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            free -= amount + tx_fee
            open_slots -= 1
            pending_mints[mint] = True

    # --- prune tapes of coins that are done and cold -------------------------
    if len(tapes) > 4000:
        for mint in list(tapes.keys()):
            if len(tapes) <= 3000:
                break
            if mint in held or mint in pending_mints:
                continue
            t = tapes[mint]
            if not t.trades or now - t.trades[-1][0] > 20_000:
                del tapes[mint]

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
