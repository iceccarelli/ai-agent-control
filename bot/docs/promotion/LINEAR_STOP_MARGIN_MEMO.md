# LINEAR MARGIN & LIQUIDATION MEMO — micro-live proposed notional
> **Purpose.** This memo is the Item 4 evidence for
> `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`:
> *"Margin and liquidation behaviour on linear is understood and documented
> for the proposed notional, including what happens at the cap."*
>
> **Filled from a real human `--margin-doc` run on Bybit testnet (2026-09-27).**
> Numbers below are copied from `bot/artifacts/linear_stop_margin_doc.json`
> tip `b8dcd84` — not invented.
>
> **The linear simulator's liquidation formula —
> `cash + dir*S*(p−E) <= mmr*S*p` (`backtest.py`) — is NOT a substitute for
> this memo and is NOT evidence about what this venue's own position/wallet
> fields say.**
---
## 0. What produced this
tool:        bot/tools/linear_stop_venue_drill.py --margin-doc
artifact:    bot/artifacts/linear_stop_margin_doc.json
tip SHA:     b8dcd84
run date:    2026-09-27 (UTC+9 / Codespace)
venue:       Bybit testnet, CATEGORY=linear

proposed notional (from shadow.SHADOW_MAX_NOTIONAL_USD): 100.0
instrument: BTCUSDT, linear


Flatten after dump: `bot/artifacts/linear_stop_margin_doc_flatten.json` → FLAT.

## 1. Position, verbatim from the venue (`dump.position`)


symbol           : BTCUSDT
side             : Buy
size             : 0.001
avgPrice (entry) : 85032.9
markPrice        : 84998.83
stopLoss         : 80781.1
tpslMode         : Full
slTriggerBy      : (absent)
positionIdx      : 0

liqPrice (alias liqPrice): (empty / null)
reason: BEYOND_VENUE_PRICE_BOUNDS_OR_EMPTY
aliases_searched: liqPrice, liquidationPrice

positionIM       : 8.54197428
positionMM       : 0.32258742
positionBalance  : 0
positionValue    : 84.99883
leverage         : 10
unrealisedPnl    : -0.03407
cumRealisedPnl   : -0.4210382
riskLimitValue   : 300000
mmRate / imRate  : (absent)


## 2. Wallet, verbatim from the venue (`dump.wallet`)


totalEquity              : 9968.76123315
totalAvailableBalance    : 9071.77284638
accountMMRate            : 0

USDT walletBalance       : 1084.57896137
USDT equity              : 1084.54489137
USDT availableToWithdraw : (absent)
USDT usdValue            : 1084.27592424
USDT borrowAmount        : 0.000000000000000000

BTC walletBalance        : 0.10472857
BTC equity               : 0.10472857
BTC usdValue             : 8884.48530891
BTC collateralSwitch     : True


## 3. Derived (`derive`)


notional_usd            : 84.99883
stop_distance_reference : avgPrice (entry)
stop_distance_abs       : 4251.799999999988
stop_distance_pct       : 5.000182282387157
liq_distance_abs        : (n/a — liq empty)
liq_distance_pct        : (n/a)
nearer                  : unknown
im_usd                  : 8.54197428
mm_usd                  : 0.32258742


`nearer` is **unknown**, not `liq`. Empty `liqPrice` is recorded explicitly; do not assume the stop is nearer than liquidation.

## 4. What happens AT THE CAP


Size limited by: venue min lot (0.001 BTC ≈ $85 notional) first;
under SHADOW_MAX_NOTIONAL_USD=100 (notional_at_or_above_cap=false).

At leverage 10 this run, IM ≈ 8.54197428 USDT (not ≈100 at 1x).

If available USDT < required IM: open REFUSED before send
(earlier testnet: have ~84.72 need ~85.02) — consistent with this dump.

Practical sizing: account was at leverage 10; riskLimitValue 300000;
mmRate/imRate absent on row; need USDT cash (BTC collateral alone does
not satisfy the client's USDT margin check). Empty liqPrice at this
size/leverage is a gap to re-check if leverage or size changes.


## 5. Plain-language conclusion

At ~$85 min-lot notional on BTCUSDT linear testnet (10x), venue reported
IM≈$8.54 and MM≈$0.32, with a protective stop ~5% below entry. Liquidation
price was not returned (`BEYOND_VENUE_PRICE_BOUNDS_OR_EMPTY`), so stop-vs-liq
ordering is unknown for this run. The $100 shadow cap did not bind; min lot
did. Open refuses when USDT < IM. Item 4 is documented with that liq gap
explicit — not waved through.

## 6. Sign-off


Verified by         : Vincenzo Ceccarelli  date: 2026-09-27
Reviewed by         : ______________________  date: __________
Evidence log path   : bot/artifacts/linear_stop_margin_doc.json

