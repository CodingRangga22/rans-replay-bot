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
50% maximum-drawdown guardrail. Version 5 adds a volatility filter
(a coin that already crashed hard inside its window is unstable), a
hard round-trip budget that caps fee drag, higher entry bars on the
loose tiers, and per-tier exits: strong-signal entries get a wider
trailing stop so winners can run, weak endgame entries are cut faster.
Version 6 added smart-degen wallet tracking; version 7 focuses that
tracking on the owner's two chosen wallets: both are pre-trusted,
coins they hold earn a bounded score bonus, the bot mirrors their
exits on coins it holds, and entries backed by them may ride to a 2x
take-profit.

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
#                 min_net_sol, min_price_up, min_near_high, stake_lamports,
#                 chase_cap, stop_loss, trail_gap, soft_hold_slots)
# v5 tuning, from the dev grade of v4 (87 round trips, net -0.073 SOL of
# which 0.064 SOL was pure fees): the loose tiers produced too many weak,
# fee-dominated trades, strong-tier winners were harvested too early and
# weak-tier losers too late. So the relaxed/endgame entry bars are higher,
# exits are per-tier (strong entries get room to run, weak ones are cut
# faster), and a hard round-trip budget caps fee drag.
TIERS = (
    ("strict",   0.40, 10, 6, 2.00,  0.05, 0.92, 80_000_000, 0.75, -0.10, 0.16, 900),
    ("standard", 0.70,  8, 5, 1.35,  0.03, 0.90, 55_000_000, 0.65, -0.10, 0.12, 900),
    ("relaxed",  0.88,  7, 5, 0.90,  0.01, 0.86, 35_000_000, 0.55, -0.08, 0.09, 700),
    ("endgame",  1.01,  6, 4, 0.55,  0.00, 0.82, 18_000_000, 0.45, -0.08, 0.09, 700),
)
TARGET_ROUND_TRIPS = 24            # guardrail is 20; aim above it for margin
RT_BUDGET = 44                     # hard cap on round trips: fee-drag brake
MAX_SLIPPAGE_BPS = 2_000

# Volatility filter (entries, every tier): a coin whose price already
# crashed more than 38% from its window high at some point inside the
# window is unstable — its falls gap straight through stop-losses.
VOL_MAX_CRASH = 0.62               # low after the high must stay >= 62% of it

# Exit rules (stop-loss / trailing gap / soft hold are per-tier, above)
TAKE_PROFIT = 0.60
TRAIL_ARM = 0.18                   # trailing stop arms at +18%
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

# ---------------------------------------------------------------------------
# Smart-degen wallet tracking (v6)
#
# Every TradeEvent names its wallet, so the bot keeps a ledger of every
# wallet's positions inside the window: SOL in, tokens in, SOL out,
# tokens out, with realized PnL on each sell priced at average cost.
# The ledger exists to follow the owner's WATCHLIST (v7, below): two
# specific smart-degen wallets are pre-trusted, and the bot tracks
# exactly where they stand in every coin. Two uses, both conservative:
# a coin a watchlist wallet is currently holding gets a bounded score
# bonus (hard filters and tier bars are unchanged), and if a watchlist
# holder of a coin WE hold exits most of its position, we mirror the
# exit. (An earlier version let any wallet earn the label from proven
# in-window profit; the owner asked to focus on his two wallets only,
# so auto-qualification is removed.)
# ---------------------------------------------------------------------------

SMART_MIN_CLOSES = 2
SMART_MIN_PROFIT = 1 * LAMPORTS    # total realized profit across closes
SMART_BONUS = 1.0                  # score bonus per proven holder
SMART_BONUS_CAP = 2.0
SMART_EXIT_FRACTION = 0.60         # proven holder sells >= 60%: mirror it

# v7: the owner's watchlist. Per the owner's instruction the tracking
# focuses on these two wallets only: both are pre-trusted as smart
# degens from the first slot (they do not need to earn the label from
# window data), and the auto-qualification of other wallets is off.
# W1 is a sniper the owner follows (buys new tokens, sells on top);
# W2 is a whale / smart degen. Entries backed by a watchlist holder
# may ride to a 2x take-profit (WATCH_TP) instead of the usual one.
WATCHLIST = {
    "DkzUg73acuQzkctMNnBnVaSYeqDGXzWCtoiKV9GyZAJc": True,
    "9NXNywh7n8d8ihgUY2FcJn9hEPwXkYCdLyBdbFiDm4rs": True,
}
WATCH_TP = 1.00                    # 2x take-profit for watchlist-backed entries
watch_backed = {}                  # mint -> True when our entry had a
                                   # watchlist holder behind it

wallet_pos = {}       # (user, mint) -> [sol_in, tok_in, sol_out, tok_out,
                      #                  realized, last_buy_slot]
wallet_book = {}      # user -> [realized_total, closes, wins]
user_mints = {}       # user -> {mint: True} open positions, insertion ordered
smart = set(WATCHLIST)  # v7: pre-trusted watchlist wallets (see below);
                      # the label can no longer be earned by other wallets
smart_in = {}         # mint -> {user: last_buy_slot} proven holders in now


