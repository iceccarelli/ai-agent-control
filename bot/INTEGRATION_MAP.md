# INTEGRATION_MAP.md

The final call graph, config flow, and threading model of the repaired stack.

Everything below is verified by `tests/test_lifecycle_integration.py` (38 tests),
`tests/test_orchestrator.py` (21) and `tests/test_slice7_integration.py` (82),
plus the structural guards in `tests/test_no_dead_imports.py` (119).

---

## 1. Module dependency graph

```
                              config.py
                       (the ONE environment reader)
                                  |
     +--------------+-------------+--------------+---------------+
     |              |             |              |               |
persistence.py  market_data.py  risk_mgmt.py  position_sizing.py |
(SQLite state)  (OHLCV + L2,    (fail-closed  (equity-space      |
     |           no lookahead)   gates)        sizing)           |
     |              |             |              |               |
  memory.py         |             |              |               |
(shared reference)  |             |              |               |
     |              |             |              |               |
     +------+-------+------+------+--------------+---------------+
            |
    bybit_connection.py
 (one V5 client, two dialects: spot | linear)
            |
     trading_engine.py
   (one order lifecycle)
            |
         main.py
     (one orchestrator)
```

Dependencies point **downward only**. There are no cycles; `python -c "import
main"` resolves without any lazy-import trickery, which the legacy code needed
in ~40 places to break its own cycles.

Import direction is enforced by construction:

| Module | Imports from the repaired stack |
|---|---|
| `config.py` | *nothing* |
| `persistence.py` | *nothing* |
| `memory.py` | `persistence` |
| `market_data.py` | `config` (no state, no exchange, no engine) |
| `risk_management.py` | `config`, `persistence` |
| `position_sizing.py` | `config`, `risk_management` |
| `bybit_connection.py` | `config`, `market_data`, `persistence`, `position_sizing`, `risk_management` |
| `pure_indicators.py` | *nothing* (no I/O, no config, no state) |
| `technical_analysis.py` | `config`, `pure_indicators` |
| `features.py` | `pure_indicators`, `market_data` (no config, no state, no exchange) |
| `policy.py` | *nothing* from this stack; sklearn **optionally** |
| `ml_strategy.py` | `config`, and `features`/`policy` **lazily**, inside functions |
| `trading_engine.py` | all of the above, plus `memory` |
| `performance_analytics.py` | `persistence` |
| `main.py` | all of the above |
| `backtest.py` | all of the above (test/eval only, not imported by `main`) |

`market_data.py` importing only `config` is the property that lets the **same**
depth, imbalance and spread arithmetic serve a live REST snapshot and a
historical archive. A liquidity gate calibrated in backtest would mean nothing
if production computed those figures a second way.

The ML modules' import lists are the cheapest possible containment. `policy.py`
and `features.py` **cannot import** `risk_management`, `trading_engine`,
`persistence` or `bybit_connection` — a module that cannot reach the gates
cannot call them, cannot resize a position, and cannot place an order, whatever
its author intended. `test_policy_and_features_cannot_reach_the_risk_manager`
asserts that by parsing the imports.

`ml_strategy.py` imports `features` and `policy` *lazily, inside functions*, so
a deployment with `POLICY_MODE=off` — or without scikit-learn installed at all —
never loads them. An optional ML dependency must never be able to stop an
execution stack from starting.

---

## 2. The live path, end to end

`main()` → `build_bot()` → `TradingBot.run()` → `startup()` → `tick()` →
`TradingEngine.execute()`.

### Startup (fixed order — every step can refuse to continue)

```
1. config.assert_sandbox(cfg)          -> refuse if live is armed without full authorisation
2. config.is_live_authorized(cfg)      -> log the mode loudly
3. client.sync_time()                  -> refuse if the exchange is unreachable
4. client.set_leverage(symbol)         -> LINEAR ONLY; refuse to start if it fails
5. engine.reconcile()                  -> resolve every persisted order intent
     |- client.reconcile_on_startup()  -> query each pending order by orderLinkId
     |- protect any naked position, or emergency-close it
6. refuse to start if any order is UNRESOLVED
7. refuse to start if the kill switch is engaged  (human-cleared only)
8. start the health server
```

Leverage is set **before** reconciliation because reconciliation may need to
close a position, and the exchange rejects a reduce order on a symbol whose
leverage is unset. A position opened at the account's default leverage is a
position sized by something other than the risk model, so a failure here refuses
the start rather than proceeding.

### One trade (`TradingEngine.execute`)

Ten stages. **Each can only stop the trade** — none rescues an earlier failure
by retrying it differently, which is what produced the legacy double-submit.

