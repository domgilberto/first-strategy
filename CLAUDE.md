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
  strategy secrets: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`.
- **Never put secrets in `config.json` / `config.example.json`** — those are committed
  to git and are for non-secret tunables only.
- **Never write state outside `TRADINGHOST_DATA_DIR`** — everything else is ephemeral.

## Platform contract

- Entrypoint is `main.py`.
- Log structured JSON to stdout with `flush=True`. Levels: `info`, `warn`, `error`, `debug`.
- Handle SIGTERM — there is a 30-second window before SIGKILL.
- Persist state under `TRADINGHOST_DATA_DIR` (default `/data`), backed by a PVC.
- Bind any server to `0.0.0.0` on a `targetPort` from `TRADINGHOST_PORTS`.
- A push to the tracked branch (`main`) redeploys automatically. Feature branches are
  invisible to the platform until merged.
- Changing a secret needs a **redeploy**, not a restart.

## Dependencies

`numpy`, `pandas`, `scipy`, `scikit-learn`, `requests`, `websockets` and `sqlite3` are
pre-installed — do **not** list them in `requirements.txt`, it only slows deploys.

Never depend on `TA-Lib` / `import talib`; the C library is absent and cannot be
installed in the non-root container. Use pure-Python `ta` or `pandas-ta` instead.

The deployment has **256 MB** of memory. Prefer stdlib and `requests` over heavyweight
SDKs; keep the dependency list empty where possible.

## Restart safety

The container can restart at any time. On startup, always query Alpaca for actual
positions and open orders and reconcile before trading — never assume a clean slate.
