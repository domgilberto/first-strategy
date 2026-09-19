#!/usr/bin/env python3
"""
Entry point. Orchestration only - the work lives in the modules:

    strategy.py    the averaging-ladder engine (opens, manages and closes cycles)
    ladder.py      ladder geometry, risk-based sizing, take-profit decay (pure)
    indicators.py  ATR (pure)
    snapshots.py   5-minute equity snapshots with incremental TWR and drawdown
    api.py         read-only bearer-token JSON API over the persisted state
    state.py       SQLite on the persistent volume - every SQL statement
    alpaca.py      the one broker client - every HTTP call

Required TradingHost strategy secrets (environment variables):
    APCA_API_KEY_ID      Alpaca PAPER key id (starts with "PK")
    APCA_API_SECRET_KEY  Alpaca PAPER secret key
    DASHBOARD_TOKEN      shared secret the dashboard presents to the API
                         (optional - without it the API stays off and trading
                         continues)
    DASHBOARD_PORT       workaround: the allocated targetPort, for when the
                         platform leaves TRADINGHOST_PORTS empty (see
                         resolve_api_port)

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
from strategy import MartingaleEngine

DATA_DIR = os.environ.get("TRADINGHOST_DATA_DIR", "/data")
PORTS = json.loads(os.environ.get("TRADINGHOST_PORTS", "[]"))


def resolve_api_port():
    """Prefer the platform-injected port list; fall back to DASHBOARD_PORT.

    TradingHost provisions the NodePort when a port is added to an existing
    deployment, but - as observed on 2026-09-18 - neither a pod restart nor a
    strategy redeploy rebuilds TRADINGHOST_PORTS afterwards; the list stays
    empty. Until that is fixed platform-side the port number can be supplied
    as a strategy secret. It is not secret; it is simply the only
    per-deployment env injection the platform offers. Once TRADINGHOST_PORTS
    is populated it takes precedence and the override is ignored.
    """
    if PORTS:
        return int(PORTS[0]["targetPort"]), "TRADINGHOST_PORTS"
    override = os.environ.get("DASHBOARD_PORT", "").strip()
    if override.isdigit():
        return int(override), "DASHBOARD_PORT"
    return None, None


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
            hint="Paper keys begin with PK, live keys begin with AK. This strategy averages "
                 "down into positions and must never touch a live account.")
        return 1

    install_signal_handlers()
    config = load_config(DATA_DIR)

    api = Alpaca(key_id, secret_key)
    store = Store(DATA_DIR)
    engine = MartingaleEngine(api, store, config)
    snapshotter = Snapshotter(api, store, config["snapshot_seconds"])

    try:
        account = api.account()
        log("info", "Connected to Alpaca paper trading",
            account_number=account.get("account_number"), status=account.get("status"),
            equity=account.get("equity"), buying_power=account.get("buying_power"))
        engine.reconcile_on_start()
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
    log("info", "Snapshotter started", interval_seconds=config["snapshot_seconds"], store=store.counts())

    api_server = None
    port, port_source = resolve_api_port()
    if port is None:
        log("warn", "No port available - dashboard API disabled",
            hint="TRADINGHOST_PORTS is empty. Allocate a port on the deployment; if it still "
                 "does not appear here after a redeploy, set DASHBOARD_PORT=<targetPort> as a "
                 "strategy secret as a workaround.")
    elif not dashboard_token:
        log("warn", "DASHBOARD_TOKEN not set - dashboard API disabled", port=port, port_source=port_source,
            hint="Set DASHBOARD_TOKEN as a strategy secret and redeploy to enable it.")
    else:
        try:
            api_server = Api(store, engine, snapshotter, dashboard_token, port, config["api_max_points"])
            api_server.serve_forever_in_thread()
            public_ip = PORTS[0].get("publicIp") if PORTS else None
            log("info", "Dashboard API enabled", port=port, port_source=port_source,
                url=f"http://{public_ip or '<publicIp>'}:{port}")
        except Exception as exc:
            log("error", "Dashboard API failed to start - trading continues without it", error=str(exc))

    # --- trading loop -------------------------------------------------------

    log("info", "Averaging ladder running", symbol=config["symbol"], poll_seconds=config["poll_seconds"],
        grid=config["grid"], risk=config["risk"], exit=config["exit"],
        note="Long-only, volatility-scaled, sized to the worst case. Paper only.")

    while running():
        started = time.time()
        engine.step()
        sleep_interruptible(max(0.0, float(config["poll_seconds"]) - (time.time() - started)))

    # --- shutdown -----------------------------------------------------------
    # The open cycle, if any, is deliberately left in place: its orders rest at
    # the broker and the plan is persisted, so a redeploy resumes it rather than
    # forcing an exit at whatever price the market happens to be.

    if api_server:
        api_server.shutdown()
    status = engine.status()
    log("info", "Stopped cleanly",
        open_cycle=status["cycle"]["id"] if status["cycle"] else None,
        counters=status["counters"], snapshots=snapshotter.status())
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