| # | Stage | Module | Failure behaviour |
|---:|---|---|---|
| 1 | derive side from signal type | `trading_engine` | HOLD / unknown → no trade |
| 2 | read equity | `bybit_connection` | unreadable → no trade |
| 3 | fetch instrument filters | `bybit_connection` | unavailable → no trade (never fabricated) |
| 3b | read funding + order book **once** | `bybit_connection` | unreadable → `None`, and the gate decides |
| 4 | pre-trade risk gate (size-independent) | `risk_management` | any gate blocks → no trade |
| 5 | size the position | `position_sizing` | zero size → no trade |
| 3c | ask the model (if any) | `ml_strategy` → `policy` | unusable → no probability, and the edge gate gets STRICTER |
| 5b | apply the memory throttle | `memory` → `trading_engine` | can only SHRINK; below min qty → no trade |
| 6 | final risk gate (with the real size) | `risk_management` | blocks → no trade |
| 7 | validate the exit plan | `trading_engine` | incoherent → no trade, **before entering** |
| 8 | submit entry (idempotent) | `bybit_connection` | rejected → no position |
| 9 | confirm the fill | `bybit_connection` | unconfirmed → **no second entry**, cooldown |
| 10 | place stop, then **read it back** (`verify_stop`, dispatched by category) | `bybit_connection` | unverified → emergency close + kill switch |
| 11 | place take-profit legs | `bybit_connection` | rejected → logged, position stays protected |

### The invariant

> **A confirmed position always has a verified protective stop.**

Enforced at three points, tested at each:

- **Stage 10** — the stop is queried back from the exchange. A stop that cannot
  be confirmed is treated as no stop: the position is market-closed and the kill
  switch trips (`test_a_rejected_stop_closes_the_position_and_trips_the_kill_switch`,
  `test_an_unverifiable_stop_is_treated_as_no_stop`).
- **Startup reconciliation** — any position found without a stop is protected or
  closed (`test_restart_finds_and_protects_a_naked_position`).
- **The risk gate** — `_gate_naked_positions` blocks *new* trades while any
  existing position is unprotected (`test_a_position_without_a_stop_blocks_new_trades`).

Stop replacement is always **place-new-then-cancel-old**
(`test_stop_replacement_places_before_cancelling`). Cancel-first leaves a window
with no protection; place-first can briefly double the protective size, which is
the strictly safer failure.

---

## 3. Config flow

```
os.environ  ->  config.load()  ->  Config (frozen dataclass)  ->  every module
```

* `config.load()` is the **only** function in the codebase that reads the
  environment. Enforced by `test_only_config_reads_the_environment` over every
  repaired module.
* No module monkey-patches `os.getenv`. The legacy `_os.getenv = _cfg.getenv`
  shim existed in all nine modules; all are removed
  (`test_no_module_monkeypatches_os_getenv`).
* Nothing writes `os.environ` (`test_no_environ_writes_in_source`).
* `FORCE_LIVE` does not exist anywhere (`test_force_live_is_gone_everywhere`).
* The exported surface is twelve names, asserted exactly
  (`test_the_exported_surface_is_small_and_deliberate`). Every legacy
  compatibility alias is gone: the modules that needed them no longer exist, and
  an unused export is a future accident.
* Cross-field validation runs at load time, so incoherent limit sets are refused
  at startup rather than discovered mid-trade — a risk budget above the position
  cap, a daily-loss brake above the drawdown kill switch, or an edge floor below
  the round-trip cost all raise `ConfigError`.

### Units

One convention: **every `*_PCT` value is a fraction** (`0.02` == 2%). A value
greater than 1.0 is **rejected at load time**, not silently divided by 100 —
silent renormalisation is how the legacy code ended up reading the same variable
as 70% in one module and 0.7% in another, in the same process.

### The live gate

Arming real money requires **all four**, with no override:

```
USE_TESTNET=0  AND  PAPER_TRADING=0  AND  LIVE_TRADING_ACK=<exact string>
               AND  BYBIT_API_KEY and BYBIT_API_SECRET both present
```

Anything less degrades to paper and logs why. `assert_sandbox()` returns
`(may_start, reason)`; `is_live_authorized()` answers the separate question "is
real money armed?".

---

## 4. Threading model — enforced, not documented

**Single writer, many readers.** `StateStore.claim_writer()` records the trading
thread's id at startup; any *other* thread that then attempts a mutating call
raises `PersistenceError` instead of interleaving. Reads are unrestricted, so the
health server is never blocked.

