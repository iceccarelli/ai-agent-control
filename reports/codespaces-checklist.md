# Codespaces runbook

Every command here has been run and its result recorded. Nothing contacts an exchange except `scripts/runner_status.sh`, which reads your own Fly runner. Do not source or print `runs/env.sh`, and never run a command here against `bot/state/trading_state.db`.

## 1. Sync and confirm your state is untouched

```bash
cd /workspaces/ai-agent-control && git pull --ff-only origin main
```
```bash
sha256sum bot/state/trading_state.db && git ls-files -v | grep '^S'
```
Your working database hash must not change as a result of a pull or a test run.

## 2. Environment (outside the repo)

```bash
python3 -m venv ~/venv-bot && ~/venv-bot/bin/pip install -q -r bot/requirements.txt pytest
```
```bash
make -C cpp
```
`make` writes only the git-ignored `bot/lib/libcarrycore.so`.

## 3. Verify the code (zero failures means ready)

The correctness gate, exactly as CI runs it:
```bash
cd /workspaces/ai-agent-control/bot && REQUIRE_NATIVE_CORE=1 ~/venv-bot/bin/python -m pytest -q -rs -p no:cacheprovider $(grep -oE 'tests/test_[A-Za-z0-9_]+\.py' ../.github/workflows/carry-verify.yml | sort -u)
```
Every other test file (the `rest-of-suite` job):
```bash
cd /workspaces/ai-agent-control/bot && ~/venv-bot/bin/python -m pytest -q -rs -p no:cacheprovider -m "not data_freshness" $(comm -23 <(ls tests/test_*.py | sort) <(grep -oE 'tests/test_[A-Za-z0-9_]+\.py' ../.github/workflows/carry-verify.yml | sort -u))
```
Date-pinned tests, which are meant to go red as time passes:
```bash
cd /workspaces/ai-agent-control/bot && ~/venv-bot/bin/python -m pytest -q -p no:cacheprovider -m data_freshness tests
```
Last verified (2026-10-29): the gate passes on Python 3.11 and 3.13 (5255 passed, 2 skipped) and on 3.12 (5257 passed, 2 skipped, with the one failing operator test fixed and re-run afterwards); the other files give 3753 passed, 2 skipped, 15 xpassed; the date-pinned tests give 13 passed. The two gate skips are `test_control_plane_boundary.py` ("no reviewer module yet").

## 4. Reconcile your real drill evidence (read-only, writes to /tmp)

```bash
cd /workspaces/ai-agent-control/bot && python3 tools/evidence_reconcile.py --evidence ../runs/drill3_evidence.jsonl --drill-report ../runs/drill3_out.json --now-ms $(date +%s%3N) --json /tmp/r.json; python3 tools/recon_summary.py /tmp/r.json
```
Exit 0 with `VENUE_RECONCILED` is the expected result for `drill3`. Exit 2 means the records do not all agree. Exit 3 means the tool refused. `runs/close_evidence.jsonl` with `close_claims.json` exits 2 with `CYCLE_NO_ENTRY` by design: that claim file covers the closing leg only.

To detect a truncated evidence file, record where a chain ends once, then pass it back later:
```bash
wc -l <file>.jsonl && tail -n 1 <file>.jsonl | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['record_hash'])"
```
```bash
cd /workspaces/ai-agent-control/bot && python3 tools/evidence_reconcile.py --evidence <file>.jsonl --expect-records <N> --expect-head-hash <HASH> --now-ms $(date +%s%3N) --json /tmp/r.json
```
The drill now records both values in its report (`evidence_capture.head_hash`, `evidence_capture.records`), and the testnet drill workflow passes them automatically.

## 5. Weekly health check of the forward clock

```bash
cd /workspaces/ai-agent-control && scripts/runner_status.sh
```
Read-only. Never re-initialise the forward book and never change a frozen constant. The forward book and the public-market data live on the Fly runner, not in Codespaces.

## 6. Recording a human promotion approval (only when you have read the evidence)

The gate no longer trusts a flag in the repository. Approve one item at a time, writing outside the repo:
```bash
cd /workspaces/ai-agent-control/bot && python3 tools/promotion_approve.py --key kill_switch_drill_recorded --out ~/promotion_approvals.json
```
Then set `PROMOTION_APPROVALS_FILE=$HOME/promotion_approvals.json` in the process environment. Changing the item's entry afterwards voids the approval. This does not open live trading: the live-arming chain and the forward evidence are still required.

## What "ready" means

Ready is steps 1-4 passing. It does not mean profitable. Live trading, any profit claim and any real capital stay blocked until the frozen protocol has venue-reconciled closed forward trades (see `forward-holdout-plan.md`).
