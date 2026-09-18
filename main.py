#!/usr/bin/env python3
"""
Entry point. Orchestration only - the work lives in the modules:

    strategy.py    the round-trip trader (intent + outcome, recorded to SQLite)
    snapshots.py   5-minute equity snapshots with incremental TWR and drawdown
    api.py         read-only bearer-token JSON API over the persisted state
    state.py       SQLite on the persistent volume
    alpaca.py      the one broker client, shared by everything above

Required TradingHost strategy secrets (environment variables):
    APCA_API_KEY_ID      Alpaca PAPER key id (starts with "PK")
    APCA_API_SECRET_KEY  Alpaca PAPER secret key
    DASHBOARD_TOKEN      shared secret the dashboard presents to the API
                         (optional - without it the API stays off and trading
                         continues)

Non-secret tunables live in config.json, seeded from config.example.json.
"""

import json
import os
import threading
import time

from alpaca import Alpaca
from api import Api
from config import load_config
from runtime import install_signal_handlers, log, running, sleep_interruptible
from snapshots import Snapshotter
from state import Store
from strategy import Trader

DATA_DIR = os.environ.get("TRADINGHOST_DATA_DIR", "/data")
PORTS = json.loads(os.environ.get("TRADINGHOST_PORTS", "[]"))


def main():
    key_id = os.environ.get("APCA_API_KEY_ID")
    secret_key = os.environ.get("APCA_API_SECRET_KEY")
    dashboard_token = os.environ.get("DASHBOARD_TOKEN")

    if not key_id or not secret_key:
        log("error", "Missing credentials - set APCA_API_KEY_ID and APCA_API_SECRET_KEY as "
                     "TradingHost strategy secrets, then redeploy (a restart will not pick up "
                     "new secrets)")
        return 1

    if not key_id.startswith("PK"):
        log("error", "Refusing to start: APCA_API_KEY_ID does not look like a paper key",
            hint="Paper keys begin with PK, live keys begin with AK. This strategy round-trips "
                 "continuously and must never touch a live account.")
        return 1

    install_signal_handlers()
    config = load_config(DATA_DIR)

    api = Alpaca(key_id, secret_key)
    store = Store(DATA_DIR)
    trader = Trader(api, store, config["symbol"], config["order_qty"], config["fill_timeout_seconds"])
    snapshotter = Snapshotter(api, store, config["snapshot_seconds"])

    try:
        account = api.account()
        log("info", "Connected to Alpaca paper trading",
            account_number=account.get("account_number"), status=account.get("status"),
            buying_power=account.get("buying_power"), currency=account.get("currency"))
        trader.reconcile()
    except Exception as exc:
        log("error", "Could not reach Alpaca paper trading - refusing to start", error=str(exc),
            hint="A 401 means the key or secret is wrong, was regenerated (which invalidates the "
                 "previous pair), or belongs to the live account. Generate fresh paper keys, "
                 "update the strategy secrets, then REDEPLOY - a restart keeps the old environment.")
        store.close()
        return 1

    # --- background workers -------------------------------------------------
    # Both run on daemon threads and neither can take trading down with it.

    threading.Thread(target=snapshotter.run, name="snapshotter", daemon=True).start()
    log("info", "Snapshotter started", interval_seconds=config["snapshot_seconds"],
        store=store.counts())

    api_server = None
    if not PORTS:
        log("warn", "No port allocated - dashboard API disabled",
            hint="Allocate a port on the deployment to expose the read-only API.")
    elif not dashboard_token:
        log("warn", "DASHBOARD_TOKEN not set - dashboard API disabled",
            hint="Set DASHBOARD_TOKEN as a strategy secret and redeploy to enable it.")
    else:
        try:
            api_server = Api(store, trader, snapshotter, dashboard_token,
                             PORTS[0]["targetPort"], config["api_max_points"])
            api_server.serve_forever_in_thread()
            log("info", "Dashboard API reachable at",
                url=f"http://{PORTS[0].get('publicIp', '<publicIp>')}:{PORTS[0]['nodePort']}")
        except Exception as exc:
            log("error", "Dashboard API failed to start - trading continues without it", error=str(exc))

    # --- trading loop -------------------------------------------------------

    log("info", "Order-path smoke test running",
        symbol=config["symbol"], order_qty=config["order_qty"],
        cycle_seconds=config["cycle_seconds"],
        note="Opens and closes one position per cycle. Paper only - loses to spread by design.")

    while running():
        started = time.time()
        try:
            trader.round_trip()
        except Exception as exc:
            trader._fail()
            log("error", "Cycle failed", error=str(exc))
        sleep_interruptible(max(0.0, config["cycle_seconds"] - (time.time() - started)))

    # --- shutdown -----------------------------------------------------------

    try:
        trader.flatten(reason="shutdown")
    except Exception as exc:
        log("error", "Could not flatten on shutdown", error=str(exc))
    if api_server:
        api_server.shutdown()

    summary = trader.status()
    log("info", "Stopped cleanly", cycles=summary["cycles"], round_trips=summary["round_trips"],
        failed_cycles=summary["failed_cycles"], cumulative_pnl=summary["cumulative_pnl"],
        snapshots=snapshotter.status())
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
