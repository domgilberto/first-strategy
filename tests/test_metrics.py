"""
Unit tests for the persisted metrics.

Run locally:  python -m pytest tests/    (or: python tests/test_metrics.py)

These pin down the properties that make the numbers trustworthy:

  * With no cash flows the TWR chain telescopes exactly to E_n / E_0 - 1.
  * A deposit is not a return: equity jumps, TWR does not.
  * Drawdown is never positive and recovers to zero at a new peak.
  * Window re-basing: TWR over [a, b] equals chain_b / chain_a - 1.
  * The summary is read off the series, so headline == last point.
  * Resuming from the last stored row continues the chain exactly.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import stride_sample, summarise, window_series  # noqa: E402
from snapshots import Snapshotter  # noqa: E402
from state import Store  # noqa: E402


class FakeAlpaca:
    """Scripted broker: each call to account() returns the next equity."""

    def __init__(self, equities, flows=None):
        self._equities = list(equities)
        self._flows = list(flows or [])

    def account(self):
        e = self._equities.pop(0)
        return {"equity": str(e), "cash": str(e), "buying_power": str(e * 4), "long_market_value": "0"}

    def activities(self, types, after_iso=None, page_token=None):
        rows, self._flows = self._flows, []
        return rows


def make_store():
    d = tempfile.mkdtemp()
    return Store(d)


class ChainTests(unittest.TestCase):
    def test_no_flows_telescopes_to_simple_return(self):
        store = make_store()
        eq = [100.0, 101.0, 99.0, 103.0, 102.5]
        snap = Snapshotter(FakeAlpaca(eq), store, 300)
        for _ in eq:
            snap.take()
        last = store.last_snapshot()
        self.assertAlmostEqual(last["twr"], eq[-1] / eq[0] - 1, places=12)
        self.assertAlmostEqual(last["chain"], eq[-1] / eq[0], places=12)
        store.close()

    def test_deposit_is_not_a_return(self):
        store = make_store()
        # Equity 100 -> 150, but 50 of that arrived as a deposit: TWR must be 0.
        now = int(time.time() * 1000)
        store.upsert_cash_flows([{"id": "dep1", "ts": now + 1, "type": "CSD", "amount": 50.0}])
        snap = Snapshotter(FakeAlpaca([100.0, 150.0]), store, 300)
        snap.take()
        time.sleep(0.01)  # ensure the deposit ts falls inside (prev.ts, now]
        snap.take()
        last = store.last_snapshot()
        self.assertAlmostEqual(last["twr"], 0.0, places=9)
        self.assertEqual(last["flow"], 50.0)
        store.close()

    def test_drawdown_bounds_and_recovery(self):
        store = make_store()
        eq = [100.0, 90.0, 95.0, 100.0, 110.0]
        snap = Snapshotter(FakeAlpaca(eq), store, 300)
        for _ in eq:
            snap.take()
        rows = store.snapshots_since(0)
        dds = [r["drawdown"] for r in rows]
        self.assertTrue(all(d <= 1e-15 for d in dds))
        self.assertAlmostEqual(min(dds), 90.0 / 100.0 - 1, places=12)
        self.assertEqual(dds[-1], 0.0)          # new peak -> fully recovered
        self.assertEqual(rows[-1]["peak"], 110.0)
        store.close()

    def test_resume_continues_chain_exactly(self):
        store = make_store()
        a = Snapshotter(FakeAlpaca([100.0, 110.0]), store, 300)
        a.take(); a.take()
        # "Restart": a fresh Snapshotter over the same store must carry on.
        b = Snapshotter(FakeAlpaca([121.0]), store, 300)
        b.take()
        last = store.last_snapshot()
        self.assertAlmostEqual(last["chain"], 1.21, places=12)
        self.assertAlmostEqual(last["twr"], 0.21, places=12)
        store.close()


class WindowTests(unittest.TestCase):
    def _rows(self):
        eq = [100.0, 105.0, 95.0, 110.0, 108.0]
        chain, peak, out = 1.0, eq[0], []
        for i, e in enumerate(eq):
            if i:
                chain *= e / eq[i - 1]
            peak = max(peak, e)
            out.append({"ts": i * 1000, "equity": e, "cash": e, "flow": 0.0,
                        "chain": chain, "twr": chain - 1, "peak": peak, "drawdown": e / peak - 1})
        return out

    def test_rebased_twr_equals_chain_ratio(self):
        rows = self._rows()
        base = rows[1]                  # window starts at the second sample
        pts = window_series(rows[2:], base)
        self.assertAlmostEqual(pts[-1]["twr"], rows[-1]["chain"] / base["chain"] - 1, places=12)
        self.assertAlmostEqual(pts[-1]["twr"], 108.0 / 105.0 - 1, places=12)

    def test_window_drawdown_uses_window_peak(self):
        rows = self._rows()
        # Window from index 2: peak inside window is 110 (not the all-time 110 -
        # same here - so test a window where they differ).
        pts = window_series(rows[2:4], rows[1])   # equities 95, 110; base 105
        # peak within window starts at base equity 105, then 110
        self.assertAlmostEqual(pts[0]["drawdown"], 95.0 / 105.0 - 1, places=12)
        self.assertEqual(pts[1]["drawdown"], 0.0)

    def test_summary_matches_series(self):
        rows = self._rows()
        pts = window_series(rows, None)
        s = summarise(pts, 300, 0, (None, None))
        self.assertEqual(s["twr"], pts[-1]["twr"])
        self.assertEqual(s["maxDrawdown"], min(p["drawdown"] for p in pts))
        self.assertEqual(s["currentDrawdown"], pts[-1]["drawdown"])
        self.assertEqual(s["sampleCount"], len(pts))
        self.assertIsNone(s["annualisedTwr"])   # 4 seconds of history: withheld

    def test_stride_keeps_last_point(self):
        pts = [{"t": i} for i in range(1000)]
        thinned = stride_sample(pts, 100)
        self.assertLessEqual(len(thinned), 101)
        self.assertIs(thinned[-1], pts[-1])
        self.assertIs(stride_sample(pts, 0), pts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
