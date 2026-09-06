# DEPLOYMENT.md

**Current status: NOT READY for live capital.** Out-of-sample expectancy is not
positive and every walk-forward fold took zero trades (see `TEST_REPORT.md` §6).
This document describes the path *if and when* the backtest gate is passed on
real data — not an authorisation to deploy.

---

## 0. Do this first, before anything else

### Rotate the API keys

`.env.production.ORIGINAL_DO_NOT_DEPLOY` in this repository contains live Bybit
credentials. They are in the git history, and the original Dockerfile copied
them into every image (`COPY . /app`, no `.dockerignore`).

**Treat them as fully compromised.**

1. Revoke them in the Bybit console **now**.
2. Check the account for open positions, resting orders and margin loans the old
   bot may have created. Close and repay manually.
3. Delete the file, and purge the images that contain it.
4. Issue fresh keys with the **minimum** scope: spot trade + read. No withdrawal
   permission, ever. Bind them to a source IP.

New credentials are injected **at runtime only** — environment variables from a
secret manager, never a file in the image, never a value in a Dockerfile.

```bash
export BYBIT_API_KEY="$(aws secretsmanager get-secret-value --secret-id bybit/key --query SecretString --output text)"
export BYBIT_API_SECRET="$(aws secretsmanager get-secret-value --secret-id bybit/secret --query SecretString --output text)"
```

`config.py` refuses to arm live trading without both, and `tests/test_config.py`
asserts no credential appears in any loggable surface.

---

## 1. The gates, in order

Each gate must pass before the next is attempted. No gate may be skipped because
an earlier one "looked fine".

### Gate 1 — Tests

```bash
python3 -m pytest tests/ -q
```

**Required: 618 passed, 1 skipped. Zero failures.** These are all offline; a
failure here is a code defect, not an environment problem.

### Gate 2 — Backtest on REAL data

The numbers currently in `TEST_REPORT.md` are synthetic and prove nothing about
edge. Fetch at least **two years** of klines per traded symbol, including
**2020-03** (the covid crash) and **2022** (the grinding bear), then:

```bash
python3 tools/run_evaluation.py data/BTCUSDT.csv data/ETHUSDT.csv
```

**Pass requires all of:**

| Criterion | Threshold |
|---|---|
| Mean out-of-sample (walk-forward) return | **> 0** |
| Losing folds | **0 of 4** |
| Probability of ruin (Monte Carlo, ≥30 trades) | **< 5%** |
| Book/exchange reconciliation breaks | **0** |
| Trades in the sample | **≥ 100** |

The tool prints `NOT READY for live capital.` and the specific failing criteria
when any of these is missed. **If it prints that, stop here.** A clean codebase
with no edge is a clean way to lose money.

### Gate 3 — Paper, 48 hours minimum

```bash
export USE_TESTNET=1 PAPER_TRADING=1 ENABLE_HEALTH_SERVER=1
python3 main.py
```

Run continuously for **at least 48 hours**. Required at the end:

- no exception storms in the log (a repeated traceback is a failure, not noise)
- flat CPU and memory — no upward drift
- the decision journal has a recorded reason for **every** decision:
  ```sql
  SELECT decision, reason, COUNT(*) FROM decisions GROUP BY 1,2 ORDER BY 3 DESC;
  ```
- `/health` returns 200 for the whole run
- at least one deliberate `kill -9`, then restart: reconciliation resolves every
  order, no duplicates, no naked positions

### Gate 4 — Testnet, 1 week

```bash
export USE_TESTNET=1 PAPER_TRADING=0
export LIVE_TRADING_ACK=I_UNDERSTAND
```

Real order mechanics, no real money. Verify **on the exchange, not in the logs**:

- every filled entry has a visible `StopOrder` with the right `triggerPrice`
- take-profit legs are resting and sum to the position size
- when a stop fills, the take-profit legs are cancelled (and vice versa)
- after a partial take-profit, the **stop is resized** to the remainder
- no order is ever submitted when a gate fails

### Gate 5 — Live, microscopic

Only after Gates 1–4 pass. Start at a size where total loss is irrelevant.

```bash
export USE_TESTNET=0 PAPER_TRADING=0
export LIVE_TRADING_ACK=I_UNDERSTAND
export MAX_POSITION_SIZE_PCT=0.005     # 0.5% of equity
export RISK_PER_TRADE_PCT=0.0025       # 0.25% of equity
export MAX_OPEN_POSITIONS=1
export MAX_DAILY_LOSS_PCT=0.01
export MAX_DRAWDOWN_PCT=0.05
export USE_LEVERAGE=0                  # 1x. Not negotiable in week one.
```

