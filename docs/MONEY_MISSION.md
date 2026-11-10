# Money mission and operator handover

**Repository:** `iceccarelli/ai-agent-control`
**Snapshot date:** 2026-11-10 (UTC)
**Snapshot main before this handover update:** `e146212e040e3bf0eca9eafa643c286cc2ec411b`
**Purpose:** give Claude Code a verified starting point and an economically ranked plan. Re-check all dated operational facts before acting.

## 1. Commercial objective

The only meaningful product outcome for this repository is a repeatable, scalable, positive **net** return on owner economic capital with survivable tail risk. Optimise the long-run expected net cash return, not gross funding, simulated P&L, notional turnover, trade count, code output, infrastructure count or the appearance of activity.

Every strategy comparison must show, in dollars and basis points:
- Net P&L after entry and exit fees, spread, realised slippage, market impact, adverse selection, funding, borrow/financing, basis movement, hedge error and operational costs.
- Return on the capital actually tied up (spot inventory, margin, collateral and reserves), not only on perp notional.
- Independent effective sample size, confidence interval, multiple-testing adjustment, out-of-sample performance and sensitivity to costs/latency.
- Worst loss, drawdown, tail events, liquidation/margin stress, liquidity/capacity and capital survival.
- Which inputs are measured, venue-stated, derived, modelled or assumed. Unknown/assumed critical inputs cannot silently become measured.

A strategy advances only when the prescribed evidence gate is met. A research flag is not a winner; a candidate is not an authorisation; a Testnet fill is not profit. **Reject a negative or inconclusive idea and move capital/research attention to a better hypothesis.** Do not tune thresholds on the same data until a result passes. New hypotheses require explicit pre-registration and family-wise/multiple-testing accounting.

## 2. What just merged

The current `main` contains these recent changes:
- **PR #241** — capital allocator and realised-only compounding for the unattended Testnet autopilot; capital sizing derives from verified flat-to-flat wallet cash, reserves part of explained gains and applies loss-streak stops.
- **PR #242** — append-only execution evidence events, read-only dataset inventory and the first `carry-research` worker core/image.
- **PR #243** — the legacy `unattended_drill.py --conformance` path now takes the same global Supabase executor lease as the autopilot and fails closed when the lease cannot be acquired. The guard only protects a deployed image built from the merged code.

PRs #241, #242 and #243 are merged. PR #243 merge commit is `e146212e040e3bf0eca9eafa643c286cc2ec411b`.

