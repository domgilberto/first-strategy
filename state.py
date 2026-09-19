"""
Persistent state, on the TradingHost persistent volume.

Every SQL statement in the project lives in this module. The tables hold
different kinds of truth:

  cycles / cycle_orders   The strategy's own record of intent and outcome: the
                          ladder it planned, every order it placed, what filled
                          at what price, and how the cycle ended. The broker
                          cannot produce this view - it sees unrelated orders.

  round_trips             One row per completed cycle in the simple shape the
                          dashboard's Trades panel reads: open/close time, both
                          legs, realised P&L.

  markouts                Price some seconds/minutes after each of our fills,
                          for calibrating spacing and take-profit against what
                          the market actually does after we trade.

  equity_snapshots        Immutable history. A past equity value never changes,
                          so it is safe to persist - unlike positions, which
                          must always be asked of the broker. Each row carries
                          the running TWR chain and all-time peak so resuming
                          after a restart is exact.

  cash_flows              External money movements from Alpaca activities,
                          keyed on the activity id so ingestion is idempotent.

Writes are serialised with a lock; the connection is shared across the engine,
snapshotter and API threads. WAL mode lets the API read while a write is in
flight. Schema changes are idempotent migrations in `_migrate`, because the
database outlives any one version of the code.
"""

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS round_trips (
    ts            TEXT NOT NULL,   -- close time (exit fill), ISO-8601 UTC
    symbol        TEXT NOT NULL,
    qty           REAL NOT NULL,
    buy_order_id  TEXT,
    buy_price     REAL,
    sell_order_id TEXT,
    sell_price    REAL,
    pnl           REAL,
    opened_ts     TEXT             -- open time (entry fill), ISO-8601 UTC; NULL on legacy rows
);

CREATE TABLE IF NOT EXISTS cycles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT    NOT NULL,
    opened_ts        INTEGER NOT NULL,          -- epoch ms
    closed_ts        INTEGER,
    status           TEXT    NOT NULL,          -- open | closed_tp | closed_stop | closed_timeout | closed_reconcile
    plan_json        TEXT    NOT NULL,          -- ladder.CyclePlan.as_dict()
    reference_price  REAL,
    atr              REAL,
    atr_pct          REAL,
    stop_price       REAL,
    tp_pct_initial   REAL,
    filled_levels    INTEGER NOT NULL DEFAULT 0,
    position_qty     REAL    NOT NULL DEFAULT 0,
    avg_entry        REAL,
    exit_price       REAL,
    exit_qty         REAL,
    pnl              REAL,                      -- estimated: buy fee via quantity, sell fee via est_fee_pct
    reason           TEXT
);
CREATE INDEX IF NOT EXISTS cycles_status ON cycles (status);
CREATE INDEX IF NOT EXISTS cycles_closed ON cycles (closed_ts);

CREATE TABLE IF NOT EXISTS cycle_orders (
    id               TEXT    PRIMARY KEY,       -- broker order id
    cycle_id         INTEGER NOT NULL,
    kind             TEXT    NOT NULL,          -- base | level | tp | exit
    level_index      INTEGER,
    side             TEXT    NOT NULL,
    qty              REAL    NOT NULL,
    limit_price      REAL,
    status           TEXT    NOT NULL,          -- open | filled | canceled | expired | rejected
    filled_qty       REAL,
    filled_avg_price REAL,
    submitted_ts     INTEGER NOT NULL,
    filled_ts        INTEGER
);
CREATE INDEX IF NOT EXISTS cycle_orders_cycle ON cycle_orders (cycle_id);

