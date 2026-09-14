# 0046 — a venue the book can reach today

## The two blockers, and which one just went away

Phase D needed an API key and a wallet with BTC in it. Both were a person's
job, and one of them had a 24-hour clock on it.

**Testnet** test coins come from a web faucet: 1 BTC and 10,000 USDT, *once per
24 hours*, PC browser only. No API. That wallet stays a person's job.

**Demo trading** is a different module: `api-demo.bybit.com`, funded by
`POST /v5/account/demo-apply-money` — up to 15 BTC — and running against **real
mainnet market data** with simulated matching. The marks, the basis and the
funding prints are the real ones. The fills are not, and no real money moves.

So the wallet stops being a blocker, and the market data gets *better* than
testnet's, which has its own thin order book and its own funding.

## The danger this creates, and what stops it

`USE_TESTNET` is a boolean that picks a base URL. **Adding a third destination
to a boolean is how a testnet key ends up authenticating against mainnet.**

So:

- `BYBIT_VENUE` ∈ `{mainnet, testnet, demo}` is the authority;
- `USE_TESTNET` becomes the **derived** sandbox flag — every existing safety
  gate already reads it, and none of them has to learn a new word;
- a configuration that states both and contradicts itself is **refused**, not
  resolved. Two sources of truth about which exchange you are pointed at is
  precisely the bug.

| set | result |
|---|---|
| nothing | `testnet`, `USE_TESTNET=True` — unchanged |
| `USE_TESTNET=0` | `mainnet` — unchanged |
| `BYBIT_VENUE=demo` | demo, and `USE_TESTNET` derives to `True` |
| `BYBIT_VENUE=demo` + `USE_TESTNET=0` | **ConfigError** |
| `BYBIT_VENUE=prod` | **ConfigError** — refused, not defaulted |

Tests assert demo is a sandbox at every gate it passes: `is_live_authorized` is
False, `assert_sandbox` is True, and `_carry_orders_permitted` returns
`(True, "DEMO")` — permitted for the same reason testnet is, and **named**, so
no log line and no drill transcript can be read as mainnet evidence.

There is one table from venue name to URL, so there is exactly one answer to
*which exchange am I pointed at*.

## Funding, and what it refuses

`tools/fund_demo.py` sends the one request. It checks the **URL the client is
actually pointed at**, not a config flag — that URL is the thing that decides
where the request lands, and a funding call is a **write**. Anything but the
demo base URL raises `NotDemo` and nothing is sent.

It also refuses an amount above the venue's own maximum and a coin the venue
does not fund, so a typo is a message rather than a rejected signed request
nobody reads.

```bash
python3 tools/fund_demo.py --btc 1 --usdt 10000
```

## The drill now records where it ran

Every transcript carries `venue` and a `venue_note` spelling out what that
venue's evidence is worth:

- **demo** — real market data, simulated matching, no real money;
- **testnet** — a separate exchange whose prices and funding are *not* the real
  ones;
- **unknown** — the default, deliberately, because a transcript that does not
  know where it ran must not be readable as the strongest possible claim.

## A missing dependency is a sentence now

Run with a bare `python3`, the drill died with `ModuleNotFoundError: No module
named 'numpy'` from four imports deep. That is INVENTORY **D16** again, which
is why `session_tail` reports `UNREADABLE` instead of `NO`.

The import moved into `_load_bot()`, and a failure prints which interpreter to
use and exits **2** — the same code `session_tail` uses for *I could not read
this*, distinct from *this is wrong*.

## One command from a checkout to a running book

```bash
./scripts/deploy.sh                    # testnet
BYBIT_VENUE=demo ./scripts/deploy.sh   # demo
```

Installs flyctl if missing, checks `fly.toml` **before** creating anything,
then app → volume → egress IP → secrets → deploy. Idempotent: every step checks
whether it already happened, so re-running after a failure costs nothing and
never duplicates an app, a volume or an IP.

It **refuses mainnet by name**. Mainnet needs a signed risk memo and a recorded
drill first, and then it is a deliberate act, not a script.

It **arms nothing**. What it leaves you with is a machine that can reach the
venue running a book that refuses every order, because `PAPER_TRADING=1` and
the promotion gate is unsigned. A test asserts every arming string appears only
in the instructions the script *prints*, never in the part it executes — and
that test was checked by breaking the script on purpose and watching it fail.

## What this still does not give you

Demo matching is simulated. It will answer F1 (`marketUnit`), F5 (lot rules),
F6 (funding history) and the shape of every response. It will **not** settle F4
(liquidation), F8 (rate limits under load), or whether a real book fills a
post-only order (F10/F11) — and its fills are not evidence about slippage on a
real order book at all.

Testnet answers the matching questions with a fake book. Mainnet at $100
answers them with a real one. Demo is the fastest way to find out whether the
software is right, which is a different question from whether the trade is.
