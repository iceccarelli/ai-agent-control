# ai-agent-control

A delta-neutral crypto carry bot. Long spot BTC, short perp BTC, collect the
funding leveraged longs pay. Zero price exposure by construction.

This is not a research project. The goal is a book that holds positions and
makes money. Everything in here is subordinate to that.

**Status: the executor is built and tested. It has never touched an exchange.**

---

## Quick start

```bash
git clone https://github.com/iceccarelli/ai-agent-control
cd ai-agent-control
python3 -m venv .venv && . .venv/bin/activate
pip install "requests<3" "numpy<3" "pytest>=8" "scipy<2" "scikit-learn<2"
./scripts/install-hooks.sh
./scripts/verify.sh              # must print: 5162 passed, 2 skipped
```

Run the book against no venue:

```bash
cd bot
BOOK_MODE=carry USE_TESTNET=1 PAPER_TRADING=1 python3 main.py
```

Check venue reachability:

```bash
python3 tools/connector_check.py
```

From Codespaces this returns Bybit 403 / Binance fapi 451. That is expected and
is the current blocker on everything downstream.

**Pull before you do anything.** Three separate failures in this project's
history were a stale local checkout wearing different clothes.

---

## The business

```
LONG spot BTC  +  SHORT perp BTC  =  zero price exposure
```

Price rises: the spot leg gains what the perp leg loses. Price falls: the
reverse. You are flat. What you keep is the funding rate, paid every eight
hours by leveraged longs to whoever holds the other side.

Measured on this repo's own 4,431-print BTCUSDT funding corpus:

```
funding positive         85.4% of prints
mean rate                +0.625 bps per 8h
collected over 4.05 yr   27.70% of notional   =  6.84%/yr GROSS
entry cost               31 bps ONE TIME, not per period
```

You are not predicting anything. You are supplying balance sheet to people who
want leverage, and charging them for it. That is a business with an economic
reason to exist.

**Read the caveat in "What has never been done" before treating 6.84% as a
return.** It is a sum of funding prints, not a simulation.

### Why the previous eleven signal families died

They tried to *predict* BTC direction on daily bars. 25 bps round trip against
~36 entries a year is 900 bps of annual friction against a ~15 bps edge. Dead on
arithmetic before a single backtest ran.

The twelfth directional family will also die. Do not build one.

### The 10x question, settled with arithmetic

BTC daily volatility is 2.53% (48% annualised). Liquidation engines use the
**wick**, not the close:

```
median daily high-low range    3.17%
95th percentile range          7.96%
max range in the corpus       20.39%
```

At **10x directional** your liquidation band is 10%, and the 95th-percentile day
already ranges 8%. That is roughly eighteen days a year where a single wick comes
close, and one day in the corpus would have liquidated 10x twice over.

At **10x on a delta-neutral pair** the legs cancel and you are levering a carry
spread, not a view. That is what carry desks actually run.

`CarryEngine.on_candle()` has no `side`, `direction`, `long`, `short` or `signal`
parameter, and a test asserts it never gains one. The engine is physically
incapable of expressing a directional bet. Keep it that way.

---

## Architecture as built

```
main.tick()
  session.tick
  → get_equity
  → engine.observe_exits          venue-side fills reach the ledger
  → risk.should_halt_trading
  → if self.carry:  run the carry book and RETURN
       the directional voter is never consulted. They share one liquidation
       price and must never run in the same process.

CarryEngine.on_candle(mark, funding_bps)          carry_engine.py
  HALTED check
  → finite mark              a book that cannot be marked cannot be hedged
  → margin headroom          the short leg is the one that liquidates
  → delta drift              measured from FILLS, never assumed
  → funding decision
  → open / rebalance / harvest / unwind

  Both legs land or neither.
  Hedges what the spot leg ACTUALLY FILLED, not what was requested.
  Rebalances the PERP (5.5 bps), never the spot (10 bps, and it is collateral).
  Unwinds perp-first: it is the leg that can be liquidated.
  Three consecutive negative funding prints → exit. One → hold.

CarryBroker                                       carry_broker.py
  place_market · get_margin_multiple · get_mark · get_funding_bps

  orderLinkId is DETERMINISTIC, derived from a persisted sequence plus the
  order intent, so a retry after a timeout carries the SAME id and the venue
  rejects the duplicate. Bybit 110072 (linear) / 170130 (spot) mean THE ORDER
  EXISTS: resolved by QUERYING the id, never by recording a rejection.

  get_margin_multiple RAISES when unreadable and catches nothing (AST-tested).
  An unreadable margin is a margin call you cannot see.

  reconcile_pair OBSERVES ONLY and names the naked side. Every call is a GET.
  It never rewrites the ledger to agree with the venue: a book that edits
  itself to match whatever it finds cannot detect a bug, a missed fill, or an
  order somebody placed by hand.

BOOK_MODE selects "carry" or "directional". build_bot attaches ONE and returns.
An unrecognised value RAISES — a typo must not select a strategy.
```

