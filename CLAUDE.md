# Project Rules

This repository is a trading strategy deployed on **TradingHost** (managed Kubernetes
hosting for trading bots). It runs a long-only, volatility-scaled averaging ladder on
BTC/USD against **Alpaca paper trading only**.

Canonical platform conventions live in the template repo this project follows:
`TradingHostDotCom/alpaca` → `.cursor/rules/tradinghost.mdc`. The essentials are below.

## Hard rules

- **Paper trading only.** The base URL is `paper-api.alpaca.markets` and `main.py` refuses
  a key that does not begin with `PK`. Do not add a live-trading code path without an
  explicit, deliberate request — this strategy averages down into positions.
- **Never hardcode credentials.** They arrive as environment variables from TradingHost
  strategy secrets: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `DASHBOARD_TOKEN`, plus the
  `DASHBOARD_PORT` workaround (see `resolve_api_port` in `main.py` — the platform list
  `TRADINGHOST_PORTS` takes precedence whenever it is populated).
- **Never put secrets in `config.json` / `config.example.json`** — those are committed
  to git and are for non-secret tunables only.
- **Never write state outside `TRADINGHOST_DATA_DIR`** — everything else is ephemeral.
- **The API is read-only.** No route may mutate state or place orders. Every data route
  checks the bearer token with `hmac.compare_digest`.
- **Never remove or weaken a risk control** (`max_cycle_loss_pct`, `max_exposure_pct`, the
  hard stop, the volatility gate, `max_hold_hours`) without an explicit request, and never
  ship such a change without the engine tests passing.

## Module boundaries

| Module | Owns |
|---|---|
| `main.py` | Orchestration only — no business logic |
| `strategy.py` | The engine: cycle lifecycle, order management, reconciliation |
| `ladder.py` | Ladder geometry, sizing, take-profit decay — pure, no I/O |
| `indicators.py` | ATR — pure, no I/O |
| `snapshots.py` | Equity history and the incremental TWR / drawdown maths |
| `api.py` | HTTP surface and window semantics (re-basing, window-local drawdown) |
| `state.py` | Every SQL statement. Nothing else touches the database directly |
| `alpaca.py` | Every HTTP call to the broker. Nothing else imports `requests` |
| `runtime.py` | `log`, the shutdown `Event`, `sleep_interruptible` |

Keep it that way: a change to how the ladder is shaped belongs in `ladder.py`, a change
to *when* the engine acts belongs in `strategy.py`, a new table belongs in `state.py`.

## Engine invariants

- **Size to the disaster.** `ladder.plan_cycle` sizes the base order so that both the
  worst-case loss and the full-ladder notional stay under their caps. If it raises, the
  engine trades nothing and logs why. Never catch that and trade anyway.
- **Fills are recorded on the ordered-quantity basis** (what was paid). The **take-profit
  is always sized to the broker's live position** (what is actually held, after the fee
  taken in quantity). Never compute the sell size from the fills.
- **The open cycle survives restarts by design.** Resting orders stay at the broker; the
  plan is persisted. `reconcile_on_start` applies anything that filled during the outage
  first, then compares position with recorded fills and resumes or closes-and-flattens.
- **A cycle has exactly three exits** — take-profit, hard stop, max hold — and each records
  its `status` and `reason`. Every close also writes a `round_trips` row with `opened_ts`.
- **The stop is software-enforced per poll.** Do not lengthen `poll_seconds` without
  understanding that it widens the gap the stop can miss.
- `tests/test_engine.py` (simulated broker) and `tests/test_ladder.py` pin all of this.
  Run both before pushing a change to `strategy.py` or `ladder.py`.

## Metrics contract

- `equity_snapshots.chain` is the running `Π(1 + rᵢ)` from the first snapshot ever.
  It is what makes restart-resume exact and window TWR a division. Never recompute
  it from scratch in a hot path; never store TWR without it.
- Stored `drawdown` is against the **all-time** peak. Window-local drawdown is
  computed on read in `api.window_series`. Both are returned, labelled.
- Cash flows are `CSD, CSW, JNLC, JNLS, ACATC, ACATS` only. `DIV`/`INT`/`FEE` are
  portfolio returns and must not be treated as flows.
- Annualised return is `None` below 7 days of history. Do not lower this.
- Snapshots use a plain `INSERT` with strictly increasing timestamps — never
  `INSERT OR REPLACE`, which silently deleted a trough once.
- `tests/test_metrics.py` and `tests/test_store.py` pin these.

## Platform contract

- Entrypoint is `main.py`.
- Log structured JSON to stdout with `flush=True`. Levels: `info`, `warn`, `error`, `debug`.
- Handle SIGTERM — there is a 30-second window before SIGKILL. Background threads are
  daemons; the open cycle is intentionally left in place.
- Persist state under `TRADINGHOST_DATA_DIR` (default `/data`), backed by a PVC. Schema
  changes go through `state.MIGRATIONS` as idempotent `ALTER TABLE`s.
- Bind any server to `0.0.0.0` on `TRADINGHOST_PORTS[0].targetPort`, falling back to
  `DASHBOARD_PORT` while the platform leaves the list empty. The public address is
  `publicIp:nodePort`, plain HTTP.
- A push to the tracked branch (`main`) redeploys automatically. Feature branches are
  invisible to the platform until merged.
- Changing a secret needs a **redeploy**, not a restart.

## Dependencies

`numpy`, `pandas`, `scipy`, `scikit-learn`, `requests`, `websockets` and `sqlite3` are
pre-installed — do **not** list them in `requirements.txt`, it only slows deploys.

Never depend on `TA-Lib` / `import talib`; the C library is absent and cannot be
installed in the non-root container. Use pure-Python `ta` or `pandas-ta` instead.

The deployment has **512 MB** of memory and **0.256 cores**, and the plan is fully
allocated — so background work runs as threads in this process, not as a second
strategy. Prefer stdlib and `requests` over heavyweight SDKs.
