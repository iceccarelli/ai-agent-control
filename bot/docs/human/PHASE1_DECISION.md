# PHASE 1 DECISION

**OVERLAY**

Written 2026-09-08. Based on the first quotable backtest this programme has
produced.

---

## The matrix

`tools/carry_backtest.py --matrix`, Binance BTCUSDT spot against Binance
USDT-M linear, same venue and same quote currency, open bar refused.

```
spot source : BINANCE_SPOT_BTC_USDT (same venue, same quote currency)
quotable    : True
window      : 2022-09-09 .. 2026-08-24

  borrow            UNGATED              GATED
    0.0%    +9.49%/yr n=18     +9.66%/yr n=15
    3.0%    +5.76%/yr n=18     +6.55%/yr n=11
    5.0%    +3.28%/yr n=18     +3.85%/yr n=9
    8.0%    -0.44%/yr n=18     +1.84%/yr n=4
```

## The decision

**OVERLAY.** This is a yield product for BTC that is already held. It is not a
market-neutral fund to raise cash for.

The threshold is the locate. Above roughly **4% financing** the spread stops
clearing its cost of capital and the product stops being interesting:

| Locate | Net | Reading |
|---|---|---|
| 0% — you own the BTC | **+9.66%/yr** | strong return on an otherwise idle asset |
| 3% | **+6.55%/yr** | a real spread |
| 5% | **+3.85%/yr** | below T-bills; not a fund |
| 8% | **+1.84%/yr** | dead |

Borrow was 57% of gross income in the ungated 0018 run and financing is still
what decides this. No amount of software creates a cheap locate.

The buyers this fits are crypto treasuries, family offices and prop desks
holding BTC they will not sell. It does not fit an allocator handing over cash
to be levered.

## Caveat on the gated column — read before quoting it

**The gated column is IN-SAMPLE.** `ASSUMED_HOLD_DAYS = 30`, `EWMA_ALPHA = 0.5`
and the basis-budget formula were all chosen *after* seeing the 0018 output. The
person who picked those parameters already knew which trades had lost money.

That is precisely the selection path `tools/reserved_holdout.py` exists to
detect, and it is how `funding_carry_fade_btc_v1` was cleared and then lost
money forward. The ungated column is clean. The gated column is a **hypothesis**
and needs its own untouched window before any figure from it is stated as fact.

Register it in `tools/hypothesis_registry.py` before scoring it again.

## A prediction that was wrong, recorded

Before the Binance data arrived, the expectation on record was that the gates
would *lower* returns at cheap financing, because refusing entries also refuses
income. On the Bitstamp proxy that is what happened: 0% went +7.96% → +5.94%.

On real Binance spot the gates improve returns at **every** borrow level. The
proxy's fake basis — cross-venue spread plus USD-vs-USDT peg — was making the
gates refuse the wrong trades. Wrong data produced a wrong conclusion about a
*tool*, not merely a wrong return.

## What this decision authorises

Phase 2 may begin: market contact on testnet, at the `$100` cap.

## What it does not authorise

Live capital. The book has never touched an exchange. Nothing here is evidence
about partial fills, rate limits, maintenance windows, or how the basis behaves
intraday — the simulation uses daily closes and cannot see the wick that
liquidates a short leg.

`promotion_gate_allows_live()` remains `False` and every one of its human items
remains unsigned.

---

```
signed:
date:
git commit at signing:
```
