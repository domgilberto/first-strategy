"""
Process-wide primitives shared by every module.

Kept tiny and dependency-free so importing it never pulls anything else in:
structured logging, the shutdown flag, and an interruptible sleep. The flag is a
threading.Event rather than a bare global because the trader, the snapshotter
and the API server all run on separate threads and all need to observe SIGTERM.
"""

import json
import signal
import threading
import time

_running = threading.Event()
_running.set()


def running():
    return _running.is_set()


def request_shutdown():
    _running.clear()


def log(level, msg, **kwargs):
    """Structured JSON to stdout, which TradingHost streams to the console.
    flush=True is required - without it output buffers and never appears."""
    print(json.dumps({"level": level, "msg": msg, **kwargs}), flush=True)


def sleep_interruptible(seconds):
    """Sleep in short slices so SIGTERM is honoured well inside the platform's
    30-second grace window regardless of how long the caller asked for."""
    deadline = time.time() + seconds
    while running() and time.time() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def install_signal_handlers():
    def handler(sig, frame):
        if running():
            log("info", "Shutdown signal received - finishing current work and exiting")
        request_shutdown()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
