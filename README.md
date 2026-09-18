# first-strategy

A BTC/USD **order-path smoke test** running on [TradingHost](https://app.tradinghost.com)
against [Alpaca](https://alpaca.markets) **paper** trading.

## What this is — and isn't

It opens and closes one position every cycle, at market. That is the entire logic.

This is a **connectivity test, not a trading strategy**. It exists to prove that code
running inside a TradingHost container can reach Alpaca, place an order, observe the
fill, and close the position — and that the whole loop (write in Claude → push to
GitHub → redeploy → execute) works end to end.

It round-trips at market every minute, so it pays the spread twice per cycle and will
steadily lose money by design. **Never point it at a live account.** The process
refuses to start on a key that does not begin with `PK`.

## Required secrets

Set as **TradingHost strategy secrets** — they arrive as environment variables. Never
put them in `config.json`.

| Variable | Value |
|---|---|
| `APCA_API_KEY_ID` | Alpaca **paper** key id — begins with `PK` |
| `APCA_API_SECRET_KEY` | Alpaca **paper** secret key |

> Changing a secret requires a **redeploy**, not a restart. A restart-in-place keeps
> the old environment and will not pick up the new value.

## Tunables

`config.example.json` is committed; it is seeded once to
`{TRADINGHOST_DATA_DIR}/config.json` on the persistent volume, where you can edit it
without redeploying code.

| Key | Default | Meaning |
|---|---|---|
| `symbol` | `BTC/USD` | Alpaca crypto pair (24/7, so it trades at any hour) |
| `order_qty` | `0.001` | Order size in BTC |
| `cycle_seconds` | `60` | Seconds between round trips |
| `fill_timeout_seconds` | `30` | How long to wait for an order to reach a terminal state |

Because the persisted copy survives deploys, it can be missing keys a newer version
expects. The loader merges it over the committed defaults and logs both the missing
keys and any it no longer uses, rather than crashing.

## Cycle

1. Market **buy** `order_qty`
2. Poll the order until it reaches a terminal state
3. Market **sell** the filled quantity back
4. Poll again, log both fill prices and the realised P&L
5. Record the round trip in SQLite, sleep until the next cycle

A cycle that fails to fill is logged and counted, not retried — the next cycle starts
clean.

## Platform behaviour

- **Logging** — structured JSON to stdout, streamed to the TradingHost console
- **Shutdown** — on SIGTERM it **flattens any open position** before exiting, well
  inside the 30-second window, so it never leaves the account holding inventory
- **Startup** — cancels orphaned orders and flattens any position left by a previous
  run before trading; never assumes a clean slate
- **Persistence** — every round trip written to `{TRADINGHOST_DATA_DIR}/state.db`
  (`round_trips` table: timestamp, order ids, both fill prices, P&L)
- **Health endpoint** — if the deployment has a port allocated, serves JSON status on
  `targetPort`: cycles, round trips, failures, last fill prices, cumulative P&L

## Dependencies

None. Standard library plus `requests`, which is pre-installed in the container — so
deploys are near-instant and memory stays well inside the allocation.

## Deploying

Push to `main`. With `trackLatest` on, TradingHost redeploys within seconds via the
GitHub App webhook (or call `sync_strategy` to trigger the check immediately).

Expect to see, within a minute:

```
Connected to Alpaca paper trading
Order-path smoke test running
BUY submitted  → BUY filled  → SELL submitted → SELL filled
Round trip complete
```
