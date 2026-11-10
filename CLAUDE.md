# Claude Code operating brief

**Read `docs/MONEY_MISSION.md` before planning or editing.** It is the canonical business objective, verified project snapshot, blockers and priority order. Then read the relevant sections of `bot/RUNBOOKS.md`. Re-verify live GitHub/Fly/Supabase state; dated snapshots are not live truth.

## Mission: maximise verified net money, not activity

Build the highest-quality, risk-adjusted money-making system this evidence can justify. Optimise for **long-run net realised dollars on owner economic capital after every fee, spread, slippage, funding, financing, borrow, impact, hedge, infrastructure and operational cost**. Capital survival, drawdown, liquidity, counterparty and model risk constrain the objective.

Rank work by:
1. Expected net return per unit of economic capital, with conservative uncertainty bounds.
2. Out-of-sample robustness, independent observations, point-in-time integrity and multiple-testing adjustment.
3. Worst-case loss, drawdown, tail exposure, liquidation risk and probability of capital survival.
4. Capacity, turnover, scalability, time-to-cash and the full operating cost.

A feature, architecture, dataset, model, green CI run, paper result or Testnet fill is not a financial result. Do not optimise for code volume, dashboards, agent count, PR count, impressive backtests, gross yield or activity. Every substantial task must state which measurable economic uncertainty or bottleneck it resolves and what evidence would make us reject the work.

**Be ambitious about finding edge and ruthless about rejecting false edge.** Compare viable hypotheses on common verified data. Prefer the simplest strategy with the strongest net evidence and usable capacity. If none clears the gate, say NO WINNER and improve the evidence or test a genuinely different economic mechanism. Never tune a failed hypothesis until it passes, silently expand a trial family, or disguise an in-sample result as a forecast.

## Operating contract (binding on every session)

- **One integration owner, one mission PR.** The lead agent owns the integrated result, the tests and the release. Do not open a PR per phase or per task: keep ONE active mission PR (reuse an existing open one) and add integrated commits to it. Never discard another branch's work: merge it in, then close the superseded PR with a reference. Close a PR as superseded only after confirming its changes (or lack of any) are included.
- **Do not stop at a milestone.** An implemented component, a passing local test, an opened PR or a pending CI run is not completion. Work continuously through the backlog until the repository-side mission is complete or the only remaining blocker is a genuine owner-only action, for which give the exact safe command or dashboard step and why it cannot be done by the agent.
- **Independent subagents are mandatory** for broad analysis and adversarial review: at minimum market-data/recorder, quantitative research, execution/microstructure, capital/accounting and an integration red team. Subagents are READ-ONLY (no branches, commits, PRs, pushes or deploys) and report findings with file, function, reproduction and proposed fix. The lead integrates, tests and decides; a subagent's completion message is never a stopping point.
- **Test yourself completely.** Run the narrow tests, then the full relevant matrix: Python 3.11 (production baseline) and 3.12, carry correctness and rest-of-suite, recorder/monitor/supervisor, research and competition, economic-identity, capital/accounting, execution/lifecycle, Testnet authority/refusal, Supabase migrations on PostgreSQL, production image build/import, forward-shadow, secret scan, dependency audit/SBOM, frozen-strategy proof and Fly build context. Wait for the actual GitHub checks on the exact head SHA and read failure logs. A pending, skipped or different-interpreter result is not a pass. Never weaken a test, drop a case or change a risk threshold to get green.
- **Self-review and permitted self-merge.** The mission PR is ready only when: recorder and monitor behaviour are tested; economic evaluation is separate from execution authority; the durable decision record is integrated; one net-P&L identity holds across every module; research is reproducible and multiple-testing controlled; capital allocation agrees with accounting and risk; execution and unwind are reconciled against the venue; the single-writer lease is tested; self-healing is bounded and fail-closed; all required CI and security checks are green on the exact head; an independent adversarial review of the integrated diff has no unresolved critical finding; and the PR description states what works, what is owner-only and what is economically proven. Then the agent may merge it with the permitted GitHub method, confirm GitHub reports it merged, verify the resulting `main` SHA and main CI, and continue with remaining work. Never fake an approval, bypass branch protection or a required check, or override a failing check; if GitHub policy blocks the merge, report the precise requirement.
- **Money is the scoreboard.** Accept work only by measured net dollars and bps on full economic capital, independent sample, out-of-sample evidence, multiple-testing adjustment, drawdown, tail loss, capacity and capital survival. A simulation return is not a real return, a Testnet return is not owner profit, and a candidate that fails its gate is not profitable: say NO WINNER. Do not trade or loosen limits to look active.
- **No unauthorised actions.** Never deploy, provision, change secrets or production Supabase, apply migrations, create or copy an owner capability or grant, place or enable an order-capable run, or touch Mainnet. Those stay owner-controlled; provide exact read-only verification and an explicit change plan.

## Non-negotiable controls

