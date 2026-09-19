# first-strategy

A long-only, volatility-scaled **averaging ladder** on BTC/USD — the mechanism
people call a martingale, built the way mature DCA bots build it and with the
controls that make it survivable — running on
[TradingHost](https://app.tradinghost.com) against
[Alpaca](https://alpaca.markets) **paper** trading.

Around it: five-minute equity snapshots with time-weighted return and drawdown,
a full intent-and-outcome ledger of every cycle, mark-outs after every fill,
and a read-only API so a dashboard can see all of it.

## The mechanism

```
   reference ──●── base order (market)                  ┐
               │                                        │ take-profit rests at
   level 1  ───●── limit buy, 0.6 ATR below             │ average × (1 + tp)
   level 2  ───●── limit buy, gap × 1.2, size × 1.5     │ and is re-placed every
   level 3  ───●── …                                    │ time a level fills
     …         │                                        ┘
   level 6  ───●── lowest level
               │
   stop     ── ✕ ── 1.5 ATR below the lowest level → market exit, cooldown
```

1. Open a cycle with a market **base** order at the current price.
2. Rest N limit **buys** below it. Spacing is in ATR and widens geometrically;
   size grows geometrically. Each fill lowers the average entry.
3. Rest one limit **sell** for the whole position at `average × (1 + tp)`, and
   re-place it whenever a level fills. `tp` is a multiple of ATR, floored at the
   round-trip cost so no target is ever a donation to the venue.
4. After `decay_start_hours`, `tp` decays linearly toward breakeven, so a stale
   cycle takes the first exit that clears costs.
5. A cycle ends one of three ways: the take-profit fills (`closed_tp`), price
   reaches the hard stop (`closed_stop` — market exit, two-hour cooldown), or it
   exceeds `max_hold_hours` (`closed_timeout` — market exit, short cooldown).
6. When flat and not cooling down, it opens the next cycle immediately, so the
   strategy is **active every day**; how often a cycle *closes* depends on the
   market, and the time decay guarantees it is bounded.

## What keeps it bounded

Averaging down has a well-known failure mode: the position grows into a trend
until it is too large to survive. Every control here exists for that.

| Control | Setting | Effect |
|---|---|---|
| **Size to the disaster** | `max_cycle_loss_pct: 0.02` | The base order is sized so that *every level filling and then the stop* costs at most 2% of equity |
| **Exposure cap** | `max_exposure_pct: 0.40` | Full-ladder notional ≤ 40% of equity. Whichever of the two caps is tighter decides the size; the plan records which |
| **Hard stop** | `stop_atr_below_last: 1.5` | If price gets here the thesis — dip, not trend — has failed. Exit everything |
| **Volatility gate** | `vol_gate_max_atr_pct: 0.03` | No new cycle when hourly ATR exceeds 3% of price |
| **Volatility floor** | `vol_floor_atr_pct: 0.002` | In a dead market the ladder is not packed into a few dollars |
| **Time stop** | `max_hold_hours: 36` | Capital is never tied up indefinitely |
| **Cooldowns** | 120 min after a stop, 30 after a timeout | No immediate re-entry into the move that just hurt |
| **Paper only** | `PK` key prefix check | The process refuses to start on a live key |

Two limitations, stated plainly:

- **The stop is enforced in software on each poll (20 s).** If the process is
  down it cannot fire; the first thing a restart does is check price against
  the stop. Alpaca has no broker-side OCO for crypto, so a resting stop would
  collide with the resting take-profit.
- **Bounded is not risk-free.** A gap through the stop before it can act costs
  more than 2%; a long trend produces a run of stopped cycles. That is why this
  runs on paper until it has a record, and why the equity history it keeps is
  the thing to judge it by.

## Layout

| Module | Job |
|---|---|
| `main.py` | Orchestration only: config, threads, poll loop, shutdown |
| `strategy.py` | The engine: opens, manages, resumes and closes cycles |
| `ladder.py` | Ladder geometry, risk-based sizing, take-profit decay — pure functions |
| `indicators.py` | ATR — pure |
| `snapshots.py` | Five-minute equity snapshots with incremental TWR and drawdown |
| `api.py` | Read-only bearer-token JSON API over the persisted state |
| `state.py` | SQLite on the persistent volume — every SQL statement |
| `alpaca.py` | The one broker client — every HTTP call |
| `config.py` | Tunables, seeded to the volume and deep-merged over committed defaults |
| `runtime.py` | Shutdown flag, structured `log`, interruptible sleep |
| `tests/` | Ladder maths, engine simulation, store migration, metrics |

## Required secrets

Set as **TradingHost strategy secrets** — they arrive as environment variables.
Never put them in `config.json`.

| Variable | Value |
|---|---|
| `APCA_API_KEY_ID` | Alpaca **paper** key id — begins with `PK` |
| `APCA_API_SECRET_KEY` | Alpaca **paper** secret key |
| `DASHBOARD_TOKEN` | Shared secret the dashboard presents to the API. Optional: without it the API stays off and trading continues |
| `DASHBOARD_PORT` | **Workaround.** The allocated `targetPort` (e.g. `13174`). Only needed while TradingHost leaves `TRADINGHOST_PORTS` empty after adding a port to an existing deployment; ignored once the platform populates the list |

> Changing a secret requires a **redeploy**, not a restart. A restart-in-place
> keeps the old environment and will not pick up the new value.

> **Platform note (2026-09-18).** Adding a port to an *existing* deployment
> provisions the NodePort — the address routes and connections are refused
> rather than timing out — but neither a pod restart nor a strategy redeploy
> rebuilds the strategy's `TRADINGHOST_PORTS`; it stays `[]` across starts.
> `main.py` therefore falls back to a `DASHBOARD_PORT` strategy secret so the
> API can still bind. The platform list takes precedence whenever populated.

## Tunables

`config.example.json` is committed and seeded once to
`{TRADINGHOST_DATA_DIR}/config.json`. The loader deep-merges the persisted copy
over the committed defaults and logs any keys that are missing or no longer
used, so an older config never crashes a newer version.

| Key | Default | Meaning |
|---|---|---|
| `poll_seconds` | `20` | Engine cadence: order sync, stop check, take-profit decay |
| `bars.timeframe` / `lookback` | `1Hour` / `200` | Newest bars for ATR (requested with an explicit window and paged - Alpaca's default `start` is midnight UTC today); refreshed every `refresh_seconds` |
| `atr_period` | `14` | Wilder ATR period |
| `grid.max_levels` | `6` | Safety levels below the base order |
| `grid.spacing_atr` / `spacing_scale` | `0.6` / `1.2` | First gap in ATR, then geometric widening |
| `grid.volume_scale` | `1.5` | Size multiplier per level |
| `grid.stop_atr_below_last` | `1.5` | Hard-stop distance below the lowest level, in ATR |
| `risk.*` | see above | The caps, gate, floor and cooldowns |
| `exit.tp_atr_mult` / `cost_floor_pct` | `1.0` / `0.008` | Take-profit = max(cost floor, ATR multiple) |
| `exit.breakeven_pct` | `0.0055` | Where the decay bottoms out — fees plus spread |
| `exit.decay_start_hours` / `max_hold_hours` | `12` / `36` | When decay begins; when the cycle is exited at market |
| `est_fee_pct` | `0.0025` | Used for the estimated P&L and expected-position checks |
| `markout_horizons_seconds` | `[300, 900, 3600]` | How long after each fill to record where price went |
| `snapshot_seconds` | `300` | Equity snapshot cadence |

## What gets persisted, and why

The broker is the system of record for **mutable state** — positions, balances —
and the engine always asks rather than remembers. What it writes down is what
the broker cannot know, or will not keep:

- **`cycles` / `cycle_orders`** — the plan it built, every order it placed, what
  filled at what price, how the cycle ended and why. The broker sees unrelated
  orders; only this record knows they were one trade. P&L is settled from this
  ledger — every filled buy against every filled sell — so a level that fills
  after the take-profit was placed is still accounted for when the remainder is
  sold on close, and no resting order ever survives a close.
- **`round_trips`** — one row per completed cycle in the simple shape the
  dashboard's Trades panel reads: opened, closed, both legs, realised P&L
  (estimated: buy fee via quantity, sell fee via `est_fee_pct`).
- **`markouts`** — price 5, 15 and 60 minutes after each fill, in basis points,
  signed so positive is favourable. This is the data for tuning spacing and
  take-profit against what the market actually does after you trade, and for
  answering "am I holding too short or too long" with numbers.
- **`equity_snapshots`** — immutable history every five minutes, carrying the
  running TWR chain and all-time peak so restart-resume is exact and window TWR
  is a division of two stored values. Alpaca's fine-resolution history ages
  out; this does not.
- **`cash_flows`** — deposits and withdrawals, so TWR never mistakes a deposit
  for a profit.

Disk cost is small: about 13 MB a year for snapshots at this cadence, and a few
kilobytes per cycle.

## API

Served on the TradingHost-allocated port, read-only, every data route behind
`Authorization: Bearer <DASHBOARD_TOKEN>` compared in constant time. Every data
route accepts `since` and `until` (epoch ms).

| Route | Returns |
|---|---|
| `GET /healthz` | `{ok:true}` — unauthenticated liveness only |
| `GET /api/performance` | `{series, summary}` for the window — the dashboard's main feed |
| `GET /api/cycles` | Cycles with plan and orders nested — the full intent-and-outcome record |
| `GET /api/trades` | Completed trades, newest first |
| `GET /api/markouts` | Per-horizon averages plus recent rows |
| `GET /api/status` | Engine state (open cycle, levels filled, current take-profit, stop, ATR, cooldown), snapshotter and store counters |
| `GET /api/snapshots` · `/api/summary` | Series only · summary only |

**Security posture, stated plainly.** The port is a plain-HTTP NodePort on a
public IP. The token stops portscanners and opportunistic reads; it does not
stop anyone on the network path, who sees the token in the clear. Acceptable
for paper money in pre-alpha; put TLS in front before this fronts a live account.

## Restart behaviour

The open cycle is deliberately **left in place** on shutdown: its orders rest at
the broker and its plan is persisted. On start the engine applies anything that
filled while it was away — a take-profit that hit during the outage closes the
cycle properly, with P&L — then compares the broker position with its recorded
fills. If they agree, it resumes; if they do not, it closes the cycle, flattens,
and starts clean. A position with no cycle on record is flattened.

## Deploying

Push to `main`. TradingHost redeploys within seconds via the GitHub App webhook.

Expect: `Connected to Alpaca paper trading` → `Reconciled with broker` →
`Snapshotter started` → `Dashboard API enabled` → `Averaging ladder running` →
`Cycle opened` with the sized plan → `Take-profit placed`, then `Level filled`
and `Cycle closed` as the market moves.

## Tests

```bash
python tests/test_ladder.py    # geometry, both caps, decay, ATR
python tests/test_engine.py    # full cycle lifecycle against a simulated broker
python tests/test_store.py     # schema migration, window queries
python tests/test_metrics.py   # TWR chain, drawdown, resume
```

The engine tests cover every way a cycle ends and both startup paths: levels
fill and the take-profit is re-sized to the real position; take-profit closes
with positive P&L; the stop exits everything inside the risk cap and enforces
the cooldown; the target decays with age; max-hold exits at market; a restart
adopts the live take-profit rather than replacing it; a take-profit that filled
during downtime closes the cycle properly; a hand-altered position is detected
and flattened; mark-outs are recorded after their horizon.
