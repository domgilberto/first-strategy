#!/usr/bin/env python3
"""
BTC/USD SMA-crossover strategy - Alpaca paper trading on TradingHost.

Dependency-free by design: uses only the Python standard library plus `requests`,
which is pre-installed in the TradingHost container. Nothing to install means
near-instant deploys and a resident set comfortably inside the 256 MB allocation.

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

running = True

_lock = threading.Lock()
_status = {
    "started_at": time.time(),
    "last_price": None,
    "last_signal": None,
    "fast_sma": None,
    "slow_sma": None,
    "position_qty": 0.0,
    "orders_submitted": 0,
    "errors": 0,
}


def log(level, msg, **kwargs):
    print(json.dumps({"level": level, "msg": msg, **kwargs}), flush=True)


def shutdown(sig, frame):
    global running
    if running:
        log("info", "Shutdown signal received, finishing current cycle")
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
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "config.json")
    if not os.path.exists(path):
        shutil.copy("config.example.json", path)
        log("info", "Seeded config.json from config.example.json", path=path)
    with open(path) as fh:
        return json.load(fh)


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


def fetch_closes(symbol, timeframe, limit):
    """Crypto market data is public - no credentials required."""
    resp = requests.get(
        f"{DATA_BASE}/bars",
        params={"symbols": symbol, "timeframe": timeframe, "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    bars = resp.json().get("bars", {}).get(symbol, [])
    return [bar["c"] for bar in bars]


def sma(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def open_db():
    db = sqlite3.connect(os.path.join(DATA_DIR, "state.db"), check_same_thread=False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            ts       TEXT NOT NULL,
            symbol   TEXT NOT NULL,
            side     TEXT NOT NULL,
            qty      REAL NOT NULL,
            price    REAL,
            order_id TEXT
        )
    """)
    db.commit()
    return db


def record_trade(db, symbol, side, qty, price, order_id):
    db.execute(
        "INSERT INTO trades (ts, symbol, side, qty, price, order_id) VALUES (?, ?, ?, ?, ?, ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), symbol, side, qty, price, order_id),
    )
    db.commit()


def reconcile(api, symbol):
    """Never trust local state after a restart - ask the broker what is actually held."""
    wanted = symbol.replace("/", "")
    qty = 0.0
    for pos in api.positions():
        if pos.get("symbol", "").replace("/", "") == wanted:
            qty = float(pos.get("qty", 0))
            break

    stale = api.open_orders()
    if stale:
        api.cancel_all_orders()
        log("warn", "Cancelled orphaned open orders from a previous run", count=len(stale))

    log("info", "Reconciled with broker", symbol=symbol, position_qty=qty)
    return qty


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
            hint="Paper keys begin with PK, live keys begin with AK. This strategy only "
                 "ever talks to paper-api.alpaca.markets.")
        return 1

    config = load_config()
    symbol = config["symbol"]
    timeframe = config["timeframe"]
    fast_window = config["fast_sma"]
    slow_window = config["slow_sma"]
    qty = config["order_qty"]
    interval = config["poll_seconds"]

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
        position_qty = reconcile(api, symbol)
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

    with _lock:
        _status["position_qty"] = position_qty

    log("info", "Strategy running",
        symbol=symbol, timeframe=timeframe,
        fast_sma=fast_window, slow_sma=slow_window,
        order_qty=qty, poll_seconds=interval)

    while running:
        try:
            closes = fetch_closes(symbol, timeframe, slow_window + 5)
            fast = sma(closes, fast_window)
            slow = sma(closes, slow_window)

            if fast is None or slow is None:
                log("warn", "Not enough bars yet", have=len(closes), need=slow_window)
                sleep_interruptible(interval)
                continue

            price = closes[-1]
            signal_name = "long" if fast > slow else "flat"

            with _lock:
                _status.update({
                    "last_price": price,
                    "last_signal": signal_name,
                    "fast_sma": round(fast, 2),
                    "slow_sma": round(slow, 2),
                })

            log("info", "Tick", symbol=symbol, price=price,
                fast_sma=round(fast, 2), slow_sma=round(slow, 2),
                signal=signal_name, position_qty=position_qty)

            if signal_name == "long" and position_qty == 0:
                order = api.submit_order(symbol, qty, "buy")
                position_qty = qty
                record_trade(db, symbol, "buy", qty, price, order.get("id"))
                with _lock:
                    _status["position_qty"] = position_qty
                    _status["orders_submitted"] += 1
                log("info", "BUY submitted", symbol=symbol, qty=qty,
                    price=price, order_id=order.get("id"))

            elif signal_name == "flat" and position_qty > 0:
                order = api.submit_order(symbol, position_qty, "sell")
                record_trade(db, symbol, "sell", position_qty, price, order.get("id"))
                log("info", "SELL submitted", symbol=symbol, qty=position_qty,
                    price=price, order_id=order.get("id"))
                position_qty = 0.0
                with _lock:
                    _status["position_qty"] = 0.0
                    _status["orders_submitted"] += 1

        except Exception as exc:
            with _lock:
                _status["errors"] += 1
            log("error", "Cycle failed", error=str(exc))

        sleep_interruptible(interval)

    log("info", "Stopped cleanly", position_qty=position_qty)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
