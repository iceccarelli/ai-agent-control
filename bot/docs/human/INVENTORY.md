# INVENTORY — what is wired, what is merely present

Facts only. Written 2026-09-11 against `241edc4` (0028) + 0029. Every line is
either a file:line, or a command whose output was watched in the session that
wrote it. Where a later patch closes a line, the STATUS column says which one.

Headline, unchanged by anything below:

> **paired executor built, never touched a venue, do not point it at money.**

---

## 1. What `main.py` constructs when `BOOK_MODE=carry`

`build_bot()` (`main.py` ~612–665), in order:

| Object | Arguments actually passed | Not passed |
|---|---|---|
| `CarryBroker` | `client=bot.client` (a `BybitClient`), `sequence_source=store.next_order_seq` | — |
| `CarryEngine` | broker, `kill_switch=store.trip_kill_switch`, `CARRY_SPOT_SYMBOL`, `CARRY_PERP_SYMBOL`, `max_notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD` | **`borrow_apr`** → constructor default `0.05` is used silently |
| `CarryRisk` | `store`, `max_notional_usd=shadow.SHADOW_MAX_NOTIONAL_USD` | — |

`main.tick()` (~440–478) when `self.carry` is set: `take_snapshot(broker)` →
`view.assert_fresh()` → `carry.snapshot = view` → `carry.on_candle(mark,
funding_bps, spot, timestamp_ms)` → log → `return`. The directional voter is
never consulted. Loop interval: `LOOP_INTERVAL_SECONDS`, default **60 s**.

## 2. The live tick path — on it vs merely present

| Module / function | On the tick path? | Evidence |
|---|---|---|
| `market_snapshot.take_snapshot`, `assert_fresh` | **yes** | `main.py` ~450 |
| `CarryEngine.on_candle` → `_open` / `_rebalance` / `_unwind` | **yes** | `main.py` ~462 |
| `carry_costs.evaluate_entry` (3 gates) | **yes**, lazy import | `carry_engine.py` ~294 |
| `CarryRisk.gate_open` / `record_entry` | yes, **but skipped** when `pair_risk` or `snapshot` is `None` | `carry_engine.py` `_open` |
| `CarryBroker.place_market`, `get_mark`, `get_spot_mark`, `get_funding_bps`, `get_margin_multiple` | **yes** | via engine / snapshot |
| `CarryBroker.get_book_top`, `place_post_only`, `cancel_order` | **yes when `CARRY_EXECUTION_STYLE=maker_first`**, and only for the overlay's entry leg; every exit and both ACQUIRE entry legs cross (0040) | `carry_engine.py` `_rest`; AST invariant in `tests/test_carry_maker.py` |
| `CarryBroker.reconcile_pair` | **no — zero call sites outside tests** | `grep -rn reconcile_pair --include=*.py . \| grep -v tests` |
| `CarryBroker.get_venue_time_s` | **does not exist** → the clock-skew check in `assert_fresh` never runs | `hasattr(bot.carry.broker, "get_venue_time_s") → False` |
| `tools/fund_demo.py` | no — funds a DEMO wallet; refuses on any other base URL, checked against the URL the client is pointed at rather than a config flag | `docs/human/VENUE_0046.md` |
| `tools/drill.py` | no — the Phase D drill; reads only without `--arm`, and with it goes through the engine and the broker `build_bot` made | `docs/human/DRILL_0045.md` |
| `ledger.py` | **yes** — `build_bot` wraps `CarryBroker` in `LedgerBroker`, the journal loads from `carry_ledger` at startup and `tick()` posts funding and writes it back | `docs/human/LEDGER_0044.md` |
| `tools/carry_replay.py` | no — offline, but it drives the REAL `CarryEngine` through `main.tick`'s sequence, so it is the only tool whose numbers are the trading class's own | `docs/human/REPLAY_0043.md` |
| `tools/fly_stack.py` | no — offline; renders and checks `bot/fly.toml`, and reimplements Docker's context filter so an unbuildable image fails the suite | `docs/human/FLY_RUNTIME_0042.md` |
| `tools/carry_walkforward.py` | no — offline; 20,520 native simulations in 1.6 s, the first decision-grade use of the 0039 core | `docs/human/WALKFORWARD_0041.md` |
| `tools/carry_backtest.py`, `tools/venue_study.py`, `tools/basis_at_settlement.py`, `tools/corpus_health.py` | no — offline tools | — |
| persistence of the carry position | **0037: yes** — `StateStore.save_carry_position` / `load_carry_position`, one row, written by the engine on every state change and read back by `TradingBot.carry_cold_start` before the first tick | `carry_position` table; `bot/tests/test_carry_cold_start.py` |

