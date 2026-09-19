"""
Geometry and sizing for a long-only averaging-down ladder.

Pure functions, no I/O, exhaustively unit-tested. The engine in strategy.py
calls plan_cycle() once when it opens a cycle and then executes the plan.

The shape is the established DCA-bot ladder: a base order at the reference
price, then N safety levels below it with geometrically widening spacing and
geometrically growing size. Everything is expressed in ATR so the ladder
breathes with volatility - quiet market, tight levels; violent market, wide
levels and a smaller base, so the same configuration puts the same fraction
of equity at risk.

Two hard caps decide the base size, and the tighter one wins:

  risk      the loss if every level fills and price then hits the hard stop
            must not exceed max_cycle_loss_pct of equity
  exposure  notional at a full ladder must not exceed max_exposure_pct of equity

Sizing to the disaster rather than to the first order is what separates a
bounded averaging strategy from a martingale that eventually blows up. The
remaining tail - a move through the stop before it can be acted on - is
bounded by the stop distance and the poll interval, and is the reason this
strategy is paper-only until it has a long record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    index: int       # 0 is the base order; 1..N are resting safety levels
    price: float     # base: reference price at open; others: limit price
    qty: float

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass(frozen=True)
class CyclePlan:
    reference_price: float
    atr: float
    atr_pct: float
    levels: tuple[Level, ...]
    stop_price: float
    tp_pct: float            # initial take-profit above average entry
    base_qty: float
    total_qty: float
    total_notional: float
    worst_case_loss: float
    binding: str             # which cap decided the size: "risk" or "exposure"

    def as_dict(self) -> dict:
        return {
            "reference_price": self.reference_price,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
            "levels": [{"index": l.index, "price": l.price, "qty": l.qty} for l in self.levels],
            "stop_price": self.stop_price,
            "tp_pct": self.tp_pct,
            "base_qty": self.base_qty,
            "total_qty": self.total_qty,
            "total_notional": self.total_notional,
            "worst_case_loss": self.worst_case_loss,
            "binding": self.binding,
        }


def plan_from_dict(d: dict) -> CyclePlan:
    """Inverse of CyclePlan.as_dict - used to resume an open cycle after a
    restart from the plan persisted in the store."""
    levels = tuple(Level(int(l["index"]), float(l["price"]), float(l["qty"])) for l in d["levels"])
    return CyclePlan(
        reference_price=float(d["reference_price"]),
        atr=float(d["atr"]),
        atr_pct=float(d["atr_pct"]),
        levels=levels,
        stop_price=float(d["stop_price"]),
        tp_pct=float(d["tp_pct"]),
        base_qty=float(d["base_qty"]),
        total_qty=float(d["total_qty"]),
        total_notional=float(d["total_notional"]),
        worst_case_loss=float(d["worst_case_loss"]),
        binding=str(d["binding"]),
    )


# --- geometry ---------------------------------------------------------------

def level_prices(reference, atr, n_levels, spacing_atr, spacing_scale):
    """Limit prices for safety levels 1..N below the reference. The first gap is
    spacing_atr * ATR and each subsequent gap is spacing_scale times the last,
    so deeper levels are progressively further apart - a move that keeps going
    has to keep going *harder* to fill the next one."""
    prices, depth, step = [], 0.0, spacing_atr * atr
    for _ in range(n_levels):
        depth += step
        prices.append(reference - depth)
        step *= spacing_scale
    return prices


def level_multipliers(n_levels, volume_scale):
    """Size multipliers for levels 0..N relative to the base order."""
    return [volume_scale ** i for i in range(n_levels + 1)]


def stop_price_for(prices, reference, atr, stop_atr_below_last):
    """Hard stop a fixed number of ATRs below the lowest level. If price gets
    there the thesis - that this is a dip, not a trend - has failed."""
    last = prices[-1] if prices else reference
    return last - stop_atr_below_last * atr


# --- sizing -----------------------------------------------------------------

def worst_case_loss_per_unit(entries, multipliers, stop):
    """USD lost if every level fills and price then hits the stop, for a base
    quantity of exactly 1 unit."""
    return sum(m * (e - stop) for m, e in zip(multipliers, entries))


def notional_per_unit(entries, multipliers):
    return sum(m * e for m, e in zip(multipliers, entries))


def floor_to_step(x, step):
    if step <= 0:
        return x
    return math.floor(x / step + 1e-9) * step


def size_base_qty(equity, entries, multipliers, stop, max_cycle_loss_pct, max_exposure_pct, qty_step):
    """Largest base quantity that satisfies both caps. Returns (qty, binding)."""
    wc = worst_case_loss_per_unit(entries, multipliers, stop)
    tn = notional_per_unit(entries, multipliers)
    if wc <= 0 or tn <= 0:
        raise ValueError("degenerate ladder: non-positive worst case or notional")
    q_risk = max_cycle_loss_pct * equity / wc
    q_expo = max_exposure_pct * equity / tn
    if q_risk <= q_expo:
        return floor_to_step(q_risk, qty_step), "risk"
    return floor_to_step(q_expo, qty_step), "exposure"


# --- exits ------------------------------------------------------------------

def take_profit_pct(atr_pct, tp_atr_mult, cost_floor_pct):
    """Take-profit above average entry: a multiple of volatility, but never
    below the cost floor. A target under round-trip costs is not a trade, it
    is a donation to the venue."""
    return max(cost_floor_pct, tp_atr_mult * atr_pct)


def tp_pct_for_age(age_hours, tp_initial, breakeven_pct, decay_start_hours, max_hold_hours):
    """Linear decay of the take-profit target from tp_initial down to breakeven
    between decay_start and max_hold.

    Holding too short is pointless - costs dominate. Holding too long ties up
    the ladder's capital and extends tail exposure. So the bar for getting out
    is lowered gradually rather than all at once: a stale cycle takes the first
    exit that clears costs, and the engine closes it at market at max_hold if
    even that never comes."""
    floor = min(breakeven_pct, tp_initial)
    if age_hours <= decay_start_hours:
        return tp_initial
    if age_hours >= max_hold_hours or max_hold_hours <= decay_start_hours:
        return floor
    frac = (age_hours - decay_start_hours) / (max_hold_hours - decay_start_hours)
    return tp_initial - frac * (tp_initial - floor)


def average_entry(fills):
    """Quantity-weighted average price of (qty, price) fills, or None if flat."""
    qty = sum(q for q, _ in fills)
    if qty <= 0:
        return None
    return sum(q * p for q, p in fills) / qty


# --- the plan ---------------------------------------------------------------

def plan_cycle(equity, reference_price, atr, grid, risk, exit_cfg, qty_step=1e-6, min_notional=10.0):
    """Build a complete, sized ladder for one cycle.

    Raises ValueError rather than returning a plan that should not be traded:
    non-positive inputs, a ladder that reaches a non-positive price, or a base
    order below the venue minimum after sizing (which means equity or the caps
    are too small for this configuration - trade nothing, log it).
    """
    if equity <= 0 or reference_price <= 0 or atr <= 0:
        raise ValueError("equity, reference price and ATR must all be positive")

    n = int(grid["max_levels"])
    prices = level_prices(reference_price, atr, n, grid["spacing_atr"], grid["spacing_scale"])
    if prices and prices[-1] <= 0:
        raise ValueError("ladder reaches a non-positive price; reduce levels or spacing")

    multipliers = level_multipliers(n, grid["volume_scale"])
    entries = [reference_price] + prices
    stop = stop_price_for(prices, reference_price, atr, grid["stop_atr_below_last"])
    if stop <= 0:
        raise ValueError("stop price is non-positive; reduce stop distance")

    base_qty, binding = size_base_qty(
        equity, entries, multipliers, stop,
        risk["max_cycle_loss_pct"], risk["max_exposure_pct"], qty_step,
    )
    if base_qty * reference_price < min_notional:
        raise ValueError(
            f"base order {base_qty * reference_price:.2f} USD is below the minimum {min_notional:.2f}"
        )

    levels = tuple(
        Level(i, e, floor_to_step(base_qty * m, qty_step))
        for i, (e, m) in enumerate(zip(entries, multipliers))
    )
    atr_pct = atr / reference_price
    tp = take_profit_pct(atr_pct, exit_cfg["tp_atr_mult"], exit_cfg["cost_floor_pct"])

    return CyclePlan(
        reference_price=reference_price,
        atr=atr,
        atr_pct=atr_pct,
        levels=levels,
        stop_price=stop,
        tp_pct=tp,
        base_qty=base_qty,
        total_qty=sum(l.qty for l in levels),
        total_notional=sum(l.notional for l in levels),
        worst_case_loss=sum(l.qty * (l.price - stop) for l in levels),
        binding=binding,
    )
