"""
Read-only JSON API over the container's persisted state.

Served on the TradingHost-allocated port so a dashboard outside the container
can read the equity history and the bot's own trade ledger. Design constraints,
in order of importance:

  * Read-only. Nothing here mutates state or places orders.
  * Bearer token on every data route, compared in constant time. The port is a
    plain-HTTP NodePort on a public IP, so the token is what stands between the
    internet and your account's equity curve. It is also why this stays scoped
    to paper trading: over plain HTTP the token itself travels in the clear.
  * JSON only, exact-match routes, no filesystem access. That removes the whole
    class of path-traversal and static-serving concerns that make the stdlib
    server inappropriate for general use. Flask + waitress is the upgrade path
    if this ever grows beyond a handful of endpoints.
  * The bot must not depend on it. The server runs on a daemon thread; if it
    cannot start (no port, no token) trading continues and a warning is logged.

Window semantics:

  TWR over a window   = chain_end / chain_at_window_start - 1   (exact, from the
                        stored chain - no recomputation)
  Drawdown in window  = decline from the peak *within* the window, computed on
                        read. The stored column is against the all-time peak;
                        both are returned, labelled.
"""

import hmac
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from runtime import log

MIN_ANNUALISE_DAYS = 7


def _annualise(total_return, elapsed_ms):
    """Refuse to extrapolate short windows - it amplifies noise into large,
    confident-looking numbers. Below the threshold this returns None and the
    caller omits the figure rather than printing something misleading."""
    if total_return is None or elapsed_ms <= 0:
        return None
    days = elapsed_ms / 86_400_000
    if days < MIN_ANNUALISE_DAYS:
        return None
    return (1.0 + total_return) ** (365.0 / days) - 1.0


def window_series(rows, base):
    """Re-express stored rows relative to a window boundary.

    `base` is the snapshot at or before the window start (or None). TWR is
    re-based to it via the chain; drawdown is recomputed against the running
    peak inside the window. All-time figures are carried through unchanged so
    a caller can show either.
    """
    if not rows:
        return []
    base_chain = (base or rows[0])["chain"] or 1.0
    peak = (base or rows[0])["equity"]
    out = []
    for r in rows:
        if r["equity"] > peak:
            peak = r["equity"]
        out.append({
            "t": r["ts"],
            "equity": r["equity"],
            "cash": r["cash"],
            "twr": r["chain"] / base_chain - 1.0 if base_chain else 0.0,
            "drawdown": r["equity"] / peak - 1.0 if peak > 0 else 0.0,
            "allTimeTwr": r["twr"],
            "allTimeDrawdown": r["drawdown"],
            "flow": r["flow"],
        })
    return out


def stride_sample(points, limit):
    """Thin a long series for transport while always keeping the last point."""
    if limit <= 0 or len(points) <= limit:
        return points
    step = math.ceil(len(points) / limit)
    thinned = points[::step]
    if thinned[-1] is not points[-1]:
        thinned.append(points[-1])
    return thinned


def summarise(points, interval_seconds, flow_count, all_time_dd):
    if len(points) < 2:
        return {
            "twr": None, "annualisedTwr": None, "maxDrawdown": None, "maxDrawdownAt": None,
            "peakEquity": None, "peakAt": None, "currentDrawdown": None, "recovered": None,
            "underwaterMs": None, "elapsedMs": 0, "sampleCount": len(points),
            "resolutionSeconds": interval_seconds, "flowCount": flow_count,
            "startEquity": points[0]["equity"] if points else None,
            "endEquity": points[-1]["equity"] if points else None,
            "allTimeMaxDrawdown": all_time_dd[0], "allTimeMaxDrawdownAt": all_time_dd[1],
        }

    first, last = points[0], points[-1]
    max_dd, max_dd_at = 0.0, None
    peak, peak_at = first["equity"], first["t"]
    run_peak, run_peak_at = first["equity"], first["t"]
    for p in points:
        if p["equity"] > run_peak:
            run_peak, run_peak_at = p["equity"], p["t"]
        if p["drawdown"] < max_dd:
            max_dd, max_dd_at = p["drawdown"], p["t"]
            peak, peak_at = run_peak, run_peak_at

    elapsed = last["t"] - first["t"]
    current_dd = last["drawdown"]
    return {
        "twr": last["twr"],
        "annualisedTwr": _annualise(last["twr"], elapsed),
        "maxDrawdown": max_dd,
        "maxDrawdownAt": max_dd_at,
        "peakEquity": peak,
        "peakAt": peak_at,
        "currentDrawdown": current_dd,
        "recovered": current_dd >= 0,
        "underwaterMs": (last["t"] - run_peak_at) if current_dd < 0 else 0,
        "elapsedMs": elapsed,
        "sampleCount": len(points),
        "resolutionSeconds": interval_seconds,
        "flowCount": flow_count,
        "startEquity": first["equity"],
        "endEquity": last["equity"],
        "allTimeMaxDrawdown": all_time_dd[0],
        "allTimeMaxDrawdownAt": all_time_dd[1],
    }


