# RISK MEMO — MICRO-LIVE PROPOSAL (**DRAFT — UNSIGNED**)

> **This is a DRAFT prepared by an agent. It is not a memo, it is not signed,
> and its existence completes no checklist item.**
>
> `human_risk_memo_signed` remains **incomplete** and was not touched. The gate
> entry still reads *"no memo path recorded"*, and it must keep reading that
> until a human writes, decides, and signs.
>
> What an agent may do is fill in the **facts**, so a human spends their time on
> the **decisions**. Every decision field below is deliberately blank. The
> signature block is deliberately blank. An agent filling either would be a
> forgery, and `RISK_MEMO_MICRO_LIVE_TEMPLATE.md` says so in those words.
>
> Source template: `docs/promotion/RISK_MEMO_MICRO_LIVE_TEMPLATE.md`
> Drafted: 2026-09-17 · Draft path is NOT the signed-copy path.

---

## 1. Product

| field | value |
|---|---|
| Signal | `funding_carry_fade_btc_v1` |
| Universe | BTCUSDT only. ETHUSDT and SOLUSDT are out of scope and unmeasured under this name. |
| Related frozen family | `funding_carry_fade_v1` — **FROZEN ABSENT**, must not be reopened |

## 2. What the evidence actually is — read before signing

| | |
|---|---|
| Cleared by | slice 57, pre-declared out-of-sample gate |
| Scored trades | **41** |
| M1 | **95.13** against a bar of 95.0 |
| M1 margin | **three rotation replicates.** 73 of 1,500 beat the observed mean; 76 would have failed it |
| M2 | 96.0 |
| Mean net R | +0.1736 |
| Control | VALID (z +1.121, KS p 0.1561, 0% incomplete) — but the instrument reads **52.30**, not 50, on this window when nothing can be timed |
| Concentration | three of 41 trades carry **41%** of the net R |
| Decay | uncapped OOS Q1 +0.3164 → Q2 +0.0236; capped pilot earlier half +0.4669 → recent half **−0.0798** |
| **Evidence type** | **a time split of data that had already been looked at — NOT held-out data** |

**The signer is asked to confirm they have read this table, not that it is
reassuring.** It is not.

### 2a. What has changed since the template was written

The template records **Forward observations: 0**. That is no longer true, and
the correct number is still small:

| | |
|---|---|
| Closed forward trades | **3 of 20** required |
| Closed forward bars | **36 of 180** days required |
| Forward ladder | 8 setups → 8 flagged → 8 eligible → 4 after schedule → 4 entries → **3 closed** |
| Frozen carry baseline | **9.6578 %/yr on 15 trades**, reproducing the figure `PHASE1_DECISION.md` quotes, under the `FROZEN_SNAPSHOT_CUT_*` bound |
| Fold-lock | prefix digests match the pinned fold lock on both corpora; history unchanged, never shrank, appends strictly after `t1` |

**Three forward trades is not evidence of anything.** It is 15% of the required
count over 20% of the required window, and no monitor can evaluate it: M1 needs
10 trades, M2 needs 5, M3 needs 10, M4 needs 20. All four currently read
`INSUFFICIENT_DATA`. This section exists so the number is not mistaken for
progress toward a conclusion — it is progress toward being *able* to conclude.

## 3. Size and scope being proposed

| field | value |
|---|---|
| Max notional per entry | **100.00 USD** (fixed dollars, not a fraction of equity) |
| Max concurrent positions | 1 |
| Max entries per calendar day | 1 |
| Symbols | BTCUSDT only |

> Raising the notional above 100.00 USD requires this memo to say so
> **explicitly, with a number and a reason**. Silence is not authorisation.

```
Proposed notional if different from 100.00 USD : ______________________
Reason                                          : ______________________
```

*(Left blank by the drafter. The gate item `notional_cap_within_policy` is
already complete at 100.00 USD; changing it is a human decision this draft does
not make.)*

## 4. The M-4 decision — this memo must answer it

M-4 (recent half vs earlier half) currently **WARNs**: earlier half +0.4669 on
17 trades, recent half −0.0798 on 18.

Choose one, and initial it:

```
[ ] ACCEPTED. I have read the decay and consider it tolerable for a
    100 USD pilot, for the following reason:
    ____________________________________________________________
                                                    initials: ____

[ ] NOT ACCEPTED. Micro-live is not proposed until the recent half
    recovers under the UNCHANGED threshold on forward data.
                                                    initials: ____
```

> **Moving the M-4 threshold is not one of the options and never will be.**

*(Both boxes left unticked by the drafter. This is the decision the memo exists
to make; an agent ticking either would be the forgery this document warns
about.)*

## 5. Revoke conditions — state them before, not after

The clear is withdrawn by a human calling `shadow.revoke_cleared_edge` with the
literal `HUMAN_REVOKED_CLEARED_EDGE`. Nothing auto-revokes.

```
I will revoke if:
  - rolling mean net R over 10 closed trades falls below : ______  (policy: ALERT at -0.25)
  - cumulative forward trades reach ______ with mean net R below ______
  - any of the following qualitative conditions:
    ____________________________________________________________
```

*(Left blank. These are thresholds a human commits to in advance; a drafted
default would become the commitment by accident.)*

## 6. What signing does and does not authorise

Signing completes **one** of eight gate items. It authorises nothing on its own.

Current gate state, read from `artifacts/slice59_promotion_gate.json` —
the file `promotion_gate.GATE_PATH` resolves to:

| item | owner | state |
|---|---|---|
| `kill_switch_drill_recorded` | human | **complete** |
| `notional_cap_within_policy` | — | **complete** |
| `models_current_absent_or_contained` | — | **complete** |
| `human_risk_memo_signed` | human | open — *this memo* |
| `linear_protective_stop_verified` | human | open — see `LINEAR_STOP_OPS_INVENTORY.md` |
| `live_trading_ack_present` | human | open |
| `m4_recent_half_accepted_or_recovered` | human or observation | open — §4 above |
| `forward_shadow_clean` | observation | open — 3/20 trades, 36/180 days |

**3 of 8 complete. `promotion_gate_allows_live()` returns False.**

It does **not** authorise: raising notional, adding a symbol, loading a model,
running unattended without monitors, or describing the result as an autonomous
profit agent.

## 7. Signatures — INTENTIONALLY BLANK

```
Proposer            : ______________________  date: __________
Risk officer        : ______________________  date: __________
Second reviewer     : ______________________  date: __________

Path of the signed copy, once it exists : ______________________________
```

> An agent must never fill these in. If a future automated process writes a name
> here, that is a forgery and the gate item it would satisfy is void.
>
> **This draft is not the signed copy and must never be recorded as one.** When
> a human signs, they should write a new file, record its path in the gate
> entry, and may delete this draft.