This is deliberately stricter than a lock. A re-entrant lock makes concurrent
writes safe *at the SQLite level* while still allowing two threads to interleave
a read-modify-write on position state — which is precisely how a position book
and an exchange drift apart. Serialising the write does not serialise the
decision that produced it.

Proven by `TestSingleWriter` and `TestConcurrentIntegrity`: 400 concurrent
sequence draws with zero duplicates, 64 threads racing for one `orderLinkId`
with exactly one winner, 120 concurrent trade writes with none lost, and a peak
equity that never regresses under 8-way contention.

| Thread | Purpose | Touches trading state? |
|---|---|---|
| main | the entire trading loop: signals, gates, sizing, orders | **yes — sole writer** |
| `health` (daemon) | serves the health endpoint | reads only, via `StateStore` |
| signal handlers | set a `threading.Event` | no |

Rules:

1. **All order decisions and all order submissions happen on the main thread.**
   There is no worker pool, no `asyncio` loop, and no background order
   placement. The legacy module had a busy-spin monitoring thread per
   `TechnicalAnalysis()` instance — and constructed a new instance per signal, so
   it progressively starved itself of CPU.
2. **Shared state goes through `StateStore`**, which guards its single SQLite
   connection with a `threading.RLock` and commits synchronously. No unlocked
   dict is mutated across threads.
3. **`BybitClient` guards its filter cache with an `RLock`.** It holds no other
   mutable cross-thread state.
4. Shutdown is cooperative: `SIGINT`/`SIGTERM` set an `Event`; the loop finishes
   its current cycle and exits. Protective stops are deliberately **left in
   place** — a stop outliving the process is a feature.

---

## 5. Persistence and restart

Everything that must survive `kill -9` is in SQLite (`persistence.py`):

| State | Why it must persist |
|---|---|
| peak equity | legacy reseeded it from `0.0`, so drawdown read zero exactly during a crash-loop |
| daily anchor (UTC) | anchored at the first equity reading of the day, never re-anchored mid-day |
| breaker + cooldowns | stored as **epoch** deadlines, not `perf_counter()` (which resets to ~0) |
| kill switch | survives restart; **only a human clears it** |
| positions + their stops | so a restart cannot forget a position and stack on top of it |
| order intents | recorded **before** submission, so a crash mid-submit is recoverable |
| order sequence | monotonic across restarts — this is what makes `orderLinkId` idempotent |
| closed trades (fee-inclusive) | the Kelly input |
| decision journal | every block records its reason |

Proven by `TestStateSurvivesRestart` (5 tests) and `TestChaos` (6 tests).

---

## 6. Order idempotency

```
orderLinkId = <prefix>-<purpose>-<persisted_seq>-<blake2b(seq|symbol|side|qty|price|purpose)>
```

* The sequence number comes from SQLite, so it does not reset on restart.
* The intent is written to the store **before** the HTTP call.
* A transient failure retries with the **same** id; the exchange rejects the
  duplicate (retCode 170130) rather than opening a second position.
* An outcome that stays unknown after retries is left `pending` for
  reconciliation and is **never** resubmitted blind.

The legacy module had 21 generators, using `perf_counter()//30` buckets, `uuid4`,
`secrets.token_hex`, salted `hash()`, and one producing `"19700101"` from
`gmtime(perf_counter())`. None survived a restart.

---

## 7. What is NOT yet wired

Stated plainly, because an integration map that implies more than exists is
worse than none:

* **The private WebSocket is not connected.** Auth-frame construction is
  implemented and tested (`ws_auth_message`), but the socket loop is not wired.
  Fills are confirmed by REST polling in `_await_fill`. This costs latency, not
  correctness.
* **Intra-candle OCO race.** Bybit does not link a conditional stop to separate
  take-profit legs. If one candle sweeps both levels, both can fill. The engine
  cancels siblings on the next cycle; a genuine sub-cycle window remains.
* **No real market data has been used.** Bybit is unreachable from the
  development sandbox, so every backtest number is synthetic. See TEST_REPORT §5.

## 8. Data-flow contracts

Each boundary has a declared unit, and a test that it survives the crossing.