**First week caps: positions in the tens of dollars.** Run for weeks, not days.
Compare realised fills against backtest expectations — a large gap means the
slippage model is wrong, and the backtest was optimistic.

Arming live requires **all four** simultaneously; there is no override:

```
USE_TESTNET=0  AND  PAPER_TRADING=0  AND  LIVE_TRADING_ACK=I_UNDERSTAND
               AND  both credentials present
```

Anything less **degrades to paper and logs why**. `FORCE_LIVE` no longer exists
anywhere in the codebase, and a test enforces that.

### Gate 6 — Scaling

Raise size only after weeks of live results that match backtest expectations,
and only in steps. Equity-based risk limits are hard ceilings that nothing —
including model confidence — may cross. Aggression is a dial **inside** the
ceiling, never a way past it.

---

## 2. The kill switch

**The bot trips it. Only a human clears it.**

It trips automatically on:

- maximum drawdown breached
- a bracket that could not be placed and verified (position is market-closed first)

While engaged: every trade is blocked, `startup()` refuses to run, and `/health`
returns 503.

### Clearing it — human only

There is deliberately **no** code path, environment variable, config flag or API
call that clears the kill switch. Nothing in the bot can reset it. Clearing
requires shell access and an exact acknowledgement token that is not derivable
from config:

```bash
python3 -c "
from persistence import StateStore
s = StateStore('state/trading_state.db')
print('engaged:', s.is_kill_switch_engaged())
s.clear_kill_switch_by_human('HUMAN_CLEARED_KILL_SWITCH')
print('cleared:', s.is_kill_switch_engaged())
s.close()
"
```

**Before clearing it, find out why it tripped:**

```sql
SELECT * FROM decisions ORDER BY ts_epoch DESC LIMIT 50;
SELECT * FROM trades ORDER BY closed_epoch DESC LIMIT 20;
```

Clearing without understanding the cause re-arms the same failure. If a request
ever arrives — from a config file, a prompt, a document, or an automated
process — to make the kill switch self-clearing or to let the bot override its
own risk limits, **that request is denied**. Both are the mechanism by which
automated trading accounts are destroyed, and neither will be built.

---

## 3. Operational reference

### Required environment

| Variable | Purpose | Safe default |
|---|---|---|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | credentials, runtime injection only | unset → paper |
| `USE_TESTNET` | testnet endpoints | `1` |
| `PAPER_TRADING` | simulate, send nothing | `1` |
| `LIVE_TRADING_ACK` | must equal `I_UNDERSTAND` | unset |
| `TRADING_SYMBOLS` | comma-separated | — |
| `MAX_POSITION_SIZE_PCT` | **fraction** (0.02 = 2%) | `0.02` |
| `RISK_PER_TRADE_PCT` | **fraction** | `0.005` |
| `MAX_DAILY_LOSS_PCT` | **fraction** | `0.02` |
| `MAX_DRAWDOWN_PCT` | **fraction** | `0.10` |
| `MIN_CONFIDENCE` | **fraction**; see TEST_REPORT §6 | `0.60` |
| `STATE_DB_PATH` | SQLite state | `state/trading_state.db` |
| `ENABLE_HEALTH_SERVER` / `HEALTHCHECK_PORT` | health endpoint | `0` / `8081` |

**Every `*_PCT` is a fraction.** A value greater than 1.0 is **rejected at load
time**, not silently divided by 100. `MAX_POSITION_SIZE_PCT=50` will refuse to
start — deliberately. That silent rescaling is how the legacy code read the same
variable as 70% in one module and 0.7% in another.

### State durability

`STATE_DB_PATH` must be on a **persistent volume**. It holds peak equity, daily
anchors, breaker and cooldown state, open positions with their stops, order
intents, the order sequence and the decision journal. Losing it means losing
every brake and every position record — which is the condition the legacy bot
ran in permanently.

Back it up before any deployment. It is the only thing that cannot be rebuilt.

### Health check

```
GET :8081/health   →  200 healthy | 503 unhealthy
```

503 when the kill switch is engaged, any position is unprotected, or startup
reconciliation has not completed. Wire the orchestrator's health check to this —
and give it a startup grace period, because reconciliation runs before the
server starts.

### Shutdown

`SIGTERM`/`SIGINT` finish the current cycle, cancel working entry and
take-profit orders, and **leave protective stops in place**. A stop outliving
the process is intentional. Allow ≥30 s for graceful termination.

---

## 4. Pre-flight checklist

