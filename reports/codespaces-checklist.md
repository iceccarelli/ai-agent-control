# Codespaces checklist: bring the bot to life on testnet and send me the evidence

Every command below was found in the repo (docstrings, README, scripts) on 2026-10-29. I have **not** run the ones that need Bybit testnet, because this cloud session has no testnet keys and no confirmed route to `api-testnet.bybit.com`. I have not checked every flag of every tool, so use each tool's `--help` if a flag is rejected.

## What this can and cannot prove (read first)

- **Testnet money is fake.** The repo's own ledger says so (`bot/artifacts/cash_ledger.json`, field `note`). A testnet run proves *execution mechanics*: orders fill, fees are booked correctly, stops are attached, positions reconcile, the kill switch works. It does **not** prove profitability.
- **Profit evidence** in this repo comes only from the promotion gate's forward shadow window: 20 closed forward trades over 180 days (`bot/promotion_gate.py`, `MIN_FORWARD_TRADES`, `MIN_FORWARD_DAYS`), currently 4 of 20 trades and 48 of 180 days. After that, a human-signed risk memo and a capped micro-live step (gate cap is $100 notional, `slice76_promotion_gate.json`). No shortcut exists, and I will not build one.
- **What I will not do**: retune shock/stop/take-profit/horizon/cost to chase the 95.0 bar, call any signal "profitable", arm live trading, or clear the kill switch from code. These are the repo's own invariants and they protect your money.
- **Honest expectation**: every directional signal tested so far came back absent. The carry book (delta-neutral spot + perp, earns funding) is the only line with a mechanism that does not depend on predicting price, so it is the right thing to bring to life first. Its yield is not guaranteed and compresses when funding rates fall.

## Step 0: safety (do this before anything)

1. In the Bybit console, confirm the **old live API keys are revoked** (`bot/DEPLOYMENT.md:7-25`).
2. New testnet keys only: trade + read, **no withdrawal**, IP-bound if possible. Put them in Codespaces **secrets** (`BYBIT_API_KEY`, `BYBIT_API_SECRET`), never in a file, never in chat.
3. Do **not** set `LIVE_TRADING_ACK`. Keep `BYBIT_VENUE=testnet`.

## Step 1: environment and baseline (read-only, no keys)

```bash
cd /workspaces/ai-agent-control        # your Codespace path may differ
git fetch origin && git checkout claude/audit-reports-20261029   # optional: the reports
git rev-parse HEAD
python3 --version
python3 -m venv .venv && . .venv/bin/activate
pip install -r bot/requirements.txt -c bot/constraints.txt pytest
cd bot
python3 -m pytest tests -q -p no:cacheprovider 2>&1 | tail -15     # ~9 min; I saw 18 failures without scipy
python3 tools/print_project_status.py                              # read-only; mode, edge status, live armed?
```
Paste back: the last 15 test lines and the full JSON from `print_project_status.py`.

## Step 2: connectivity (no keys, no orders)

```bash
cd bot
python3 tools/connector_check.py --require-bybit-testnet --out /tmp/connector_check.json
cat /tmp/connector_check.json
```
Paste back the verdict (`CARRY_READS_OK` is what we want; `BYBIT_TESTNET_BLOCKED` means run from another host, e.g. the Fly runner).

## Step 3: read-only preflight with testnet keys (cannot send an order)

```bash
cd bot
BYBIT_VENUE=testnet python3 tools/carry_preflight.py --require-economics-pass
```
It states whether the venue is readable, whether safety preconditions hold, and whether the entry-cost gate would open (`carry_costs.entry_gate`). **Paste the full output, with any key/secret lines redacted** (the tool prints none by design). This single output tells me whether the carry book is economically viable at today's funding rates.

## Step 4: the deployed runner's history (if the Fly app `carry-drill` exists)

```bash
scripts/runner_status.sh          # read-only; last 48 monitor cycles
LAST=0 scripts/runner_status.sh   # all cycles
```
Paste back: how often the entry gate was open and why not.

## Step 5: offline replay of the real engine (no keys)

```bash
cd bot
python3 tools/carry_replay.py --help
python3 tools/carry_replay.py --notional 100000 --mode overlay --style maker_first
```
`carry_replay.py` replays the actual `CarryEngine` class (its docstring explains earlier backtests used a separate loop). Paste back the full output. Treat results as research evidence on historical data, not as a promise.

## Step 6: mechanics proof on testnet (this is the README's own session; fake dollars)

Run only after Steps 1-3 look sane and you are in the Codespace that holds the testnet secrets:
```bash
BYBIT_VENUE=testnet python3 bot/tools/testnet_session.py     # README: exit 0 only if flat and cash_ledger.json exists; exit 2 = zero fills
cat bot/artifacts/cash_ledger.json
```
Then, for any carry state database that run produced:
```bash
cd bot && python3 tools/carry_report.py <path-to-state.db>   # read-only; says REALISED vs PROVISIONAL
```
Paste back: exit code, `cash_ledger.json`, and the `carry_report` output. `carry_report` will call a number PROVISIONAL unless every fee was venue-confirmed; I will quote only what it calls REALISED.

## Step 7: the human gates only you can do

From `slice76_promotion_gate.json`, 6 of 8 items need you: signed risk memo, kill-switch drill (`tools/kill_switch_drill.py`), linear protective-stop verification (`tools/linear_stop_venue_drill.py`), M-4 acceptance, `LIVE_TRADING_ACK`, and the forward shadow window. The kill-switch drill is safe to run first and takes minutes.

## What I will do with what you paste

1. Tell you whether the carry entry gate is open at current funding and what yield after fees it implies, labelled as an estimate.
2. Diagnose any failing test or venue error (the 18 failures should disappear once scipy is installed).
3. Prepare the licensing, docs and CI fixes from `product-roadmap.md` Stage 1 on a branch.
4. Draft the forward-shadow monitoring plan so the 20-trade/180-day clock keeps ticking without manual work.