| Boundary | Carries | Unit | Test |
|---|---|---|---|
| config → every module | `*_PCT` | fraction (0.02 = 2%) | `test_pct_fields_are_fractions_everywhere_they_are_consumed` |
| config → risk | fees, spreads | **bps**, converted once | `test_bps_never_leaks_into_a_fraction_field` |
| strategy → engine | `TradeIntent` | prices absolute, TP fractions of position | `test_take_profit_legs_sum_to_the_position` |
| engine → sizer | `stop_loss_distance` | fraction of price | `test_stop_distance_fraction_survives_into_quantity` |
| sizer → engine | `qty` | base units, never notional | same |
| sizer → risk gate | `risk_fraction` | fraction of equity | `test_risk_fraction_round_trips_through_the_gate` |
| engine → exchange | prices | tick-aligned | `test_prices_crossing_into_the_exchange_are_tick_aligned` |
| engine → exchange | quantities | step-aligned, snapped **down** | `test_quantities_crossing_into_the_exchange_are_step_aligned` |
| exchange → store | fills | fee-inclusive | `test_ledger_explains_the_entire_equity_change` |

The last one is the strongest available assertion: after a full backtest, the
sum of recorded trade PnL must equal the change in equity to within a cent. A
gap means the book and the exchange disagree — which is how the same-symbol
re-entry defect was found (equity +7,673, ledger −26).

---

## 9. The gate chain, in evaluation order

`gate_order` returns the **first** blocking decision. Order is deliberate:
account-level facts before trade-level ones, and cheap definitive checks before
anything that needs a network read.

| # | Gate | Blocks when | Skips when |
|---:|---|---|---|
| 1 | `_gate_short_capability` | sell-to-open on a venue without shorts | side is not Sell |
| 2 | `_gate_kill_switch` | engaged — **human clears only** | never |
| 3 | `_gate_breaker` | circuit breaker is cooling down | never |
| 4 | `_gate_consecutive_losses` | streak at the limit | never |
| 5 | `_gate_symbol_cooldown` | this symbol traded too recently | never |
| 6 | `_gate_daily_loss` | today's loss exceeds the brake | never |
| 7 | `_gate_drawdown` | drawdown from persisted peak exceeds the limit | never |
| 8 | `_gate_naked_positions` | **any** position lacks a stop | never |
| 9 | `_gate_existing_position` | already in this symbol | never |
| 10 | `_gate_position_count` | at max open positions | never |
| 11 | `_gate_stop_sanity` | stop missing, or on the wrong side of entry | never |
| 12 | `_gate_confidence` | below the floor, or outside [0,1] | confidence is `None` |
| 13 | `_gate_risk_reward` | reward/risk below the minimum | no take-profit supplied |
| 14 | `_gate_expected_edge` | expected move cannot pay the round trip | never |
| 15 | `_gate_spread` | spread too wide, quote invalid or unparseable | no quote supplied |
| 16 | `_gate_data_freshness` | data stale, or age unknown | age not supplied |
| 17 | `_gate_funding` | carry unknown or too expensive **(perps)** | category is spot |
| 18 | `_gate_book_liquidity` | exit side too thin, or book stacked against | no book supplied |
| 19–22 | per-trade risk, notional, exposure, correlation bucket | any cap exceeded | quantity is 0 (pre-trade probe) |

The three "skips when" values that are *not* `never` share one rule: a gate
skips only when the caller supplied **nothing** to evaluate. Supplying a bad
value always blocks. `None` funding on a perpetual blocks, because an unknown
carry is not a zero carry — and funding is the one cost that accrues while the
bot does nothing at all.

Any exception anywhere in the chain returns `GATE_EXCEPTION`, which blocks. That
is the whole point of the file.

---

## 10. The memory layer — what may read it, and what it may do

`memory.py` is a read-mostly view over `StateStore`. It is constructed **once**,
in `TradingBot.__init__`, and handed to everything that needs it.

| Reader | Uses | Effect on live behaviour |
|---|---|---|
| `trading_engine` | `size_multiplier` | **shrinks** a position, or skips it |
| `trading_engine` | `record_execution` | write only — intended price versus fill |
| `technical_analysis` | `remember` | write only — counts suppressed shorts |
| `main.health()` | `snapshot` | read only — served to the operator |

The one authority it holds over live behaviour is `size_multiplier`, clamped to
`[MEMORY_MIN_THROTTLE, 1.0]` **at its source**. The engine applies it with a
single multiplication, and `test_the_engine_multiplies_and_never_divides`
asserts that at the source level — a division would turn the same clamped value
silently into an enlargement.

What it deliberately **cannot** do:

* raise a position size (a value ≥ 1.0 means "no reduction warranted", never "grow");
* relax any gate — no gate reads memory;
* clear the kill switch — the string does not appear in the module, asserted by
  `test_memory_cannot_clear_the_kill_switch`;
* persist anything that looks like a credential — the write is **refused**, not
  redacted, because the store outlives the process and a redactor only has to be
  forgotten once.

