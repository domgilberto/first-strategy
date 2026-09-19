"""
Tests for the ladder geometry, sizing and exit maths, and for ATR.

These are the properties that keep an averaging-down strategy bounded. If any
of them fails, the engine must not trade.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indicators import atr, true_range  # noqa: E402
from ladder import (  # noqa: E402
    average_entry,
    floor_to_step,
    level_multipliers,
    level_prices,
    plan_cycle,
    size_base_qty,
    stop_price_for,
    take_profit_pct,
    tp_pct_for_age,
)

GRID = {"max_levels": 6, "spacing_atr": 0.6, "spacing_scale": 1.2,
        "volume_scale": 1.5, "stop_atr_below_last": 1.5}
RISK = {"max_cycle_loss_pct": 0.02, "max_exposure_pct": 0.40}
EXIT = {"tp_atr_mult": 1.0, "cost_floor_pct": 0.008}

EQUITY, PRICE, ATR = 100_000.0, 81_000.0, 450.0


class GeometryTests(unittest.TestCase):
    def test_levels_descend_with_widening_gaps(self):
        prices = level_prices(PRICE, ATR, 6, 0.6, 1.2)
        self.assertEqual(len(prices), 6)
        self.assertTrue(all(b < a for a, b in zip([PRICE] + prices, prices)))
        gaps = [a - b for a, b in zip([PRICE] + prices, prices)]
        self.assertTrue(all(g2 > g1 for g1, g2 in zip(gaps, gaps[1:])))
        self.assertAlmostEqual(gaps[0], 0.6 * ATR)
        self.assertAlmostEqual(gaps[1], 0.6 * ATR * 1.2)

    def test_multipliers_grow_geometrically_from_one(self):
        m = level_multipliers(3, 1.5)
        self.assertEqual(m, [1.0, 1.5, 2.25, 3.375])

    def test_stop_sits_below_last_level(self):
        prices = level_prices(PRICE, ATR, 6, 0.6, 1.2)
        stop = stop_price_for(prices, PRICE, ATR, 1.5)
        self.assertAlmostEqual(stop, prices[-1] - 1.5 * ATR)
        self.assertLess(stop, prices[-1])


class SizingTests(unittest.TestCase):
    def _ladder(self):
        prices = level_prices(PRICE, ATR, GRID["max_levels"], GRID["spacing_atr"], GRID["spacing_scale"])
        mults = level_multipliers(GRID["max_levels"], GRID["volume_scale"])
        stop = stop_price_for(prices, PRICE, ATR, GRID["stop_atr_below_last"])
        return [PRICE] + prices, mults, stop

    def test_both_caps_respected_and_binding_named(self):
        entries, mults, stop = self._ladder()
        q, binding = size_base_qty(EQUITY, entries, mults, stop, 0.02, 0.40, 1e-6)
        worst = sum(m * q * (e - stop) for m, e in zip(mults, entries))
        notional = sum(m * q * e for m, e in zip(mults, entries))
        self.assertLessEqual(worst, 0.02 * EQUITY + 1e-6)
        self.assertLessEqual(notional, 0.40 * EQUITY + 1e-6)
        self.assertIn(binding, ("risk", "exposure"))
        # With these numbers the full ladder is ~32x the base, so exposure binds.
        self.assertEqual(binding, "exposure")
        self.assertAlmostEqual(notional, 0.40 * EQUITY, delta=PRICE * 1e-6 * sum(mults))

    def test_risk_binds_when_exposure_cap_is_loose(self):
        entries, mults, stop = self._ladder()
        q, binding = size_base_qty(EQUITY, entries, mults, stop, 0.005, 5.0, 1e-6)
        self.assertEqual(binding, "risk")
        worst = sum(m * q * (e - stop) for m, e in zip(mults, entries))
        self.assertLessEqual(worst, 0.005 * EQUITY + 1e-6)

    def test_floor_to_step(self):
        self.assertAlmostEqual(floor_to_step(0.0123456789, 1e-6), 0.012345)
        self.assertEqual(floor_to_step(5.0, 0), 5.0)

    def test_plan_is_internally_consistent(self):
        plan = plan_cycle(EQUITY, PRICE, ATR, GRID, RISK, EXIT)
        self.assertEqual(len(plan.levels), GRID["max_levels"] + 1)
        self.assertEqual(plan.levels[0].price, PRICE)
        self.assertLess(plan.stop_price, plan.levels[-1].price)
        self.assertLessEqual(plan.total_notional, RISK["max_exposure_pct"] * EQUITY + 1)
        self.assertLessEqual(plan.worst_case_loss, RISK["max_cycle_loss_pct"] * EQUITY + 1)
        self.assertGreaterEqual(plan.tp_pct, EXIT["cost_floor_pct"])
        self.assertAlmostEqual(plan.total_qty, sum(l.qty for l in plan.levels))
        self.assertGreater(plan.base_qty * PRICE, 10.0)

    def test_plan_refuses_degenerate_inputs(self):
        with self.assertRaises(ValueError):
            plan_cycle(0.0, PRICE, ATR, GRID, RISK, EXIT)
        with self.assertRaises(ValueError):
            plan_cycle(EQUITY, PRICE, 0.0, GRID, RISK, EXIT)
        # Tiny equity: base order falls below the venue minimum -> refuse, do not trade dust.
        with self.assertRaises(ValueError):
            plan_cycle(50.0, PRICE, ATR, GRID, RISK, EXIT, min_notional=10.0)
        # Absurd spacing drives the ladder below zero -> refuse.
        with self.assertRaises(ValueError):
            plan_cycle(EQUITY, PRICE, ATR, {**GRID, "spacing_atr": 100.0}, RISK, EXIT)


class ExitTests(unittest.TestCase):
    def test_take_profit_never_below_cost_floor(self):
        self.assertEqual(take_profit_pct(0.002, 1.0, 0.008), 0.008)
        self.assertAlmostEqual(take_profit_pct(0.02, 1.0, 0.008), 0.02)

    def test_tp_decay_is_flat_then_monotone_then_floored(self):
        tp0, be, start, maxh = 0.012, 0.0055, 12.0, 36.0
        self.assertEqual(tp_pct_for_age(0.0, tp0, be, start, maxh), tp0)
        self.assertEqual(tp_pct_for_age(12.0, tp0, be, start, maxh), tp0)
        self.assertAlmostEqual(tp_pct_for_age(24.0, tp0, be, start, maxh), (tp0 + be) / 2)
        self.assertEqual(tp_pct_for_age(36.0, tp0, be, start, maxh), be)
        self.assertEqual(tp_pct_for_age(99.0, tp0, be, start, maxh), be)
        series = [tp_pct_for_age(h, tp0, be, start, maxh) for h in range(0, 48)]
        self.assertTrue(all(b <= a for a, b in zip(series, series[1:])))
        self.assertTrue(all(v >= be for v in series))

    def test_tp_decay_when_initial_below_breakeven_uses_initial(self):
        # cost floor already guarantees tp >= breakeven, but the function must
        # still behave if someone misconfigures it.
        self.assertEqual(tp_pct_for_age(99.0, 0.004, 0.0055, 12, 36), 0.004)

    def test_average_entry(self):
        self.assertIsNone(average_entry([]))
        self.assertAlmostEqual(average_entry([(1.0, 100.0), (1.0, 90.0)]), 95.0)
        self.assertAlmostEqual(average_entry([(1.0, 100.0), (3.0, 80.0)]), 85.0)


class AtrTests(unittest.TestCase):
    def _bars(self, closes, rng=10.0):
        return [{"h": c + rng / 2, "l": c - rng / 2, "c": c} for c in closes]

    def test_constant_range_no_gaps_equals_range(self):
        bars = self._bars([100.0] * 30, rng=10.0)
        self.assertAlmostEqual(atr(bars, 14), 10.0)

    def test_insufficient_bars_returns_none(self):
        self.assertIsNone(atr(self._bars([100.0] * 14), 14))
        self.assertIsNotNone(atr(self._bars([100.0] * 15), 14))

    def test_gap_widens_true_range(self):
        self.assertEqual(true_range({"h": 105, "l": 95, "c": 100}, None), 10)
        self.assertEqual(true_range({"h": 105, "l": 95, "c": 100}, 120), 25)   # gap down from 120
        self.assertEqual(true_range({"h": 105, "l": 95, "c": 100}, 80), 25)    # gap up from 80

    def test_atr_responds_to_a_volatility_shock(self):
        calm = self._bars([100.0] * 30, rng=10.0)
        shocked = calm + self._bars([100.0] * 5, rng=50.0)
        self.assertGreater(atr(shocked, 14), atr(calm, 14))
        self.assertLess(atr(shocked, 14), 50.0)   # smoothed, not the raw spike


if __name__ == "__main__":
    unittest.main(verbosity=2)