- **Paper/Testnet only.** Never set `LIVE_TRADING_ACK`, `USE_TESTNET=0` or `PAPER_TRADING=0`. Never weaken `config.is_live_authorized`, the promotion gate, venue identity checks, transport guards, risk limits, owner authority, kill switches, reconciliation or global leases. No Mainnet order, credential, transport, deployment or promotion work.
- Use the venue registry in `bot/venues.py`; do not infer venue identity from a string comparison. EU venue remains unqualified and refused.
- Bybit Testnet credentials must use only `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET`. Generic `BYBIT_API_KEY` / `BYBIT_API_SECRET` are not Testnet credentials. Never read, echo, copy, commit or request secret values. Never commit `.env`, tokens, keys, private account exports, `runs/`, databases, SQLite WAL/SHM, raw evidence streams or owner capability/grant files.
- Treat the owner capability as self-attested, not a cryptographic signature. A capability is not an unattended grant. Do not create, copy, sign, renew or delete either authority file for the owner.
- **One order-capable executor per Testnet account.** The autopilot and legacy conformance drill share the Supabase lease after PR #243. The deployed 2026-11-05 `carry-drill` image predates that guard; never start its conformance path until safely updated and independently verified. `carry-book` remains stopped. Keep `CARRY_DRILL_ENABLED` false/unset.
- An execution is complete only when the venue itself proves flat and open orders are reconciled. Never infer flatness or profit from fills, local state, an event outbox or arithmetic alone.
- Testnet uses fake money. Testnet returns cannot be represented as owner profit or proof of Mainnet expectancy.
- Changes to Fly, Bybit, Supabase production, secrets, database migrations, owner grants, branch protection, or other external authority are owner-controlled. Give exact read-only verification and an explicit change plan; do not perform a remote mutation without the owner's specific approval.

## Mandatory first-session procedure

1. Inspect `git status --short --branch`, `git rev-parse HEAD`, `git log -8 --oneline --decorate`, `git worktree list`, and `git branch -vv`. Fetch `origin/main` and compare. Preserve dirty trees, worktrees, untracked data, state databases and evidence. Never use `reset --hard`, `clean -fdx`, force push, destructive stash/pop or delete/recreate a worktree to "get current".
2. Read `docs/MONEY_MISSION.md`, this file, and relevant runbook sections. Reconcile every status claim against current GitHub checks, current PRs and owner-supplied Fly/Supabase/Bybit output. Distinguish code merged, CI complete, cloud deployed, owner-authorised, venue reachable, session reconciled and economically profitable.
3. Investigate the active recorder-staleness issue first: inspect PR #244 (it was zero-diff at the recorded snapshot), `.github/workflows/carry-watch.yml`, its alert parser, and the exact Fly recorder/supervisor status and logs supplied by the owner. Find the underlying cause. Do not suppress a valid alert or call a read failure "no data" without proof.
4. Produce a ranked, finite plan with (a) economic objective, (b) the evidence gap, (c) implementation, (d) tests, (e) stop/rejection criterion and (f) owner-only operational dependency. Then implement the highest-value repository-side fix; do not return an audit-only report when code can safely be fixed.
5. Before each commit, run the narrow tests, the correct CI bucket, secret scan and relevant image/import checks. New tests belong in exactly one CI suite. New modules imported by an image must be added to its explicit Docker COPY list and structural tests. Keep PRs narrow and mergeable.
6. Re-read the diff for risk-control changes, hidden network/order paths, stale instructions and claims stronger than the evidence. Report exact SHA, tests, remaining blockers and owner commands.

## Engineering rules

- `signals/funding_carry_fade_v1.py` is frozen and pinned by `tools/frozen_proof.py`. Do not alter it. A separate candidate is not permission to trade.
- `bot/fly.toml` is generated: edit `bot/tools/fly_stack.py`, then render/check it.
- Keep research, paper, Testnet and owner-authorised status separate. Modelled fills are not measured fills; a replay is not out-of-sample evidence; a public-market edge is not reconciled account profit.
- Use independent reviewers to challenge assumptions, not to create parallel competing implementations or multiple PRs against the same files. One integration owner controls the final branch.
- No speculative infrastructure. Do not provision the research worker until the owner-approved data-transfer and storage plan fits the actual data, backups, seals and growth budget. Do not build another agent or dashboard unless it removes a quantified bottleneck in research quality, execution cost, reliability or monetisation.
- Do not promise background work. Work in the current session, report what was actually completed and what remains owner-side.

## Commands and source of truth

See `bot/RUNBOOKS.md` for current operational procedures. The main code is under `bot/`; Python 3.11 is the production baseline and Python 3.12 is tested. Use the repository's pinned constraints and Makefile/CI definitions rather than inventing commands. Typical local checks include:

```bash
python3 bot/tools/secret_scan.py
python3 bot/tools/frozen_proof.py
python3 bot/tools/fly_stack.py --check
```

Do not treat a successful local subset as the full CI gate. The canonical snapshot and next money priorities are in `docs/MONEY_MISSION.md`.
