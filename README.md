# first-strategy

A BTC/USD SMA-crossover bot running on [TradingHost](https://app.tradinghost.com)
against [Alpaca](https://alpaca.markets) **paper** trading.

Built as an end-to-end pipeline test: write the strategy in Claude, push to GitHub,
watch TradingHost redeploy it into a container automatically.

## Why BTC/USD

Crypto trades 24/7 on Alpaca, so the bot produces observable activity the moment it
deploys — regardless of the hour or day. A US equities strategy would sit idle outside
market hours, making "working" indistinguishable from "broken".

## Required secrets

Set these as **TradingHost strategy secrets** (they arrive as environment variables).
Never put them in `config.json` or commit them.

| Variable | Value |
|---|---|
| `APCA_API_KEY_ID` | Alpaca **paper** key id — begins with `PK` |
| `APCA_API_SECRET_KEY` | Alpaca **paper** secret key |

Generate them at **app.alpaca.markets** → switch the account selector to **Paper
Trading** → *API Keys* panel → *Generate New Key*. The secret is shown exactly once.

> Changing a secret requires a **redeploy**, not a restart. A restart-in-place keeps
> the old environment and will not pick up the new value.

The strategy refuses to start if the key does not begin with `PK`, and only ever talks
to `paper-api.alpaca.markets`. There is no code path to live trading.

## Tunables

Non-secret settings live in `config.example.json`, seeded once to
`{TRADINGHOST_DATA_DIR}/config.json` on the persistent volume — edit it there to
change behaviour without redeploying code.

| Key | Default | Meaning |
|---|---|---|
| `symbol` | `BTC/USD` | Alpaca crypto pair |
| `timeframe` | `1Min` | Bar size for the moving averages |
| `fast_sma` | `9` | Fast moving-average window |
| `slow_sma` | `21` | Slow moving-average window |
| `order_qty` | `0.001` | Order size in BTC (~$78 at current prices) |
| `poll_seconds` | `60` | Seconds between evaluation cycles |

## Logic

Each cycle: fetch the most recent bars, compute the fast and slow SMAs, and hold a
long position whenever fast > slow.

- fast crosses **above** slow while flat → market **buy** `order_qty`
- fast crosses **below** slow while long → market **sell** the whole position

Deliberately simple. The point of this repo is to prove the deploy pipeline, not to
make money — an SMA crossover on 1-minute bars will churn and lose to fees in any
real setting.

## Platform behaviour

- **Logging** — structured JSON to stdout, streamed to the TradingHost console
- **Shutdown** — SIGTERM handled, current cycle finishes well inside the 30s window
- **Persistence** — fills recorded in SQLite at `{TRADINGHOST_DATA_DIR}/state.db`
- **Restart safety** — on boot it queries Alpaca for the real position and cancels
  orphaned orders rather than trusting local state
- **Health endpoint** — if the deployment has a port allocated, serves JSON status on
  `targetPort` (uptime, last price, last signal, position, order count, error count)

## Dependencies

None. Standard library plus `requests`, which is pre-installed in the container —
so deploys are near-instant and memory stays well inside the 256 MB allocation.

## Deploying

1. Push to `main`
2. TradingHost redeploys within seconds via the GitHub App webhook
3. Watch the console — you should see `Connected to Alpaca paper trading`, then a
   `Tick` line every 60 seconds