class Api:
    def __init__(self, store, trader, snapshotter, token, port, max_points=2000):
        self.store = store
        self.trader = trader
        self.snapshotter = snapshotter
        self.token = token
        self.port = port
        self.max_points = max_points
        self.started_at = time.time()
        self._server = None

    # --- handlers -----------------------------------------------------------

    def _window(self, query):
        now = int(time.time() * 1000)
        since = int(query.get("since", [now - 86_400_000])[0])
        limit = min(int(query.get("limit", [self.max_points])[0]), self.max_points)
        rows = self.store.snapshots_since(since)
        base = self.store.snapshot_at_or_before(since)
        points = window_series(rows, base)
        flow_total, flow_count = self.store.flows_between(since, now)
        return points, limit, flow_count, since

    def h_snapshots(self, query):
        points, limit, _, since = self._window(query)
        return {"since": since, "total": len(points), "points": stride_sample(points, limit)}

    def h_summary(self, query):
        points, _, flow_count, since = self._window(query)
        summary = summarise(points, self.snapshotter.interval, flow_count, self.store.all_time_max_drawdown())
        summary["since"] = since
        return summary

    def h_performance(self, query):
        """Series and summary together, from one read, so a client can never
        show a headline that disagrees with the chart beneath it."""
        points, limit, flow_count, since = self._window(query)
        return {
            "since": since,
            "series": stride_sample(points, limit),
            "summary": summarise(points, self.snapshotter.interval, flow_count,
                                 self.store.all_time_max_drawdown()),
        }

    def h_trades(self, query):
        limit = min(int(query.get("limit", [200])[0]), 1000)
        return {"trades": self.store.recent_round_trips(limit)}

    def h_status(self, query):
        return {
            "uptimeSeconds": round(time.time() - self.started_at, 1),
            "trader": self.trader.status(),
            "snapshotter": self.snapshotter.status(),
            "store": self.store.counts(),
        }

    # --- server -------------------------------------------------------------

    def serve_forever_in_thread(self):
        api = self
        routes = {
            "/api/snapshots": api.h_snapshots,
            "/api/summary": api.h_summary,
            "/api/performance": api.h_performance,
            "/api/trades": api.h_trades,
            "/api/status": api.h_status,
        }

        class Handler(BaseHTTPRequestHandler):
            server_version = "strategy-api"
            sys_version = ""

            def log_message(self, *args):
                pass  # keep the console for the bot's own structured logs

            def _send(self, code, body):
                payload = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def _authorised(self):
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return False
                presented = header[len("Bearer "):].strip()
                # Constant-time compare: a plain == short-circuits on the first
                # differing byte and leaks the token to a timing attack.
                return hmac.compare_digest(presented.encode(), api.token.encode())

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/healthz":
                    return self._send(200, {"ok": True})
                handler = routes.get(url.path)
                if handler is None:
                    return self._send(404, {"error": "not found"})
                if not self._authorised():
                    return self._send(401, {"error": "unauthorised"})
                try:
                    return self._send(200, handler(parse_qs(url.query)))
                except (ValueError, KeyError) as exc:
                    return self._send(400, {"error": f"bad request: {exc}"})
                except Exception as exc:  # never let a handler take the thread down
                    log("error", "API handler failed", path=url.path, error=str(exc))
                    return self._send(500, {"error": "internal error"})

            def do_POST(self):
                self._send(405, {"error": "read-only"})

            do_PUT = do_DELETE = do_PATCH = do_POST

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._server.daemon_threads = True
        thread = threading.Thread(target=self._server.serve_forever, name="api", daemon=True)
        thread.start()
        log("info", "API listening", port=self.port,
            routes=sorted(routes) + ["/healthz"], auth="bearer")
        return thread

    def shutdown(self):
        if self._server:
            self._server.shutdown()