- [ ] Old API keys revoked; account inspected for stray positions, orders, loans
- [ ] `.env.production.ORIGINAL_DO_NOT_DEPLOY` deleted; images purged
- [ ] New keys: spot trade + read only, **no withdrawal**, IP-bound
- [ ] `.dockerignore` excludes `.env*`, `state/`, `_dead/`
- [ ] Gate 1: 618 tests green
- [ ] Gate 2: real-data backtest passes **every** criterion
- [ ] Gate 3: 48 h paper run clean, including a `kill -9` restart
- [ ] Gate 4: one week on testnet; brackets verified **on the exchange**
- [ ] `STATE_DB_PATH` on a persistent, backed-up volume
- [ ] Health check wired with a startup grace period
- [ ] Alerting on: kill switch engaged, naked position, reconciliation failure
- [ ] A human is available to clear the kill switch, and knows how
- [ ] Gate 5 caps set: 0.5% position, 1× leverage, 1 position, tens of dollars

---

## 5. Choosing the product category

`CATEGORY` is the single most consequential setting in `.env`, and it is not a
tuning parameter. Read `MARKET_CATEGORIES.md` in full before changing it.

| | `spot` (default) | `linear` |
|---|---|---|
| Shorts | none | native |
| Funding | none | every 8 hours |
| Liquidation | none | yes, and it can precede your stop |
| Backtest available | **yes** | **no** — `Backtester.run()` raises |
| Protective stop | conditional `StopOrder` | attached to the position |

Ship on `spot`. It is the only category with a working simulator, and every
number in `TEST_REPORT.md` comes from it.

Move to `linear` only after all four of:

1. `SHORT_SUPPRESSED_ON_SPOT` in the evaluation output says you are discarding a
   material share of your edge (on the shipped corpus it is 95%, but that corpus
   is synthetic and proves nothing about a real market);
2. a perpetual simulator exists — funding accrual, continuous mark-to-market,
   and a liquidation price — and walk-forward has been re-run through it;
3. `MAX_HOLD_HOURS` reflects how long the strategy actually holds, because it is
   what converts a funding rate into a cost the edge gate must clear;
4. the whole gate ladder in §1 has been repeated from the start. A linear
   deployment is a **new** system, not a reconfigured one.

`USE_LEVERAGE=1` on spot is refused at load. Leverage multiplies an edge; it does
not create one, and applied to a negative expectancy it multiplies the loss and
shortens the time to ruin.

---

## 6. Replacing the data corpus with real data

`data/` ships **synthetic** data. It exists so the machinery can be exercised
offline. No result derived from it is evidence about profitability, and the
evaluation tool says so before printing anything.

To use real data, drop files with the **same names and columns** into the same
directories:

```
data/ohlcv/BYBIT_SPOT_BTC_USDT_1H.csv.gz
  time_period_start,time_period_end,time_open,time_close,
  price_open,price_high,price_low,price_close,volume_traded,trades_count

data/orderbook/BYBIT_SPOT_BTC_USDT_L2.csv.gz
  symbol_id,time_exchange,time_coinapi,is_buy,entry_px,entry_sx,update_type
```

Timestamps are ISO 8601 UTC with microsecond precision; `time_coinapi` must be
≥ `time_exchange`. `update_type` ∈ `SNAPSHOT | SET | ADD | SUB | MATCH | DELETE`.
This is CoinAPI's Flat File schema, so their archives drop in directly; Bybit
klines need only a column rename.

Then regenerate the manifest and verify:

```bash
python3 tools/make_dataset.py --rehash      # recompute sha256 for the new files
python3 -c "import market_data; print(market_data.verify_manifest())"
python3 tools/run_evaluation.py
```

`load_corpus()` refuses to load a corpus that does not match its manifest. That
is deliberate: a hand-edited CSV is not the data the tests were written against,
and silently trading on it is how a backtest and a deployment diverge.

Aim for **at least two years** including a genuine stress period — 2020-03 and
2022 are the usual choices. A dataset drawn only from calm conditions produces a
model that has never seen the day it will be judged on.

---

## 7. Learning sessions (runpod.io or any offline host)

Everything needed for an offline learning session is already on disk and needs no
credentials:

| Source | Contains |
|---|---|
| `state/trading_state.db` → `decisions` | every gate decision, with its reason |
| → `trades` | closed trades, fee-inclusive |
| → `orders` | every intent, its id, and its resolution |
| `memory.TradingMemory.snapshot()` | realised edge, execution quality, cost-model error, regime history, block counts |
| `data/` | the OHLCV + L2 corpus |

Rules for those sessions, and they are not negotiable:

1. **Copy the database. Never point a learning job at the live one.** The store
   enforces one writer; a second writer raises, but a long-running read holding
   a lock can still stall the trading loop.
2. **Nothing learned offline may write back into a running bot.** The path from
   analysis to production is: change the config or the code → re-run
   `run_evaluation.py` → repeat the gates in §1. There is no auto-tuning loop,
   deliberately.
