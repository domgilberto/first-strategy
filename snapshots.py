"""
Equity snapshotter: the one thing the broker will not keep for you.

Alpaca serves recent portfolio history at fine resolution, but that resolution
ages out. Once it is gone it cannot be reconstructed, and a drawdown measured
on coarser samples is systematically shallower. So the container - which is
always on and has a persistent volume - captures equity itself on a fixed
cadence.

Time-weighted return is computed *incrementally*. Each row stores the running
chain Π(1 + r_i), so resuming after a restart is exact (read the last row, carry
on) and TWR over any window is simply chain_end / chain_start - 1 with no
recomputation.

    r_i = (E_i - E_{i-1} - flow_i) / E_{i-1}

where flow_i is the net external cash flow in the interval - deposits and
withdrawals, which are ingested from Alpaca activities so a deposit is never
mistaken for a profit.

Gaps are reported rather than papered over. If the process was down and
snapshots were missed, the next row logs a warning with the gap size, because a
gap that happens to span a trough silently deletes that drawdown.
"""

import time
from datetime import datetime, timezone

from alpaca import CASH_FLOW_TYPES
from runtime import log, running, sleep_interruptible


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_ts(value):
    """Alpaca activity timestamps are ISO 8601, sometimes date-only."""
    if not value:
        return None
    try:
        if len(value) == 10:
            dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


class Snapshotter:
    def __init__(self, api, store, interval_seconds):
        self.api = api
        self.store = store
        self.interval = int(interval_seconds)
        self.taken = 0
        self.errors = 0

    # --- cash flows ---------------------------------------------------------

    def ingest_cash_flows(self):
        """Page forward from the last known flow. The upsert is keyed on Alpaca's
        activity id, so re-reading an overlap is harmless."""
        last_ts = self.store.last_flow_ts()
        after_iso = None
        if last_ts:
            # Small overlap guards against same-timestamp rows split across pages.
            after_iso = datetime.fromtimestamp((last_ts - 60_000) / 1000, tz=timezone.utc).isoformat()

        inserted = 0
        page_token = None
        for _ in range(5):
            rows = self.api.activities(CASH_FLOW_TYPES, after_iso=after_iso, page_token=page_token)
            if not rows:
                break
            parsed = []
            for r in rows:
                ts = _parse_ts(r.get("date") or r.get("transaction_time") or r.get("activity_time"))
                amount = _num(r.get("net_amount"))
                if amount is None:
                    amount = _num(r.get("qty"))
                if ts is None or amount is None or not r.get("id"):
                    continue
                parsed.append({"id": r["id"], "ts": ts, "type": r.get("activity_type", ""), "amount": amount})
            inserted += self.store.upsert_cash_flows(parsed)
            if len(rows) < 100:
                break
            page_token = rows[-1].get("id")
            if not page_token:
                break

        if inserted:
            log("info", "Ingested external cash flows", count=inserted)
        return inserted

    # --- snapshots ----------------------------------------------------------

    def take(self):
        acct = self.api.account()
        equity = _num(acct.get("equity"))
        if equity is None:
            raise RuntimeError("account returned no equity value")

        now = int(time.time() * 1000)
        prev = self.store.last_snapshot()

        if prev is None:
            row = {
                "ts": now, "equity": equity,
                "cash": _num(acct.get("cash")),
                "buying_power": _num(acct.get("buying_power")),
                "long_market_value": _num(acct.get("long_market_value")),
                "flow": 0.0, "chain": 1.0, "twr": 0.0, "peak": equity, "drawdown": 0.0,
            }
            self.store.insert_snapshot(row)
            log("info", "First equity snapshot", equity=equity)
            self.taken += 1
            return row

        gap_ms = now - prev["ts"]
        if gap_ms > 2 * self.interval * 1000:
            log("warn", "Gap in equity snapshots - drawdown across this gap is unobservable",
                gap_seconds=round(gap_ms / 1000), expected_seconds=self.interval)

        flow, flow_count = self.store.flows_between(prev["ts"], now)

        r = 0.0
        if prev["equity"] > 0:
            r = (equity - prev["equity"] - flow) / prev["equity"]
        chain = prev["chain"] * (1.0 + r)
        peak = max(prev["peak"], equity)
        drawdown = equity / peak - 1.0 if peak > 0 else 0.0

        row = {
            "ts": now, "equity": equity,
            "cash": _num(acct.get("cash")),
            "buying_power": _num(acct.get("buying_power")),
            "long_market_value": _num(acct.get("long_market_value")),
            "flow": flow, "chain": chain, "twr": chain - 1.0, "peak": peak, "drawdown": drawdown,
        }
        self.store.insert_snapshot(row)
        self.taken += 1

        log("info", "Equity snapshot", equity=equity, twr=round(chain - 1.0, 6),
            drawdown=round(drawdown, 6), peak=peak,
            **({"flow": flow, "flow_count": flow_count} if flow_count else {}))
        return row

    # --- loop ---------------------------------------------------------------

    def run(self):
        """Take one snapshot immediately (bounds the gap after a restart), then
        align to interval boundaries so timestamps land on tidy multiples."""
        self._safe_cycle()
        while running():
            now = time.time()
            next_at = (int(now) // self.interval + 1) * self.interval
            sleep_interruptible(max(0.0, next_at - now))
            if not running():
                break
            self._safe_cycle()
        log("info", "Snapshotter stopped", taken=self.taken, errors=self.errors)

    def _safe_cycle(self):
        try:
            self.ingest_cash_flows()
        except Exception as exc:
            self.errors += 1
            log("error", "Cash flow ingestion failed", error=str(exc))
        try:
            self.take()
        except Exception as exc:
            self.errors += 1
            log("error", "Equity snapshot failed", error=str(exc))

    def status(self):
        return {"interval_seconds": self.interval, "taken": self.taken, "errors": self.errors}
