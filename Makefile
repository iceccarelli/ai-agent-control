# Operator targets for the Codespace. EVERY target here is offline, read-only or PUBLIC-data only. There is deliberately NO target that submits an order:
# a Testnet order is the single explicit command in bot/RUNBOOKS.md ("Testnet execution: owner procedure"), typed by the owner after reading the plan digest.
# Mainnet cannot be reached from this tree. tests/test_ops_layer.py fails if a target here references an execute path, a live switch or a credential.
SHELL := /bin/bash
REPO  := $(CURDIR)
BOT   := $(REPO)/bot
DATA  ?= $(HOME)/datasets
OPS   ?= $(HOME)/.ai-agent-control/ops
ROOT  ?= $(DATA)/md-collector
UNI   ?= $(shell ls -d $(DATA)/universe-v3-* 2>/dev/null | sort | tail -1)
HIST  ?= $(shell ls -d $(DATA)/md-2* 2>/dev/null | sort | tail -1)
ART   ?= $(DATA)/pipeline-out
PY    ?= python3
PORT  ?= 8787
S     := $(PY) tools/ms_testnet_session.py --state-dir $(HOME)/.ai-agent-control/ms_testnet

.PHONY: help setup check test-quick scope diagnose plan backfill collect-start collect-stop collect-status collect-log seal backup restore-check audit \
        control-centre-start control-centre-stop pipeline testnet-preflight testnet-plan testnet-status ops-resume

help:
	@echo "setup | check | test-quick | scope | diagnose | plan | backfill | collect-start|stop|status|log | seal | backup | restore-check | audit"
	@echo "control-centre-start|stop | pipeline | testnet-preflight | testnet-plan | testnet-status      (no order target exists)"

setup:
	cd $(BOT) && $(PY) -m pip install -q -r requirements.txt -c constraints.txt pytest
	$(MAKE) -C cpp
	mkdir -p -m 700 $(DATA) $(HOME)/seals $(HOME)/backups $(OPS)

check:
	cd $(BOT) && $(PY) tools/secret_scan.py && $(PY) tools/frozen_proof.py | grep '"ok"' && $(PY) tools/fly_stack.py --check

test-quick:
	cd $(BOT) && REQUIRE_NATIVE_CORE=1 $(PY) -m pytest -q -p no:cacheprovider tests/test_universe_capture.py tests/test_funnel_contracts.py tests/test_ms_pipeline_cli.py tests/test_control_centre.py tests/test_ops_layer.py

scope:
	@test -n "$(UNI)" || { echo "no universe capture under $(DATA); set UNI=..."; exit 2; }
	cd $(BOT) && $(PY) tools/universe_capture.py scope --dir $(UNI) | tee $(DATA)/scope-latest.json | head -c 4000; echo

diagnose:
	@test -n "$(HIST)" -a -n "$(UNI)" || { echo "set HIST= and UNI="; exit 2; }
	cd $(BOT) && $(PY) tools/backfill_plan.py diagnose --universe-capture $(UNI) --history-root $(HIST) --include-statuses Trading,Closed > $(DATA)/diagnose-latest.json; head -c 4000 $(DATA)/diagnose-latest.json; echo

plan:
	@test -n "$(HIST)" -a -n "$(UNI)" || { echo "set HIST= and UNI="; exit 2; }
	@mkdir -p -m 700 $(DATA)/full-history
	cd $(BOT) && $(PY) tools/backfill_plan.py plan --universe-capture $(UNI) --history-root $(HIST) --include-statuses Trading,Closed --out $(DATA)/full-history/plan.json | head -c 4000; echo

# PUBLIC unsigned GETs only; resumable: re-run the same target to continue.
backfill:
	@test -f $(DATA)/full-history/plan.json || { echo "run 'make plan' first"; exit 2; }
	cd $(BOT) && $(PY) tools/market_history.py run-plan --plan $(DATA)/full-history/plan.json --out-root $(DATA)/full-history/data --universe-capture $(UNI)

# --- continuous PUBLIC collector, supervised; ENABLED marks the owner's intent so a Codespace restart resumes it (see ops-resume)
collect-start:
	@mkdir -p -m 700 $(OPS) && rm -f $(DATA)/STOP && touch $(OPS)/ENABLED
	@if [ -f $(OPS)/supervisor.pid ] && kill -0 $$(cat $(OPS)/supervisor.pid) 2>/dev/null; then echo "already running (pid $$(cat $(OPS)/supervisor.pid))"; else \
	  nohup $(PY) ops/collector_supervisor.py --root $(ROOT) --state-dir $(OPS) --stop-file $(DATA)/STOP >> $(OPS)/supervisor.log 2>&1 & echo $$! > $(OPS)/supervisor.pid; echo "started pid $$(cat $(OPS)/supervisor.pid); log: $(OPS)/supervisor.log"; fi

