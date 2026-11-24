"""Graduated-momentum paper-trading strategy for the pump.fun replay.

Idea
----
pump.fun bonding curves mostly die young: a fresh launch is often a bundle
that dumps within minutes. The coins that pay are the ones where *real,
broad* buying keeps arriving after the launch noise fades. This bot only
buys a curve once it has survived a minimum age and its recent tape shows
accelerating, widely spread buy pressure with the price sitting near its
recent high. It starts small, adds into strength only, and exits on a
disciplined set of rules (stop loss, trailing stop, flow fade, creator
sell, max hold) instead of hoping a dead coin comes back.

Every decision is a pure function of the batches the harness feeds it:
no clock, no randomness, no I/O, no environment reads. Iteration is over
insertion-ordered dicts only, so two runs of the same replay produce the
same action log byte for byte.

Money management
----------------
* Capital is 10 SOL of paper money; at most 5 open positions.
* First buy 0.05 SOL; add 0.05 SOL at a time while the price is above the
  last buy and the position is up, up to 0.20 SOL per coin.
* Total cost across open positions is capped at 6 SOL.
* Every buy is also capped by the harness's 1%-of-real-reserves size cap.
* A round trip costs roughly 2.5% of the stake in curve fees (1.25% each
  way) plus 0.000105 SOL of tx fees per order, so small probes are kept
  rare and only used to keep the entry guardrails met.

Guardrails
----------
The competition requires a round trip of at least 0.01 SOL on each of at
least 20 distinct coins and a maximum drawdown of 50%. The main strategy
usually trades far more than 20 coins; a lightweight probe mechanism opens
tiny 0.02 SOL round trips on liquid curves only if the bot's own fills or
coins fall behind the pace implied by elapsed block time.
"""

LAMPORTS = 1_000_000_000
# Curve fee taken on each side of a trade (protocol 0.95% + creator 0.30%).
CURVE_FEE = 0.0125


class Params:
    """All tunables in one place; defaults were picked for robustness on the
    dev window, not for a single best cell."""

    # --- entry signal (window in slots) ---
    window = 40            # lookback for the entry tape
    min_net_sol = 3.0      # net buy SOL (buys minus sells) over the window
    min_buys = 8           # buy trades in the window
    min_buyers = 8         # distinct buying wallets in the window
    min_price_up = 0.1    # price now / price at window start - 1, at least
    max_price_up = 1.0     # ... and at most (skip coins that already ran)
    accel = 1.0            # newer-half net inflow >= accel * older-half net
    near_high = 0.98       # price now >= near_high * window high
    max_buyer_share = 0.6  # largest single buyer <= share of window buy SOL
    min_rq_sol = 1.0       # real SOL reserves at entry, at least
    max_rq_sol = 80.0      # ... and at most
    min_age = 300          # slots since CreateEvent before an entry
    max_age = 40000        # slots after CreateEvent after which no new entry
    cooldown = 200         # slots to wait after an exit before re-entry
    max_entries = 2        # most first entries per curve over the run

    # --- sizing ---
    stake_sol = 0.2       # most SOL at cost in one position
    initial_sol = 0.05     # first buy of a position
    add_sol = 0.05         # each add-on buy
    add_step = 0.0         # add only if price >= last buy price * (1 + add_step)
    add_if_up = 0.02       # add only while position value >= cost * (1 + add_if_up)
    min_buy_sol = 0.01     # smallest buy worth paying the tx fee for
    buy_slip_bps = 1500    # slippage on buys; sells go out wide open
    max_exposure_sol = 6.0  # most SOL at cost across all positions at once
    max_main = 5           # most momentum positions open at once

    # --- exits ---
    stop_loss = 0.08       # exit when value < cost * (1 - stop_loss)
    trail = 0.12           # exit when value < peak * (1 - trail) ...
    trail_arm = 0.05       # ... once value reached cost * (1 + trail_arm)
    fade_window = 20       # exit when net SOL over fade_window < -fade_net_sol
    fade_net_sol = 1.0
    max_hold = 800        # exit after this many slots held
    hold_if_up = 0.50      # ... unless the position is up at least this much

    # --- guardrail pacing ---
    target_coins = 25      # coins to have traded by the end of the window
    target_fills = 240     # fills to have by the end of the window
    pace_secs = 14400      # block seconds over which the pace is measured
    probe_sol = 0.02       # probe buy size
    max_probes = 1         # most probe round trips open at once


