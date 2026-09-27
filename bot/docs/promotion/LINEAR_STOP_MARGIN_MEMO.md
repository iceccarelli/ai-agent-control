# LINEAR MARGIN & LIQUIDATION MEMO — micro-live proposed notional

> **Purpose.** This memo is the Item 4 evidence for
> `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`:
> *"Margin and liquidation behaviour on linear is understood and documented
> for the proposed notional, including what happens at the cap."*
>
> **This file ships EMPTY.** Every number below is a blank for a human to
> fill in from a real `--margin-doc` run on Bybit testnet — an agent does
> not fill this in, and a number copied in by anyone other than the person
> who ran the drill is not evidence.
>
> **The linear simulator's liquidation formula —
> `cash + dir*S*(p−E) <= mmr*S*p` (`backtest.py`) — is NOT a substitute for
> this memo and is NOT evidence about what this venue's own position/wallet
> fields say.** It models a simulated venue's approximation of liquidation;
> Item 4 asks what the REAL venue reports for a REAL position. The two can
> disagree, and only the real one counts here.

---

## 0. What produced this

```
tool:        bot/tools/linear_stop_venue_drill.py --margin-doc
artifact:    bot/artifacts/linear_stop_margin_doc.json
tip SHA:     ______________________
run date:    ______________________ (UTC)
venue:       Bybit testnet, CATEGORY=linear
```

The proposed notional is `shadow.SHADOW_MAX_NOTIONAL_USD` — **do not
hardcode a second cap here**; read it from the artifact's `cap_usd` field
and copy it verbatim:

```
proposed notional (from shadow.SHADOW_MAX_NOTIONAL_USD): ______________________
instrument: BTCUSDT, linear
```

## 1. Position, verbatim from the venue (`dump.position`)

Every field below is either the exact value `/v5/position/list` returned, or
`(absent)` if the artifact's `position_absent_keys` names it — never a
guess, never carried over from a different run.

```
symbol           : ______________________
side             : ______________________
size             : ______________________
avgPrice (entry) : ______________________
markPrice        : ______________________
stopLoss         : ______________________
tpslMode         : ______________________
slTriggerBy      : ______________________
positionIdx      : ______________________

liqPrice (or its found alias — name which): ______________________
  reason (OK / BEYOND_VENUE_PRICE_BOUNDS_OR_EMPTY / ABSENT): ____________

positionIM       : ______________________
positionMM       : ______________________
positionBalance  : ______________________
positionValue    : ______________________
leverage         : ______________________
unrealisedPnl    : ______________________
cumRealisedPnl   : ______________________ (if present)
riskLimitValue   : ______________________ (if present)
mmRate / imRate  : ______________________ (if present)
```

## 2. Wallet, verbatim from the venue (`dump.wallet`)

```
totalEquity              : ______________________
totalAvailableBalance    : ______________________
accountMMRate            : ______________________ (if present)

USDT walletBalance       : ______________________
USDT equity              : ______________________
USDT availableToWithdraw : ______________________
USDT usdValue            : ______________________
USDT borrowAmount        : ______________________

BTC walletBalance        : ______________________
BTC equity               : ______________________
BTC usdValue             : ______________________
BTC collateralSwitch     : ______________________ (context)
```

## 3. Derived (`derive`) — computed FROM the numbers above, never venue-native

```
notional_usd            : ______________________ (size * markPrice)
stop_distance_reference : avgPrice (entry)
stop_distance_abs       : ______________________
stop_distance_pct       : ______________________
liq_distance_abs        : ______________________ (blank if liq absent)
liq_distance_pct        : ______________________ (blank if liq absent)
nearer (stop/liq/unknown/tied): ______________________
im_usd                  : ______________________
mm_usd                  : ______________________
```

**If `nearer` is `liq`:** the venue would liquidate the position before the
protective stop could fire. That is not a pass with an asterisk — the memo
must say so plainly in §5 and the checklist's `observed:` line must not omit
it.

## 4. What happens AT THE CAP

The cap is fixed by `shadow.SHADOW_MAX_NOTIONAL_USD`, not chosen per run.
Answer each, from the artifact's `derive.at_cap` block and your own reading
of §1–2:

```
Is the position's size limited by the cap, or by the venue's own minimum
lot (whichever bound bit first)?
  ______________________

Margin needed to hold the cap notional at 1x leverage ≈ the cap itself
(USDT-margined). At the leverage actually used this run, IM required ≈:
  ______________________

What happens if available USDT < required IM at the cap?
  Already observed on testnet (Items 1–3): the open is REFUSED before an
  order is sent — record whether this run's numbers are consistent with
  that, or whether they show something different:
  ______________________

Anything else the wallet/position dump above changes about how a $100
notional should be sized in practice (leverage tier, risk-limit bracket,
maintenance-margin rate)?
  ______________________
```

## 5. Plain-language conclusion (human, not the artifact's `risk_line` alone)

```
______________________________________________________________
______________________________________________________________
______________________________________________________________
```

## 6. Sign-off

```
Verified by         : ______________________  date: __________
Reviewed by         : ______________________  date: __________
Evidence log path   : bot/artifacts/linear_stop_margin_doc.json
```

Then, and only then, fill Item 4's `observed:` line in
`LINEAR_STOP_VERIFICATION_CHECKLIST.md` — pointing at this memo and the
artifact path. Do not tick `[x]` from this PR; that box is a human's.