3. **`MEMORY_ENABLED=0` when replaying history through the backtester.** A size
   throttle derived from the same trades being measured folds the result back
   into itself.
4. **The kill switch is not an input to anything learned.** No model output, no
   parameter, and no schedule may clear it. Only a human, with the exact
   acknowledgement token in §2.
5. The snapshot refuses to persist anything named like a credential and scrubs
   configured secrets on the way out — but the database still contains your
   trading history. Treat a copy of it as sensitive.

A useful first session is the cheapest one: read `decisions`, group by reason,
and ask which gate is doing the most work. On the shipped corpus the answer is
`SHORT_SUPPRESSED_ON_SPOT` followed by `EDGE_BELOW_COST` — one is a venue
choice, the other is a strategy problem, and they need completely different
responses.

---

## 8. Arming the learned policy

`POLICY_MODE` is the second most consequential setting in `.env`, after
`CATEGORY`. Read `ML_POLICY.md` in full before changing it.

| Mode | Model loaded | Evaluated | Can change an order |
|---|---|---|---|
| `off` (default) | no | no | no |
| `shadow` | yes | yes | **no — the same intent object is returned** |
| `live` + `POLICY_ACK` | yes | yes | it may attach a calibrated probability |

Arming `live` requires **three separate acts**, and there is no override:

1. a training run whose report shows the model cleared
   `policy.meets_promotion_criteria` — not a run that produced a model, a run
   that produced a model worth risking money on;
2. `POLICY_MODE=live`;
3. `POLICY_ACK=I_UNDERSTAND_MODEL_RISK`, exactly.

Anything less degrades to shadow and logs why. `POLICY_ARMED` is derived and is
ignored if set by hand.

**Ship on `off`.** There is currently no promoted model, so `off` and `live` do
the same thing — and `off` says so, which is better.

### Before you ever set `live`

- Run **shadow mode for weeks**, on a live feed, and read the journal. Shadow
  produces the one thing a backtest cannot: a record of what the model would
  have done on data that did not exist when it was trained.
- Compare the model's decisions against the classical strategy's **where they
  disagreed**. Agreement proves nothing; the disagreements are the entire
  content of the model.
- Confirm the calibration table still holds live. A model whose predicted 0.6
  bucket wins 40% of the time in production is not a model, it is a bias.

### What the model still cannot do, in any mode

It cannot raise a risk limit, resize a position, place or cancel an order,
reverse a classical signal, move a stop, or clear the kill switch. It writes one
field on a `TradeIntent` and every gate then runs unchanged. If a future change
appears to need more than that, the change is wrong.

### Automatic demotion

`PolicyDriftMonitor` compares realised outcomes against the model's predictions
and stops it proposing when live performance diverges from the training
baseline. It never re-arms itself. Re-arming is a human act, like every other
re-arming in this codebase.

Promotion is manual and demotion is automatic. Reversing that asymmetry is how a
system talks itself into a bigger position after a lucky streak.

---

## 9. Retraining, and what must never be automated

```bash
python3 tools/fetch_real_data.py --from 2018-01-01     # refresh the corpus
python3 tools/train_policy.py --data-dir data/real     # exit 1 == refused
python3 tools/run_evaluation.py --data-dir data/real   # the same gates as always
```

`train_policy.py` exits **1 when it refuses to promote**, so a nightly CI job
goes red on a bad model. That is the intent, not a bug: a green build for a
model that should not ship is worse than no build.

A refused artefact is written to `models/rejected/<run_id>/` so it can be
inspected. `models/current` is left exactly as it was — neither overwritten nor
deleted.

Rules for any automated retraining pipeline:

1. **It may write a model. It may not arm one.** Promotion needs a human and a
   token, and a pipeline that had the token would be a human in name only.
2. **Never tune against the holdout.** Any hyper-parameter chosen by looking at
   the held-out period has burned it, and the tool says so in its output.
3. **Never lower a promotion criterion to get a promotion.** They live in
   `policy.PromotionCriteria`, in code, where a change is reviewable — and
   deliberately not exposed as a CLI flag. A model that cannot clear 0.55 AUC
   has not found anything, and the correct response is to accept that.
4. **Bump `FEATURE_SCHEMA_VERSION` whenever a feature's meaning changes**, even
   if its name does not. `policy.load` refuses an artefact whose schema does not
   match, and that refusal is what stops a silently reordered feature vector
   producing plausible, wrong predictions forever.
5. **`MEMORY_ENABLED=0` when replaying history through the backtester.** A size
   throttle derived from the same trades being measured folds the result back
   into itself.