At the time of the last GitHub inspection, push CI on that main commit had 8 checks successful and 2 carry-correctness jobs still running (Python 3.11 and 3.12). The production image, forward-shadow runtime, both Supabase migration-on-Postgres jobs, both rest-of-suite jobs, secret scan and dependency audit were successful. Re-check the run before claiming CI is complete: [main carry-verify run](https://github.com/iceccarelli/ai-agent-control/actions/runs/42881230305).

The default branch was reported by GitHub as **not protected** at this snapshot. Do not silently change repository settings, but tell the owner this is a material merge-control gap.

## 3. Active repository issue: recorder staleness

Scheduled `carry-watch` run `42874673419` failed after successfully reading `/data/monitor/monitor.jsonl` (219,847 bytes). The alert parser reported:

`recorder_down: the last 3 cycles found the market recorder not recording (state STALE: no detail recorded). Order-book depth is not accumulating.`

The alert was sent to Slack, then the watcher exited non-zero by design. This is an operational/data-capture alert, not evidence that GitHub Actions itself could not read the history. The run used main SHA `1cc75011f41bac6779233750c14943c0d77a73c8`; inspect the latest Fly state before assuming the condition persists.

PR #244, `Diagnose external watcher failure: stale market recorder`, was opened by the Copilot cloud agent at `c4641983daea377fb2d44d3ebc8679fbcc7d23da`. At the snapshot it had **zero changed files** and only advised the owner to inspect recorder/supervisor status on Fly. It is not a code fix. Do not merge a zero-diff PR or suppress the alert to get a green badge.

**First engineering priority:** establish the exact recorder condition from the current Fly machine, `/data/market/recorder_status.json`, `/data/market/supervisor_status.json`, free disk, latest monitor cycles and recorder logs. Establish whether it is stale because the process stopped, the feed is refusing, data is not being written, health metadata is wrong, or the watcher is interpreting the status incorrectly. Fix the cause, add a regression test, and preserve the alert's fail-closed semantics. The old Fly image is not upgraded just because source code changes or a PR merges.

## 4. Verified owner-side execution state (reported in this session; not repository state)

### Bybit Testnet
- Read-only preflight passed against `api-testnet.bybit.com`.
- Dedicated `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET` were present. Do not print or move them.
- Server clock skew was -131 ms; account was one-way mode; BTCUSDT instrument filters and book/ticker reads worked; there were no open orders or positions at preflight time.
- Account permissions included contract order/position and spot-trade permissions. The preflight's permission output did not indicate withdrawal/transfer permission.
- The account and keys are Testnet. Their displayed balances are simulated funds and have zero standing as realised owner profit.

### Local authority files
- `~/.ai-agent-control/ms_testnet_capability.json` existed in the owner's Codespace, mode `0600`, owned by the current user, schema `ms_testnet_capability/1`, manifest digest consistent, and `ms_testnet_adapter.py status` returned `enabled: true`.
- It was bound to `cash_and_carry@spec-1`, BTCUSDT, plan digest `bb25510ba0c491a85c3a85fdfa6914509f857fe5382e74e2271a0682f647019b`.
- This capability check is self-attested, not a cryptographic human signature. The file is outside Git; never commit it.
- `~/.ai-agent-control/ms_autopilot_grant.json` was absent. `ms_testnet_autopilot.py status` reported `GRANT_MISSING`, engine `INITIALISING`, and both dedicated credentials present. Unattended execution must therefore refuse.
- The last stored result was an older skip at 1.00 bp funding versus a 5.00 bp additional floor. Its timestamp was 2026-11-10 06:15:06 UTC; it is not a current quote. The economics gate, not an artificial activity target, decides whether an opportunity can be traded.
- No Testnet order was placed by the preflight. Do not create a grant or start a loop automatically.

## 5. Fly and Supabase: not yet reconciled/deployed

Last owner-supplied Fly inventory:
- `carry-drill`: running Frankfurt machine, deployed 2026-11-05, image predates the PR #243 lease guard. Do not start the conformance order path until a reviewed image containing the guard is deployed and verified.
- `carry-book`: suspended/stopped. Keep it stopped.
- No `carry-autopilot` or `carry-research` app existed in that inventory.
- `CARRY_DRILL_ENABLED=false`; keep it false or unset.
- No owner approval or deployment evidence was supplied for a new executor. Do not infer it deployed from the merged TOML/Dockerfile.

Supabase:
- The CLI linked the production project and showed 12 local migrations with no matching remote version rows; `supabase db push --dry-run --remote` said it would push all 12. No actual push or migration repair was performed.
- A proposed read-only schema inspection was accidentally pasted into Bash, not Supabase SQL Editor; it did not execute. Therefore the actual production schema and migration-history reconciliation remain unknown.
- Do **not** run `supabase db push`, migration repair, write SQL or change PostgREST exposed schemas until the owner has run the read-only inventory in Supabase Dashboard → SQL Editor and the result is reconciled with all migration files.
- The lease migration expects the `carry` schema to be routed for RPCs. Never expose `carry_private`. Prefer narrowly scoped wrapper functions if they meet the required security model; confirm service-role-only execution and RLS/grants.

## 6. Research/data status

- The `carry-research` worker is currently an infrastructure inventory/freshness queue with quarantined proposals. It is **not an alpha-research agent** and does not discover, validate or prove a profitable strategy.
- The configured research volume example is 3 GB. The owner inventory found about 4.156 GB in `~/datasets`, 2.858 GB in `~/backups`, and 5.66 MB in `~/seals`. Even the datasets alone exceed that volume. Do not provision it with the current size or copy files blindly. Define data selection, checksums, transport, encryption, restore and growth/cost budget first.
- The tracked `cash_and_carry@spec-1` document labels itself SPECIFICATION ONLY. Its 0.259 bp/settlement expected funding input is derived from an in-sample historical mean; important requirements (whole hedge-leg basis, depth, measured fill probabilities, financing) are absent or assumed. It is not a validated candidate.
- The existing runbook describes the committed BTC corpus as `NO WINNER` / `INSUFFICIENT_DATA`. The unattended gate's taker cost example is approximately 31 bps round trip for spot+perp legs, with an indicative requirement around 53 bps per settlement after haircut and hurdle; that is a stated model example, not a fresh fee quote. Recalculate from the exact strategy (owner-owned spot overlay versus two new legs), actual account fee tier, target hold and measured execution costs. Never apply one fee model to every strategy.
- The funding regime evaluator has a pre-registered one-trial protocol and a post-hoc reg-2 explicitly counted as a second trial. Do not rewrite reg-1 or make reg-2 look prospective.

## 7. Ranked money-first execution plan

### Priority 0 — make the evidence feed trustworthy
Fix the stale recorder cause. The money engine cannot exploit or validate an edge without fresh, complete, sealed market data. Prove the recorder is writing current data, the supervisor is healthy, storage headroom is adequate, chains/seals verify, backups restore, and watcher semantics match the real status. Add a failing test first. Do not just change alert thresholds.

### Priority 1 — produce a decision-grade strategy comparison
Use the existing verified-universe and history tooling. Establish exact eligible universe, funding intervals, coverage, missing ranges, point-in-time spot/perp/index data and data integrity. Complete the prescribed 457-day window where required. Use identical data windows and fee/financing assumptions across candidate families; persist every trial in the existing append-only ledgers. Compare at least the plausible economic mechanisms already represented in `bot/research/candidates/` (cash-and-carry, cross-sectional funding dispersion, basis convergence, relative value, liquidity/maker execution) but do not manufacture evidence for missing inputs. Test out-of-sample and stressed costs. A candidate with no viable cost margin is rejected quickly.

### Priority 2 — optimise execution only for a surviving edge
Measure maker fill probability against real trade-through/depth, queue position, partial fills, cancellation latency, hedge completion, impact and actual account fee tier. A maker rebate or assumed fill is never free P&L. Quantify whether maker-first execution changes net expectancy enough to justify its fill risk and operational complexity. Select the best risk-adjusted strategy and explicit capacity range; do not run every candidate live.

### Priority 3 — bounded Testnet lifecycle, only when justified
Keep the existing Testnet preflight/authority checks and single-writer Supabase lease. Run read-only economics evaluation without requiring an execution grant where technically separable. Only request an owner grant after economics and infrastructure gates pass. If a mechanics-only session is specifically approved, label it as mechanics, record expected negative cost and never present it as a trading opportunity. Capture venue orders/fills/fees/funding/wallet cash and venue-flat proof; reconcile the capital book from flat-to-flat cash reads.

### Priority 4 — production control plane, data placement and capital decision
Reconcile Supabase migration history/schema before applying anything. Deploy one order-capable Testnet executor only after the lease RPCs are verifiably reachable and the owner installs authority files/secrets. Keep the research worker credential-free and order-free; deploy it only after a data transfer/volume plan is sized correctly. No Mainnet promotion is in scope.

## 8. Claude's definition of done

Every work item must close with:
- The economic question answered or explicitly falsified.
- Before/after evidence and the exact immutable data/code revision.
- Net dollars and bps after all costs where a strategy is evaluated; capital basis and uncertainty stated.
- Tests run and CI results linked; exact commit SHA.
- Risk/authority changes enumerated (ideally none); no hidden remote side effects.
- Remaining owner-only action stated as a precise command or dashboard operation.

Do not return another broad architecture proposal, a dashboard, a speculative ML layer, a new worker, or a documentation-only PR unless it unblocks one of the ranked priorities. The shortest path to more money is the path with the best verified net edge and the least risk of losing capital—not the largest codebase.

### Phase B: read-only decision without execution authority (branch `claude/money-phase-b-readonly-eval`, base `70be57e`)

- **Finding (verified in code):** `run_once(dry_run=True)`, which `evaluate` calls, loaded the capability file, the grant and the dedicated credentials BEFORE reading funding or computing the economics verdict, so the owner could not ask "should I trade?" without holding the authority to trade. The `evaluate` output also only described the session class, not a decision.
- **Fix:** `evaluate_readonly` (the single implementation; `run_once(dry_run=True)` delegates to it before any authority check). It needs no grant, no capability and no lease, writes nothing, and returns one of `WAIT_FOR_DATA`, `SKIP_NO_OPPORTUNITY`, `REJECT_UNECONOMIC`, `INCONCLUSIVE`, `ELIGIBLE_FOR_OWNER_TESTNET_DECISION` with an `authority` snapshot and `authority_gaps`. Eligibility is explicitly not authority. Dedicated Testnet credentials remain required only to build the client and read the account's fee tier; without them the answer is `WAIT_FOR_DATA`.
- **Unchanged (tested):** the order-capable cycle still refuses without the capability file and the grant, and no gate was weakened.
- **Not yet done in this decision engine:** the full decision RECORD (data and code digests, candidate and specification digest, capital basis, worst credible loss) is only partly present in the evaluate output; making it a durable evidence event is the next Phase B item.
