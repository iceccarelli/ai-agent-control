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
| D37 | **`fly launch` rewrote `fly.toml` and the rewritten file failed its own checks.** It printed "Wrote config file fly.toml" on the run that first deployed, so the pre-launch check had validated a document that no longer existed; the next `./scripts/verify.sh` then failed on `test_the_repo_fly_toml_parses_and_passes`. What launch adds is precisely what this book cannot have — it reads the Dockerfile's `EXPOSE` and offers an `[http_service]`, which drags in `auto_stop`, and a machine stopped while holding a hedge leaves a client unhedged. | `M bot/fly.toml` after the first deploy, then a red suite | **CLOSED 0050** — `fly apps create` registers the app and writes nothing; a test refuses any `launch` invocation in the script, while still allowing the word in a comment or an `echo` |
| D38 | **The drill opened the running book's database.** It honoured `STATE_DB_PATH`, which on Fly names the live book's SQLite — and the drill is a separate process, so that is two writers on one file: against the one-writer rule, and against a ledger meant to explain the entire equity change. It also read and wrote `os.environ` to steer `persistence`, when `config.load()` is the only environment reader in this repo. | the repo's own product law, read properly | **CLOSED 0050** — the drill always constructs its own scratch `StateStore` and passes it to `build_bot`; it needs nothing from the book's database, building its own journal and reading its position from the VENUE. An AST test refuses any `os.environ` / `os.getenv` in the module |
| D35 | **The image did not contain the tools the instructions name.** The Dockerfile copied `connector_check.py` and `session_tail.py`; `scripts/deploy.sh` told the operator to run `drill.py` and `fund_demo.py` inside the container. The first command after a successful deploy would have failed with *no such file*. `test_dockerfile.py` checks `main.py`'s import closure — it guarantees the BOT starts and never asked whether the TOOLS a human is told to run are shipped. | first deploy to Fly, 2026-09-14 | **CLOSED 0049** — all four are COPY'd; `test_image_ships_the_tools.py` parses `deploy.sh` for every `tools/*.py` it names and requires each in the image, plus each shipped tool's own import closure. `COPY tools/` is still refused |
| D36 | **`fly launch` rewrites `fly.toml`, after the script had checked it.** The pre-launch check validated a document that no longer existed by deploy time; a launch that dropped `strategy = immediate` or added an `[http_service]` would have given a machine that can run two books or be stopped while holding a hedge. | `Wrote config file fly.toml` in the deploy log | **CLOSED 0049** — the same check runs again after launch and a failure exits 1 rather than warning |
| F12 | The venue key is IP-pinned | **It is not, on the deployed demo app.** `fly ips allocate-egress` is disabled for trial organisations until a card is on file, so there is no static egress address to pin to. **Accepted for `BYBIT_VENUE=demo` only**: no real money, no withdrawal rights, simulated matching, and a key that is useless anywhere but the demo venue. **Not accepted for mainnet** — `deploy.sh` refuses mainnet outright and the pinning rule is unchanged. | open by decision, demo only |
| D34 | **The one open item that loses the position rather than embarrassing us.** The book guarded its short with `positionIM/positionMM >= 2.0`, a leverage/risk-tier ratio that sits near 2.0 by construction and barely moves with price. A squeeze would not have tripped it, and a liquidated perp leg leaves the client naked long in exactly the market that just moved against them. | F4, open since 0038 | **0048** — `get_liquidation_view` reads `liqPrice` and `accountMMRate`; the HELD book (it cannot be an entry gate — there is no position to have a liquidation price yet) unwinds below 15.0% distance or above 0.50 account rate, and HALTS if the venue will not say. `""` from Bybit means no reachable liquidation price and is treated as safe, not as missing. Off by default in the constructor, ON in `build_bot` — disclosed, and asserted by a test |
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
| D39 | **A PARTIALLY FILLED CLOSE WAS RECORDED AS A COMPLETE ONE.** All three exit paths — `_unwind` overlay, `_unwind` acquire, `_emergency_unwind_spot` — tested only `is None` / `filled_qty <= 0` on the closing order, then set `position = None; state = FLAT`. A market order that fills half leaves the rest AT THE VENUE. The book read FLAT, the next candle was free to open a SECOND hedge against the same margin and liquidation price, and `plan_cold_start` could not catch it on a restart because the ledger agreed with itself — it had been told the position was closed. This is D3 with a cause cold start cannot see. The trigger is correlated with the damage: the orders that fill short are the ones sent into the market that just moved, which is the market that fired the unwind. `tools/drill.py`'s `final_reconcile` has always checked the venue for a residual after an unwind; the engine never did. | stub venue filling closes at 50%: a 1.0 BTC overlay hedge unwound on a margin collapse left **0.5 BTC / $50,000 of unhedged short** the process did not know about. All three paths reproduced | **CLOSED** — each path now compares filled against requested; a residual above `QTY_DUST` rewrites the position to what the venue still holds, writes it down, and HALTS (`UNWIND_PARTIAL` / `NAKED_SPOT_UNWIND_PARTIAL`). It deliberately does NOT retry. A short fill on the way IN is still a smaller hedge, unchanged — the asymmetry is the point. `bot/tests/test_unwind_completeness.py` |
| D40 | **THE OVERLAY NEVER RE-READ THE INVENTORY IT HEDGES.** `get_spot_inventory` was called once, in `_open_overlay`, and then only at cold start. The overlay's entire safety argument is that the long side is the CLIENT's coin — so a client who sold, withdrew or re-pledged their BTC left this book naked short, reporting `HEDGED_AND_COLLECTING` every sixty seconds, until somebody happened to restart the process. `plan_cold_start` has always refused exactly that state at startup ("the inventory this short was written against has left"); nothing checked it where the exposure actually accrues. | stub venue, inventory set to 0 after the open: three consecutive ticks returned `hold / HEDGED_AND_COLLECTING` against 1.0 BTC of naked short | **CLOSED** — `_check_inventory_cover()` runs every candle in OVERLAY, after the margin gate and before delta/funding. Covered → nothing. Uncovered → buy the excess back at once (the rule `_open_overlay` already used for overselling), HALT if it will not close or the wallet is unreadable. A shortfall below one venue lot is logged, not halted — there is no order that would reduce it. `bot/tests/test_unwind_completeness.py` |
| D41 | **EVERY CARRY NUMBER THIS PROGRAMME HAS PUBLISHED CAME FROM ONE ASSET OVER ONE REGIME.** `carry_backtest` hard-coded the BTC triple at five places, so PHASE1_DECISION's matrix, 0041's walk-forward and D20's deflated Sharpe are all one asset over 2022-08 → 2026-08. ETH and SOL had complete Binance funding + perp + spot corpora **already committed to this tree** and no tool could read them; Binance's own REST had three more years nobody had fetched (7,688 BTC prints against the frozen 4,431). So the two cheapest checks on D20 — *does this replicate on another asset*, and *does it survive another regime* — had never been run. | five hard-coded paths: `carry_backtest.py` SPOT_SOURCES + `simulate` perp/funding + `corpus_health.SERIES` | **CLOSED 0052** — `--asset {BTC,ETH,SOL}` and `--corpus {frozen,full}`, both defaulting to the previous behaviour. BTC/frozen reproduces PHASE1_DECISION **exactly** (+9.49/+9.66, +5.76/+6.55, +3.28/+3.85, −0.44/+1.84, n=18/15/11/9/4). The extension is verified, not trusted: on every overlapping timestamp the funding rates are IDENTICAL (4,431/4,383/4,458 rows, zero mismatches), and `corpus_health` **refused the first fetch** for `OPEN_BAR_IN_FILE` — the gate caught a real defect in the new data, which was fixed in the data, not the gate |
| D42 | **THE RESULT IS ONE OBSERVATION WEARING SIX.** Reading D41's new numbers naively says the book got better: 7 years gives +27.4%/yr BTC, +38.9% ETH, +35.5% SOL at 0% borrow against the frozen window's +9.66/+12.40/+12.27. It is the same finding as D20, relocated. **2021 alone carries 68.4% / 62.5% / 58.3% of the seven-year net, on 3 / 2 / 1 trades.** The single best trade is still 58–69% of net on every asset and every window, and adding three years and doubling the trade count did not move it (frozen TOP1 60.7/80.8/117.0 → full 68.6/62.6/58.3). Three assets that concentrate in the SAME calendar year are not three confirmations of a carry edge; they are one observation of a funding regime, measured three times. Ex-2021 the lines are +10.1/+17.1/+17.8%/yr; in the current regime (trades closing 2025 onward) they are **+6.06 / +5.56 / −1.36 %/yr at ZERO borrow**, before the financing that decides the product. | `tools/carry_backtest.py --asset X --corpus full`, trade-log attribution by calendar year | **open — this is the headline, and it outranks the returns above.** It does not say the carry is absent; it says the corpus cannot distinguish a carry edge from one leverage mania, and that no amount of re-reading these files will. Only a registered FORWARD holdout can (0041), and D20's conclusion is unchanged and now better evidenced |
| D43 | **THE FINANCING LEG IS A CONSTANT EVERYWHERE, AND THE REAL ONE MOVED 10x.** `borrow_apr` is a single number at every call site and the matrices sweep 0/3/5/8%; PHASE1_DECISION's whole threshold — "above roughly 4% financing the spread stops clearing its cost of capital" — is stated against that constant. Measured instead: OKX publishes hourly USDT lending rates and serves **every epoch probed back to 2021-12-15**, so a backfill is paging, not availability. The rate is nothing like flat. Half-year medians: 1.00, 2.00, 1.00, 1.00, **10.00**, 3.00, 3.00, 1.00, 3.20, 2.50, 3.50 %/yr. Charged against the BTC carry measured over the same half-years, plus the overlay's 0.636 bps/day round trip, a financed book **LOSES in 2023H2** (10%/yr borrow against 2.32 bps/day of carry) **and in 2026H1**, and of the ten half-years it clears, four clear by less than 0.27 bps/day. | `GET /api/v5/finance/savings/lending-rate-history?ccy=USDT`, joined to the 7-year funding corpus by half-year | **open.** Two consequences, opposite in sign. It EVIDENCES the OVERLAY decision: at 0% borrow — the client already owns the BTC — none of this touches the book, which is exactly why 0036 chose it, and that choice was previously an argument rather than a measurement. It also means **no financed variant in this tree has ever been honestly costed**, and the 3%/5%/8% columns are decoration until `carry_backtest` charges a per-period series instead of a constant. NOTE the direction of the error: this is the LENDING rate, what a lender EARNS. A borrower pays more, so every figure above is a FLOOR |
| D44 | **A SPOT SALE REALISED AGAINST ITSELF, AND THE FEE FIELD HAS NO DECLARED UNIT.** Two defects on the same boundary, both found by running `carry_replay --mode acquire`, which nothing had ever done. (a) `LedgerBroker._book` called `spot_sell(cost_basis=price)` — the SALE price — so `cost` equalled `proceeds` and **EQUITY:REALISED booked exactly $0.00 on every spot sale the book ever made**. The money was not lost: the entry still balances, so the gain landed in ASSET:BTC, which then held **value at zero quantity** (−$10,010 against −0.0001 BTC in the reproduction), and `reconcile()` compares QUANTITIES, which were right, and returned RECONCILED. `spot_sell` itself was correct and is tested directly with a distinct basis; the WIRING realised a sale against itself and no test drove a spot Sell through the wrapper. (b) `fee` crosses the broker boundary with **no declared unit**. The live `CarryBroker` returns raw `cumExecFee` — BTC on a spot BUY, quote elsewhere — and `ledger.spot_buy` multiplies by price to match. `ReplayBroker` computed fees in USD and returned the same number, so every spot buy fee was inflated by the BTC price: **$388,719,591 of fees on a four-year acquire replay** against the $20,542 the replay's own totals report, and `agrees: False`. | `tools/carry_replay.py --mode acquire`; unit repro: the same economic $10 fee books as $10 or $1,000,000, a ratio of exactly the BTC price | **CLOSED 0053** — `LedgerBroker` now tracks a weighted spot entry price exactly as it has tracked the perp's since 0044, and falls back to the sale price only when nothing was bought through it (an overlay never buys the client's coin, so zero is the honest realisation there). `ReplayBroker._as_venue_fee` converts only what crosses the boundary; `Fill.fee` stays USD because the replay's own `fees_usd` total was never wrong. `ledger.spot_buy` is UNCHANGED — it matches the live venue, and bending production to suit a harness is backwards. `bot/tests/test_spot_cost_basis.py`. **Note for LEDGER_0044**: its "agrees to the cent, fees agree exactly" was measured in THREE OVERLAY configurations, and overlay never buys spot — the claim was true and mode-scoped, and acquire was never cross-checked |
| D44c | **AND UNDERNEATH THOSE TWO, A COMPENSATING PAIR.** With (a) and (b) repaired, acquire STILL disagreed — by $151,904.02, which is the acquire revaluation total **to the cent**. `carry_replay` posts `L.revalue_inventory` on every close in BOTH modes. That function is overlay-scoped by its own docstring: it exists because the overlay never TRADES the client's coin, so nothing else books that leg's move. In acquire the book buys the spot and sells it again, so `spot_sell` realises it — and the revaluation counts the same dollars a second time. **It was invisible because it was compensating**: while (a) made spot realisation identically zero, the revaluation was the only thing booking that leg at all, so revaluing in both modes was accidentally right, and (b)'s $388m buried the residue. Repairing the basis is what turned a silent cancellation into a visible double-count. | `--mode acquire`, gap $151,904.02 == `hedge.inventory_change_usd` exactly | **CLOSED 0053** — the journal revaluation is guarded to OVERLAY. The replay's own `basis_usd` term is untouched: it is correct in both modes and is a different accumulator. `bot/tests/test_replay_agrees_in_both_modes.py` runs the cross-check on BOTH modes every time, which is the thing whose absence let three defects stack on one code path |

## 5. FakeClient-only assumptions — venue semantics never exercised

| # | Assumption in code | Venue fact (Bybit V5, from `research/exchange_study/bybit/*` or `bybit_connection.py`) | STATUS |
|---|---|---|---|
| F1 | Spot market Buy `qty` is in BTC | Without `marketUnit=baseCoin` a spot market BUY is read as a **quote (USDT)** amount. `BybitClient` sets it (`bybit_connection.py` ~916); `CarryBroker` builds its own body and omits it. | **CLOSED 0033** — `marketUnit=baseCoin` on every spot market order |
| F2 | `/v5/order/create` returns `cumExecQty`, `avgPrice` | It returns `orderId`, `orderLinkId`. The broker then queries `/v5/order/realtime` immediately — a race against the fill. | open, testnet |
| F3 | The spot fill equals the BTC received | Spot taker fee on a BUY is charged in BTC. Wallet = `cumExecQty − fee`; the perp hedges `cumExecQty`; `reconcile_pair` (dust 1e-6) would call every pair an INCIDENT. | **0044 partial** — the ledger books the fee in BTC where the venue takes it and `hedgeable_btc()` returns the wallet, not the fill. Whether Bybit does this is still unobserved: open, Phase D |
| F4 | `positionIM / positionMM` measures liquidation headroom | It is a leverage/risk-tier ratio (risk_limit tier 1: IM 0.66%, MM 0.33%). Under UTA cross margin liquidation is account-level (`accountMMRate`). The margin floor would not trip on a squeeze. | **0048 addressed in code** — the held book now reads `liqPrice` and `accountMMRate` and unwinds below **15.0%** distance or above **0.50** account MM rate. The floor is MEASURED: worst 8h move against a short 8.00%, worst intra-settlement excursion 11.52% over 4,500 settlements. The IM/MM floor is unchanged and still runs; this is an additional gate. **Still open at the venue**: no `liqPrice` has ever been read from a real position, and whether Bybit liquidates at `accountMMRate` 1.0 is documented nowhere precise — Phase D |
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

### 8a. Extended corpora, fetched to inception (0052)

Same three legs, same venue, same quote currency, back to each contract's
start. **The frozen corpora above are NOT touched** — CORPUS_POLICY forbids
refreshing them in git, so this is a separate set of directories selected by
`--corpus full`.

| Series | Rows | Range (UTC) |
|---|---:|---|
| `data/real_funding_full/…/BINANCE_LINEAR_BTC_USDT_FUNDING` | **7,688** | 2019-09-10 → 2026-09-15 |
| `data/real_funding_full/…/BINANCE_LINEAR_ETH_USDT_FUNDING` | 7,454 | 2019-11-27 → 2026-09-15 |
| `data/real_funding_full/…/BINANCE_LINEAR_SOL_USDT_FUNDING` | 6,655 | 2020-09-13 → 2026-09-15 |
| `data/real_linear_1d_full/…/{BTC,ETH,SOL}_USDT_1D` | 2,564 / 2,484 / 2,192 | from 2019-09-08 / 2019-11-27 / 2020-09-14 |
| `data/real_spot_full/…/{BTC,ETH,SOL}_USDT_1D` | 2,628 / 2,628 / 2,226 | from 2019-07-06 / 2019-07-06 / 2020-08-11 |

Two things were verified rather than assumed. **The overlap is byte-identical**:
every funding timestamp the extension shares with the frozen corpus carries the
same rate — 4,431 / 4,383 / 4,458 rows, zero mismatches. And the first fetch was
**refused** by `corpus_health` with `OPEN_BAR_IN_FILE:2026-09-15`, because the
fetchers wrote the in-progress UTC day as a close; one row was dropped from each
of the six kline files and the correction is recorded in their manifests.

What the extra years contain is why they matter: BTC carry ran at **12.9 bps/day
in 2021H1** against **0.31 in 2026H1**, a 40× range across half-years. See D42
before quoting any mean over this window.

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