A bot that grows its size after a winning streak reaches its largest position
immediately before the streak ends. Experience here is allowed to make the bot
more careful and is not allowed to make it braver.

Health is served from a **different thread**, so `snapshot()` performs only
reads — which `StateStore` permits from any thread, unlike writes.

---

## 11. Market data — one measurement, two sources

```
data/*.csv.gz  --load_ohlcv/load_book_events-->  replay()  --> per-bar ladders
                                                                     |
GET /v5/market/orderbook  ------------------------------------>  BookState
                                                                     |
                                                              BookFeatures
                                                     (spread, depth, imbalance,
                                                      microprice, slippage)
                                                                     |
                                                          risk._gate_book_liquidity
```

`replay()` folds in only those events whose timestamp is at or before each bar's
close, and its cursor never reads ahead. Lookahead is not prevented by a check —
it is unreachable. `load_corpus()` asserts one book snapshot per bar, so a future
change that skipped a bar would raise rather than shift every book by one and
produce a subtly optimistic result.

The cut uses `time_coinapi` (when the data was *received*), not `time_exchange`.
The difference is a few hundred microseconds on this corpus and therefore
harmless — which is exactly why it has to be right here. The same code against a
feed with 400 ms of latency would be granting the strategy a whole trading
decision of free foresight.

---

## 12. Where a learned model sits, and what bounds it

```
   candles (closed bars, from the SAME fetch the strategy used)
        |
   features.compute_features   <- slices to [i-264 : i+1] before computing;
        |                          the future is not present, not merely unused
   policy.predict_edge         <- refuses on NaN, cold window, schema mismatch,
        |                          or an unpromoted artefact
   ml_strategy._attach_probability
        |                       <- writes EXACTLY ONE field: win_probability
   TradeIntent
        |
   risk_management.gate_order  <- all 22 gates, unchanged, in the same order
        |
   position_sizing -> memory throttle (shrinks only) -> trading_engine
```

The model is upstream of every gate and downstream of nothing. It occupies
exactly the position a hand-written estimate would occupy, which is the point:
nothing in the safety architecture had to be relaxed to admit it, and nothing in
the safety architecture knows or cares that it exists.

### The containment, as five assertions

| Claim | How it is enforced | Test |
|---|---|---|
| The model writes one field | `_attach_probability` is AST-parsed; the set of replaced fields must equal `{win_probability}` | `test_the_only_field_the_model_writes_is_win_probability` |
| The model cannot reverse a trade | a disagreeing direction returns the intent unmodified | `test_the_model_cannot_reverse_a_trade` |
| The model cannot create a trade | no classical intent → `None`, however confident | `test_the_model_cannot_create_a_trade_from_nothing` |
| A broken model tightens the system | no probability → the gate falls back to reward-only | `test_a_broken_model_makes_the_system_MORE_cautious` |
| Shadow mode cannot change an order | the *same object* is returned, asserted with `is` | `test_in_shadow_the_intent_is_returned_byte_for_byte_unchanged` |

### Two ordering rules worth naming

**The model sees the same bars the strategy saw.** `MarketStrategy` stashes
`last_rows`; `ml_strategy` reads those rather than re-fetching. Two fetches a
few hundred milliseconds apart can straddle a bar close, and then the model and
the strategy are reasoning about different markets while appearing to agree.

**The model is asked before sizing, not after.** Its probability is an input to
the cost gate, which runs before the sizer. A model consulted *after* sizing
could only be used to adjust a quantity, and adjusting a quantity is exactly the
authority it must not have.

---

## 13. Config flow, extended: two arming gates with the same shape

```
os.environ -> config.load() -> Config (frozen) -> every module
                                 |
                                 +-- LIVE_AUTHORIZED  (real money)
                                 +-- POLICY_ARMED     (model proposals)
```

Both are **derived**, both require an exact acknowledgement token, both degrade
safely rather than failing, and neither can be set directly:

| | live trading | learned policy |
|---|---|---|
| switch | `USE_TESTNET=0` + `PAPER_TRADING=0` | `POLICY_MODE=live` |
| token | `LIVE_TRADING_ACK=I_UNDERSTAND` | `POLICY_ACK=I_UNDERSTAND_MODEL_RISK` |
| also needs | both credentials present | an artefact that cleared promotion |
| if incomplete | degrades to paper, logs why | degrades to shadow, logs why |
| derived field | `LIVE_AUTHORIZED` | `POLICY_ARMED` |
| set by hand? | no — ignored | no — ignored |

Same shape on purpose. They are the same kind of decision: something that can
lose money is being switched on, and it should take a specific human act rather
than a default.