def wallet_sell(user, mint, sol, tok, slot):
    """Fold one sell into the wallet ledger; returns the fraction of the
    seller's token position this sell represents (0 when unknown)."""
    key = (user, mint)
    wp = wallet_pos.get(key)
    if wp is None:
        return 0.0
    frac = (tok / wp[1]) if wp[1] > 0 else 0.0
    if wp[1] > 0 and tok > 0:
        basis = wp[0] * min(tok, wp[1]) // wp[1]
        wp[4] += sol - basis
    wp[2] += sol
    wp[3] += tok
    if wp[1] > 0 and wp[3] >= wp[1]:
        # Position fully closed: score the wallet on the outcome.
        book = wallet_book.get(user)
        if book is None:
            book = [0, 0, 0]
            wallet_book[user] = book
        book[0] += wp[4]
        book[1] += 1
        if wp[4] > 0:
            book[2] += 1
        del wallet_pos[key]
        um = user_mints.get(user)
        if um is not None:
            um.pop(mint, None)
        si = smart_in.get(mint)
        if si is not None:
            si.pop(user, None)
        # v7: no auto-qualification — the smart set is the watchlist
        # alone, fixed at start-up (see WATCHLIST below).
    return frac


def wallet_buy(user, mint, sol, tok, slot):
    key = (user, mint)
    wp = wallet_pos.get(key)
    if wp is None:
        wp = [0, 0, 0, 0, 0, slot]
        wallet_pos[key] = wp
        user_mints.setdefault(user, {})[mint] = True
    wp[0] += sol
    wp[1] += tok
    wp[5] = slot
    if user in smart:
        smart_in.setdefault(mint, {})[user] = slot


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
    low_after_high = 0.0
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
            low_after_high = price
        elif low_after_high > 0.0 and price < low_after_high:
            low_after_high = price
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
        "low_after_high": (low_after_high / high) if high else 1.0,
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
    smart_exit = {}   # mint -> True when a proven holder exits most of it
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
            tok = int(e["token_amount"])
            user = e["user"]
            t.trades.append((e["slot"], bool(e["is_buy"]), sol, user, price))
            if len(t.trades) > TAPE_MAX:
                del t.trades[: len(t.trades) - TAPE_MAX]
            if e["is_buy"]:
                t.total_buys += 1
                wallet_buy(user, mint, sol, tok, e["slot"])
            else:
                t.total_sells += 1
                if t.creator is not None and user == t.creator:
                    t.creator_sold = True
                if sol >= LAMPORTS // 2:
                    big_sell[mint] = True
                frac = wallet_sell(user, mint, sol, tok, e["slot"])
                if user in smart and frac >= SMART_EXIT_FRACTION:
                    smart_exit[mint] = True
                    si = smart_in.get(mint)
                    if si is not None:
                        si.pop(user, None)
            traded[mint] = True
        else:  # CompleteEvent
            traded[mint] = True

    # --- sync our own book with the account --------------------------------
    for mint in list(held.keys()):
        if mint not in state.positions:
            # Position gone: sold by us, completed, or flattened.
            closed_mints[mint] = now
            del held[mint]
    cur_tier = tier_for(state)
    for mint in state.positions:
        if mint not in held and mint not in closed_mints:
            # Exit parameters are frozen from the tier in force when the
            # position appears: strong-signal entries get room to run,
            # weak endgame entries are cut faster.
            held[mint] = {"entry_slot": now, "peak_pnl": 0.0,
                          "sl": cur_tier[9], "tg": cur_tier[10],
                          "soft": cur_tier[11],
                          "tp": WATCH_TP if mint in watch_backed
                          else TAKE_PROFIT}
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
        if pnl <= h.get("sl", -0.10):
            sell = True
            dead_mints.add(mint)
        elif pnl >= h.get("tp", TAKE_PROFIT):
            sell = True
        elif h["peak_pnl"] >= TRAIL_ARM and pnl <= h["peak_pnl"] - h.get("tg", 0.12):
            sell = True
        elif mint in big_sell or (t is not None and t.creator_sold):
            sell = True
            dead_mints.add(mint)
        elif mint in smart_exit:
            sell = True  # a proven degen is leaving: mirror the exit
        elif fade is not None and fade["net"] < 0 and pnl < 0.02:
            sell = True
        elif age >= HOLD_HARD_SLOTS:
            sell = True
        elif age >= h.get("soft", 900) and pnl < 0.15:
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
    (_, _, min_buys, min_uniq, min_net_sol, min_up, min_near, stake,
     chase_cap, _sl, _tg, _soft) = tier
    min_net = int(min_net_sol * LAMPORTS)

    if not entries_paused and len(closed_mints) + len(held) < RT_BUDGET:
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
            if st["up"] > chase_cap:
                continue  # do not chase a coin that already ran too far
            if st["low_after_high"] < VOL_MAX_CRASH:
                continue  # already crashed hard once inside this window
            score = momentum_score(st)
            backed = False
            si = smart_in.get(mint)
            if si:
                recent = 0
                for u, buy_slot in si.items():
                    if buy_slot >= now - EVAL_WINDOW and u in smart:
                        recent += 1
                score += min(SMART_BONUS_CAP, recent * SMART_BONUS)
                backed = recent > 0
            candidates.append((score, mint, backed))
        # Highest score first; ties keep first-touch order (stable sort).
        candidates.sort(key=lambda c: -c[0])
        for _, mint, backed in candidates:
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
            if backed:
                watch_backed[mint] = True
            free -= amount + tx_fee
            open_slots -= 1
            pending_mints[mint] = True

    # --- prune wallet ledger entries for coins gone cold ---------------------
    if len(wallet_pos) > 200_000:
        for (u, m) in list(wallet_pos.keys()):
            if len(wallet_pos) <= 150_000:
                break
            if u in smart or m in held:
                continue
            wp = wallet_pos[(u, m)]
            if now - wp[5] > 20_000:
                del wallet_pos[(u, m)]
                um = user_mints.get(u)
                if um is not None:
                    um.pop(m, None)

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
