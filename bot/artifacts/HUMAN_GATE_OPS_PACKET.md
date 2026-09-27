# HUMAN GATE OPS PACKET — linear-stop verification + what to sign

> **Facts and commands only. Nothing here is verified, nothing here is
> signed.** This packet sequences work that already exists in
> `docs/promotion/LINEAR_STOP_OPS_INVENTORY.md`,
> `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`, and
> `docs/promotion/RISK_MEMO_MICRO_LIVE_DRAFT.md` into one runnable session —
> it does not restate their detail and does not replace them as the source
> of truth. Every `[ ]` and `______` below is for a human hand, not an
> agent's.

## Before you start

```bash
cd bot
python3 tools/connector_check.py
```
Read `bybit_testnet_verdict` in `artifacts/connector_check.json`. If it is
not `BYBIT_TESTNET_OK`, stop — nothing below is attemptable from this host
today, and that belongs in the memo, not a workaround. (It passed as of the
last committed run, with `keys_used: false`, `orders_placed: false` — that
is a same-day fact and must be re-checked, not assumed.)

Point at the sandbox correctly — two sources of truth about the venue is
"how a testnet key ends up on mainnet" (`LINEAR_STOP_OPS_INVENTORY.md` §2):

```bash
export BYBIT_VENUE=testnet      # authoritative; do NOT also set USE_TESTNET
export PAPER_TRADING=0          # a paper run cannot verify a venue stop
export CATEGORY=linear
```

## Part 1 — the four linear-stop items (Bybit testnet)

Full detail, evidence paths, and the exact Bybit endpoints for each item are
in `docs/promotion/LINEAR_STOP_OPS_INVENTORY.md` §3. Condensed sequence:

```
[ ] 1. PLACE and READ BACK
       # NOT tools/drill.py - that drills the CARRY book (paired spot+linear
       # legs) and never calls place_stop_order/verify_stop at all. Its
       # transcript (artifacts/linear_stop_drill.json) is real Phase D carry
       # evidence and is NOT stop evidence - do not cite it here.
       python3 tools/linear_stop_venue_drill.py                 # read-only first
       python3 tools/linear_stop_venue_drill.py --arm --notional 100 \
           --out artifacts/linear_protective_stop_venue.json
       # verify_stop's call IS the tool's `verify` stage; read the
       # transcript's verdict (must be PASSED), not a grep for "live=True"
       observed: ______________________________________________

[ ] 2. SURVIVES a restart
       # NOT tools/session_tail.py - that is a read-only gate-invariant
       # checker (allows_live/FUND_ABS/cap/LIVE_AUTHORIZED); it never calls
       # verify_stop or reads a position's stop fields.
       python3 tools/linear_stop_venue_drill.py --hold --notional 100 \
           --out artifacts/linear_stop_restart_hold.json
       # HOLD exits leaving the position+stop live on the venue - that
       # exit is the "process death" under test. If it is somehow still
       # running, kill -9 <pid> (not graceful). Then, a NEW process:
       python3 tools/linear_stop_venue_drill.py --verify \
           --out artifacts/linear_stop_restart_verify.json
       # verdict must be VERIFIED. Then required cleanup:
       python3 tools/linear_stop_venue_drill.py --flatten \
           --out artifacts/linear_stop_restart_flatten.json
       observed: ______________________________________________

[ ] 3. NAKED position detected within one cycle
       # NOT tools/drill.py (carry drill, no stop mechanism) and NOT
       # tools/session_tail.py (read-only, never reads a position's stop).
       python3 tools/linear_stop_venue_drill.py --induce-naked --notional 100 \
           --out artifacts/linear_stop_naked_induce.json
       # verdict must be NAKED. Then, a NEW process, same --state-db:
       python3 tools/linear_stop_venue_drill.py --observe-naked \
           --from artifacts/linear_stop_naked_induce.json \
           --out artifacts/linear_stop_naked_observe.json
       # verdict must be REPROTECTED or FLATTENED (fails closed otherwise) -
       # this calls the SAME TradingEngine.check_naked_positions() the live
       # loop's tick() calls every cycle, not a drill-only fork. Required
       # cleanup if still open:
       python3 tools/linear_stop_venue_drill.py --flatten \
           --out artifacts/linear_stop_naked_flatten.json
       observed: ______________________________________________

[ ] 4. Margin/liquidation at 100 USD notional, BTCUSDT linear, documented
       observed: ______________________________________________
```

Then in `LINEAR_STOP_VERIFICATION_CHECKLIST.md`, tick exactly one:

```
[ ] micro-live proceeds on TESTNET only, with the simulator gap accepted
    in writing, and the memo says so explicitly (memo section: ______)
[ ] OR the four items above are all satisfied on mainnet-equivalent venue
    behaviour
```

Sign `LINEAR_STOP_VERIFICATION_CHECKLIST.md`'s own sign-off block (`Verified
by`, `Reviewed by`, `Evidence log path`) — not this packet.

## Part 2 — what the risk memo needs from you

`docs/promotion/RISK_MEMO_MICRO_LIVE_DRAFT.md` has every fact filled in
already (3/20 trades, 46/180 days, forward mean net R −0.8996, historical
M-4 WARN). Three things in it are yours, and only yours:

1. **§4 — the M-4 decision.** Tick ACCEPTED or NOT ACCEPTED and initial it.
   Moving the M-4 threshold is not, and will never be, a third option.
2. **§5 — revoke conditions.** Fill in the three blanks (rolling-mean floor,
   cumulative-trades-with-mean-below trigger, qualitative conditions) *before*
   anything is armed, not after something happens that makes you want to.
3. **§7 — signatures.** Proposer, risk officer, second reviewer, and the path
   where you save the signed copy (this draft is not that copy — write a new
   file per its own header note).

Nothing else in the memo is a decision for you to make; the facts sections
are already correct as of `forward_shadow_current.json`'s
`corpus_last_bar_utc: 2026-09-24T00:00:00Z`.

## What this packet is not

It does not complete `linear_protective_stop_verified`, `human_risk_memo_signed`,
or any other gate item by existing. `promotion_gate.evaluate_promotion_gate()`
only reads what a human recorded in `artifacts/slice59_promotion_gate.json`
and the signed files themselves — never this index. `allows_live` stays
`false` throughout every step above; nothing here arms, clears a kill switch,
or places a real order outside the testnet drill explicitly named in Part 1.