CREATE TABLE IF NOT EXISTS markouts (
    order_id    TEXT    NOT NULL,
    horizon_s   INTEGER NOT NULL,
    side        TEXT    NOT NULL,
    fill_price  REAL    NOT NULL,
    mark_price  REAL    NOT NULL,
    markout_bps REAL    NOT NULL,               -- positive = market moved in our favour after the fill
    ts          INTEGER NOT NULL,
    PRIMARY KEY (order_id, horizon_s)
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

# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS does not
# touch an existing table, so each is applied with ALTER TABLE when missing.
MIGRATIONS = [
    ("round_trips", "opened_ts", "TEXT"),
]

CYCLE_UPDATABLE = {"filled_levels", "position_qty", "avg_entry", "status"}


def iso_utc(ms):
    """round_trips timestamps are ISO-8601 UTC strings; this produces the same
    shape so window bounds compare correctly as text."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


class Store:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "state.db")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self._lock = threading.Lock()

    def _migrate(self):
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self):
        with self._lock:
            self.db.commit()
            self.db.close()

    def _write(self, sql, params=()):
        with self._lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur

    # --- cycles -------------------------------------------------------------

    def open_cycle(self, symbol, plan, opened_ms):
        cur = self._write(
            "INSERT INTO cycles (symbol, opened_ts, status, plan_json, reference_price, atr, "
            "atr_pct, stop_price, tp_pct_initial) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?)",
            (symbol, opened_ms, json.dumps(plan.as_dict()), plan.reference_price, plan.atr,
             plan.atr_pct, plan.stop_price, plan.tp_pct),
        )
        return cur.lastrowid

    def get_open_cycle(self):
        row = self.db.execute(
            "SELECT * FROM cycles WHERE status = 'open' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def update_cycle(self, cycle_id, **fields):
        bad = set(fields) - CYCLE_UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._write(f"UPDATE cycles SET {sets} WHERE id = ?", (*fields.values(), cycle_id))

    def close_cycle(self, cycle_id, status, closed_ms, exit_price, exit_qty, pnl, reason,
                    avg_entry, filled_levels):
        self._write(
            "UPDATE cycles SET status = ?, closed_ts = ?, exit_price = ?, exit_qty = ?, pnl = ?, "
            "reason = ?, avg_entry = ?, filled_levels = ?, position_qty = 0 WHERE id = ?",
            (status, closed_ms, exit_price, exit_qty, pnl, reason, avg_entry, filled_levels, cycle_id),
        )

    def recent_cycles(self, limit):
        rows = self.db.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def cycles_between(self, since_ms, until_ms, limit):
        """Open cycles are always included; closed ones by close time."""
        rows = self.db.execute(
            "SELECT * FROM cycles WHERE status = 'open' OR (closed_ts >= ? AND closed_ts <= ?) "
            "ORDER BY id DESC LIMIT ?",
            (since_ms, until_ms, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- orders -------------------------------------------------------------

    def add_order(self, o):
        self._write(
            "INSERT OR REPLACE INTO cycle_orders (id, cycle_id, kind, level_index, side, qty, "
            "limit_price, status, filled_qty, filled_avg_price, submitted_ts, filled_ts) "
            "VALUES (:id, :cycle_id, :kind, :level_index, :side, :qty, :limit_price, :status, "
            ":filled_qty, :filled_avg_price, :submitted_ts, :filled_ts)",
            {"level_index": None, "limit_price": None, "filled_qty": None,
             "filled_avg_price": None, "filled_ts": None, **o},
        )

    def update_order(self, order_id, status, filled_qty=None, filled_avg_price=None, filled_ts=None):
        self._write(
            "UPDATE cycle_orders SET status = ?, "
            "filled_qty = COALESCE(?, filled_qty), filled_avg_price = COALESCE(?, filled_avg_price), "
            "filled_ts = COALESCE(?, filled_ts) WHERE id = ?",
            (status, filled_qty, filled_avg_price, filled_ts, order_id),
        )

    def orders_for_cycle(self, cycle_id, kinds=None, statuses=None):
        sql, params = "SELECT * FROM cycle_orders WHERE cycle_id = ?", [cycle_id]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += list(kinds)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += list(statuses)
        sql += " ORDER BY submitted_ts ASC, level_index ASC"
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    # --- markouts -----------------------------------------------------------

    def pending_markouts(self, horizons, now_ms, limit=50):
        """Filled entry/exit orders whose horizon has elapsed and which have no
        markout row yet for that horizon."""
        out = []
        for h in horizons:
            rows = self.db.execute(
                "SELECT o.id, o.side, o.filled_avg_price FROM cycle_orders o "
                "WHERE o.status = 'filled' AND o.filled_ts IS NOT NULL AND o.filled_avg_price IS NOT NULL "
                "AND o.filled_ts + ? <= ? "
                "AND NOT EXISTS (SELECT 1 FROM markouts m WHERE m.order_id = o.id AND m.horizon_s = ?) "
                "LIMIT ?",
                (int(h) * 1000, now_ms, int(h), limit),
            ).fetchall()
            out += [(r["id"], int(h), r["side"], float(r["filled_avg_price"])) for r in rows]
        return out

    def add_markout(self, order_id, horizon_s, side, fill_price, mark_price, markout_bps, ts):
        self._write(
            "INSERT OR IGNORE INTO markouts (order_id, horizon_s, side, fill_price, mark_price, "
            "markout_bps, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, horizon_s, side, fill_price, mark_price, markout_bps, ts),
        )

    def recent_markouts(self, limit):
        rows = self.db.execute(
            "SELECT * FROM markouts ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def markout_summary(self):
        rows = self.db.execute(
            "SELECT side, horizon_s, COUNT(*) AS n, AVG(markout_bps) AS avg_bps "
            "FROM markouts GROUP BY side, horizon_s ORDER BY side, horizon_s"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- round trips --------------------------------------------------------

    def record_round_trip(self, symbol, qty, buy_id, buy_price, sell_id, sell_price, pnl,
                          opened_ms=None):
        self._write(
            "INSERT INTO round_trips (ts, symbol, qty, buy_order_id, buy_price, "
            "sell_order_id, sell_price, pnl, opened_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (iso_utc(time.time() * 1000), symbol, qty, buy_id, buy_price, sell_id, sell_price,
             pnl, iso_utc(opened_ms) if opened_ms else None),
        )

    def recent_round_trips(self, limit):
        rows = self.db.execute(
            "SELECT rowid AS id, * FROM round_trips ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def round_trips_between(self, since_iso, until_iso, limit):
        """Filtered on close time - a trade belongs to the window it finished in."""
        rows = self.db.execute(
            "SELECT rowid AS id, * FROM round_trips WHERE ts >= ? AND ts <= ? "
            "ORDER BY rowid DESC LIMIT ?",
            (since_iso, until_iso, limit),
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
        self._write(
            "INSERT INTO equity_snapshots (ts, equity, cash, buying_power, "
            "long_market_value, flow, chain, twr, peak, drawdown) "
            "VALUES (:ts, :equity, :cash, :buying_power, :long_market_value, "
            ":flow, :chain, :twr, :peak, :drawdown)",
            row,
        )

    def snapshots_since(self, since_ms):
        rows = self.db.execute(
            "SELECT * FROM equity_snapshots WHERE ts >= ? ORDER BY ts ASC", (since_ms,)
        ).fetchall()
        return [dict(r) for r in rows]

    def snapshots_between(self, since_ms, until_ms):
        rows = self.db.execute(
            "SELECT * FROM equity_snapshots WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
            (since_ms, until_ms),
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
        snaps = self.db.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last FROM equity_snapshots"
        ).fetchone()
        trips = self.db.execute("SELECT COUNT(*) AS n FROM round_trips").fetchone()
        flows = self.db.execute("SELECT COUNT(*) AS n FROM cash_flows").fetchone()
        cyc = self.db.execute(
            "SELECT SUM(status = 'open') AS open, SUM(status != 'open') AS closed FROM cycles"
        ).fetchone()
        marks = self.db.execute("SELECT COUNT(*) AS n FROM markouts").fetchone()
        return {
            "snapshots": snaps["n"],
            "first_snapshot": snaps["first"],
            "last_snapshot": snaps["last"],
            "round_trips": trips["n"],
            "cash_flows": flows["n"],
            "cycles_open": cyc["open"] or 0,
            "cycles_closed": cyc["closed"] or 0,
            "markouts": marks["n"],
        }