---

## What has never been done

This is the honest gap list. Read it before writing anything.

### 1. The carry strategy has never been backtested

**This is the most important line in this document.**

The 6.84% figure is a naive sum of funding prints over four years. It is not a
simulation. Nothing has ever modelled:

- entry and exit timing across the actual corpus
- rebalance costs when delta drifts past the band
- slippage at size on both legs
- the spot borrow / margin cost on the long leg
- basis drift — the spot-perp spread moves and is a real P&L term
- what happens in a negative-funding regime lasting weeks
- funding-rate *changes* mid-position

The repo has a rigorous backtester (`tools/skill_test.barrier_r_for_all_bars`)
and it only understands **single-leg directional trades**. There is no two-leg
carry backtester anywhere in this tree.

**You are about to deploy a strategy whose returns have never been simulated.**
Build the carry backtester first. Everything else is downstream of knowing
whether this works.

### 2. It has never touched an exchange

`FakeClient` proves the state machine. It proves nothing about partial fills,
rate limits, maintenance windows, or Bybit's spot/perp settlement behaviour.

### 3. Missing capabilities, ranked by how much they cost you

| Gap | Why it matters |
|---|---|
| **No carry backtester** | You don't know if the strategy works. See above. |
| **No basis monitoring** | Spot-perp spread is a P&L term you currently ignore. Entering at a wide basis can cost more than a month of carry. |
| **No borrow / margin cost model** | The spot leg is financed. That cost comes straight off the 6.84%. |
| **No P&L attribution** | You cannot tell carry from basis from fees. Without this you cannot know *why* you made or lost money. |
| **No WebSocket feed** | Polling only. You learn about fills late and miss funding-window timing. |
| **No depth/slippage model** | At $100 it doesn't matter. At $1m it is the whole game. |
| **No partial-fill handling in the pair** | A half-filled leg is currently unwound. At size you want to complete it instead. |
| **No funding forecaster** | Entry is reactive. Predicting the next 8h rate is tractable and lets you size by expected carry. |
| **No cross-venue routing** | Funding differs across Bybit/Binance/OKX. Routing to the richest short leg widens the edge materially. |
| **No stress scenarios** | 2020-03-12, 2022-11 (FTX), 2024-08-05. What does the book do when basis blows out and funding inverts simultaneously? |
| **No observability** | No metrics, no dashboards, no alerting. You cannot operate what you cannot see. |
| **No cold-start reconciliation** | If the container restarts holding a pair, it rebuilds from the ledger, not from venue truth. |
| **No accounting ledger** | Realised/unrealised, funding received, fees paid, per-period. Required for anyone to allocate to this. |
| **Carry legs bypass `RiskManager.gate_order`** | The notional cap is the ONLY size control on the pair. Defensible — a hedged pair has no stop distance — but it is a *decision*, not an oversight. |
| `trading_engine.py:1010` `fee=0.0` | Exit fees unbooked. Directional path only today. |
| `backtest.py:833` `NotImplementedError` | Linear protective stop. Doesn't block carry (it hedges instead of stopping). |

---

## Hard rules

Violating any of these is the failure mode this codebase exists to stop.

