# LINEAR PROTECTIVE-STOP VERIFICATION

> **Status (2026-09-27):** Venue Item 1 is recorded below from a real Bybit
> **testnet** armed drill. Items 2–4 are still open. This file does **not**
> flip `allows_live` and does **not** complete the promotion gate by itself.
> Sign-off stays blank until Items 2–4 are done (or until a separate written
> acceptance of remaining gaps is recorded).

---

## Why this item exists, and why it is the one most likely to be waved through

`docs/PAPER_OPERATOR_RUNBOOK.md` §H records a limitation that matters more here
than anywhere else in the system:

> **Perp (`linear`) simulation raises `NotImplementedError`.** The linear *live*
> path is implemented and unit-tested; the *simulator* models spot cash
> accounting only, so a linear backtest would omit funding, margin and
> liquidation and report a flattering number.

The consequence for this pilot is specific and uncomfortable:

**`funding_carry_fade_btc_v1` trades a USDT-margined linear perpetual. The
protective-stop behaviour that every other part of this system takes for granted
had therefore never been exercised end to end in simulation on the instrument
this signal actually trades.**

**That half is now done, and it is the SIMULATION half only.** ROADMAP Stage A
item 1 added `LinearSimulatedExchange`, in which the stop is written by
`/v5/position/trading-stop`, read back off `/v5/position/list` through the real
`BybitClient.verify_stop`, and triggered against the bar — including the case
where liquidation is nearer than the stop and fills first. See
`bot/tests/test_linear_simulator.py::TestTheProtectiveStopEndToEnd`.

**It changes nothing about Items 2–4 below.** Those are statements about a
VENUE, not about a simulator, and no simulator can make them true.

**Item 1 below is now venue-proven on testnet** via
`bot/tools/linear_stop_venue_drill.py` (not the Phase D carry drill
`bot/tools/drill.py` / `artifacts/linear_stop_drill.json`, which never calls
`trading-stop` / `verify_stop`).

Every standing safety claim in this repository — *"a confirmed position always
has a verified protective stop"* — is asserted and tested on the spot path and
on the linear simulator. This checklist exists because the claim must also be
demonstrated on the **live venue** for linear. Item 1 is that demonstration;
Items 2–4 remain open.

## What "verified" has to mean before micro-live

Not "the code exists". Not "a unit test passes". All four, demonstrated and
recorded:

