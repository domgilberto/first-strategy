"""
Order-path smoke test: open and close one position per cycle.

This is a CONNECTIVITY TEST, not a trading strategy. It deliberately round-trips
at market to prove that code inside the TradingHost container can reach Alpaca,
place an order, see it fill, and close it. It pays the spread twice per cycle
and will lose money by design - never point it at anything but a paper account.
"""

import threading
import time

from runtime import log

TERMINAL_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def _price(order):
    raw = order.get("filled_avg_price")
    return float(raw) if raw else None


def find_position(api, symbol):
    """Return the raw position record, or None. Alpaca reports crypto positions
    without the slash (BTCUSD, not BTC/USD), so match on the normalised form and
    hand back whatever spelling the API used - the close endpoint expects it."""
    wanted = symbol.replace("/", "")
    for pos in api.positions():
        if pos.get("symbol", "").replace("/", "") == wanted:
            return pos
    return None


def await_terminal(api, order_id, timeout):
    """Poll an order until it reaches a terminal state or the timeout expires."""
    deadline = time.time() + timeout
    order = api.get_order(order_id)
    while order.get("status") not in TERMINAL_STATES and time.time() < deadline:
        time.sleep(1)
        order = api.get_order(order_id)
    return order


class Trader:
    def __init__(self, api, store, symbol, qty, fill_timeout):
        self.api = api
        self.store = store
        self.symbol = symbol
        self.qty = qty
        self.fill_timeout = fill_timeout
        self._lock = threading.Lock()
        self._status = {
            "cycles": 0, "round_trips": 0, "failed_cycles": 0,
            "last_buy_price": None, "last_sell_price": None, "last_pnl": None,
            "cumulative_pnl": 0.0, "position_qty": 0.0,
        }

    def status(self):
        with self._lock:
            return dict(self._status)

    def _update(self, **fields):
        with self._lock:
            self._status.update(fields)

    # --- reconciliation -----------------------------------------------------

    def reconcile(self):
        """Never trust local state after a restart - ask the broker."""
        stale = self.api.open_orders()
        if stale:
            self.api.cancel_all_orders()
            log("warn", "Cancelled orphaned open orders from a previous run", count=len(stale))
        self.flatten(reason="startup reconciliation")

    def flatten(self, reason):
        """Close any open position. Used on startup and on shutdown."""
        pos = find_position(self.api, self.symbol)
        if pos is None:
            return 0.0
        qty = float(pos.get("qty", 0))
        log("warn", "Flattening open position", symbol=pos.get("symbol"), qty=qty, reason=reason)
        order = self.api.close_position(pos["symbol"])
        order = await_terminal(self.api, order["id"], self.fill_timeout)
        log("info", "Flatten complete", symbol=pos.get("symbol"), qty=qty,
            status=order.get("status"), price=_price(order))
        self._update(position_qty=0.0)
        return qty

    # --- one cycle ----------------------------------------------------------

    def round_trip(self):
        """Buy at market, confirm the fill, then close the position straight back
        out. Returns True on a completed round trip."""
        api, symbol = self.api, self.symbol
        with self._lock:
            self._status["cycles"] += 1

        buy = api.submit_order(symbol, self.qty, "buy")
        log("info", "BUY submitted", symbol=symbol, qty=self.qty, order_id=buy.get("id"))

        buy = await_terminal(api, buy["id"], self.fill_timeout)
        if buy.get("status") != "filled":
            log("error", "BUY did not fill - skipping cycle", order_id=buy.get("id"), status=buy.get("status"))
            return self._fail()

        buy_price = _price(buy)
        bought = float(buy.get("filled_qty") or self.qty)
        log("info", "BUY filled", symbol=symbol, qty=bought, price=buy_price, order_id=buy.get("id"))
        self._update(position_qty=bought, last_buy_price=buy_price)

        # Close the position rather than selling `bought`: Alpaca deducts its
        # crypto fee from the filled quantity, so the position is a little
        # smaller than the amount ordered and selling the ordered size is
        # rejected for insufficient balance.
        pos = find_position(api, symbol)
        if pos is None:
            log("error", "No position found after a filled buy - cannot close", order_id=buy.get("id"))
            return self._fail()

        holding = float(pos.get("qty", 0))
        sell = api.close_position(pos["symbol"])
        log("info", "SELL submitted", symbol=pos.get("symbol"), qty=holding, order_id=sell.get("id"))

        sell = await_terminal(api, sell["id"], self.fill_timeout)
        if sell.get("status") != "filled":
            log("error", "SELL did not fill - position may still be open",
                order_id=sell.get("id"), status=sell.get("status"))
            return self._fail()

        sell_price = _price(sell)
        sold = float(sell.get("filled_qty") or holding)

        # Value in, value out - captures the fee drag that a naive
        # (sell_price - buy_price) * qty would hide entirely.
        pnl = None
        if buy_price and sell_price:
            pnl = round((sell_price * sold) - (buy_price * bought), 6)

        log("info", "SELL filled", symbol=pos.get("symbol"), qty=sold, price=sell_price, order_id=sell.get("id"))
        log("info", "Round trip complete", symbol=symbol, bought=bought, sold=sold,
            fee_drag=round(bought - sold, 10), buy_price=buy_price, sell_price=sell_price, pnl=pnl)

        self.store.record_round_trip(symbol, sold, buy.get("id"), buy_price, sell.get("id"), sell_price, pnl)

        with self._lock:
            s = self._status
            s["position_qty"] = 0.0
            s["last_sell_price"] = sell_price
            s["last_pnl"] = pnl
            s["round_trips"] += 1
            if pnl is not None:
                s["cumulative_pnl"] = round(s["cumulative_pnl"] + pnl, 6)
        return True

    def _fail(self):
        with self._lock:
            self._status["failed_cycles"] += 1
        return False
