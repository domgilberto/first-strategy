"""
Long-only, volatility-scaled averaging ladder on BTC/USD.

The mechanism people call a martingale, built the way mature DCA bots build it
and with the controls that make it survivable:

  1. Open a cycle with a market BASE order at the current price.
  2. Rest N limit BUY orders below it ("safety levels"), spaced in ATR with
     geometrically widening gaps and geometrically growing size. Each fill
     lowers the average entry.
  3. Rest one limit SELL (take-profit) for the whole position at
     average * (1 + tp), and re-place it every time a level fills. If the venue
     refuses a resting sell while buys are open, the take-profit is enforced in
     software instead - checked every poll, exited at market when reached.
  4. The take-profit target decays linearly toward breakeven after a holding
     period, so a stale cycle takes the first exit that clears costs.
  5. Three ways a cycle ends:
        take-profit reached                     -> closed_tp
        price reaches the hard stop below the
        lowest level                            -> closed_stop  (market exit, cooldown)
        the cycle exceeds max_hold_hours        -> closed_timeout (market exit, cooldown)
     Every close cancels whatever is still resting and sells whatever is still
     held, then settles P&L from the order ledger - so a level that filled after
     the take-profit was placed is still accounted for.
  6. The base order is sized so that BOTH the worst-case loss (every level
     fills, then the stop) and the full-ladder notional stay under fixed
     fractions of equity. A volatility gate refuses to open a cycle in
     extreme conditions.

Frequency: with BTC's intraday range the base order fills within seconds of
being flat, so the strategy is active daily. How often a cycle *closes*
depends on the market; the time decay guarantees it is bounded.

Restart safety: the plan, every order and every fill are persisted. On start
the engine applies anything that filled while it was away, then reconciles
its record against the broker's position and either resumes the cycle or, if
they disagree, closes it and flattens. Resting orders stay at the broker
across restarts by design - a redeploy should not force an exit at whatever
price the market happens to be.

Known limitation, stated plainly: the hard stop is enforced in software on
each poll. If the process is down, it cannot fire; the first thing a restart
does is check price against the stop. Broker-side OCO is not available for
crypto on Alpaca.

Everything here is paper-only. main.py refuses a key that is not a paper key.
"""

import json
import threading
import time

from indicators import atr as compute_atr
from ladder import average_entry, plan_cycle, plan_from_dict, tp_pct_for_age
from runtime import log

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}
ENTRY_KINDS = ("base", "level")

# Below this notional a leftover position is dust: not worth an order the venue
# may reject for being under its minimum, and worth far less than a cent.
DUST_NOTIONAL_USD = 1.0


def _f(value, default=0.0):
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def find_position(api, symbol):
    """Return the raw position record, or None. Alpaca reports crypto positions
    without the slash (BTCUSD, not BTC/USD), so match on the normalised form and
    hand back whatever spelling the API used - the close endpoint expects it."""
    wanted = symbol.replace("/", "")
    for pos in api.positions():
        if pos.get("symbol", "").replace("/", "") == wanted:
            return pos
    return None


def await_terminal(api, order_id, timeout, sleep=time.sleep):
    """Poll an order until it reaches a terminal state or the timeout expires."""
    deadline = time.time() + timeout
    order = api.get_order(order_id)
    while order.get("status") not in TERMINAL and time.time() < deadline:
        sleep(1)
        order = api.get_order(order_id)
    return order


