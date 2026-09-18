"""
Persistent state, on the TradingHost persistent volume.

Three tables, three different kinds of truth:

  round_trips       The bot's own record of what it *intended* and what came
                    back - both order ids, both fill prices, the fee drag. The
                    broker cannot produce this view; it sees two unrelated orders.

  equity_snapshots  Immutable history. A past equity value never changes, so it
                    is safe to persist - unlike positions or balances, which must
                    always be asked of the broker. Each row also carries the
                    running TWR chain and the all-time peak so that resuming after
                    a restart is exact and needs no rescan.

  cash_flows        External money movements, ingested from Alpaca activities.
                    Keyed on Alpaca's activity id so ingestion is idempotent.

Writes are serialised with a lock; the connection is shared across the trader,
snapshotter and API threads. WAL mode lets the API read while a write is in
flight.
"""

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS round_trips (
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    qty           REAL NOT NULL,
    buy_order_id  TEXT,
    buy_price     REAL,
    sell_order_id TEXT,
    sell_price    REAL,
    pnl           REAL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts                INTEGER PRIMARY KEY,   -- epoch ms
    equity            REAL    NOT NULL,
    cash              REAL,
    buying_power      REAL,
    long_market_value REAL,
    flow              REAL    NOT NULL DEFAULT 0,  -- net external flow since previous row
    chain             REAL    NOT NULL,            -- running product of (1 + r_i)
    twr               REAL    NOT NULL,            -- chain - 1, since first snapshot
    peak              REAL    NOT NULL,            -- all-time running peak equity
    drawdown          REAL    NOT NULL             -- equity / peak - 1, against all-time peak
);

CREATE TABLE IF NOT EXISTS cash_flows (
    id     TEXT PRIMARY KEY,   -- Alpaca activity id: makes ingestion idempotent
    ts     INTEGER NOT NULL,   -- epoch ms
    type   TEXT NOT NULL,
    amount REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cash_flows_ts ON cash_flows (ts);
"""


class Store:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "state.db")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            self.db.commit()
            self.db.close()

    # --- round trips --------------------------------------------------------

    def record_round_trip(self, symbol, qty, buy_id, buy_price, sell_id, sell_price, pnl):
        with self._lock:
            self.db.execute(
                "INSERT INTO round_trips (ts, symbol, qty, buy_order_id, buy_price, "
                "sell_order_id, sell_price, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 symbol, qty, buy_id, buy_price, sell_id, sell_price, pnl),
            )
            self.db.commit()

    def recent_round_trips(self, limit):
        rows = self.db.execute(
            "SELECT rowid AS id, * FROM round_trips ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- snapshots ----------------------------------------------------------

    def last_snapshot(self):
        row = self.db.execute(
            "SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def insert_snapshot(self, row):
        """Plain INSERT, deliberately not INSERT OR REPLACE.

        A snapshot is immutable history. Two rows sharing a millisecond must
        never collapse into one - that silently deletes whatever happened in
        the earlier row, which for drawdown means deleting the trough. The
        snapshotter guarantees strictly increasing timestamps; if that ever
        fails, a loud IntegrityError is the correct outcome, not a quiet
        overwrite."""
        with self._lock:
            self.db.execute(
                "INSERT INTO equity_snapshots (ts, equity, cash, buying_power, "
                "long_market_value, flow, chain, twr, peak, drawdown) "
                "VALUES (:ts, :equity, :cash, :buying_power, :long_market_value, "
                ":flow, :chain, :twr, :peak, :drawdown)",
                row,
            )
            self.db.commit()

    def snapshots_since(self, since_ms):
        rows = self.db.execute(
            "SELECT * FROM equity_snapshots WHERE ts >= ? ORDER BY ts ASC", (since_ms,)
        ).fetchall()
        return [dict(r) for r in rows]

    def snapshot_at_or_before(self, ts_ms):
        """The row that defines the start of a window: the latest snapshot at or
        before the boundary, so window TWR is measured from the boundary rather
        than from the first sample inside it."""
        row = self.db.execute(
            "SELECT * FROM equity_snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts_ms,)
        ).fetchone()
        return dict(row) if row else None

    def all_time_max_drawdown(self):
        row = self.db.execute(
            "SELECT MIN(drawdown) AS dd, ts FROM equity_snapshots"
        ).fetchone()
        return (row["dd"], row["ts"]) if row and row["dd"] is not None else (None, None)

    # --- cash flows ---------------------------------------------------------

    def upsert_cash_flows(self, rows):
        if not rows:
            return 0
        with self._lock:
            cur = self.db.executemany(
                "INSERT OR IGNORE INTO cash_flows (id, ts, type, amount) "
                "VALUES (:id, :ts, :type, :amount)",
                rows,
            )
            self.db.commit()
            return cur.rowcount

    def last_flow_ts(self):
        row = self.db.execute("SELECT MAX(ts) AS ts FROM cash_flows").fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def flows_between(self, after_ms, until_ms):
        """Net external flow in the half-open interval (after, until]."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n "
            "FROM cash_flows WHERE ts > ? AND ts <= ?",
            (after_ms, until_ms),
        ).fetchone()
        return float(row["total"]), int(row["n"])

    # --- stats --------------------------------------------------------------

    def counts(self):
        snaps = self.db.execute("SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last FROM equity_snapshots").fetchone()
        trips = self.db.execute("SELECT COUNT(*) AS n FROM round_trips").fetchone()
        flows = self.db.execute("SELECT COUNT(*) AS n FROM cash_flows").fetchone()
        return {
            "snapshots": snaps["n"],
            "first_snapshot": snaps["first"],
            "last_snapshot": snaps["last"],
            "round_trips": trips["n"],
            "cash_flows": flows["n"],
        }
