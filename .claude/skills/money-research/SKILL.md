---
name: money-research
description: Run or prepare the next VALID pre-registered research experiment on verified data with the existing multiple-testing controls; report net economics, sample quality, uncertainty and a decision. Never tune a failed experiment.
---

# /money-research

1. Inventory the data the experiment needs: `python3 bot/tools/dataset_inventory.py --root <data root> --out <file outside git>` (read-only). Name what is missing for each candidate family (see `bot/research/candidates/*.json`, `repo_has=false` items).
1b. BEFORE the first evaluation read of any `market_history` root: `make holdout-status`, then once `make reserve-holdout` (RUNBOOKS, Full-universe history, step 8). Every research CLI refuses an unreserved root and cuts the reserved last 30% of the window out of every read; `--unreserved-exploratory` is stamped NOT_A_HOLDOUT_STUDY and forbids reserving that data afterwards. Plan the backfill at about 660 days (one registered test needs 457 inside the explore window).
2. Status of each candidate: NOT_TESTABLE / INSUFFICIENT_DATA / REJECTED_UNECONOMIC / RESEARCH_CANDIDATE / PROSPECTIVE_EDGE_SUPPORTED, from `bot/tools/candidate_competition.py` and `bot/tools/candidate_readiness.py` on COMMON data. Never rank candidates that did not see the same inputs.
3. Run only a PRE-REGISTERED experiment (`bot/tools/hypothesis_registry.py`, `research_harness.VariantLedger`). Count every run: the trial family only grows. Do not alter a registration, do not re-run a variant until it passes, do not use the holdout for tuning. `reg-1` is not touched; `reg-2` is already counted as the second trial.
4. Costs: net = gross - fees - spread - slippage - impact - adverse selection - funding/financing - borrow - basis/hedge cost, each once, with provenance MEASURED / VENUE_STATED / DERIVED / MODELLED / ASSUMED / UNKNOWN. An assumed critical input can never PASS.
5. Report: net bps and dollars on FULL economic capital, independent sample size, confidence interval, out-of-sample result, multiple-testing adjustment, drawdown, tail loss, capacity. The competition may and often should conclude NO WINNER; say so with the missing-data list.

Never present an in-sample result as a forecast, a Testnet result as owner profit, or a modelled fill as a measured one.