```
[x] 1. A protective stop is PLACED on the venue for a linear position, and its
       existence is confirmed by reading it back from the exchange — not from
       local state.
       observed: 2026-09-27 Codespace; tip 2c84e89 (PR #41 loader fix on
         PR #40 tool). Command:
           cd bot && python3 tools/linear_stop_venue_drill.py --arm \
             --notional 100 \
             --out artifacts/linear_protective_stop_venue.json
         Venue testnet, CATEGORY=linear. Stages all ok.
         open: order_link_id=BB-line-1-111dc1e450b286feb6ae8b64
               filled_size=0.001 avg_price=84707
         attach: order_link_id=BB-stop-2-144dcdb4c35cd5d5cdd3c2aa
                 trigger_price≈80471.365 mechanism=position
                 (real BybitClient place_stop_order / trading-stop path)
         verify: live=True detail=stopLoss=80471.3
                 endpoint=GET /v5/position/list (stopLoss field)
         position_evidence: side=Buy size=0.001 stopLoss=80471.3
                            tpslMode=Full positionIdx=0
         flatten: BB-line-3-11db9bce8a55029af1808a70
         final_reconcile: venue_perp_qty=0.0 FLAT
                          stop_still_live=False stop_detail=NO_POSITION
         orders_sent=2 verdict=PASSED
         transcript: bot/artifacts/linear_protective_stop_venue.json
         NOTE: Phase D carry drill (artifacts/linear_stop_drill.json) is NOT
         this evidence.

[x] 2. The stop SURVIVES a process restart. Kill the process with a position
       open; confirm on restart that the stop is still on the venue and that
       reconciliation sees it.
       observed: 2026-09-27 Codespace tip 228fb87. HOLD→exit→VERIFY→FLATTEN
         via bot/tools/linear_stop_venue_drill.py (not session_tail).
         HOLD: --hold --notional 100 --out artifacts/linear_stop_hold.json
           open BB-line-1-ca5950b3b936bada4a415a02 size 0.001 @ 84704;
           attach BB-stop-2-25ad10a46cd165956d771743 stopLoss=80468.7
           live=True; process exited (pid 3097) leaving position+stop.
         VERIFY (new process pid 3668): --verify
           --out artifacts/linear_stop_verify_after_restart.json
           verdict=VERIFIED venue_perp_qty=0.001 live=True
           detail=stopLoss=80468.7 (GET /v5/position/list).
         FLATTEN: --flatten --out artifacts/linear_stop_flatten.json
           BB-line-1-36e7bad50964a10a825f7a91 → FLAT stop_still_live=False.

[x] 3. A position that somehow ends up NAKED is detected within one cycle and
       either re-protected or flattened. Induce this deliberately.
       observed: 2026-09-27 Codespace tip b10cfc7. induce→observe→flatten via
         bot/tools/linear_stop_venue_drill.py; production entrypoint
         trading_engine.TradingEngine.check_naked_positions.
         INDUCE: --induce-naked --notional 100
           --out artifacts/linear_stop_naked_induce.json
           open BB-line-1-d2fc7168d05e30b0f45ce4cb @ 85020.1;
           attach stopLoss=80769; clear_stop POST trading-stop stopLoss=0
           (BB-stop-3-ef4b2d0c…); confirm_naked live=False
           detail=POSITION_HAS_NO_STOP_LOSS size=0.001.
         OBSERVE: --observe-naked --from induce.json
           --state-db artifacts/linear_stop_naked_state.db
           --out artifacts/linear_stop_naked_observe.json
           verdict=REPROTECTED; reprotected=['BTCUSDT'];
           confirm live=True stopLoss=83319.6 (one cycle).
         FLATTEN: artifacts/linear_stop_naked_flatten.json → FLAT.

[x] 4. Margin and liquidation behaviour on linear is understood and documented
       for the proposed notional, including what happens at the cap.
       observed: 2026-09-27 Codespace tip b8dcd84.
         --margin-doc --notional 100
         --out artifacts/linear_stop_margin_doc.json → DOCUMENTED;
         --flatten → artifacts/linear_stop_margin_doc_flatten.json FLAT.
         size 0.001 @ avgPrice 85032.9 mark 84998.83 notional≈84.999 (< cap 100);
         stopLoss 80781.1 (~5.00% below entry); leverage 10;
         positionIM 8.54197428 USDT; positionMM 0.32258742 USDT;
         liqPrice empty (BEYOND_VENUE_PRICE_BOUNDS_OR_EMPTY) → nearer=unknown;
         USDT walletBalance≈1084.58; totalEquity≈9968.76.
         Memo: docs/promotion/LINEAR_STOP_MARGIN_MEMO.md (filled same numbers).
```

## The simulator gap — say which of the two was done

```
[x] the linear simulator was extended to model margin/funding/liquidation,
    and the extension is tested
        path to the work: backtest.LinearSimulatedExchange
                          bot/tests/test_linear_simulator.py (25 tests)
    NOTE: this box is an ENGINEERING fact and it does not sign anything off.
    Venue Item 1 is separately recorded above; Items 2–4 and the signature
    block below are still open / blank.

[ ] OR micro-live proceeds on TESTNET only, with the simulator gap accepted
    in writing, and the memo says so explicitly
        memo section: ______________________
```

> Choosing neither is not an option. An unverified stop path on the instrument
> the signal trades is the failure mode that turns a 100 USD pilot into an
> unbounded one. Item 1 closes the “is a stop on the venue and readable?”
> question only.

## Sign-off

```
Verified by         : ______________________  date: __________
Reviewed by         : ______________________  date: __________
Evidence log path   : bot/artifacts/linear_protective_stop_venue.json
                      (Item 1 only; Items 2–4 not yet evidenced)
```
