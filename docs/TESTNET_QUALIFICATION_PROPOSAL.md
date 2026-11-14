# Proposed Testnet qualification session (Round 19, 2026-11-13; prerequisites restated Round 21, 2026-11-14) — NOT SUBMITTED, NOT AUTHORISED

**Status: a proposal for the owner's decision. No grant, capability, lease, credential or order was created, copied, read or placed by the agent that wrote this. No approval means no order.**
Testnet is fake money. A Testnet result is never owner profit and never evidence of Mainnet expectancy (`CLAUDE.md`). Mainnet stays disabled.

## What this session is for (and what it cannot do)

The repository's only order-capable executor is the Testnet autopilot (acquire structure: buy spot + short perp, 2.04x capital at leverage 1). Its profit-seeking gate cannot accumulate evidence: on the committed replay the break-even funding is 1.43 bps per print at an illustrative 4.4% cash alternative and only 6.3% of prints clear it (0.0% in 2026), about 1.5 eligible entries a year against the repository's own 50-observation floor. So the gated path will not measure execution. (All of these figures are IN-SAMPLE on the committed corpus. 1.43 bps assumes a 51-print (17-day) hold, a 31.0 bp spot+perp round trip, an illustrative 4.4%/yr cash alternative and 2.04x capital; 6.3% is the share of prints whose trailing-3-day mean funding clears it and 0.0% is the share of 2026's RAW prints that do; "about 1.5 entries a year" is 6 non-overlapping eligible decisions in 4.11 years.) A **bounded `TESTNET_MECHANICS` session** is the only way to measure, on fake money and at a known expected loss:

| Measured by the session | Where it is recorded |
|---|---|
| venue acknowledgement, maker/taker as the venue states it, fees by currency | executions + `execution_quality.json` |
| fills, slippage and spread against the books walked at plan time | `execution_quality.json`, `exec_measurements.json` |
| committed initial margin, borrow, accrued interest **while a hedge is open** (never yet observed: all 23 committed wallet reads are flat) | `exec_measurements.json` -> `__wallet_facts__` (new in Round 19; unstated fields are `None`, never 0) |
| funding settlement rows | `funding_settlement` evidence |
| venue-confirmed flat + no open orders | `reconciliation.json`, durable `SESSION_RECONCILED` / `DECISION_RECORDED` |

It **cannot** establish: an edge, the owner's opportunity cost, Mainnet tiered collateral ratios, a real borrow APR, or behaviour under an adverse path unless one happens during the hold. Expected net is **negative** (about 31 bps round trip + basis on the notional, below).

## The exact session

| Item | Value | Source in code |
|---|---|---|
| Venue | Bybit **Testnet** (`bot/venues.py` registry id `testnet`, sandbox) | `venues.py:101` |
| Credentials | `BYBIT_TESTNET_API_KEY` / `BYBIT_TESTNET_API_SECRET` only (presence only; generic `BYBIT_API_*` are not Testnet credentials) | `CLAUDE.md` |
| Instruments | `BTCUSDT` spot (Buy) + `BTCUSDT` linear perpetual (Sell), leverage **1** | `ms_testnet_session.build_plan`, adapter `MAX_LEVERAGE` 3.0 |
| Strategy identity | the autopilot's cash-and-carry **acquire** candidate; the decision record's `authority.candidate_id` names it. Class: `TESTNET_MECHANICS` (never `PROFIT_SEEKING`) | `ms_testnet_autopilot.py:219` |
| Notional | **$100** per leg (`MECHANICS_CAP_USD`); floor $20 (`MIN_NOTIONAL_USD`); the plan refuses below the venue's min (`NOTIONAL_BELOW_MIN`; spot `minOrderAmt` 5 USDT in the committed real capture; the linear filter is read at preflight) | `ms_capital.py:45-46` |
| Capital committed | 2.04 x $100 = **$204** (spot paid in full + perp margin at 1x + 2% buffer) of Testnet USDT; the real Testnet account held 1,084.53 USDT + 0.1047 BTC in the committed capture | `economic_model.structure_capital_usd(ACQUIRED_SPOT_PERP)` |
| Max exposure | $100 long spot + $100 short perp = $200 gross; aggregate ceiling $500 | `AD.AGGREGATE_CEILING_USD` |
| **Loss limit** | **$5.55** per session is the kill-switch TRIGGER, not a cap: at `start_equity - equity >= 5.55` the session trips `LOSS_LIMIT` and unwinds, so the realised loss can exceed it by the unwind's own slippage and fees | `AD.MAX_LOSS_CEILING_USD`, `ms_testnet_session.py:857` |
| Max holding time | grant `max_hold_s` 1500 s (hard cap 3300 s); entry wait 60 s, exit wait 180 s; the session lifecycle is one hour | `ms_testnet_autopilot.py:61,1444` |
| Entry conditions | grant valid + owner capability valid + shared executor lease held (strong check before every entry order) + market-data quality blockers clear + enough seconds to settlement; mechanics sessions run only when the grant says `--allow-mechanics-sessions` | autopilot `once()`, `held_fresh()` |
| Exit conditions | hold elapsed -> reduce-only unwind of both legs; kill switch, lease loss, loss limit or a refused leg -> reduce-only unwind first, then verify | session engine |
| Emergency close-out | the engine's own reduce-only unwind; if the executor is stopped with a perp short open, `tools/close_testnet_short.py` (now takes the same single-writer guard as every other order tool: it REFUSES, exit 3, while the autopilot or another guarded process holds the account lock or the lease, which can last up to the lease TTL after a crash; `--arm` is owner-only; the other protective path is `ms_testnet_session.py reconcile --unwind`) | Round 19 guard + 8 tests |
| Expected result | net **about -$0.31 to -$0.50** (31.4 bps modelled round trip on $100 per leg incl. an ASSUMED 0.4 bp spread, plus basis, plus funding of either sign); this is a plumbing/measurement cost, not an opportunity | `carry_costs`, `ms_edge_gate` |

## Stop conditions (any one ends the session; none is overridden by the agent)

`LOSS_LIMIT` ($5.55), lease lost or unprovable, kill switch engaged, either leg refused or unhedged beyond the engine's tolerance, `NOT_FLAT` at the end, venue read incomplete (a wallet, position or order read fails: the session ends as NOT_FLAT, never inferred flat), grant or capability expired/revoked/future-dated (`GRANT_FROM_THE_FUTURE` is now refused), an `ALERT` marker.

## How it is verified afterwards (all read-only)

1. `python3 tools/ms_testnet_autopilot.py status` and the session directory's `reconciliation.json`: `venue_reconciled`, `view_complete`, finding codes, `flat_verified`.
2. The venue's own position list and open-orders list are empty (never inferred from fills or arithmetic).
3. `exec_measurements.json` -> `__wallet_facts__`: first and last samples (pre-entry, with the hedge open, post-flat) with `committed_initial_margin`, `borrow_stated`, `accrued_interest_stated`, `usdt_shortfall`.
4. The durable `SESSION_RECONCILED` / `DECISION_RECORDED` events (Supabase, owner-read only).

## What the owner must do (the agent will not, and cannot)

Each step is owner-only because it creates authority or starts an order-capable process.

**Prerequisite 0 (Round 21): the executor does not exist yet.** The owner reports (2026-11-14) that the Fly apps are `carry-drill` (read-only recorder and monitor; its 2026-11-05 image predates the lease guard and must not run its conformance path) and a stopped `carry-book`; `carry-autopilot` and `carry-research` do not exist. The `fly ... -a carry-autopilot` commands in steps 2 and 4 therefore cannot run as written until the owner has created and deployed that app from the merged commit by RUNBOOKS "Fly autopilot: owner procedure" (image comparison first, `fly.autopilot.toml`, the Supabase migrations and `carry` schema exposure, the lease secrets). Creating it is itself an owner decision, separate from this session, and is not requested here.

**Read-only readiness the agent can and cannot establish (Round 21).** Without a capability, `ms_testnet_session.py preflight` stops after its four local checks and makes no venue read, so the user-id basis, key permissions, account mode, one-way mode, wallet balance (at least 500 USDT), instrument filters and flat / no-open-orders facts are provable only AFTER the owner creates the capability (step 2). `ms_testnet_autopilot.py evaluate` needs only the dedicated Testnet credentials and reads the account fee tier, instrument filters and books. Both are listed with their effects in RUNBOOKS "Testnet readiness checklist (Round 21)". Until they have been run and read, the venue facts are UNKNOWN, not assumed.

1. **Decide** whether to allow a mechanics session at all. It costs about $0.5 of fake money and buys the first measured fills, fees and margin facts. Not doing it leaves execution cost unmeasured and every research number modelled.
2. **Provide the capability and grant** on the selected executor once prerequisite 0 is done (typed by you; the repository never creates, copies or renews them). The capability file is RUNBOOKS autopilot step 4 (it holds your manifest, no key). The grant is step 5: `fly ssh console -a carry-autopilot` (the app of prerequisite 0), then `autopilot-ctl grant --max-sessions 1 --max-notional-usd 100 --max-hold-s 1500 --min-funding-bps 0.5 --days 1 --allow-mechanics-sessions` (RUNBOOKS autopilot section, step 5). *What it changes:* writes `~/.ai-agent-control/ms_autopilot_grant.json` (0600, expiring, bound to the manifest). *Reversible:* delete the file or let it expire; `drain` before any deploy.
3. **Confirm the shared lease and the single executor:** `carry-book` stays stopped and `CARRY_DRILL_ENABLED` stays unset; the deployed 2026-11-05 `carry-drill` image predates the lease guard and must not run its conformance path.
4. **Start** the autopilot only after 0, 2 and 3 (per RUNBOOKS; there is no machine to start until prerequisite 0 exists). *Risk:* fake-money loss of about $5.55 or a little more (the limit is a trigger, not a cap); an unhandled fault leaves a Testnet hedge that the engine's startup recovery proves flat or alerts on.
5. Read the verification list above and send back `reconciliation.json` and the `__wallet_facts__` samples.

**I am not asking for approval to place any order; I am asking whether you want this session proposed again as an explicit request. Until you say so in your own words, nothing runs.**

## The exact approval request (Round 21; NOT SUBMITTED; to be put to the owner only after the readiness checklist in RUNBOOKS is proven)

Prerequisites that are NOT yet true, so this request is not sent today: the autopilot app does not exist (prerequisite 0 above), the Supabase lease objects have not been read back, no capability file exists, `capital --anchor-init` has not been run, and no venue fact (fee tier, wallet, one-way mode, flat) has been read by anyone. When they have, the request below is the one to present, in these words, for the owner to answer in their own.

> **Request: one bounded Bybit TESTNET mechanics session. Nothing runs unless you answer yes in your own words.**
>
> *What it is.* Fake Testnet money. A plumbing measurement: fills, fees by currency, maker/taker flags, committed margin, borrow and interest while hedged, funding settlement rows, a venue-confirmed flat. It is not income and not evidence of Mainnet expectancy. Mainnet stays disabled and this request does not touch it.
>
> *Expected result.* A small LOSS of about $0.31 to $0.50 (a 31.4 bps modelled round trip on $100 per leg, plus basis, with funding of either sign). It buys the first measured costs. It does not establish an edge, and the economic verdict stays NO WINNER.
>
> *Limits (from the code).* Venue: Bybit Testnet only (`venues.py` registry). Credentials: `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET` only. Instrument: BTCUSDT spot Buy + BTCUSDT linear perpetual Sell, leverage 1. Notional $100 per leg (`MECHANICS_CAP_USD`; the plan refuses below $20); committed capital about $204 (2.04x); aggregate ceiling $500; at most 4 entries per UTC day; the grant caps it at 1 session. Hold at most 1500 s in the grant (hard cap 3300 s); the hedge is held 120 s past settlement and entry needs at least 90 s before it; the session lifecycle is one hour; the grant is valid 1 day (maximum 30). Capital stops also apply: entries stop after 4 consecutive losses, a drawdown stop at $11.10 and a lifetime stop at $22.20. Mechanics sessions run only if the grant says `--allow-mechanics-sessions` and funding is not negative.
>
> *Loss trigger.* $5.55 (`MAX_LOSS_CEILING_USD`) is a TRIGGER, not a cap: when equity falls by that much the session ends as LOSS_LIMIT and unwinds reduce-only, so the realised loss can exceed it by the unwind's own slippage and fees. The theoretical exposure is the $100 long spot and the $100 short perp.
>
> *Stop conditions (any one ends it; the agent overrides none).* LOSS_LIMIT; lifecycle exceeded; lease lost or unprovable; kill switch engaged; a leg refused or unhedged beyond tolerance; NOT_FLAT at the end (a failed wallet, position or order read ends as NOT_FLAT, never inferred flat); grant or capability expired, revoked or future-dated; an ALERT marker. If the executor stops with a short open, `tools/close_testnet_short.py` (takes the single-writer guard; `--arm` is yours) or `reconcile --unwind` (yours).
>
> *What YOU create yourself (the agent will not, and cannot).* (a) deploy the autopilot app from the merged commit (it does not exist today), confirm `carry-book` stays stopped and `CARRY_DRILL_ENABLED` unset; (b) the owner capability file (RUNBOOKS, "Testnet execution: owner procedure" step 4; it holds your manifest, no key); (c) the dedicated Testnet credentials as secrets; (d) one interactive `capital --anchor-init`; (e) the grant, typed by you on the selected executor: `autopilot-ctl grant --max-sessions 1 --max-notional-usd 100 --max-hold-s 1500 --min-funding-bps 0.5 --days 1 --allow-mechanics-sessions`; (f) starting the process, and afterwards sending back `reconciliation.json` and the `__wallet_facts__` samples.
>
> *Reversible by* deleting the grant, letting it expire, or `drain`. If you do not answer, nothing runs.

## Not part of this proposal

Mainnet, any real-money order, the carry-book, any change to a risk limit, refusal code, lease, kill switch or reconciliation, the frozen `funding_carry_fade_v1`, the owner holdout, the capital anchor or any owner ledger.

## Round 22 addendum: what "mechanics verified" means, and the one correction to the grant

**Correction (REPRODUCED by running the documented grant's floor through `funding_decision`).** The grant above said `--min-funding-bps 5`. The loop starts a session only when the funding print pays at least that floor, and the usual Bybit BTCUSDT print is about 1 bp (the largest historical print in the committed corpus is 11.28 bps), so a 5 bp floor would have waited for a rare print and run nothing for days. For a MECHANICS session the floor only has to be positive: use `--min-funding-bps 0.5` (a print of 0 or less never qualifies: the loop requires funding strictly above zero). A test now runs the grant text of this document through the loop's own decision at 1 bp (runs), 0.4 bp (does not) and 0 (does not).

**Plan sizing.** The plan rounds the quantity DOWN to the venue's lot step, so at the Testnet price in the committed capture one BTC lot is about $83 per leg (about $170 committed at 2.04x), not $100: the notional above is a ceiling. The refusals to expect if the sizing does not fit are `QTY_BELOW_MIN_QTY`, `LINEAR_NOTIONAL_BELOW_MIN`, `SPOT_NOTIONAL_BELOW_MIN` and `BELOW_MIN_NOTIONAL` (read at preflight; nothing is sent). These are offline figures from the committed capture and are UNVERIFIED against a live read.

**What this session may be called, in a closed vocabulary** (the level is never higher than the evidence; `tools/mechanics_verify.py` produces the middle ones):

| Level | Evidence required |
|---|---|
| NOT BUILT / BUILT | the code exists and its tests pass |
| NOT DEPLOYED / DEPLOYED | the image built from a merged commit is running (registry digest, the in-machine provenance check) |
| NOT AUTHORISED / AUTHORISED | the owner's capability and grant exist (the owner's act) |
| NOT RUN / RUNNING | a session started |
| **MECHANICS VERIFIED** | `mechanics_verify` = MECHANICS_VERIFIED on the session directory AND an independent venue snapshot taken after the last fill (positions on every symbol zero, ordinary and conditional open orders empty, spot base within dust of the baseline, cash change equal to the recomputed net) |
| **REPEATED MECHANICS VERIFIED** | at least two distinct sessions (different group, plan digest and decision) each MECHANICS VERIFIED |

Neither verified level says anything about profit: Testnet money is fake and the session's expected result is a small loss. A funding settlement that is observed but whose sign convention is not yet settled (a `QUARANTINED_SIGN` row) stays UNKNOWN, which makes a session meant to span a settlement INCONCLUSIVE rather than verified. The first session therefore resolves two measurable uncertainties: the executed cost against the 31.4 bps model (fees by currency, spread, slippage), and the sign convention of the funding row.

**Read-only verification after the session** (nothing here sends an order; run on the executor or any machine with the dedicated Testnet credentials and the session directory):

    autopilot-ctl venue-snapshot --out /data/snapshot_after.json          # the venue's own account-wide end state: every symbol, ordinary AND conditional open orders
    python3 bot/tools/mechanics_verify.py <session_dir> --venue-snapshot snapshot_after.json --json

Evidence to return to the engineer: the session directory (`testnet_session.json`, `plan.json`, `preflight.json`, `engine_journal.jsonl`, `execution_quality.json`, `exec_measurements.json`, `funding_settlement.json`, `reconciliation.json`, `decision.json`), the snapshot, `mechanics_verify`'s JSON, and `autopilot-ctl quality`. The request volume of a session is about 19 requests per second at peak (a measured figure from the fake venue's request log, UNVERIFIED against the venue's real rate limits).