1. **No LLM on the order path.** `main.py` lines 1–36 document how the
   predecessor died: a contrarian mode that flipped SELL into BUY, and an EMA
   fallback that fabricated BUY signals at confidence 0.66 when the strategy
   returned HOLD. An LLM may explain the book, draft a verdict, flag an
   anomaly. It may never be the entry function.

2. **No silent defaults on risk parameters.** The notional cap comes from
   `shadow.SHADOW_MAX_NOTIONAL_USD` — the same constant `promotion_gate`
   polices. A test patches it to 250 and asserts the book follows. Never read a
   cap with `getattr(..., default)`. Never guard a dependency with `hasattr`:
   an `AttributeError` at construction is the correct outcome for a misnamed
   method.

3. **No module assigns `live_authorized`.** An AST test enforces it.

4. **Every gate fails closed.** An exception is a refuse, never a pass.

5. **Only CLOSED TRADES count.** `setups → flags → eligible → candidates →
   entries → CLOSED TRADES`. A bar arriving is not an observation.

6. **Do not weaken or skip a test.** When one blocks you, read what it asserts.
   It has been right every single time.

7. **Never commit a test count you did not watch print.** `./scripts/verify.sh`
   writes a receipt binding the number to `git write-tree`; the `commit-msg`
   hook refuses anything else. This failed twice before the mechanism existed.

8. **Do not move the frozen constants:** `FUND_ABS 0.0001`, the funding join,
   `HORIZON 5`, the barrier function, the folds, the caps, `M1`/`M2`,
   `GATE_PATH`.

---

## How this tree treats you

It will catch you. It caught the previous engineer **seven times**:

| What was attempted | What caught it |
|---|---|
| Over-scoped patch across 49 tools | Digest pin on 16 archival provenance tools |
| Undeclared payload key | AST test splitting *scoring* from *payload* |
| Import into `market_data` | `INTEGRATION_MAP` §1, dependencies point downward |
| `except: pass` | *"an exception swallowed without a word is a failure nobody will ever see"* |
| Dockerfile drift (twice) | COPY import-closure check |
| A `getattr` risk default | Found by **running** the code, not by the tests |
| A test stub with wrong method names | Passed while production would have failed |

Every time, the tree was right. Treat a failing test as information, never as an
obstacle.

Note the sixth row: **a silent risk-cap default survived 5,148 passing tests and
died to a one-line sanity check.** Verify by running, not by reading.

---

## History

| # | What |
|---|---|
| 0001 | Dockerfile COPY = `main`'s AST-computed import closure. Shipped 10 modules; `main` needs 20. The image died on its first import. |
| 0002 | A live-armed process must pass the promotion gate to start. It previously had zero call sites. |
| 0003 | Broker/persistence: recover landed orders, chase unknowns |
| 0004 | Observe exchange-side exits every cycle. `record_exit_fill` was never called in live. |
| 0005 | The three Track C operator scripts |
| 0006 | Loopback health bind, bounded decision journal |
| 0007 | `ROADMAP_FROM_100_DOLLARS` |
| 0008 | Barrier empty-input arity, negative-quantity gate, corpus unit audit |
| 0009 | Git provenance guard — 53 tools crashed on a fresh unzip |
| 0010 | Hypothesis registry + Deflated Sharpe |
| 0011 | Data-read ledger, purged CPCV, freeze note |
| 0012 | The read ledger records itself via `market_data.READ_OBSERVER` |
| 0013 | Carry engine: two legs, one position, executes on candle |
| 0014 | Carry broker: the venue adapter |
| 0015 | **Wire the carry book.** Before this, `CarryEngine` was a module nothing constructed. |
| 0016 | No silent defaults on the carry path |
| 0017 | Verify receipt: refuse test counts no run produced for this tree |

### The finding that closed the old research programme

`funding_carry_fade_btc_v1` was the only cleared signal. Its holdout was
contaminated: slice 55 measured all 1,461 BTC bars across three symbols and
picked BTC **because BTC won** (97.0/97.5 vs ETH 47.7 vs SOL 2.3). Slice 57 then
declared a BTC-only product on **the late half of those same bars** — out of
sample for *fitting*, not for *selection*.

