# MARKET_CATEGORIES.md — longs, shorts, and the fork between them

The brief asked for a bot that handles **buys and sells and shorts and longs**
by itself. Delivering that honestly required naming something the previous six
slices had quietly assumed away:

> **Bybit spot cannot short.**

Not "does not yet". Cannot. On a spot venue you sell an asset you hold; there is
no position, no borrow in this codebase, and a sell order beyond your balance is
rejected. Every slice up to and including 6 hardcoded `category="spot"`, and the
strategy layer was cheerfully emitting SELL signals with short-shaped stops and
ladders that could never have been filled.

This slice resolves that. It does not resolve it by pretending.

---

## 1. What each category actually is

| Concern | `spot` | `linear` (USDT perpetual) |
|---|---|---|
| Short a symbol | **impossible** without margin borrow | native |
| What a "position" is | a coin balance | a margined position with a side |
| Quantity step field | `basePrecision` | `qtyStep` |
| Minimum notional field | `minOrderAmt` | `minNotionalValue` |
| Market-order quantity unit | `marketUnit="baseCoin"` **required** | base units implicitly |
| Leverage expressed as | `isLeverage` | `positionIdx` + `/v5/position/set-leverage` |
| Protective stop | conditional `orderFilter="StopOrder"` | `/v5/position/trading-stop` |
| Stop resizes with the position | **no** — must be replaced manually | yes, `tpslMode="Full"` |
| Funding | none | charged every 8 hours |
| What "sufficient balance" means | do you hold what you are giving away | do you have the initial margin |
| Liquidation | none | yes, and it can precede your stop |

Two of these are the reasons this is a fork rather than a flag.

**`/v5/position/trading-stop` is valid for linear, inverse and option — not
spot.** That is the vendor's own documentation, and it is the single most
expensive fact in this project's history: the legacy code posted spot stop
losses to that endpoint at twelve call sites, every one inside `except: pass`.
No stop was ever placed and no error was ever logged. Every live position was
downside-naked.

**Funding is a cost that accrues while you do nothing.** A perp held across
three funding stamps pays three times. A cost model that ignores it reports an
edge the account never receives.

---

## 2. What the code does about it

`CATEGORY` is read **once**, by `config.load()`. `ALLOW_SHORTS` is derived from
it and cannot be set by hand — an operator who writes `ALLOW_SHORTS=1` on spot is
ignored, not obeyed, because the alternative is orders the venue rejects or, with
margin on, a leveraged short the risk model never budgeted for.

Four layers then act on that one decision, and they all give the same answer:

| Layer | Behaviour on spot | Behaviour on linear |
|---|---|---|
| `config` | `ALLOW_SHORTS=False`; `USE_LEVERAGE=1` is a **load-time error** | `ALLOW_SHORTS=True`; funding enters `ROUND_TRIP_COST_BPS` |
| `risk_management` | `_gate_short_capability` blocks a sell-to-open: **`SHORTS_NOT_AVAILABLE_ON_SPOT`** | permitted; `_gate_funding` blocks an unknown or expensive carry |
| `bybit_connection` | `SHORT_NOT_AVAILABLE_ON_SPOT` before the balance check; conditional `StopOrder` | `positionIdx=0`, margin-based balance check, position-attached stop |
| `technical_analysis` | SELL signals are **suppressed and counted**, not silently dropped | SELL becomes a short `TradeIntent` |

The engine itself needed almost no change: `TradeIntent.exit_side`,
`ExitPlan.validate` and the PnL direction were already symmetric. That is what a
clean seam buys you — the direction was never the hard part; the *venue* was.

### The read-back is the invariant, and it dispatches

```python
live, detail = client.verify_stop(symbol=..., order_link_id=...)
```

The engine asks one question and does not need to know which venue it is on to
ask it. Underneath, a spot stop is verified as an **order** (`orderStatus` in
`Untriggered`/`New`) and a linear stop as a **field on the position**
(`stopLoss` non-empty). Both fail closed: any exception, any missing record, any
unexpected status is reported as *not live*, and a stop that cannot be proven is
treated as no stop — the position is market-closed and the kill switch trips.

The linear mechanism is strictly the safer of the two, and it removes a bug
class outright: a position-attached stop with `tpslMode="Full"` resizes with the
position, so the "partial take-profit leaves a stop sized for the original
quantity" defect (TEST_REPORT §4, bug 4) **cannot occur there**.

One spelling note, because it cost the legacy code a real stop: the parameter is
`tpslMode`, not `tpSlMode`. Bybit ignores unknown parameters silently, so the
wrong casing returns `200 OK` with no stop set. `test_a_position_with_an_empty_stop_loss_field_is_unprotected`
exists specifically to catch that failure shape.

---

## 3. What is deliberately NOT delivered

**There is no linear backtest.** `Backtester.run()` raises `NotImplementedError`
for any category other than spot.

The simulator keeps cash accounting: a Buy debits quote and credits base, a Sell
does the reverse, a Sell is rejected when the base balance is short. That is
spot, exactly. A perpetual has no base asset changing hands, marks unrealised
PnL against equity continuously, accrues funding, and can be liquidated before
its stop is reached.

Running the linear path through cash accounting would not fail. It would
*succeed* — and produce an equity curve with no funding drag and no liquidation.
Wrong in the flattering direction, which is the worst kind of wrong.

So the state of play is stated plainly:

- the linear **order path** is implemented and unit-tested against a strict fake
  exchange: position-attached stops, `positionIdx`, margin-based balance checks,
  leverage setup, the funding gate, and stop verification;
- the linear **simulation** does not exist, so there is no honest linear
  backtest to report;
- a missing number is better than an invented one.

**Spot margin borrow is not implemented either.** It is the other way to short a
spot venue, and it brings borrow interest, a separate liability balance, and
margin-call mechanics. `USE_LEVERAGE=1` with `CATEGORY=spot` is refused at load
with a message pointing at `CATEGORY=linear`.

---

## 4. What the constraint actually costs — measured

This is the number the fork exists to expose. On the shipped corpus, spot,
4,799 bars across three symbols:

```
signals evaluated          : 14,400  (5,085 actionable)
SHORT_SUPPRESSED_ON_SPOT   :  4,836
```

**Roughly 95% of the directional views this strategy formed were shorts that a
spot account cannot express.** Before slice 7 those signals reached the engine
and died as `INSUFFICIENT_BTC` — a true message that explained nothing, and one
that made a venue limitation look like a funding problem.

Two readings of that number, and the honest answer is that this data cannot
distinguish them:

1. the synthetic corpus contains a crash and a bear leg, so a short bias is
   partly the data, not the strategy; or
2. the strategy is genuinely short-biased and is being run on the one venue type
   that cannot act on it.

Either way, an operator can now *see* it, count it, and decide. `CATEGORY=linear`
is the lever, and it is a deliberate architectural choice with real
consequences — funding, liquidation, and a backtest that does not yet exist —
not a feature flag to flip on the way to production.

---

## 5. Choosing, in order

1. **Stay on spot** if you are long-only, want no funding, no liquidation, and
   the simplest possible failure modes. This is the shipped default, and it is
   the only category with a working backtest.
2. **Move to linear** if the suppressed-short count says you are discarding most
   of your edge. Before you do: write the perp simulator (funding accrual,
   mark-to-market, liquidation price), re-run walk-forward, and only then run
   paper → testnet → microscopic live, per `DEPLOYMENT.md`.
3. **Never** enable leverage as a way to compensate for a weak edge. Leverage
   multiplies an edge; it does not create one, and on a negative expectancy it
   multiplies the loss and shortens the time to ruin.