collect-stop:
	@rm -f $(OPS)/ENABLED; touch $(DATA)/STOP; echo "stop file written: the collector stops cleanly after its current cycle"

collect-status:
	@if [ -f $(OPS)/supervisor.pid ] && kill -0 $$(cat $(OPS)/supervisor.pid) 2>/dev/null; then echo "supervisor: running (pid $$(cat $(OPS)/supervisor.pid))"; else echo "supervisor: not running"; fi
	@test -f $(OPS)/ALERT && { echo "ALERT:"; cat $(OPS)/ALERT; } || true
	@test -f $(ROOT)/health.json && $(PY) -c "import json;d=json.load(open('$(ROOT)/health.json'));print({k:d.get(k) for k in ('status','stop_reason','last_cycle','consecutive_failures','refusal','last_seal','last_backup','last_restore_check')})" || echo "no health.json yet"

collect-log:
	@tail -n 40 $(OPS)/supervisor.log

ops-resume:
	@if [ -f $(OPS)/ENABLED ]; then $(MAKE) --no-print-directory collect-start; fi; if [ -f $(OPS)/ENABLED -a -f $(OPS)/control-centre.enabled ]; then $(MAKE) --no-print-directory control-centre-start; fi

seal:
	cd $(BOT) && $(PY) tools/market_collector.py seal --root $(ROOT) --manifest $(HOME)/seals/md-seal.jsonl

backup:
	cd $(BOT) && $(PY) tools/market_collector.py backup --root $(ROOT) --to $(HOME)/backups/md --receipts $(HOME)/seals/md-receipts.jsonl --manifest $(HOME)/seals/md-seal.jsonl

restore-check:
	cd $(BOT) && $(PY) tools/market_collector.py restore-check --backup $(HOME)/backups/md --receipts $(HOME)/seals/md-receipts.jsonl --manifest $(HOME)/seals/md-seal.jsonl

audit:
	cd $(BOT) && $(PY) tools/data_freshness_audit.py --root $(ROOT) --backup-dir $(HOME)/backups/md --receipts $(HOME)/seals/md-receipts.jsonl --seal-manifest $(HOME)/seals/md-seal.jsonl

# --- read-only control centre on 127.0.0.1 (Codespaces forwards the port privately)
control-centre-start:
	@mkdir -p -m 700 $(OPS) $(ART) && touch $(OPS)/control-centre.enabled
	@if [ -f $(OPS)/cc.pid ] && kill -0 $$(cat $(OPS)/cc.pid) 2>/dev/null; then echo "control centre already running"; else \
	  cd $(BOT) && nohup $(PY) tools/control_centre.py --data-root $(ROOT) --universe-dir "$(UNI)" --artifacts-dir $(ART) --port $(PORT) >> $(OPS)/cc.log 2>&1 & echo $$! > $(OPS)/cc.pid; echo "control centre on http://127.0.0.1:$(PORT) (Ports tab, keep it private)"; fi

control-centre-stop:
	@rm -f $(OPS)/control-centre.enabled; test -f $(OPS)/cc.pid && kill $$(cat $(OPS)/cc.pid) 2>/dev/null; rm -f $(OPS)/cc.pid; echo stopped

# --- REPLAY research run on verified history (simulated transport only; TESTNET and live modes are refused by the CLI). Needs START/END (epoch ms) and a candidate.
pipeline:
	@test -n "$(START)" -a -n "$(END)" -a -n "$(SPOT)" || { echo "usage: make pipeline START=<ms> END=<ms> SPOT=<spot-snapshot-dir> [CAND=research/candidates/cash_and_carry.json]"; exit 2; }
	@mkdir -p -m 700 $(ART) $(DATA)/ledgers
	cd $(BOT) && $(PY) tools/ms_pipeline.py run --history-root $(HIST) --universe-dir $(UNI) --spot-dir $(SPOT) --artifacts-dir $(ART)/run-$(END) --ledger-dir $(DATA)/ledgers \
	  --start-ms $(START) --end-ms $(END) --candidate $${CAND:-research/candidates/cash_and_carry.json} --execution-policy MAKER_FIRST --derive-risk-policy --mode REPLAY

# --- Testnet: READ-ONLY steps only. The execute step is intentionally not a make target (RUNBOOKS: Testnet execution: owner procedure).
testnet-preflight:
	cd $(BOT) && $(S) preflight

testnet-plan:
	cd $(BOT) && $(S) plan --symbol $${SYMBOL:-BTCUSDT} --notional-usd $${NOTIONAL:-90}

testnet-status:
	cd $(BOT) && $(S) status
