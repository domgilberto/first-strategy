"""
Engine tests against a simulated broker.

FakeBroker models the parts of Alpaca that matter for correctness: market
orders fill at the current price, resting limits fill when price crosses them
(at the limit), buys are charged the fee in quantity and sells in cash, and the
position is whatever those fills add up to. `tick(price)` moves the market and
fills whatever should fill. A FakeClock lets the tests age a cycle without
waiting.

Each scenario is one of the ways a cycle can end, plus the two things that
must happen on startup. Every scenario also asserts that the engine swallowed
zero exceptions: step() catches errors to protect the process, so without this
assertion a broken code path could pass by simply not doing anything.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import Store  # noqa: E402
from strategy import MartingaleEngine  # noqa: E402

CONFIG = {
    "symbol": "BTC/USD",
    "poll_seconds": 20,
    "fill_timeout_seconds": 1,
    "snapshot_seconds": 300,
    "api_max_points": 2000,
    "min_order_notional_usd": 10,
    "qty_step": 0.000001,
    "est_fee_pct": 0.0025,
    "atr_period": 14,
    "bars": {"timeframe": "1Hour", "lookback": 50, "refresh_seconds": 300},
    "grid": {"max_levels": 6, "spacing_atr": 0.6, "spacing_scale": 1.2,
             "volume_scale": 1.5, "stop_atr_below_last": 1.5},
    "risk": {"max_cycle_loss_pct": 0.02, "max_exposure_pct": 0.40,
             "vol_gate_max_atr_pct": 0.03, "vol_floor_atr_pct": 0.002,
             "cooldown_minutes_after_stop": 120, "cooldown_minutes_after_timeout": 30,
             "cooldown_minutes_after_tp": 0},
    "exit": {"tp_atr_mult": 1.0, "cost_floor_pct": 0.008, "breakeven_pct": 0.0055,
             "decay_start_hours": 12, "max_hold_hours": 36, "tp_replace_threshold_pct": 0.0005},
    "markout_horizons_seconds": [300, 900, 3600],
}

FEE = 0.0025


class FakeClock:
    def __init__(self, t=1_789_800_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeBroker:
    def __init__(self, price, atr_range=450.0, cash=100_000.0):
        self.price = price
        self.atr_range = atr_range
        self.cash = cash
        self.qty = 0.0
        self.orders = {}
        self._seq = 0

    # -- market data --
    def fetch_bars(self, symbol, timeframe, limit):
        half = self.atr_range / 2
        return [{"t": i, "o": self.price, "h": self.price + half, "l": self.price - half,
                 "c": self.price, "v": 1} for i in range(limit)]

    def latest_quote(self, symbol):
        return {"ap": self.price, "bp": self.price, "mid": self.price, "t": None}

    # -- account --
    def account(self):
        return {"equity": str(self.cash + self.qty * self.price), "cash": str(self.cash),
                "buying_power": str(self.cash * 4), "long_market_value": str(self.qty * self.price)}

    def positions(self):
        return [{"symbol": "BTCUSD", "qty": f"{self.qty:.9f}"}] if self.qty > 1e-12 else []

    # -- orders --
    def _new(self, symbol, qty, side, kind, limit):
        self._seq += 1
        o = {"id": f"o{self._seq}", "symbol": symbol, "qty": str(qty), "side": side, "type": kind,
             "limit_price": None if limit is None else str(limit), "status": "new",
             "filled_qty": "0", "filled_avg_price": None}
        self.orders[o["id"]] = o
        return o

    def _fill(self, o, px):
        q = float(o["qty"])
        if o["side"] == "buy":
            self.cash -= q * px
            self.qty += q * (1 - FEE)           # Alpaca takes the buy fee in quantity
        else:
            q = min(q, self.qty)
            self.qty -= q
            self.cash += q * px * (1 - FEE)     # and the sell fee in cash
            o["qty"] = str(q)
        o["status"], o["filled_qty"], o["filled_avg_price"] = "filled", str(q), str(px)

    def _try_fill(self, o):
        if o["status"] != "new" or o["type"] != "limit":
            return
        lp = float(o["limit_price"])
        if o["side"] == "buy" and self.price <= lp:
            self._fill(o, lp)
        elif o["side"] == "sell" and self.price >= lp:
            self._fill(o, lp)

    def tick(self, price):
        self.price = price
        for o in list(self.orders.values()):
            self._try_fill(o)

    def submit_market_order(self, symbol, qty, side):
        o = self._new(symbol, qty, side, "market", None)
        self._fill(o, self.price)
        return dict(o)

    def submit_limit_order(self, symbol, qty, side, limit_price):
        o = self._new(symbol, qty, side, "limit", limit_price)
        self._try_fill(o)
        return dict(o)

    def open_orders(self):
        return [dict(o) for o in self.orders.values() if o["status"] == "new"]

    def get_order(self, oid):
        return dict(self.orders[oid])

    def cancel_order(self, oid):
        if self.orders[oid]["status"] == "new":
            self.orders[oid]["status"] = "canceled"

    def cancel_all_orders(self):
        for o in self.orders.values():
            if o["status"] == "new":
                o["status"] = "canceled"

    def close_position(self, symbol):
        o = self._new(symbol, self.qty, "sell", "market", None)
        self._fill(o, self.price)
        return dict(o)

    def activities(self, *args, **kwargs):
        return []


def make_engine(broker, store=None, clock=None):
    store = store or Store(tempfile.mkdtemp())
    clock = clock or FakeClock()
    eng = MartingaleEngine(broker, store, CONFIG, clock=clock)
    return eng, store, clock


class EngineCase(unittest.TestCase):
    def assertClean(self, eng):
        self.assertEqual(eng.status()["counters"]["errors"], 0, "engine swallowed an exception")

    def assertFlat(self, broker):
        self.assertLess(broker.qty, 1e-9, "position should be flat")
        self.assertEqual(broker.open_orders(), [], "nothing should still be resting")


class OpeningTests(EngineCase):
    def test_opens_a_cycle_with_base_fill_levels_and_tp(self):
        broker = FakeBroker(81_000.0)
        eng, store, _ = make_engine(broker)
        eng.reconcile_on_start()
        eng.step()

        self.assertIsNotNone(eng.cycle)
        self.assertEqual(len(eng.fills), 1)
        plan = eng.plan
        self.assertEqual(len(plan.levels), 7)
        # 6 resting buys + 1 resting take-profit
        open_orders = broker.open_orders()
        self.assertEqual(sum(1 for o in open_orders if o["side"] == "buy"), 6)
        self.assertEqual(sum(1 for o in open_orders if o["side"] == "sell"), 1)
        # Position equals base qty less the fee; the TP is for exactly that.
        tp = next(o for o in open_orders if o["side"] == "sell")
        self.assertAlmostEqual(float(tp["qty"]), broker.qty, places=6)
        self.assertAlmostEqual(float(tp["limit_price"]), round(81_000.0 * (1 + plan.tp_pct), 2), places=2)
        # Caps hold.
        self.assertLessEqual(plan.total_notional, 0.40 * 100_000 + 1)
        self.assertLessEqual(plan.worst_case_loss, 0.02 * 100_000 + 1)
        self.assertEqual(eng.status()["counters"]["cycles_opened"], 1)
        self.assertEqual(store.get_open_cycle()["filled_levels"], 1)
        self.assertClean(eng)

    def test_volatility_gate_blocks_opening(self):
        broker = FakeBroker(81_000.0, atr_range=81_000.0 * 0.05)   # 5% ATR > 3% gate
        eng, _, _ = make_engine(broker)
        eng.reconcile_on_start()
        eng.step()
        self.assertIsNone(eng.cycle)
        self.assertEqual(broker.open_orders(), [])
        self.assertEqual(eng.status()["counters"]["skipped_vol_gate"], 1)
        self.assertClean(eng)

    def test_leftover_position_without_cycle_is_flattened_on_start(self):
        broker = FakeBroker(81_000.0)
        broker.qty = 0.01
        broker.submit_limit_order("BTC/USD", 0.001, "buy", 70_000.0)   # a stray resting order
        eng, store, _ = make_engine(broker)
        eng.reconcile_on_start()
        self.assertFlat(broker)
        self.assertIsNone(store.get_open_cycle())


class LifecycleTests(EngineCase):
    def _opened(self, price=81_000.0):
        broker = FakeBroker(price)
        eng, store, clock = make_engine(broker)
        eng.reconcile_on_start()
        eng.step()
        return broker, eng, store, clock

    def test_levels_fill_average_falls_and_tp_is_replaced(self):
        broker, eng, store, _ = self._opened()
        plan = eng.plan
        first_tp = eng.tp_order_id

        broker.tick(plan.levels[1].price - 1)   # through level 1
        eng.step()
        self.assertEqual(len(eng.fills), 2)
        self.assertNotEqual(eng.tp_order_id, first_tp)
        broker.tick(plan.levels[2].price - 1)   # through level 2
        eng.step()
        self.assertEqual(len(eng.fills), 3)

        avg = eng.status()["cycle"]["avg_entry"]
        self.assertLess(avg, plan.reference_price)
        tp = next(o for o in broker.open_orders() if o["side"] == "sell")
        self.assertAlmostEqual(float(tp["limit_price"]), round(avg * (1 + plan.tp_pct), 2), places=2)
        self.assertAlmostEqual(float(tp["qty"]), broker.qty, places=6)   # TP sized to the real position
        self.assertEqual(store.get_open_cycle()["filled_levels"], 3)
        self.assertEqual(eng.status()["counters"]["levels_filled"], 2)
        self.assertClean(eng)

    def test_take_profit_closes_cycle_with_positive_pnl(self):
        broker, eng, store, _ = self._opened()
        plan = eng.plan
        broker.tick(plan.levels[1].price - 1)
        eng.step()
        broker.tick(plan.levels[2].price - 1)
        eng.step()

        tp_price = float(next(o for o in broker.open_orders() if o["side"] == "sell")["limit_price"])
        broker.tick(tp_price + 50)
        eng.step()

        self.assertIsNone(eng.cycle)
        self.assertFlat(broker)                              # flat, and remaining levels cancelled
        cycles = store.recent_cycles(1)
        self.assertEqual(cycles[0]["status"], "closed_tp")
        self.assertEqual(cycles[0]["filled_levels"], 3)
        self.assertGreater(cycles[0]["pnl"], 0)              # tp above the cost floor -> net positive
        trips = store.recent_round_trips(1)
        self.assertEqual(len(trips), 1)
        self.assertIsNotNone(trips[0]["opened_ts"])
        self.assertAlmostEqual(trips[0]["pnl"], cycles[0]["pnl"])
        self.assertEqual(eng.status()["counters"]["tp_hits"], 1)
        self.assertClean(eng)
        # Fresh cycle opens on the next poll (no cooldown after a take-profit).
        eng.step()
        self.assertIsNotNone(eng.cycle)

    def test_level_and_take_profit_filling_between_polls_sells_the_remainder(self):
        """The race: a level fills after the TP was placed, then the TP (sized
        for the smaller position) fills - all before the engine polls. The
        cycle must still end flat, with the remainder sold and accounted."""
        broker, eng, store, _ = self._opened()
        plan = eng.plan
        tp_price = float(broker.get_order(eng.tp_order_id)["limit_price"])
        broker.tick(plan.levels[1].price - 1)   # level 1 fills; engine has not seen it
        broker.tick(tp_price + 10)              # TP for base-only qty fills too
        eng.step()

        self.assertIsNone(eng.cycle)
        self.assertFlat(broker)
        c = store.recent_cycles(1)[0]
        self.assertEqual(c["status"], "closed_tp")
        self.assertEqual(c["filled_levels"], 2)
        self.assertGreater(c["pnl"], 0)
        expected_sold = (plan.levels[0].qty + plan.levels[1].qty) * (1 - FEE)
        self.assertAlmostEqual(c["exit_qty"], expected_sold, places=6)
        orders = store.orders_for_cycle(c["id"])
        self.assertEqual(sum(1 for o in orders if o["kind"] == "exit"), 1)   # the remainder, at market
        self.assertEqual(sum(1 for o in orders if o["status"] == "open"), 0)
        self.assertClean(eng)

    def test_hard_stop_exits_everything_within_the_risk_cap(self):
        broker, eng, store, clock = self._opened()
        plan = eng.plan
        broker.tick(plan.stop_price - 1)     # straight through every level and the stop
        eng.step()

        self.assertIsNone(eng.cycle)
        self.assertFlat(broker)
        c = store.recent_cycles(1)[0]
        self.assertEqual(c["status"], "closed_stop")
        self.assertEqual(c["filled_levels"], 7)
        self.assertLess(c["pnl"], 0)
        # Loss is bounded by the planned worst case plus fees and a tick of slippage.
        self.assertGreaterEqual(c["pnl"], -(plan.worst_case_loss * 1.01 + plan.total_notional * FEE * 2.5))
        self.assertGreaterEqual(c["pnl"], -0.02 * 100_000 * 1.05)
        self.assertEqual(eng.status()["counters"]["stops"], 1)
        self.assertClean(eng)
        # Cooldown: nothing opens for two hours.
        eng.step()
        self.assertIsNone(eng.cycle)
        clock.advance(121 * 60)
        eng.step()
        self.assertIsNotNone(eng.cycle)
        self.assertClean(eng)

    def test_take_profit_decays_with_age(self):
        broker, eng, _, clock = self._opened()
        plan = eng.plan
        initial_tp = eng.tp_pct_current
        clock.advance(24 * 3600)              # halfway through the decay window
        eng.step()
        expected = (plan.tp_pct + CONFIG["exit"]["breakeven_pct"]) / 2
        self.assertAlmostEqual(eng.tp_pct_current, expected, places=6)
        self.assertLess(eng.tp_pct_current, initial_tp)
        tp = next(o for o in broker.open_orders() if o["side"] == "sell")
        self.assertAlmostEqual(float(tp["limit_price"]), round(81_000.0 * (1 + expected), 2), places=2)
        self.assertClean(eng)

    def test_max_hold_exits_at_market(self):
        broker, eng, store, clock = self._opened()
        clock.advance(37 * 3600)
        eng.step()
        self.assertIsNone(eng.cycle)
        self.assertFlat(broker)
        self.assertEqual(store.recent_cycles(1)[0]["status"], "closed_timeout")
        self.assertEqual(eng.status()["counters"]["timeouts"], 1)
        self.assertClean(eng)


class ResumeTests(EngineCase):
    def test_restart_resumes_open_cycle_and_then_takes_profit(self):
        broker = FakeBroker(81_000.0)
        eng, store, clock = make_engine(broker)
        eng.reconcile_on_start()
        eng.step()
        broker.tick(eng.plan.levels[1].price - 1)
        eng.step()
        tp_id = eng.tp_order_id
        self.assertEqual(len(eng.fills), 2)
        self.assertClean(eng)

        # "Restart": a fresh engine over the same store and the same broker.
        eng2 = MartingaleEngine(broker, store, CONFIG, clock=clock)
        eng2.reconcile_on_start()
        self.assertIsNotNone(eng2.cycle)
        self.assertEqual(eng2.cycle["id"], eng.cycle["id"])
        self.assertEqual(len(eng2.fills), 2)
        self.assertEqual(eng2.tp_order_id, tp_id)             # adopted the live TP, did not re-place
        self.assertEqual(len(broker.open_orders()), 6)         # 5 levels + 1 TP, untouched

        tp_price = float(broker.get_order(tp_id)["limit_price"])
        broker.tick(tp_price + 10)
        eng2.step()
        self.assertIsNone(eng2.cycle)
        self.assertFlat(broker)
        self.assertEqual(store.recent_cycles(1)[0]["status"], "closed_tp")
        self.assertClean(eng2)

    def test_take_profit_that_filled_during_downtime_closes_cycle_on_start(self):
        broker = FakeBroker(81_000.0)
        eng, store, clock = make_engine(broker)
        eng.reconcile_on_start()
        eng.step()
        tp_price = float(broker.get_order(eng.tp_order_id)["limit_price"])
        broker.tick(tp_price + 10)             # fills while the engine is "down"

        eng2 = MartingaleEngine(broker, store, CONFIG, clock=clock)
        eng2.reconcile_on_start()
        self.assertIsNone(eng2.cycle)
        c = store.recent_cycles(1)[0]
        self.assertEqual(c["status"], "closed_tp")           # a proper close with P&L, not a reconcile
        self.assertGreater(c["pnl"], 0)
        self.assertFlat(broker)                              # the five remaining levels were cancelled
        self.assertClean(eng2)

    def test_position_mismatch_on_start_closes_and_flattens(self):
        broker = FakeBroker(81_000.0)
        eng, store, clock = make_engine(broker)
        eng.reconcile_on_start()
        eng.step()
        broker.qty *= 0.5                    # someone sold half by hand
        eng2 = MartingaleEngine(broker, store, CONFIG, clock=clock)
        eng2.reconcile_on_start()
        self.assertIsNone(eng2.cycle)
        self.assertEqual(store.recent_cycles(1)[0]["status"], "closed_reconcile")
        self.assertFlat(broker)
        self.assertClean(eng2)


class SoftwareTakeProfitTests(EngineCase):
    def test_falls_back_to_software_tp_when_venue_refuses_a_resting_sell(self):
        """Alpaca's wash-trade check may reject a resting sell while resting
        buys are open on the same symbol. The engine must keep the cycle safe
        without it: hold the target, check it each poll, exit at market."""

        class NoRestingSells(FakeBroker):
            def submit_limit_order(self, symbol, qty, side, limit_price):
                if side == "sell":
                    raise RuntimeError("POST /v2/orders -> 403 potential wash trade detected")
                return super().submit_limit_order(symbol, qty, side, limit_price)

        broker = NoRestingSells(81_000.0)
        eng, store, _ = make_engine(broker)
        eng.reconcile_on_start()
        eng.step()

        self.assertIsNotNone(eng.cycle)
        self.assertEqual(eng.tp_mode, "software")
        self.assertIsNone(eng.tp_order_id)
        self.assertEqual(sum(1 for o in broker.open_orders() if o["side"] == "sell"), 0)
        self.assertEqual(sum(1 for o in broker.open_orders() if o["side"] == "buy"), 6)
        self.assertEqual(eng.status()["counters"]["software_tp_fallbacks"], 1)
        self.assertEqual(eng.status()["cycle"]["tp_mode"], "software")

        # Levels still fill and lower the average with no resting TP involved.
        broker.tick(eng.plan.levels[1].price - 1)
        eng.step()
        self.assertEqual(len(eng.fills), 2)
        self.assertClean(eng)

        # Not yet at target: nothing happens.
        avg = eng.status()["cycle"]["avg_entry"]
        target = avg * (1 + eng.plan.tp_pct)
        broker.tick(target - 5)
        eng.step()
        self.assertIsNotNone(eng.cycle)

        # At target: sold at market, levels cancelled, cycle closed as a take-profit.
        broker.tick(target + 5)
        eng.step()
        self.assertIsNone(eng.cycle)
        self.assertFlat(broker)
        c = store.recent_cycles(1)[0]
        self.assertEqual(c["status"], "closed_tp")
        self.assertGreater(c["pnl"], 0)
        self.assertClean(eng)   # a handled fallback is not an engine error

    def test_quantity_formatting_floors_never_rounds(self):
        from alpaca import fmt_qty
        self.assertEqual(fmt_qty(0.0156607500), "0.01566075")
        self.assertEqual(fmt_qty(0.0009975), "0.0009975")
        self.assertEqual(fmt_qty(0.0157), "0.0157")
        # A hair above the 9-dp grid is floored, so a sell never exceeds the position.
        self.assertEqual(fmt_qty(0.0156607509999), "0.01566075")
        self.assertEqual(fmt_qty(1.0), "1")
        self.assertEqual(fmt_qty(0), "0")


class MarkoutTests(EngineCase):
    def test_markouts_recorded_after_horizon_at_a_fresh_price(self):
        broker = FakeBroker(81_000.0)
        eng, store, clock = make_engine(broker)
        eng.reconcile_on_start()
        eng.step()
        self.assertEqual(store.recent_markouts(10), [])
        clock.advance(301)
        broker.tick(81_000.0 * 1.01)          # also fills the TP - the cycle closes in this step
        eng.step()
        marks = [m for m in store.recent_markouts(10) if m["side"] == "buy"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["horizon_s"], 300)
        self.assertAlmostEqual(marks[0]["markout_bps"], 100.0, places=3)   # +1% after our buy
        self.assertClean(eng)


if __name__ == "__main__":
    unittest.main(verbosity=2)