class _Tape:
    """Per-curve rolling trade tape."""

    __slots__ = ("created", "creator", "trades", "entries", "last_exit")

    def __init__(self):
        self.created = None      # slot of the CreateEvent
        self.creator = None      # creator wallet
        self.trades = []         # list of (slot, is_buy, sol, user, price)
        self.entries = 0         # first entries taken on this curve
        self.last_exit = -10 ** 12


class _Held:
    """A position the bot is managing."""

    __slots__ = ("opened", "peak_value", "invested", "exiting",
                 "filled", "last_add_price", "last_value")

    def __init__(self, opened, invested, last_add_price):
        self.opened = opened
        self.peak_value = 0.0
        self.invested = invested
        self.exiting = False
        self.filled = False
        self.last_add_price = last_add_price
        self.last_value = 0.0


def _price(curve):
    return curve.virtual_quote_reserves / max(1, curve.virtual_token_reserves)


def _close_value(state, mint, tokens, curve, mark):
    """What the position is worth in SOL right now, net of the curve fee
    and one tx fee, if sold at the latest known reserves."""
    if mark is not None:
        vq = int(mark["virtual_quote_reserves"])
        vt = int(mark["virtual_token_reserves"])
    else:
        vq = curve.virtual_quote_reserves
        vt = curve.virtual_token_reserves
    gross = tokens * vq / (vt + tokens) / LAMPORTS
    return gross * (1.0 - CURVE_FEE) - state.tx_fee_lamports() / LAMPORTS


