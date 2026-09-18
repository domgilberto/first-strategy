# first-strategy

A BTC/USD **order-path smoke test** running on [TradingHost](https://app.tradinghost.com)
against [Alpaca](https://alpaca.markets) **paper** trading — plus the persistence
layer a real strategy needs: a 5-minute equity history with time-weighted return
and drawdown, the bot's own trade ledger, and a read-only API so a dashboard can
see all of it.

## What this is — and isn't

The trader opens and closes one position every cycle, at market. That is its
entire logic. It is a **connectivity test, not a trading strategy**: it exists to
prove that code inside a TradingHost container can reach Alpaca, place an order,
observe the fill and close it. It pays the spread twice a minute and loses money
by design. **Never point it at a live account.** The process refuses to start on
a key that does not begin with `PK`.

The persistence and API around it are the reusable part.

## Layout

| Module | Job |
|---|---|
| `main.py` | Orchestration only: config, threads, trading loop, shutdown |
| `strategy.py` | The round-trip trader; records intent + outcome per trade |
| `snapshots.py` | Every 5 min: equity snapshot with incremental TWR chain and running peak |
| `api.py` | Read-only bearer-token JSON API over the persisted state |
| `state.py` | SQLite on the persistent volume: `round_trips`, `equity_snapshots`, `cash_flows` |
| `alpaca.py` | The one broker client, shared by the trader and the snapshotter |
| `config.py` | Tunables, seeded to the volume and merged over committed defaults |
| `runtime.py` | Shutdown flag, structured `log`, interruptible sleep |
| `tests/` | Pins the properties that make the numbers trustworthy |

## Required secrets

Set as **TradingHost strategy secrets** — they arrive as environment variables.
Never put them in `config.json`.

| Variable | Value |
|---|---|
| `APCA_API_KEY_ID` | Alpaca **paper** key id — begins with `PK` |
| `APCA_API_SECRET_KEY` | Alpaca **paper** secret key |
| `DASHBOARD_TOKEN` | Shared secret the dashboard presents to the API. Optional: without it the API stays off and trading continues |

> Changing a secret requires a **redeploy**, not a restart. A restart-in-place
> keeps the old environment and will not pick up the new value.

## Tunables

`config.example.json` is committed and seeded once to
`{TRADINGHOST_DATA_DIR}/config.json`, where you can edit it without redeploying.
The loader merges the persisted copy over the committed defaults and logs any
keys that are missing or no longer used, so an older config never crashes a
newer version.

| Key | Default | Meaning |
|---|---|---|
| `symbol` | `BTC/USD` | Alpaca crypto pair — 24/7, so it trades at any hour |
| `order_qty` | `0.001` | Order size in BTC |
| `cycle_seconds` | `60` | Seconds between round trips |
| `fill_timeout_seconds` | `30` | How long to wait for an order to reach a terminal state |
| `snapshot_seconds` | `300` | Equity snapshot cadence, aligned to the interval boundary |
| `api_max_points` | `2000` | Longest series the API will return before thinning |

## What gets persisted, and why

The broker is the system of record for **mutable state** — positions, balances —
and the bot always asks rather than remembers. But two things are worth writing
down here, because the broker either cannot or will not keep them:

**`equity_snapshots`** — immutable history. A past equity value never changes, so
it is safe to persist. Alpaca serves recent portfolio history at fine resolution
but that resolution ages out, and once gone it cannot be reconstructed. Drawdown
measured on coarser samples is systematically shallower, so the always-on
container captures equity itself every five minutes.

Each row also stores the running TWR chain `Π(1 + rᵢ)` and the all-time peak.
That makes two things exact and cheap: resuming after a restart (read the last
row, carry on), and TWR over any window (`chain_end / chain_start − 1`, no
recomputation).

    r_i = (E_i − E_{i−1} − flow_i) / E_{i−1}

`flow_i` is the net external cash flow in the interval — deposits, withdrawals,
journals — ingested from Alpaca activities into **`cash_flows`** so a deposit is
never mistaken for a profit. `DIV`, `INT` and `FEE` are deliberately excluded:
they are returns generated *by* the portfolio, not external contributions.

**Gaps are reported, not hidden.** If the process was down and snapshots were
missed, the next row logs a warning with the gap size — a gap that spans a
trough silently deletes that drawdown.

**`round_trips`** — the bot's own record of what it *intended* and what came
back: both order ids, both fill prices, the fee drag. The broker sees two
unrelated orders; only the container knows they were one trade.

## API

Served on the TradingHost-allocated port, read-only, every data route behind
`Authorization: Bearer <DASHBOARD_TOKEN>` compared in constant time.

| Route | Returns |
|---|---|
| `GET /healthz` | `{ok:true}` — unauthenticated liveness only |
| `GET /api/performance?since=<ms>&limit=<n>` | `{series, summary}` for the window — the dashboard's main feed |
| `GET /api/snapshots?since=<ms>&limit=<n>` | Series only |
| `GET /api/summary?since=<ms>` | Summary only |
| `GET /api/trades?limit=<n>` | Round trips, newest first |
| `GET /api/status` | Trader, snapshotter and store counters |

**Window semantics.** TWR over a window is re-based to the snapshot at or before
the window start via the stored chain. Drawdown within a window is measured from
the peak *inside* that window — "1D drawdown" should not mean a decline from a
peak three weeks ago. The all-time figures are returned alongside, labelled.

**Series and summary arrive together** from one read, so a client can never show
a headline that disagrees with the chart beneath it.

**Security posture, stated plainly.** The port is a plain-HTTP NodePort on a
public IP. The token stops portscanners and opportunistic reads; it does not
stop anyone on the network path, who sees the token in the clear. That is an
acceptable trade for paper money in pre-alpha. For a live account, put TLS in
front (Cloudflare Tunnel, per TradingHost's own docs) before exposing this.

## Platform behaviour

- **Logging** — structured JSON to stdout, streamed to the TradingHost console
- **Shutdown** — on SIGTERM the trader flattens any open position before exit,
  well inside the 30-second window; the API and snapshotter are daemon threads
- **Startup** — cancels orphaned orders and flattens any leftover position; the
  snapshotter takes one snapshot immediately to bound the restart gap
- **Isolation** — a failure in the snapshotter or the API is logged and never
  reaches the trading loop
- **Resources** — stdlib plus pre-installed `requests`; nothing to install, so
  deploys are near-instant and the process stays small inside 512 MB

## Deploying

Push to `main`. TradingHost redeploys within seconds via the GitHub App webhook.

Expect, within a minute: `Connected to Alpaca paper trading` → `Snapshotter
started` → `API listening` → `BUY submitted` … `Round trip complete`, then an
`Equity snapshot` line every five minutes.

## Tests

```bash
python tests/test_metrics.py
```

Pins the invariants: the chain telescopes to simple return with no flows, a
deposit is not a return, drawdown is never positive and recovers to zero at a new
peak, window re-basing equals the chain ratio, and the summary is read off the
series rather than recomputed.