```bash
python3 tools/reserved_holdout.py --certify BINANCE_LINEAR_BTC_USDT_1D \
  --from 2024-08-09T00:00:00Z --to 2026-08-09T00:00:00Z    # ALREADY_READ
python3 tools/hypothesis_registry.py                        # 29 trials, 77.4%
```

29 recorded trials. Family-wise false-positive rate 77.4%. One clear at that
width is what a null search produces on its own. Forward: two closed trades,
−1.0632 and −0.6084.

`docs/human/FREEZE_NOTE_SLICE77_funding_carry_fade_btc_v1.md` is **unsigned**,
and a test keeps it that way.

---

## Roadmap

| # | Work | Done when |
|---|---|---|
| **0018** | **Carry backtester.** Two-leg simulation over the real corpus with rebalance costs, slippage, borrow, and basis. | You know what this strategy actually returns |
| 0019 | AWS: VPC eu-central-1, ECS Fargate **one task**, private subnet, NAT egress allowlist to Bybit only, secrets in Secrets Manager, state on EFS/RDS | `connector_check.py` returns 200 from inside the VPC |
| 0020 | Testnet paired orders — real fills, partials, rate limits, $100 cap | A pair opens, drifts, rebalances and unwinds on a real venue |
| 0021 | Basis monitoring + entry gating on spot-perp spread | Entries stop paying away a month of carry |
| 0022 | P&L attribution: carry / basis / fees / borrow | You know *why* you made money |
| 0023 | Observability: metrics, dashboard, incident alerting | You can operate it |
| 0024 | Kill-switch drill on AWS, recorded | An unsigned gate item becomes signed |
| 0025 | Funding forecaster (RunPod/Colab) — output is a **notional**, never a direction | Book sized by expected carry |
| 0026 | Cross-venue routing (Bybit/Binance/OKX) | The edge widens |
| 0027 | Staged leverage on the hedged spread: 2x → 5x → 10x, drilled each step | The return |

**Vercel/MCP is read-only.** An ops dashboard and read-only MCP tools
(`get_book_state`, `get_incidents`, `get_funding_history`) so an agent can
*explain* the book. Order tools do not exist in that surface.
**Vercel reads, AWS trades. Never inverted.**

### AWS specifics that are not optional

- `desired_count=1`, `maximumPercent=100`. Two tasks against one Bybit account
  means two engines placing pairs against one liquidation price, each unaware of
  the other's legs.
- Egress allowlist to Bybit only. If the container is compromised it cannot
  exfiltrate anywhere else. This is the control people skip.
- Bybit key: read-only → testnet-trade → mainnet with **withdrawals disabled**
  and IP pinned to the NAT EIP. A key that cannot withdraw cannot lose principal.
- State on EFS/RDS. Fargate storage is ephemeral; a task that dies holding a
  pair and returns with an empty ledger doesn't know it has a position.

---

## Working method

Read before write. Name the files you will touch before touching them. Smallest
change that preserves fail-closed behaviour. Tests first for any gate, broker or
persistence change. Bytes beat prose: publish sha256 when you append data.

**Verify by running, not by reading.**

End every session with: files changed · tests run + result · gate still false? ·
`FUND_ABS` still 0.0001? · what is still naked · what was NOT done · the next
concrete command.

Be blunt. If the honest summary is *"paired executor built, never touched a
venue, do not point it at money"* — write exactly that. Refuse rather than guess
a threshold. Never claim autonomy because a loop exists.

---

## The honest ceiling

Total crypto perp open interest across all venues is $80–150bn. BTCUSDT USDT-M
median daily volume is ~$11.5bn. A world-class single-venue crypto carry book
tops out around **$100m–$2bn AUM**.

Renaissance's Medallion is capped near $10bn and returns capital annually,
because capacity binds before skill does. BlackRock and Vanguard are asset
gatherers who would not allocate here; the realistic buyers are crypto-native
funds, family offices, and prop desks — and they will ask for exactly the two
things this repo is closest to having: an auditable evidence process and a
strategy with an economic reason to exist.

Aim at the top of that range. It is a real fund and it is reachable. Anyone
promising more is selling something.
