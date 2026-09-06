# RISK MEMO — MICRO-LIVE PROPOSAL (TEMPLATE — UNSIGNED)

> **This is a blank template. It is not a memo, it is not signed, and its
> existence completes no checklist item.** A template is a form; a memo is a
> decision. Confusing the two is the same error as calling a replay a forward
> observation — a placeholder mistaken for the thing it stands in for.

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
| Forward observations | **0** |

**The signer is asked to confirm they have read this table, not that it is
reassuring.** It is not.

## 3. Size and scope being proposed

| field | value |
|---|---|
| Max notional per entry | **100.00 USD** (fixed dollars, not a fraction of equity) |
| Max concurrent positions | 1 |
| Max entries per calendar day | 1 |
| Symbols | BTCUSDT only |

> Raising the notional above 100.00 USD requires this memo to say so
> **explicitly, with a number and a reason**. Silence is not authorisation.
> A percentage-of-equity cap is specifically not proposed: it grows with the
> account, so it authorises more risk the better things go — backwards for a
> pilot whose purpose is to find out whether the rule works at all.

```
Proposed notional if different from 100.00 USD : ______________________
Reason                                          : ______________________
```

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

> **Moving the M-4 threshold is not one of the options and never will be.** A
> threshold adjusted because it fired is a description of the past.

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

## 6. What signing does and does not authorise

Signing completes **one** of eight gate items. It authorises nothing on its own.
`promotion_gate_allows_live()` also requires: ≥ 20 forward closed trades with no
ALERT across ≥ 180 days, the M-4 decision above, a recorded kill-switch drill, a
documented linear stop-verification path, the notional cap, the absence of
`models/current`, **and** the live-arming chain independently satisfied.

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