class Strategy:
    """Stateful strategy; one instance serves the whole replay."""

    def __init__(self, params=None):
        self.p = params or Params()
        self.tapes = {}          # mint -> _Tape, first-seen order
        self.held = {}           # mint -> _Held
        self.probes = {}         # mint -> bool (False buying, True selling)
        self.my_fills = 0
        self.my_coins = {}       # mint -> True, insertion ordered
        self.first_time = None
        self.last_time = None

    # -- tape helpers ---------------------------------------------------

    def _tape(self, mint):
        t = self.tapes.get(mint)
        if t is None:
            t = _Tape()
            self.tapes[mint] = t
        return t

    def _window_stats(self, tape, now):
        """Aggregate the entry window: net flow, buys, buyers, price path."""
        p = self.p
        start = now - p.window
        mid = now - p.window // 2
        buy_sol = 0.0
        sell_sol = 0.0
        buys = 0
        net_old = 0.0
        net_new = 0.0
        start_price = -1.0
        high = 0.0
        last = 0.0
        by_user = {}
        for slot, is_buy, sol, user, price in tape.trades:
            if slot <= start:
                start_price = price
                continue
            if start_price < 0:
                start_price = price
            signed = sol if is_buy else -sol
            if slot <= mid:
                net_old += signed
            else:
                net_new += signed
            if is_buy:
                buy_sol += sol
                buys += 1
                by_user[user] = by_user.get(user, 0.0) + sol
            else:
                sell_sol += sol
            if price > high:
                high = price
            last = price
        top = 0.0
        for v in by_user.values():
            if v > top:
                top = v
        return {
            "net": buy_sol - sell_sol,
            "buys": buys,
            "buyers": len(by_user),
            "start_price": start_price,
            "last": last,
            "high": high,
            "net_old": net_old,
            "net_new": net_new,
            "top_share": (top / buy_sol) if buy_sol > 0 else 1.0,
        }

    def _entry_ok(self, tape, now, rq_sol):
        p = self.p
        if rq_sol < p.min_rq_sol or rq_sol > p.max_rq_sol:
            return False
        if tape.created is None:
            return False
        age = now - tape.created
        if age < p.min_age or age > p.max_age:
            return False
        s = self._window_stats(tape, now)
        if s["buys"] < p.min_buys or s["buyers"] < p.min_buyers:
            return False
        if s["net"] < p.min_net_sol:
            return False
        if s["start_price"] <= 0:
            return False
        up = s["last"] / s["start_price"] - 1.0
        if up < p.min_price_up or up > p.max_price_up:
            return False
        if s["last"] < s["high"] * p.near_high:
            return False
        if s["net_new"] <= 0 or s["net_new"] < p.accel * s["net_old"]:
            return False
        if s["top_share"] > p.max_buyer_share:
            return False
        return True

    def _flow(self, tape, now, window):
        net = 0.0
        for slot, is_buy, sol, _user, _price in reversed(tape.trades):
            if slot <= now - window:
                break
            net += sol if is_buy else -sol
        return net

    # -- main callback ---------------------------------------------------

    def on_batch(self, state, batch):
        p = self.p
        now = batch["slot"]
        creator_sold = set()
        traded = []              # mints with a trade this batch, in order
        seen_traded = set()
        for f in batch["fills"]:
            if f["status"] == "filled" and not f["client_id"].startswith("_"):
                self.my_fills += 1
                self.my_coins[f["mint"]] = True
        for e in batch["events"]:
            if e.get("block_time") is not None:
                if self.first_time is None:
                    self.first_time = e["block_time"]
                if self.last_time is None or e["block_time"] > self.last_time:
                    self.last_time = e["block_time"]
            mint = e["mint"]
            if e["event"] == "CreateEvent":
                t = self._tape(mint)
                t.created = e["slot"]
                t.creator = e.get("creator")
            elif e["event"] == "TradeEvent":
                t = self._tape(mint)
                sol = int(e["sol_amount"]) / LAMPORTS
                price = (int(e["virtual_quote_reserves"])
                         / max(1, int(e["virtual_token_reserves"])))
                t.trades.append((e["slot"], bool(e["is_buy"]), sol,
                                 e["user"], price))
                if mint not in seen_traded:
                    seen_traded.add(mint)
                    traded.append(mint)
                if (not e["is_buy"] and t.creator is not None
                        and e["user"] == t.creator):
                    creator_sold.add(mint)
            elif e["event"] == "CompleteEvent":
                self._tape(mint)
        # Trim tapes to the longest window we still need.
        keep = max(p.window, p.fade_window) + 5
        for mint in traded:
            t = self.tapes[mint]
            cut = 0
            while cut < len(t.trades) and t.trades[cut][0] <= now - keep:
                cut += 1
            if cut:
                del t.trades[:cut]

        marks = {m["mint"]: m for m in batch["marks"]}
        pending_by_mint = {}
        for po in state.pending.values():
            pending_by_mint[po.order["mint"]] = \
                pending_by_mint.get(po.order["mint"], 0) + 1

        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]
        tx = state.tx_fee_lamports() / LAMPORTS

        # --- exits first ------------------------------------------------
        for mint in list(self.held):
            h = self.held[mint]
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    del self.held[mint]
                    t = self._tape(mint)
                    if h.filled:
                        t.last_exit = now
                    else:
                        t.entries -= 1
                continue
            h.filled = True
            if curve is None or curve.complete:
                continue
            mark = marks.get(mint)
            rq_sol = (int(mark["real_quote_reserves"]) if mark is not None
                      else curve.real_quote_reserves) / LAMPORTS
            tokens = int(pos.token_amount)
            value = _close_value(state, mint, tokens, curve, mark)
            cost = int(pos.cost_lamports) / LAMPORTS
            if value > h.peak_value:
                h.peak_value = value
            h.last_value = value
            t = self._tape(mint)
            exit_now = h.exiting
            if not exit_now and value < cost * (1.0 - p.stop_loss):
                exit_now = True
            if (not exit_now and h.peak_value >= cost * (1.0 + p.trail_arm)
                    and value < h.peak_value * (1.0 - p.trail)):
                exit_now = True
            if (not exit_now
                    and self._flow(t, now, p.fade_window) < -p.fade_net_sol):
                exit_now = True
            if not exit_now and mint in creator_sold:
                exit_now = True
            if (not exit_now and now - h.opened > p.max_hold
                    and value < cost * (1.0 + p.hold_if_up)):
                exit_now = True
            if not exit_now:
                continue
            h.exiting = True
            if mint in pending_by_mint:
                sell_pending = any(
                    po.order["mint"] == mint and po.order["side"] == "sell"
                    for po in state.pending.values())
                if sell_pending:
                    continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": 10000})

        # --- probe round trips: sell as soon as the probe buy lands ----
        for mint in list(self.probes):
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    del self.probes[mint]
                continue
            if curve is None or curve.complete or mint in pending_by_mint:
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": 10000})
            self.probes[mint] = True

        # --- entries and adds -------------------------------------------
        free = state.free_cash_lamports() / LAMPORTS
        slots_left = (state.hello["caps"]["max_open_positions"]
                      - state.committed_position_count())
        main_left = p.max_main - len(self.held)
        exposure = 0.0
        for mint, h in self.held.items():
            pos = state.positions.get(mint)
            exposure += max(h.invested,
                            int(pos.cost_lamports) / LAMPORTS if pos else 0.0)
        for mint in traded:
            if len(orders) >= cap:
                break
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if mint in pending_by_mint or mint in self.probes:
                continue
            t = self.tapes[mint]
            h = self.held.get(mint)
            price = _price(curve)
            rq_sol = curve.real_quote_reserves / LAMPORTS
            if h is None:
                if slots_left <= 0 or main_left <= 0:
                    continue
                if t.entries >= p.max_entries or now - t.last_exit < p.cooldown:
                    continue
                if not self._entry_ok(t, now, rq_sol):
                    continue
                want = min(p.initial_sol, p.stake_sol)
            else:
                if (not p.add_sol or h.exiting or not h.filled
                        or h.invested >= p.stake_sol - p.min_buy_sol):
                    continue
                pos = state.positions.get(mint)
                if pos is None:
                    continue
                if price < h.last_add_price * (1.0 + p.add_step):
                    continue
                if h.last_value < int(pos.cost_lamports) / LAMPORTS * (1.0 + p.add_if_up):
                    continue
                if self._flow(t, now, p.window) < p.min_net_sol:
                    continue
                if rq_sol > p.max_rq_sol:
                    continue
                want = min(p.add_sol, p.stake_sol - h.invested)
            cap_sol = state.max_buy_lamports(mint) / LAMPORTS
            amount = min(cap_sol, want, p.max_exposure_sol - exposure)
            if amount > free - tx:
                amount = free - tx
            if amount < p.min_buy_sol:
                continue
            lamports = int(amount * LAMPORTS)
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": lamports,
                           "max_slippage_bps": p.buy_slip_bps})
            free -= amount + tx
            exposure += amount
            if h is None:
                self.held[mint] = _Held(now, amount, price)
                t.entries += 1
                slots_left -= 1
                main_left -= 1
            else:
                h.invested += amount
                h.last_add_price = price

        # --- guardrail pacing: tiny probe round trips when behind pace --
        if (len(self.probes) < p.max_probes and slots_left > 0
                and self.first_time is not None and self.last_time is not None):
            pace = min(1.0, (self.last_time - self.first_time) / p.pace_secs)
            need_coins = len(self.my_coins) < int(p.target_coins * pace + 0.999)
            need_fills = self.my_fills < int(p.target_fills * pace + 0.999)
            if need_coins or need_fills:
                min_rq = (p.probe_sol * 10000
                          / state.hello["caps"]["max_order_bps_of_real_reserve"])
                for mint in traded:
                    if (len(orders) >= cap
                            or len(self.probes) >= p.max_probes):
                        break
                    if (mint in self.held or mint in self.probes
                            or mint in pending_by_mint
                            or mint in state.positions):
                        continue
                    if need_coins and not need_fills and mint in self.my_coins:
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.real_quote_reserves / LAMPORTS < min_rq * 1.5:
                        continue
                    if free < p.probe_sol + 2 * tx:
                        break
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": int(p.probe_sol * LAMPORTS),
                                   "max_slippage_bps": p.buy_slip_bps})
                    self.probes[mint] = False
                    free -= p.probe_sol + tx
        return orders
