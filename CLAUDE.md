# Project Rules

This repository is a trading strategy deployed on **TradingHost** (managed Kubernetes
hosting for trading bots). It runs against **Alpaca paper trading only**.

Canonical platform conventions live in the template repo this project follows:
`TradingHostDotCom/alpaca` → `.cursor/rules/tradinghost.mdc`. The essentials are below.

## Hard rules

- **Paper trading only.** The base URL is `paper-api.alpaca.markets` and the code
  refuses to start on a key that does not begin with `PK`. Do not add a live-trading
  code path without an explicit, deliberate request.
- **Never hardcode credentials.** They arrive as environment variables from TradingHost
  strategy secrets: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `DASHBOARD_TOKEN`, plus the
  `DASHBOARD_PORT` workaround (see `resolve_api_port` in `main.py` — the platform list
  `TRADINGHOST_PORTS` takes precedence whenever it is populated).
- **Never put secrets in `config.json` / `config.example.json`** — those are committed
  to git and are for non-secret tunables only.
- **Never write state outside `TRADINGHOST_DATA_DIR`** — everything else is ephemeral.
- **The API is read-only.** No route may mutate state or place orders. Every data route
  checks the bearer token with `hmac.compare_digest`.

## Module boundaries

| Module | Owns |
|---|---|
| `main.py` | Orchestration only — no business logic |
| `strategy.py` | Trading decisions and order handling |
| `snapshots.py` | Equity history and the incremental TWR / drawdown maths |
| `api.py` | HTTP surface and window semantics (re-basing, window-local drawdown) |
| `state.py` | Every SQL statement. Nothing else touches the database directly |
| `alpaca.py` | Every HTTP call to the broker. Nothing else imports `requests` |
| `runtime.py` | `log`, the shutdown `Event`, `sleep_interruptible` |

Keep it that way: a change to how equity is measured belongs in `snapshots.py`,
a change to what the dashboard sees belongs in `api.py`, and a new table belongs
in `state.py`.

## Metrics contract

- `equity_snapshots.chain` is the running `Π(1 + rᵢ)` from the first snapshot ever.
  It is what makes restart-resume exact and window TWR a division. Never recompute
  it from scratch in a hot path; never store TWR without it.
- Stored `drawdown` is against the **all-time** peak. Window-local drawdown is
  computed on read in `api.window_series`. Both are returned, labelled.
- Cash flows are `CSD, CSW, JNLC, JNLS, ACATC, ACATS` only. `DIV`/`INT`/`FEE` are
  portfolio returns and must not be treated as flows.
- Annualised return is `None` below 7 days of history. Do not lower this.
- `tests/test_metrics.py` pins these. Run it before pushing a change to
  `snapshots.py` or `api.py`.

## Platform contract

- Entrypoint is `main.py`.
- Log structured JSON to stdout with `flush=True`. Levels: `info`, `warn`, `error`, `debug`.
- Handle SIGTERM — there is a 30-second window before SIGKILL. The trader flattens on
  shutdown; background threads are daemons.
- Persist state under `TRADINGHOST_DATA_DIR` (default `/data`), backed by a PVC.
- Bind any server to `0.0.0.0` on `TRADINGHOST_PORTS[0].targetPort`. The public address is
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

## Restart safety

The container can restart at any time. On startup, always query Alpaca for actual
positions and open orders and reconcile before trading — never assume a clean slate.
The snapshotter resumes from the last stored row and warns if the gap is more than
twice the interval.