## 3. README claims vs bytes

| README says | Bytes say | STATUS |
|---|---|---|
| `verify.sh` "must print: 5162 passed, 2 skipped" | 0028 = 5366 passed on 3.12; on python 3.11 (the Dockerfile's) 1 failed | 0029 fixed the 3.11 failure |
| "There is no two-leg carry backtester anywhere in this tree" | `tools/carry_backtest.py` exists since 0018 | stale line |
| "No borrow / margin cost model" | `carry_costs` gate 2 charges borrow — but `build_bot` never passes `borrow_apr`, so the live engine uses a silent `0.05` | 0033 |
| "No basis monitoring" | basis is gated at ENTRY (`carry_costs` gate 1); nothing watches it during a hold | open |
| "No P&L attribution" | backtest decomposes funding/basis/fees/borrow; live path books none of it | backtest: 0031; live: Phase E |
| "Run the book against no venue: `BOOK_MODE=carry USE_TESTNET=1 PAPER_TRADING=1`" | **false.** `PAPER_TRADING` gates the directional `TradingEngine` only. `CarryBroker` calls `client._request("POST", "/v5/order/create")` directly. With `USE_TESTNET=0 PAPER_TRADING=1` config reports `SANDBOX (PAPER)` and the carry broker still sends a **mainnet** order body (watched, §4 D2) | 0033 |
| "Three consecutive negative funding prints → exit" | the engine counts **calls to `on_candle`**, one per 60 s tick. Three negative *minutes* of the ticker's predicted rate unwind the book (watched, §4 D1) | **CLOSED 0034** |
| "get_margin_multiple RAISES when unreadable" | true. It returns `positionIM / positionMM`, which on Bybit is set by leverage and risk tier and does **not** shrink as price squeezes the short (§5 F4) | open, Phase D |
| "reconcile_pair OBSERVES ONLY and names the naked side" | true, and nothing calls it | open, Phase D |
| "No cold-start reconciliation" | true. Worse: the directional orphan check (`bybit_connection.py` ~1557) runs only `if self.is_linear`; the default client category is `spot`, so it is skipped | open, Phase D |
| Roadmap rows 0018–0027 | what landed under those numbers differs (0018 backtest … 0028 venue study) | stale table |
| `6.84%/yr` | README correctly calls it gross income, not a return | ok |

## 4. Defects found by running, ranked by damage

Reproduced in-session with a stub broker; no venue.

| # | Defect | Reproduction / evidence | STATUS |
|---|---|---|---|
| D1 | **Funding is booked per tick, not per print.** `on_candle` adds `funding_bps × notional` on every call. 60 one-minute ticks at 1.0 bps on $100 booked **$0.60**; one real 8h print is **$0.01**. The negative-funding streak and the 8-print EWMA also count ticks. | stub engine, 60 calls | **CLOSED 0034** — prints carry a settlement stamp; a repeated stamp is a tick, not a print; 60 ticks book one print |
| D2 | **`PAPER_TRADING=1` does not stop carry orders.** `USE_TESTNET=0 PAPER_TRADING=1 BOOK_MODE=carry` → `assert_sandbox` = `SANDBOX (PAPER)`, and `bot.carry.broker.place_market(...)` sent `POST /v5/order/create` with a spot body. The client's base URL is chosen by `USE_TESTNET` alone. | `build_bot` + mocked `_request` | **CLOSED 0033** — `CarryBroker(order_gate=...)` required; PAPER refuses; mainnet needs live authorisation AND a signed promotion gate, asked per order |
| D3 | **Restart while HEDGED opens a second pair.** The position is memory-only, `reconcile_pair` is never called, the orphan check is skipped on a spot-category client, and `CarryRisk.has_open_pair` reads `engine.position` (None after restart). | code path | **CLOSED 0037** — the position is written to `carry_position` on every change and reconciled against the venue before the first tick; any disagreement halts and refuses to start |
| D4 | **A venue that rejects the perp leg drains the book.** Spot buys, perp is rejected, spot is sold, state returns to FLAT, and the next tick repeats: **8 spot round trips in 10 ticks**. `record_entry` is (correctly) only called for a landed pair, so a broken pair consumes no allowance. | stub broker rejecting `linear` | **CLOSED 0033** — a broken pair spends the day (`CarryRisk.record_broken_pair`); 10 ticks → 1 spot round trip |
| D5 | **`carry_backtest` prints `QUOTABLE: True`** on daily closes with no impact term. The flag tracks provenance only (same venue + same quote). Rule 19 says daily-close settlement is not quotable. | `python3 tools/carry_backtest.py --repo .` | **CLOSED 0031** — five conditions, printed; attribution identity enforced |
| D6 | **carry_backtest / venue_study reads are not in the data-read ledger.** `reserved_holdout.install()` hooks `market_data.load_corpus`; both tools read with their own loaders, so the hook never fires. `artifacts/data_read_ledger.json` has 6 reads, none by either tool. Worse: run as a script, carry_backtest's `install()` raised on `import market_data` and the bare except set the whole ledger to None. | `data_read_ledger.json` | **CLOSED 0032** — explicit `_record()` in both tools; 15 retroactive + 6 new reads |
| D7 | `borrow_apr` has a default at every level: `CarryEngine(..., borrow_apr=0.05)`, `evaluate_entry(..., borrow_apr=DEFAULT_BORROW_APR)`, `simulate(..., borrow_apr=BORROW_APR)`; `build_bot` passes none. | `bot.carry.borrow_apr → 0.05` | **CLOSED 0033** — no default in `CarryEngine` or `evaluate_entry`; `CARRY_BORROW_APR` required for carry, refused when absent |
| D8 | The pair gate is skipped when `pair_risk` **or** `snapshot` is `None`, and `test_carry_live_gates::test_the_gate_is_skipped_only_when_absent` asserts that a bare engine opens. | `carry_engine.py` `_open` | **CLOSED 0033** — `PAIR_GATE_ABSENT` / `SNAPSHOT_ABSENT` / `SNAPSHOT_MISMATCH`; the test asserting the bypass was inverted |
| D9 | Clock-skew check is dead code on the live path (no `get_venue_time_s`). | §2 | open |
| D16 | **`session_tail` reported UNREADABLE as a violation.** Run with a bare interpreter it printed `GATE STILL FALSE?: NO (UNREADABLE: No module named numpy)` — which reads as "the gate opened". | seen in Codespaces | **CLOSED 0037** — three outcomes and three exit codes |
| D15 | **The engine bought spot the client already owned.** PHASE1_DECISION chose the overlay on 2026-09-08; the engine went on paying a four-leg round trip and financing for inventory that was already there. | `--clock 8h --mode overlay` | **CLOSED 0036** — `CARRY_EXECUTION_MODE` required; overlay trades the perp only (`docs/human/OVERLAY_0036.md`) |
| D10 | `funding_collected` books only non-negative prints; negative prints are paid and never booked. | `carry_engine.py` on_candle | **CLOSED 0034** — every held print booked signed |
| D11 | The carry path records no fees. `Leg` has no fee field; `LegFill` ignores `cumExecFee`. | `carry_engine.py`, `carry_broker.py` | **0033 partial** — `cumExecFee` carried per leg; unknown is `None`, never 0.0. Booking it is Phase E |
| D12 | **The engine runs the GATED rule.** `on_candle` always calls `evaluate_entry`. The gated column is in-sample (PHASE1_DECISION). The clean, ungated column is not what the engine does. There is no out-of-sample number for the rule the book would run. | `carry_engine.py` ~294 | **recorded 0032** (`carry_cost_gates_v1@BTCUSDT`, forward-only holdout); a quotable condition |
| D13 | `deflated_sharpe` refused zero variance on 3.12, not on 3.11. | suite on 3.11 | **0029** |
| D31 | **The drill could never attach the book it drills.** `_load_bot` called `build_bot(attach_strategy=False)` to keep the directional voter out — and the CARRY book is gated behind the same flag, so it returned `carry is None` and the drill reported "BOOK_MODE is not carry" against a config that plainly said carry. It would have failed identically on Fly, where the preflight is the whole point. | `python3 tools/drill.py` with carry env set | **CLOSED 0047** — `build_bot()` with no flag, as `main()` does: the config decides, and the carry branch returns before the voter is constructed. An AST test refuses any `attach_strategy=` keyword on that call |
| D32 | **A read-only tool modified a tracked file.** `STATE_DB_PATH` defaults to `state/trading_state.db` — the committed FIXTURE — so `tools/drill.py` left it dirty, and `scripts/verify.sh` then refused a receipt ("FIXTURE DB MUTATED"). Run the preflight, then run the suite, and the suite refuses. | `git status` after a preflight | **CLOSED 0047** — the drill takes a scratch database outside the repository unless `STATE_DB_PATH` names one (on Fly, fly.toml does). A test fingerprints every file in `state/` before and after a preflight |
| D33 | **`deploy.sh` looked for `fly`; the installer lays down `flyctl`.** So it reinstalled flyctl on a machine that already had it, then called a command that was not there. | `bash: fly: command not found` with `which flyctl` succeeding | **CLOSED 0047** — one resolved `$FLY`, `flyctl` preferred; a test refuses any bare `fly ` call left in the script |
| D29 | **`USE_TESTNET` is a boolean and there are three venues.** Bybit demo trading (`api-demo.bybit.com`) funds itself by API and runs on REAL mainnet market data, which removes the testnet-faucet blocker — but adding a third destination to a boolean is how a testnet key authenticates against mainnet. | 0046 design | **CLOSED 0046** — `BYBIT_VENUE` is the three-way authority, `USE_TESTNET` is DERIVED from it so every existing gate keeps working, and a config that states both and contradicts itself is refused. Demo is a sandbox at `is_live_authorized`, `assert_sandbox` and the carry order gate, which names it so a transcript cannot be read as mainnet evidence |
| D30 | **The drill died with a numpy traceback under a bare interpreter.** `ModuleNotFoundError` from four imports deep — D16 again: a tool whose first failure is a traceback about a transitive dependency is a tool that gets run wrong at 2am. | `python3 tools/drill.py` in Codespaces | **CLOSED 0046** — the import moved into `_load_bot()`; a failure names the virtualenv and exits 2, the same code `session_tail` uses for "could not read" as distinct from "this is wrong" |
| D27 | **There was no drill.** Phase D says "recorded drill"; what existed was a list of steps in prose, with nothing checking they happened or happened in order. | there was nothing to run | **CLOSED 0045** — `tools/drill.py`: twelve stages, each producing a venue-returned NUMBER rather than a checkmark, refusing to advance on failure and writing a transcript. Safe without `--arm`; no order path of its own (tests assert it builds no client and never touches `_request`). Every failure mode exercised against a venue that misbehaves on command |
| D28 | **A freshly started book is blind for 24 hours.** `evaluate_entry` refuses below EWMA_MIN_PRINTS settled prints, prints come every 8h, so a new deploy — new volume, first run, a machine the host moved — stood aside with `INSUFFICIENT_FUNDING_HISTORY` for a day while `/v5/market/funding/history` was returning the last 200 prints on request. | the armed drill could not open on its first run | **CLOSED 0045** — `warm_funding_history()` seeds the EWMA from SETTLED prints and `carry_cold_start` reads them before the first tick. No look-ahead: already settled, already paid. Refuses to overwrite a restored history; the newest stamp becomes the watermark |
| D26 | **No fee was ever booked and there was no ledger.** The engine knew `funding_collected`; nothing recorded a cost, reconciled a balance, or produced a statement. D11 had said so since 0033. It is why 0043's funding defect ran for four years unnoticed: nothing added the books up. | there was no statement to run | **CLOSED 0044** — `bot/ledger.py`, a double-entry journal where every entry balances to zero or is refused, append-only with reversing entries, wrapped around the BROKER so a fill cannot escape it. Over the Bybit corpus it reproduces the replay's four-year P&L **to the cent** in three cost configurations (`docs/human/LEDGER_0044.md`) |
| D23 | **Every published number came from a program that is not the trading program.** `tools/carry_backtest.py` does not import `CarryEngine` and never has: it shares `carry_costs.evaluate_entry` and reimplements sizing, both-legs-or-neither, the streak, the margin floor, the unwind, the fee accounting and the day's allowance. It printed `[x] rule_is_what_the_engine_runs`, which is true of the GATE and was never true of the STATE MACHINE. | `tools/carry_replay.py --diff` | **CLOSED 0043** — the real engine is driven through history via `main.tick`'s exact sequence. Engine **+7.25 %/yr** vs simulator **+7.23 %/yr**, 79 trades each, 90.98% exposure each; the $70.63 residual is attributed to the cent and both tests pin it |
| D24 | **The risk gate read the wall clock.** `CarryRisk.gate_open` and `record_entry` dated entries from `datetime.now(utc)` while `_open` had `timestamp_ms` in hand and never passed it. NOT a live bug — the two clocks coincide live — but the daily allowance could only be observed in production: replayed, the first run opened **1 trade in 4.11 years** and refused 3,571 times with `ENTRY_LIMIT_REACHED_TODAY`. | first run of the replay | **CLOSED 0043** — the tick's market time is passed to the gate; live behaviour byte-identical |
| D25 | **Funding was booked on the entry price.** `funding_collected += rate * perp.notional`, and `notional` is `filled_qty * avg_price`, frozen at the fill. The venue pays on the position's value at the SETTLEMENT mark. The engine booked **$29,371 where the same 79 trades earn $39,444** — a quarter of the funding missing — and **the error's sign follows the price**, which is intolerable in a book whose claim is that price direction does not matter. Changes no decision; it is the number Phase E would call a bankable ledger. | replay vs simulator, funding line | **CLOSED 0043** — booked on `filled_qty * mark`; the residual (tick mark vs settlement mark, <= LOOP_INTERVAL_SECONDS) is named |
| D21 | **The image had never built.** `.dockerignore` excluded `tools/` with no negation while the Dockerfile does `COPY tools/connector_check.py tools/session_tail.py`. Docker filters the build CONTEXT before the Dockerfile runs, so that COPY fails with "file not found". 0038's task definition, its one-off connector task and the entire Phase D drill plan all pointed at an artefact nobody could produce. `test_dockerfile.py` checked the COPY LIST against the import closure and never asked whether the context could deliver it. | `fly_stack.context_conflicts()` | **CLOSED 0042** — `tools/` no longer excluded; the no-operator-scripts protection is now static (the Dockerfile names each file; a test refuses any bare-directory COPY) because re-inclusion under an excluded dir cannot be exercised without a daemon. Docker's filter is reimplemented and run on every suite |
| D22 | **`main.py` exits 1 before the health server binds when the venue is unreachable.** Correct — fail-closed — but it is what the restart policy has to be chosen for, and it means a deploy against an unreachable venue never becomes healthy. | image tree reconstructed from the COPY list, run with the .dockerignore filter applied: `CRITICAL cannot reach the exchange: time sync transport error` | **MEASURED 0042** — `restart.policy = "on-failure"`, and the health check's `grace_period` is 180s because cold start reconciles before the bind |
| D18 | **The rule sweep searched an axis the rule does not read.** `may_open_gated` never looks at `entry_bps` — it decides on the EWMA, the cost of capital and the basis budget — so 0039's 1,440 cells were 60 distinct gated rules printed 24 times, the multiple-testing correction was fed a number describing the loop, and `ewma_alpha` was never searched at all. | `entry 0.1 -> $29,696.41` = `entry 2.4 -> $29,696.41` | **CLOSED 0041** — axes are now `hold_days x negative_exit_prints x ewma_alpha`, 540 distinct rules; `grid_id` changed so the registry cannot reuse the old key |
| D19 | **The shipped exit rule churns 10x for the same exposure.** Blind monthly refitting over 38 OOS months: 57 trades and $6,353 of fees against **6 trades and $779**, at the same 89-92% market exposure. It leaves after 3 negative prints; the refit picks 6, and funding is positive 85.4% of the time. | `tools/carry_walkforward.py --repo .` | **MEASURED 0041, NOT ADOPTED** — the cheap line holds **168 days per trade** against 18, so it is a different risk product whose short must survive a +168% adverse excursion (F4, open). Changing the exit rule is a new registered hypothesis, not a retune (rule 20) |
| D20 | **Neither rule survives its own search width.** Deflated Sharpe on the 38 OOS months: walk-forward +0.635 against an expected-max of +0.663 under 20,520 trials (z **-0.272**); the frozen constants +0.495 against +0.506 under 540 (z **-0.109**, an upper bound — they were chosen after 0018 with this corpus visible, so 540 is a floor). Best 6 months of 38 carry 59-72% of the net; skew +3.2, kurtosis 15. | same | **open — this is the headline.** The corpus is too short and too spiky to establish that any version beats zero. Only a registered FORWARD holdout can, and `docs/human/WALKFORWARD_0041.md` says why that is now the work that matters |
| D17 | **Every fill crosses the spread.** The book placed market orders only, so the overlay paid 11.0 bps a round trip when this account's maker tier is 4.0. On the Bybit settlement clock, gated, 0% borrow: $8,780.94 of fees against $39,444 of funding. | `--clock 8h --mode overlay --gated` | **0040** — `CARRY_EXECUTION_STYLE=maker_first` rests the overlay's entry at the touch and crosses what does not fill. Worth **at most +1.36 %/yr** ($8,781 → $3,193 of fees, +7.23 → +8.59 %/yr, 54 → 42 losing trades); the realised value is `fill_rate ×` that and the fill rate is **unmeasured** (`docs/human/MAKER_FIRST_0040.md`). Default stays `taker` |
| D14 | **On the settlement clock the engine's exit rule churns.** Three negative PRINTS (one day) exit; 86 of 87 ungated trades exit that way, median hold 5.3 days vs `ASSUMED_HOLD_DAYS` 30; fees $313 vs funding $27 per median trade. Ungated at 0% borrow: +9.49%/yr (daily) → +2.24%/yr (8h, Bybit, impact). Not retuned (rule 20). | `--clock 8h --matrix` | **0036 addressed the cost side**: overlay pays 11 bps, not 31 (+2.24 → +6.99%/yr at 0% borrow). The exit rule itself is untouched and still marginal at the median hold — a new rule is a new, forward-scored hypothesis |

## 5. FakeClient-only assumptions — venue semantics never exercised

| # | Assumption in code | Venue fact (Bybit V5, from `research/exchange_study/bybit/*` or `bybit_connection.py`) | STATUS |
|---|---|---|---|
| F1 | Spot market Buy `qty` is in BTC | Without `marketUnit=baseCoin` a spot market BUY is read as a **quote (USDT)** amount. `BybitClient` sets it (`bybit_connection.py` ~916); `CarryBroker` builds its own body and omits it. | **CLOSED 0033** — `marketUnit=baseCoin` on every spot market order |
| F2 | `/v5/order/create` returns `cumExecQty`, `avgPrice` | It returns `orderId`, `orderLinkId`. The broker then queries `/v5/order/realtime` immediately — a race against the fill. | open, testnet |
| F3 | The spot fill equals the BTC received | Spot taker fee on a BUY is charged in BTC. Wallet = `cumExecQty − fee`; the perp hedges `cumExecQty`; `reconcile_pair` (dust 1e-6) would call every pair an INCIDENT. | **0044 partial** — the ledger books the fee in BTC where the venue takes it and `hedgeable_btc()` returns the wallet, not the fill. Whether Bybit does this is still unobserved: open, Phase D |
| F4 | `positionIM / positionMM` measures liquidation headroom | It is a leverage/risk-tier ratio (risk_limit tier 1: IM 0.66%, MM 0.33%). Under UTA cross margin liquidation is account-level (`accountMMRate`). The margin floor would not trip on a squeeze. | open, Phase D |
| F5 | Any `qty` is accepted | Linear BTCUSDT `qtyStep = minOrderQty = 0.001`. At the $100 cap and BTC ≈ $79k the engine asks for 0.00127 → not a step multiple → perp rejected → D4. At BTC > $100k no valid perp size fits under $100 at all. | **CLOSED 0036** — lot rules read from `/v5/market/instruments-info`, size snapped to the step, refused below the minimum |
| F6 | Ticker `fundingRate` is the funding print | It is the rate for the NEXT settlement (predicted); the settled print is `/v5/market/funding/history`. | **CLOSED 0034** — `get_funding_print` reads `/v5/market/funding/history`; the snapshot keeps forecast and settled apart; a missed settlement is a stale view |
| F7 | 110072 / 170130 mean "the order exists" | Taken from docs; never observed. | open, testnet |
| F8 | No rate-limit / 10006 / maintenance handling in `CarryBroker` | A transient error on the perp leg returns `None` → emergency spot unwind. | open, testnet |
| F9 | `symbol[:-4]` is the base coin | True for `BTCUSDT` only. | open |
| F10 | A post-only order the venue has just been asked to create, and for which `/v5/order/realtime` returns no row, was **rejected** | Assumed, not observed. If Bybit drops a fully-FILLED order from `realtime` instead, `place_post_only` raises `PairIncident` and the book HALTS — fail-closed, but a spurious halt. No error code is interpreted anywhere on this path, deliberately (0040). | open, Phase D drill |
| F11 | A resting post-only order can be cancelled, and what it filled read back | Neither call has touched a venue. A cancel that arrives late is handled (the fill is read from the order, never from the cancel's response); a `realtime` query that fails after a cancel HALTS. | open, Phase D drill |

## 6. Secrets

Scanned on 2026-09-11: all 56 commits (`git log --all -p`) and the three
committed archives (`files (3).zip`, `tradingbot_slice31 (1) (1).zip`,
`tradingbot_slice76 (2) (1).zip`, unpacked). Patterns: `API_KEY|API_SECRET`
followed by ≥16 alphanumerics, `AKIA[0-9A-Z]{16}`, `ghp_`, `github_pat_`,
`sk-`, `xox?-`, `AIza`, `-----BEGIN … PRIVATE KEY`, and any added file named
`.env*`, `*secret*`, `*credential*`, `*.pem`, `*.key`.

Result: **no key material.** Hits were test sentinels
(`bot/tests/test_memory.py`: `"AKIA_NOT_A_REAL_KEY_9times"` and a 26-char
`s3c…` placeholder used to prove secrets never reach memory) and an
`.env.example` with empty values. No `REVOKE.md` is written because nothing was
found to revoke.

Standing rule regardless of this result: the predecessor programme recorded
that keys were exposed in an earlier history. Any Bybit key created before this
repository existed is **burned** — revoke it in the Bybit UI. New keys:
withdrawals disabled, IP-pinned to the NAT EIP, injected at runtime from
Secrets Manager, never in a file, a log, or this repository.

## 7. Invariants, checked mechanically

```
python3 tools/session_tail.py
```

prints the four invariants and exits **0** when they hold, **1** when one has
MOVED, **2** when one could not be READ (0037 — run outside the virtualenv it
used to print "NO" for a gate it had merely failed to import). Run it with the
interpreter the suite uses:

```
. .venv/bin/activate && python3 bot/tools/session_tail.py
```

## 8. Market data present

| Series | Rows | Range (UTC) | sha256 (uncompressed, 16) | Status |
|---|---:|---|---|---|
| `data/real_linear_1d/…/BINANCE_LINEAR_BTC_USDT_1D` | 1,476 | 2022-08-10 → 2026-08-24 | `293774ee35fcac24` | frozen corpus |
| `data/real_spot_btc/…/BINANCE_SPOT_BTC_USDT_1D` | 1,461 | 2022-09-09 → 2026-09-08 | `622b3375cdf19947` | frozen corpus |
| `data/real_funding/…/BINANCE_LINEAR_BTC_USDT_FUNDING` | 4,431 | 2022-08-09 → 2026-08-25 | `c652c9b0b6332b5b` | frozen corpus |
| `data/real_1d/…/BITSTAMP_SPOT_BTC_USD_1D` | 3,135 | 2018-01-01 → 2026-08-01 | `89dd4abbd1aab374` | cross-venue, cross-currency proxy |
| `research/exchange_study/bybit/spot_BTCUSDT_240` | 9,000 | 2022-08-01 12:00 → 2026-09-09 08:00, 0 gaps | file `59f9b94269cb716f` | study set (0028) |
| `research/exchange_study/bybit/linear_BTCUSDT_240` | 9,000 | same, 0 gaps | file `ab5ccc96aea6106c` | study set (0028) |
| `research/exchange_study/bybit/funding_BTCUSDT` | 4,600 | 2022-06-29 08:00 → 2026-09-09 08:00, 0 gaps, all on the 00/08/16 clock | file `4faa404c5f2a751c` | study set (0028) |
| `research/exchange_study/stress/{spot,perp}_1m_*` | 3 days | 2024-08-05, 2025-02-03, 2026-04-15 | — | study set (0028). **Binance** 12-field kline format, not Bybit |

The Bybit 4h set is the only data in the tree where spot, perp and funding are
the **same venue the broker trades on**, at a resolution that lands on the
funding clock. **0032 promoted it** to `data/real_bybit_btc_4h/` (8,999 bars
per leg after dropping one open bar; 4,600 prints). Result and sha256s:
`docs/human/SETTLEMENT_CLOCK_0032.md`.

## 8b. Speed, measured (0039)

Numbers, so "Python is too slow" is never argued from vibes again:

| | |
|---|---|
| one `CarryEngine.on_candle` decision | 1.8 µs |
| decisions the book makes | 3 per day |
| one 4-year settlement simulation (4,500 prints) | 5 ms in Python |
| the same simulation in `cpp/carrycore` | 29 µs inside a sweep |
| 1,440-configuration sweep | 42 ms vs 9 s — **212x** |

The decision path has five orders of magnitude of headroom; the order path is
bounded by a 50–200 ms venue round trip. The native core exists for SEARCH
(`docs/human/CPP_CORE_0039.md`), and a differential test holds it to the
Python's answer to the cent.

## 9. Network from the hosts used so far

| Host | Bybit testnet | Bybit mainnet | Binance fapi | Binance www/fapi |
|---|---|---|---|---|
| Codespaces (README) | 403 | 403 | 451 | — |
| this session's sandbox | 0 (egress denied) | 0 | 0 | 0 |

`connector_check.py` has never returned `BYBIT_TESTNET_OK` from any host.
0038 generates the VPC that is meant to change that
(`tools/aws_stack.py`, runbook in `docs/human/AWS_RUNTIME_0038.md`); it has
not been deployed, so the table above still has no row that says OK.
Since 0035 it probes every public read the carry book makes on testnet and
`--require-bybit-testnet` exits 2 unless all answer 200. From this session's
sandbox: `CARRY_READS_BLOCKED`, exit 2. The first command inside the intended
VPC is:

```
python3 bot/tools/connector_check.py --require-bybit-testnet
```
