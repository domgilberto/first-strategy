#!/usr/bin/env python3
"""
BTC/USD order-path smoke test - Alpaca paper trading on TradingHost.

Opens and closes one position every cycle. This is a CONNECTIVITY TEST, not a
trading strategy: it deliberately round-trips at market to prove that code
running inside a TradingHost container can reach Alpaca, place an order, see it
fill, and close it. It will lose money to the spread by design - never point it
at anything but a paper account.

Required TradingHost strategy secrets (environment variables):
    APCA_API_KEY_ID      Alpaca PAPER key id (starts with "PK")
    APCA_API_SECRET_KEY  Alpaca PAPER secret key

Non-secret tunables live in config.json, seeded once from config.example.json
onto the persistent volume so they can be edited without a redeploy.
"""

import json
import os
import shutil
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ---------------------------------------------------------------------------
# Platform wiring
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("TRADINGHOST_DATA_DIR", "/data")
PORTS = json.loads(os.environ.get("TRADINGHOST_PORTS", "[]"))

TRADING_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets/v1beta3/crypto/us"

TERMINAL_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day"}

running = True

_lock = threading.Lock()
_status = {
    "started_at": time.time(),
    "cycles": 0,
    "round_trips": 0,
    "failed_cycles": 0,
    "last_buy_price": None,
    "last_sell_price": None,
    "last_pnl": None,
    "cumulative_pnl": 0.0,
    "position_qty": 0.0,
}


def log(level, msg, **kwargs):
    print(json.dumps({"level": level, "msg": msg, **kwargs}), flush=True)


def shutdown(sig, frame):
    global running
    if running:
        log("info", "Shutdown signal received - will flatten and exit")
    running = False


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def sleep_interruptible(seconds):
    """Sleep in short slices so SIGTERM is honoured well inside the 30s window."""
    deadline = time.time() + seconds
    while running and time.time() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


# ---------------------------------------------------------------------------
# Configuration (non-secret tunables)
# ---------------------------------------------------------------------------

def load_config():
    """Load tunables, backfilling any key the persisted copy is missing.

    config.json lives on the persistent volume and is only seeded when absent, so
    a config written by an earlier version of this strategy will survive a deploy
    and can be missing keys the new code expects. Merge over the committed
    defaults rather than trusting it to be complete.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "config.json")

    with open("config.example.json") as fh:
        defaults = json.load(fh)

    if not os.path.exists(path):
        shutil.copy("config.example.json", path)
        log("info", "Seeded config.json from config.example.json", path=path)
        return defaults

    with open(path) as fh:
        live = json.load(fh)

    missing = sorted(k for k in defaults if k not in live)
    if missing:
        log("warn", "config.json is missing keys - falling back to committed defaults",
            path=path, missing=missing)

    stale = sorted(k for k in live if k not in defaults)
    if stale:
        log("info", "config.json has keys this version ignores", path=path, stale=stale)

    return {**defaults, **live}


# ---------------------------------------------------------------------------
# Alpaca REST
# ---------------------------------------------------------------------------

class Alpaca:
    def __init__(self, key_id, secret_key):
        self.session = requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        })

    def _request(self, method, path, **kwargs):
        resp = self.session.request(method, TRADING_BASE + path, timeout=15, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.text else {}

    def account(self):
        return self._request("GET", "/v2/account")

    def positions(self):
        return self._request("GET", "/v2/positions")

    def open_orders(self):
        return self._request("GET", "/v2/orders", params={"status": "open"})

    def get_order(self, order_id):
        return self._request("GET", f"/v2/orders/{order_id}")

    def cancel_all_orders(self):
        self.session.delete(TRADING_BASE + "/v2/orders", timeout=15)

    def submit_order(self, symbol, qty, side):
        return self._request("POST", "/v2/orders", json={
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "gtc",  # crypto rejects "day"
        })


def position_qty(api, symbol):
    wanted = symbol.replace("/", "")
    for pos in api.positions():
        if pos.get("symbol", "").replace("/", "") == wanted:
            return float(pos.get("qty", 0))
    return 0.0


def await_terminal(api, order_id, timeout):
    """Poll an order until it reaches a terminal state or the timeout expires."""
    deadline = time.time() + timeout
    order = api.get_order(order_id)
    while order.get("status") not in TERMINAL_STATES and time.time() < deadline:
        time.sleep(1)
        order = api.get_order(order_id)
    return order


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def open_db():
    db = sqlite3.connect(os.path.join(DATA_DIR, "state.db"), check_same_thread=False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS round_trips (
            ts            TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            qty           REAL NOT NULL,
            buy_order_id  TEXT,
            buy_price     REAL,
            sell_order_id TEXT,
            sell_price    REAL,
            pnl           REAL
        )
    """)
    db.commit()
    return db


def record_round_trip(db, symbol, qty, buy, sell, pnl):
    db.execute(
        "INSERT INTO round_trips (ts, symbol, qty, buy_order_id, buy_price, "
        "sell_order_id, sell_price, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), symbol, qty,
         buy.get("id"), _price(buy), sell.get("id"), _price(sell), pnl),
    )
    db.commit()


def _price(order):
    raw = order.get("filled_avg_price")
    return float(raw) if raw else None


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------

def flatten(api, symbol, timeout, reason):
    """Close any open position. Used on startup and on shutdown."""
    qty = position_qty(api, symbol)
    if qty <= 0:
        return 0.0
    log("warn", "Flattening open position", symbol=symbol, qty=qty, reason=reason)
    order = api.submit_order(symbol, qty, "sell")
    order = await_terminal(api, order["id"], timeout)
    log("info", "Flatten complete", symbol=symbol, qty=qty,
        status=order.get("status"), price=_price(order))
    return qty


