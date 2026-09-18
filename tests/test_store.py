"""
Store tests - schema migration and window queries.

The database lives on a persistent volume and outlives every version of the
code, so the migration path from an older schema is the one most worth pinning.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import Store, iso_utc  # noqa: E402

LEGACY_ROUND_TRIPS = """
CREATE TABLE round_trips (
    ts TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL,
    buy_order_id TEXT, buy_price REAL, sell_order_id TEXT, sell_price REAL, pnl REAL
)
"""


class MigrationTests(unittest.TestCase):
    def test_legacy_round_trips_gains_opened_ts_and_keeps_rows(self):
        d = tempfile.mkdtemp()
        legacy = sqlite3.connect(os.path.join(d, "state.db"))
        legacy.execute(LEGACY_ROUND_TRIPS)
        legacy.execute(
            "INSERT INTO round_trips VALUES ('2026-09-18T10:00:00Z','BTC/USD',0.001,'b1',80000,'s1',80100,0.1)"
        )
        legacy.commit()
        legacy.close()

        store = Store(d)  # must migrate in place, not fail or recreate
        cols = {r["name"] for r in store.db.execute("PRAGMA table_info(round_trips)")}
        self.assertIn("opened_ts", cols)

        rows = store.recent_round_trips(10)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["opened_ts"])   # legacy row: honest NULL, not a guess
        self.assertEqual(rows[0]["pnl"], 0.1)

        # Opening the same store again must be a no-op, not a second ALTER.
        store.close()
        again = Store(d)
        self.assertEqual(len(again.recent_round_trips(10)), 1)
        again.close()

    def test_new_round_trip_records_open_and_close(self):
        store = Store(tempfile.mkdtemp())
        opened = 1_789_700_000_000
        store.record_round_trip("BTC/USD", 0.001, "b", 80000.0, "s", 80100.0, 0.1, opened_ms=opened)
        row = store.recent_round_trips(1)[0]
        self.assertEqual(row["opened_ts"], iso_utc(opened))
        self.assertGreaterEqual(row["ts"], row["opened_ts"])  # ISO strings order correctly
        store.close()


class WindowQueryTests(unittest.TestCase):
    def test_round_trips_between_filters_on_close_time(self):
        store = Store(tempfile.mkdtemp())
        # Insert with controlled close timestamps by writing directly.
        for ts in ("2026-09-18T10:00:00Z", "2026-09-18T12:00:00Z", "2026-09-18T14:00:00Z"):
            store.db.execute(
                "INSERT INTO round_trips (ts, symbol, qty) VALUES (?, 'BTC/USD', 0.001)", (ts,)
            )
        store.db.commit()
        rows = store.round_trips_between("2026-09-18T11:00:00Z", "2026-09-18T13:00:00Z", 10)
        self.assertEqual([r["ts"] for r in rows], ["2026-09-18T12:00:00Z"])
        store.close()

    def test_snapshots_between_is_inclusive_both_ends(self):
        store = Store(tempfile.mkdtemp())
        for ts in (1000, 2000, 3000, 4000):
            store.insert_snapshot({"ts": ts, "equity": 100.0, "cash": 100.0, "buying_power": 400.0,
                                   "long_market_value": 0.0, "flow": 0.0, "chain": 1.0, "twr": 0.0,
                                   "peak": 100.0, "drawdown": 0.0})
        self.assertEqual([r["ts"] for r in store.snapshots_between(2000, 3000)], [2000, 3000])
        self.assertEqual(store.snapshot_at_or_before(2500)["ts"], 2000)
        self.assertIsNone(store.snapshot_at_or_before(500))
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