class MartingaleEngine:
    def __init__(self, api, store, config, clock=None):
        self.api = api
        self.store = store
        self.cfg = config
        self.symbol = config["symbol"]
        self.grid = config["grid"]
        self.risk = config["risk"]
        self.exit = config["exit"]
        self.bars_cfg = config["bars"]
        self.fee = float(config.get("est_fee_pct", 0.0025))
        self.clock = clock or time.time

        self.cycle = None            # open cycle row from the store
        self.plan = None             # ladder.CyclePlan for that cycle
        self.fills = []              # (qty, price) of filled entry orders, ordered-qty basis
        self.tp_order_id = None
        self.tp_pct_current = None
        self.tp_mode = "limit"       # "limit" = resting sell at the venue; "software" = checked each poll
        self.cooldown_until_ms = 0

        self._bars = None
        self._bars_at = 0
        self._polls = 0
        self.last_price = None
        self.last_atr = None
        self.last_atr_pct = None

        self._lock = threading.Lock()
        self._counters = {
            "cycles_opened": 0, "cycles_closed": 0, "tp_hits": 0, "stops": 0, "timeouts": 0,
            "levels_filled": 0, "skipped_vol_gate": 0, "skipped_sizing": 0,
            "software_tp_fallbacks": 0, "errors": 0,
        }

    # --- helpers ------------------------------------------------------------

    def now_ms(self):
        return int(self.clock() * 1000)

    def _count(self, key, n=1):
        with self._lock:
            self._counters[key] += n

    def bars(self):
        now = self.now_ms()
        if self._bars is None or now - self._bars_at > int(self.bars_cfg["refresh_seconds"]) * 1000:
            self._bars = self.api.fetch_bars(self.symbol, self.bars_cfg["timeframe"], int(self.bars_cfg["lookback"]))
            self._bars_at = now
        return self._bars

    def price(self):
        quote = self.api.latest_quote(self.symbol)
        p = quote.get("mid") if quote else None
        if not p:
            b = self.bars()
            p = _f(b[-1]["c"]) if b else None
        self.last_price = p
        return p

    def volatility(self, price):
        """ATR with a floor: in a dead-calm market a raw ATR would pack the
        ladder into a few dollars and every wobble would fill every level."""
        raw = compute_atr(self.bars(), int(self.cfg["atr_period"]))
        if raw is None or not price:
            return None, None
        floor = float(self.risk["vol_floor_atr_pct"]) * price
        eff = max(raw, floor)
        self.last_atr, self.last_atr_pct = eff, eff / price
        return eff, eff / price

    def _position_qty(self):
        pos = find_position(self.api, self.symbol)
        return (_f(pos.get("qty")) if pos else 0.0), pos

    def _expected_position(self):
        return sum(q for q, _ in self.fills) * (1.0 - self.fee)

    def _refresh_cycle(self):
        self.cycle = self.store.get_open_cycle()

    def _tp_price(self, tp_pct):
        avg = average_entry(self.fills)
        return round(avg * (1.0 + tp_pct), 2) if avg and tp_pct is not None else None

    # --- startup ------------------------------------------------------------

    def reconcile_on_start(self):
        """Never trust local state after a restart - ask the broker, then decide."""
        open_cycle = self.store.get_open_cycle()
        pos_qty, _ = self._position_qty()
        broker_open = self.api.open_orders()

        if open_cycle is None:
            if pos_qty > 0 or broker_open:
                log("warn", "Position or resting orders found with no open cycle - flattening",
                    position_qty=pos_qty, open_orders=len(broker_open))
                self._flatten_everything()
            else:
                log("info", "Reconciled with broker: flat, no open cycle")
            return

        self.cycle = open_cycle
        self.plan = plan_from_dict(json.loads(open_cycle["plan_json"]))
        self.fills = [
            (_f(o["filled_qty"]), _f(o["filled_avg_price"]))
            for o in self.store.orders_for_cycle(open_cycle["id"], kinds=ENTRY_KINDS)
            if _f(o["filled_qty"]) > 0 and _f(o["filled_avg_price"]) > 0
        ]
        tp_rows = self.store.orders_for_cycle(open_cycle["id"], kinds=("tp",), statuses=("open",))
        self.tp_order_id = tp_rows[-1]["id"] if tp_rows else None
        self.tp_mode = "limit"
        if self.tp_order_id and self.fills:
            avg = average_entry(self.fills)
            self.tp_pct_current = _f(tp_rows[-1]["limit_price"]) / avg - 1.0 if avg else None

        # Anything that filled while we were away is applied first - a
        # take-profit that hit during the outage closes the cycle properly.
        if self._sync_orders():
            return

        pos_qty, _ = self._position_qty()
        expected = self._expected_position()
        if not self.fills or pos_qty <= 0 or expected <= 0 or abs(pos_qty - expected) / expected > 0.02:
            log("warn", "Open cycle does not match the broker position - closing it and flattening",
                cycle_id=open_cycle["id"], position_qty=pos_qty, expected_qty=expected)
            self._finish_cycle("closed_reconcile", "startup: position did not match recorded fills")
            return

        log("info", "Resumed open cycle", cycle_id=open_cycle["id"], filled_levels=len(self.fills),
            avg_entry=average_entry(self.fills), position_qty=pos_qty,
            tp_order=self.tp_order_id is not None, stop_price=self.plan.stop_price)

    # --- one poll -----------------------------------------------------------

    def step(self):
        """One poll. Exceptions are logged and counted rather than allowed to
        kill the process - but they are counted, and the engine tests assert
        the count is zero, so a swallowed error cannot pass unnoticed."""
        self._polls += 1
        try:
            if self.cycle is None:
                self._maybe_open()
            else:
                self._manage()
        except Exception as exc:
            self._count("errors")
            log("error", "Engine step failed", error=str(exc), cycle_id=self.cycle["id"] if self.cycle else None)
        try:
            self._record_markouts()
        except Exception as exc:
            log("warn", "Markout recording failed", error=str(exc))

    # --- opening ------------------------------------------------------------

    def _maybe_open(self):
        now = self.now_ms()
        if now < self.cooldown_until_ms:
            return
        price = self.price()
        atr_eff, atr_pct = self.volatility(price)
        if price is None or atr_eff is None:
            log("warn", "No price or ATR available yet - not opening")
            return
        if atr_pct > float(self.risk["vol_gate_max_atr_pct"]):
            self._count("skipped_vol_gate")
            log("info", "Volatility gate closed - not opening a cycle",
                atr_pct=round(atr_pct, 5), gate=self.risk["vol_gate_max_atr_pct"])
            return

        equity = _f(self.api.account().get("equity"))
        try:
            plan = plan_cycle(equity, price, atr_eff, self.grid, self.risk, self.exit,
                              float(self.cfg["qty_step"]), float(self.cfg["min_order_notional_usd"]))
        except ValueError as exc:
            self._count("skipped_sizing")
            log("warn", "Cannot size a ladder - not opening", error=str(exc), equity=equity)
            return

        base = plan.levels[0]
        order = self.api.submit_market_order(self.symbol, base.qty, "buy")
        order = await_terminal(self.api, order["id"], float(self.cfg["fill_timeout_seconds"]))
        if order.get("status") != "filled":
            log("error", "Base order did not fill - no cycle opened",
                order_id=order.get("id"), status=order.get("status"))
            try:
                self.api.cancel_order(order["id"])
            except Exception:
                pass
            return

        fq, fp = _f(order.get("filled_qty")), _f(order.get("filled_avg_price"))
        cycle_id = self.store.open_cycle(self.symbol, plan, now)
        self.store.add_order({
            "id": order["id"], "cycle_id": cycle_id, "kind": "base", "level_index": 0,
            "side": "buy", "qty": base.qty, "limit_price": None, "status": "filled",
            "filled_qty": fq, "filled_avg_price": fp, "submitted_ts": now, "filled_ts": now,
        })
        self.fills = [(fq, fp)]
        self.tp_mode = "limit"

        for lvl in plan.levels[1:]:
            o = self.api.submit_limit_order(self.symbol, lvl.qty, "buy", lvl.price)
            self.store.add_order({
                "id": o["id"], "cycle_id": cycle_id, "kind": "level", "level_index": lvl.index,
                "side": "buy", "qty": lvl.qty, "limit_price": lvl.price, "status": "open",
                "submitted_ts": now,
            })

        self.plan = plan
        self.store.update_cycle(cycle_id, filled_levels=1, avg_entry=fp, position_qty=fq * (1.0 - self.fee))
        self._refresh_cycle()
        self._place_tp(plan.tp_pct)
        self._count("cycles_opened")
        log("info", "Cycle opened", cycle_id=cycle_id, reference_price=price, atr=round(atr_eff, 2),
            atr_pct=round(atr_pct, 5), base_qty=base.qty, base_fill=fp, safety_levels=len(plan.levels) - 1,
            lowest_level=round(plan.levels[-1].price, 2), stop_price=round(plan.stop_price, 2),
            tp_pct=round(plan.tp_pct, 5), tp_mode=self.tp_mode, total_notional=round(plan.total_notional, 2),
            worst_case_loss=round(plan.worst_case_loss, 2), binding=plan.binding)

    # --- managing -----------------------------------------------------------

    def _manage(self):
        if self._sync_orders():
            return

        price = self.price()
        if price is None:
            return
        now = self.now_ms()

        if price <= self.plan.stop_price:
            self._count("stops")
            log("warn", "Hard stop hit", cycle_id=self.cycle["id"], price=price, stop_price=self.plan.stop_price)
            self._finish_cycle("closed_stop", f"price {price:.2f} <= stop {self.plan.stop_price:.2f}")
            return

        age_h = (now - int(self.cycle["opened_ts"])) / 3_600_000
        if age_h >= float(self.exit["max_hold_hours"]):
            self._count("timeouts")
            log("warn", "Max hold reached - exiting at market", cycle_id=self.cycle["id"], age_hours=round(age_h, 2))
            self._finish_cycle("closed_timeout", f"held {age_h:.1f}h >= {self.exit['max_hold_hours']}h")
            return

        # Occasional drift check against the broker - cheap insurance against a
        # fill we somehow missed. Every 15th poll (~5 min at 20s).
        if self._polls % 15 == 0 and self.fills:
            pos_qty, _ = self._position_qty()
            expected = self._expected_position()
            if expected > 0 and abs(pos_qty - expected) / expected > 0.05:
                log("warn", "Position drift versus recorded fills", position_qty=pos_qty, expected_qty=expected)

        target = tp_pct_for_age(age_h, self.plan.tp_pct, float(self.exit["breakeven_pct"]),
                                float(self.exit["decay_start_hours"]), float(self.exit["max_hold_hours"]))

        if self.tp_mode == "software":
            self.tp_pct_current = target
            tp_price = self._tp_price(target)
            if tp_price is not None and price >= tp_price:
                log("info", "Software take-profit reached", cycle_id=self.cycle["id"], price=price, tp_price=tp_price)
                self._finish_cycle("closed_tp", f"software take-profit: {price:.2f} >= {tp_price:.2f}")
            return

        if (self.tp_order_id is None or self.tp_pct_current is None
                or abs(target - self.tp_pct_current) >= float(self.exit["tp_replace_threshold_pct"])):
            if self._cancel_tp():
                self._finish_cycle("closed_tp", "take profit filled while being replaced")
                return
            self._place_tp(target)

    def _sync_orders(self):
        """Apply everything that changed at the broker since the last poll.
        Returns True if the cycle was closed as a result."""
        cid = self.cycle["id"]
        broker_open_ids = {o["id"] for o in self.api.open_orders()}
        now = self.now_ms()
        newly_filled, tp_filled = [], False

        for o in self.store.orders_for_cycle(cid, statuses=("open",)):
            if o["id"] in broker_open_ids:
                continue
            final = self.api.get_order(o["id"])
            status = final.get("status")
            if status not in TERMINAL:
                continue  # transient state; look again next poll
            fq, fp = _f(final.get("filled_qty")), _f(final.get("filled_avg_price"))
            self.store.update_order(o["id"], status, fq or None, fp or None, now if fq > 0 else None)
            if o["kind"] in ENTRY_KINDS:
                if fq > 0:
                    newly_filled.append((o, fq, fp))
            elif o["kind"] == "tp":
                if status == "filled":
                    tp_filled = True
                else:
                    self.tp_order_id = None  # vanished - re-placed below

        for o, fq, fp in newly_filled:
            self.fills.append((fq, fp))
            self._count("levels_filled")
            log("info", "Level filled", cycle_id=cid, level_index=o["level_index"], qty=fq, price=fp,
                avg_entry=round(average_entry(self.fills), 2), filled_levels=len(self.fills))
        if newly_filled:
            self.store.update_cycle(cid, filled_levels=len(self.fills),
                                    avg_entry=average_entry(self.fills),
                                    position_qty=self._expected_position())

        if tp_filled:
            # Whatever the take-profit did not sell - levels that filled after it
            # was placed - is sold at market inside _finish_cycle.
            self._finish_cycle("closed_tp", "take profit")
            return True

        if newly_filled and self.tp_mode == "limit":
            if self._cancel_tp():
                self._finish_cycle("closed_tp", "take profit filled while being replaced")
                return True
            self._place_tp(self.tp_pct_current if self.tp_pct_current is not None else self.plan.tp_pct)
        return False

    # --- take profit --------------------------------------------------------

    def _place_tp(self, tp_pct):
        """Rest a limit sell for the whole position at average * (1 + tp).

        If the venue refuses - Alpaca's wash-trade check can reject a resting
        sell while resting buys are open on the same symbol - fall back to a
        software take-profit for the rest of this cycle: the target is checked
        every poll and the position is sold at market when price reaches it.
        A handled fallback, not an error."""
        pos_qty, _ = self._position_qty()
        avg = average_entry(self.fills)
        if pos_qty <= 0 or not avg:
            log("error", "Cannot place take-profit", position_qty=pos_qty, avg_entry=avg)
            return
        limit = round(avg * (1.0 + tp_pct), 2)
        try:
            o = self.api.submit_limit_order(self.symbol, pos_qty, "sell", limit)
        except RuntimeError as exc:
            self.tp_mode = "software"
            self.tp_order_id = None
            self.tp_pct_current = tp_pct
            self._count("software_tp_fallbacks")
            log("warn", "Venue refused the resting take-profit - enforcing it in software for this cycle",
                cycle_id=self.cycle["id"], tp_price=limit, tp_pct=round(tp_pct, 5), error=str(exc))
            return
        self.store.add_order({
            "id": o["id"], "cycle_id": self.cycle["id"], "kind": "tp", "level_index": None,
            "side": "sell", "qty": pos_qty, "limit_price": limit, "status": "open",
            "submitted_ts": self.now_ms(),
        })
        self.tp_order_id = o["id"]
        self.tp_pct_current = tp_pct
        log("info", "Take-profit placed", cycle_id=self.cycle["id"], qty=pos_qty,
            avg_entry=round(avg, 2), limit_price=limit, tp_pct=round(tp_pct, 5))

    def _cancel_tp(self):
        """Cancel the resting take-profit. Returns True if it turns out to have
        filled in the meantime, so the caller can close the cycle."""
        if not self.tp_order_id:
            return False
        oid = self.tp_order_id
        try:
            self.api.cancel_order(oid)
        except Exception as exc:
            log("warn", "Cancel take-profit failed", order_id=oid, error=str(exc))
        final = self.api.get_order(oid)
        status = final.get("status", "canceled")
        fq, fp = _f(final.get("filled_qty")), _f(final.get("filled_avg_price"))
        self.store.update_order(oid, status if status in TERMINAL else "canceled",
                                fq or None, fp or None, self.now_ms() if fq > 0 else None)
        self.tp_order_id = None
        return status == "filled"

    # --- closing ------------------------------------------------------------

    def _cancel_remaining(self, cid):
        """Cancel every order of this cycle still resting at the broker, and
        record any partial fill that happened on the way out - it cost money."""
        now = self.now_ms()
        for o in self.store.orders_for_cycle(cid, statuses=("open",)):
            try:
                self.api.cancel_order(o["id"])
            except Exception as exc:
                log("warn", "Cancel failed", order_id=o["id"], error=str(exc))
            final = self.api.get_order(o["id"])
            st = final.get("status", "canceled")
            fq, fp = _f(final.get("filled_qty")), _f(final.get("filled_avg_price"))
            self.store.update_order(o["id"], st if st in TERMINAL else "canceled",
                                    fq or None, fp or None, now if fq > 0 else None)
            if o["kind"] in ENTRY_KINDS and fq > 0 and (fq, fp) not in self.fills:
                self.fills.append((fq, fp))
        self.tp_order_id = None

    def _finish_cycle(self, status, reason):
        """The one way a cycle ends: cancel what is still resting, sell what is
        still held, then settle from the ledger."""
        cid = self.cycle["id"]
        self._cancel_remaining(cid)

        pos_qty, pos = self._position_qty()
        if pos_qty > 0 and pos is not None:
            notional = pos_qty * (self.last_price or 0.0)
            if self.last_price and notional < DUST_NOTIONAL_USD:
                log("info", "Dust below the venue minimum left in the account", cycle_id=cid,
                    qty=pos_qty, notional_usd=round(notional, 4))
            else:
                order = self.api.close_position(pos["symbol"])
                order = await_terminal(self.api, order["id"], float(self.cfg["fill_timeout_seconds"]))
                fq, fp = _f(order.get("filled_qty")), _f(order.get("filled_avg_price"))
                self.store.add_order({
                    "id": order["id"], "cycle_id": cid, "kind": "exit", "level_index": None,
                    "side": "sell", "qty": pos_qty, "limit_price": None,
                    "status": order.get("status", "filled"),
                    "filled_qty": fq or None, "filled_avg_price": fp or None,
                    "submitted_ts": self.now_ms(), "filled_ts": self.now_ms() if fq > 0 else None,
                })
                log("info", "Remaining position sold at market", cycle_id=cid, qty=pos_qty,
                    price=fp or None, status=order.get("status"), reason=reason)

        self._close_cycle(status, reason)

    def _close_cycle(self, status, reason):
        """Settle from the order ledger: every filled buy against every filled
        sell. Value in versus value out net of the estimated sell fee; the buy
        fee is already in the quantity, because we sold less than we bought."""
        now = self.now_ms()
        cid = self.cycle["id"]
        orders = self.store.orders_for_cycle(cid)

        buys = [(_f(o["filled_qty"]), _f(o["filled_avg_price"])) for o in orders
                if o["side"] == "buy" and _f(o["filled_qty"]) > 0 and _f(o["filled_avg_price"]) > 0]
        sells = [(o["id"], _f(o["filled_qty"]), _f(o["filled_avg_price"])) for o in orders
                 if o["side"] == "sell" and _f(o["filled_qty"]) > 0 and _f(o["filled_avg_price"]) > 0]

        cost = sum(q * p for q, p in buys)
        gross = sum(q * p for _, q, p in sells)
        exit_qty = sum(q for _, q, _ in sells)
        exit_price = gross / exit_qty if exit_qty > 0 else None
        proceeds = gross * (1.0 - self.fee)
        pnl = round(proceeds - cost, 6) if sells else None
        avg = average_entry(buys)

        self.store.close_cycle(cid, status, now, exit_price, exit_qty, pnl, reason, avg, len(buys))
        base_id = next((o["id"] for o in orders if o["kind"] == "base"), None)
        self.store.record_round_trip(self.symbol, exit_qty, base_id, avg,
                                     sells[-1][0] if sells else None, exit_price, pnl,
                                     opened_ms=int(self.cycle["opened_ts"]))

        held_h = (now - int(self.cycle["opened_ts"])) / 3_600_000
        self._count("cycles_closed")
        if status == "closed_tp":
            self._count("tp_hits")
        log("info", "Cycle closed", cycle_id=cid, status=status, reason=reason,
            filled_levels=len(buys), avg_entry=round(avg, 2) if avg else None,
            exit_qty=exit_qty, exit_price=round(exit_price, 2) if exit_price else None,
            pnl=pnl, held_hours=round(held_h, 2))

        cooldown_min = {
            "closed_stop": self.risk.get("cooldown_minutes_after_stop", 120),
            "closed_timeout": self.risk.get("cooldown_minutes_after_timeout", 30),
            "closed_tp": self.risk.get("cooldown_minutes_after_tp", 0),
            "closed_reconcile": self.risk.get("cooldown_minutes_after_timeout", 30),
        }.get(status, 0)
        self.cooldown_until_ms = now + int(float(cooldown_min) * 60_000)

        self.cycle, self.plan, self.fills = None, None, []
        self.tp_order_id, self.tp_pct_current, self.tp_mode = None, None, "limit"

    def _flatten_everything(self):
        """Startup only: no cycle on record, but the broker has something."""
        self.api.cancel_all_orders()
        pos_qty, pos = self._position_qty()
        if pos_qty > 0 and pos is not None:
            order = self.api.close_position(pos["symbol"])
            order = await_terminal(self.api, order["id"], float(self.cfg["fill_timeout_seconds"]))
            log("info", "Flattened leftover position", qty=pos_qty, status=order.get("status"),
                price=_f(order.get("filled_avg_price")) or None)

    # --- markouts -----------------------------------------------------------

    def _record_markouts(self):
        horizons = self.cfg.get("markout_horizons_seconds") or []
        if not horizons:
            return
        now = self.now_ms()
        pending = self.store.pending_markouts(horizons, now)
        if not pending:
            return
        price = self.price()  # always fresh - a stale price makes a markout meaningless
        if not price:
            return
        for order_id, horizon, side, fill_price in pending:
            bps = (price / fill_price - 1.0) * 10_000
            if side == "sell":
                bps = -bps  # after a sell, a falling price is the favourable outcome
            self.store.add_markout(order_id, horizon, side, fill_price, price, round(bps, 3), now)

    # --- status -------------------------------------------------------------

    def status(self):
        with self._lock:
            counters = dict(self._counters)
        cycle = None
        if self.cycle and self.plan:
            avg = average_entry(self.fills)
            age_h = (self.now_ms() - int(self.cycle["opened_ts"])) / 3_600_000
            cycle = {
                "id": self.cycle["id"],
                "opened_ts": self.cycle["opened_ts"],
                "age_hours": round(age_h, 3),
                "filled_levels": len(self.fills),
                "safety_levels": len(self.plan.levels) - 1,
                "avg_entry": avg,
                "position_qty_est": self._expected_position(),
                "tp_mode": self.tp_mode,
                "tp_pct_current": self.tp_pct_current,
                "tp_price": self._tp_price(self.tp_pct_current),
                "stop_price": self.plan.stop_price,
                "reference_price": self.plan.reference_price,
                "atr": self.plan.atr,
                "atr_pct": self.plan.atr_pct,
                "total_notional_planned": self.plan.total_notional,
                "worst_case_loss": self.plan.worst_case_loss,
                "binding": self.plan.binding,
                "next_level_price": self.plan.levels[len(self.fills)].price if len(self.fills) < len(self.plan.levels) else None,
            }
        return {
            "mode": "martingale-long",
            "symbol": self.symbol,
            "cycle": cycle,
            "cooldown_until": self.cooldown_until_ms if self.cooldown_until_ms > self.now_ms() else None,
            "last_price": self.last_price,
            "last_atr": self.last_atr,
            "last_atr_pct": self.last_atr_pct,
            "counters": counters,
        }