def round_trip(api, db, symbol, qty, timeout):
    """Buy at market, confirm the fill, then sell it straight back."""
    buy = api.submit_order(symbol, qty, "buy")
    log("info", "BUY submitted", symbol=symbol, qty=qty, order_id=buy.get("id"))

    buy = await_terminal(api, buy["id"], timeout)
    if buy.get("status") != "filled":
        log("error", "BUY did not fill - skipping cycle",
            order_id=buy.get("id"), status=buy.get("status"))
        return False

    buy_price = _price(buy)
    filled = float(buy.get("filled_qty") or qty)
    log("info", "BUY filled", symbol=symbol, qty=filled, price=buy_price,
        order_id=buy.get("id"))

    with _lock:
        _status["position_qty"] = filled
        _status["last_buy_price"] = buy_price

    sell = api.submit_order(symbol, filled, "sell")
    log("info", "SELL submitted", symbol=symbol, qty=filled, order_id=sell.get("id"))

    sell = await_terminal(api, sell["id"], timeout)
    if sell.get("status") != "filled":
        log("error", "SELL did not fill - position may still be open",
            order_id=sell.get("id"), status=sell.get("status"))
        return False

    sell_price = _price(sell)
    pnl = round((sell_price - buy_price) * filled, 6) if buy_price and sell_price else None

    log("info", "SELL filled", symbol=symbol, qty=filled, price=sell_price,
        order_id=sell.get("id"))
    log("info", "Round trip complete", symbol=symbol, qty=filled,
        buy_price=buy_price, sell_price=sell_price, pnl=pnl)

    record_round_trip(db, symbol, filled, buy, sell, pnl)

    with _lock:
        _status["position_qty"] = 0.0
        _status["last_sell_price"] = sell_price
        _status["last_pnl"] = pnl
        _status["round_trips"] += 1
        if pnl is not None:
            _status["cumulative_pnl"] = round(_status["cumulative_pnl"] + pnl, 6)

    return True


# ---------------------------------------------------------------------------
# Optional health endpoint
# ---------------------------------------------------------------------------

def start_health_server():
    if not PORTS:
        log("info", "No ports allocated - health endpoint disabled")
        return
    port = PORTS[0]["targetPort"]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with _lock:
                body = dict(_status)
            body["uptime_seconds"] = round(time.time() - body["started_at"], 1)
            body["status"] = "ok"
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass  # suppress default stderr access logging

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("info", "Health endpoint listening", port=port, path="/health")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    key_id = os.environ.get("APCA_API_KEY_ID")
    secret_key = os.environ.get("APCA_API_SECRET_KEY")

    if not key_id or not secret_key:
        log("error", "Missing credentials - set APCA_API_KEY_ID and APCA_API_SECRET_KEY "
                     "as TradingHost strategy secrets, then redeploy (a restart will not "
                     "pick up new secrets)")
        return 1

    if not key_id.startswith("PK"):
        log("error", "Refusing to start: APCA_API_KEY_ID does not look like a paper key",
            hint="Paper keys begin with PK, live keys begin with AK. This strategy "
                 "round-trips continuously and must never touch a live account.")
        return 1

    config = load_config()
    symbol = config["symbol"]
    qty = config["order_qty"]
    cycle_seconds = config["cycle_seconds"]
    fill_timeout = config["fill_timeout_seconds"]

    api = Alpaca(key_id, secret_key)
    db = open_db()
    start_health_server()

    try:
        account = api.account()
        log("info", "Connected to Alpaca paper trading",
            account_number=account.get("account_number"),
            status=account.get("status"),
            buying_power=account.get("buying_power"),
            currency=account.get("currency"))

        stale = api.open_orders()
        if stale:
            api.cancel_all_orders()
            log("warn", "Cancelled orphaned open orders from a previous run",
                count=len(stale))

        flatten(api, symbol, fill_timeout, reason="startup reconciliation")
    except Exception as exc:
        log("error", "Could not reach Alpaca paper trading - refusing to start",
            error=str(exc),
            hint="A 401 means the key or secret is wrong, was regenerated (which "
                 "invalidates the previous pair), or belongs to the live account. "
                 "Generate fresh paper keys at app.alpaca.markets, update the "
                 "strategy secrets, then REDEPLOY - a restart keeps the old "
                 "environment and will not pick them up.")
        db.close()
        return 1

    log("info", "Order-path smoke test running",
        symbol=symbol, order_qty=qty, cycle_seconds=cycle_seconds,
        note="Opens and closes one position per cycle. Paper only - loses to spread by design.")

    while running:
        cycle_started = time.time()
        with _lock:
            _status["cycles"] += 1

        try:
            if not round_trip(api, db, symbol, qty, fill_timeout):
                with _lock:
                    _status["failed_cycles"] += 1
        except Exception as exc:
            with _lock:
                _status["failed_cycles"] += 1
            log("error", "Cycle failed", error=str(exc))

        elapsed = time.time() - cycle_started
        sleep_interruptible(max(0.0, cycle_seconds - elapsed))

    try:
        flatten(api, symbol, fill_timeout, reason="shutdown")
    except Exception as exc:
        log("error", "Could not flatten on shutdown", error=str(exc))

    with _lock:
        summary = dict(_status)
    log("info", "Stopped cleanly",
        cycles=summary["cycles"], round_trips=summary["round_trips"],
        failed_cycles=summary["failed_cycles"],
        cumulative_pnl=summary["cumulative_pnl"])
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
