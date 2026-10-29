# Security and safety review

Date: 2026-10-28. Scope: what I actually read (see `repository-audit.md` section 0). No exploits attempted, no external systems contacted, no secrets printed. A regex scan for hard-coded `api_key|secret = "<16+ chars>"` in `bot/**/*.py` returned nothing [V]; this is a weak check, not a secret audit. `mcp__github__run_secret_scanning` was not run.

## Code-verified controls (strengths)

| Control | Evidence |
|---|---|
| Live trading requires testnet=false, paper=false, exact `LIVE_TRADING_ACK="I_UNDERSTAND"`, and both credentials; a typo in a safety flag is read as *not live* | `bot/config.py:63`, `694-760` |
| Kill switch read failure blocks (`KILL_SWITCH_UNREADABLE`), engaged blocks; trip API exists, clear is human-only by documented contract | `bot/risk_management.py:454-461`, `1495-1498` |
| Promotion gate is a predicate that cannot arm; missing/unreadable file refuses; flipping JSON bools does not open it because two items are re-derived | `bot/promotion_gate.py:1-60` |
| No LLM module is importable on the order path (AST walk plus dynamic-import scan) | `bot/tests/test_control_plane_boundary.py:1-80` |
| Tiny dependency set, pinned test versions | `bot/requirements.txt`, `bot/constraints.txt` |
| Many negative tests: NaN margin, negative quantity, unstated venue fields, hedge-mode refusal, late-landing orders | file names under `bot/tests/` (all passed except the 18 environmental failures) |

## Findings

| # | Finding | Type | Severity | Evidence |
|---|---|---|---|---|
| S1 | Original Bybit credentials were in the source this repo was imported from; the doc says treat as compromised. The tree cannot show rotation. | Verified statement, unknown outcome | High until confirmed rotated | `bot/DEPLOYMENT.md:7-25` |
| S2 | Mutable runtime state (`bot/state/trading_state.db`, includes the kill-switch table and 87 decision rows) is tracked in git and not ignored. I counted rows only; I did not inspect values for secrets. | Verified | Low-Medium | `git ls-files bot/state`, `.gitignore` |
| S3 | Safety claims depend on documents that are absent (`AGENT_CONTROL_PLANE.md`, `EDGE.md`, certification doc). A reviewer cannot confirm the stated role matrix. | Verified | Medium | test docstring line 3 |
| S4 | CI runs a subset and admits a failing baseline; no evidence the whole suite gates merges. | Verified | Medium | `.github/workflows/carry-verify.yml:1-12` |
| S5 | `carry-watch.yml` and the drill workflow use `FLY_API_TOKEN` and `SLACK_WEBHOOK_URL`; token scope/lifetime (`-x 720h` appears in a comment) not reviewed. | Verified existence, scope unknown | Low-Medium | `.github/workflows/carry-watch.yml:16-18` |
| S6 | Single-process SQLite store is the sole source of kill-switch truth ("one writer, many readers"). A second process, restored backup, or corrupted DB could change the state; no external attestation. | Inference | Medium for any multi-tenant use | `persistence.py` (not read) |
| S7 | No third-party security audit, no threat model document in tree. | Verified absence | Medium | file list |

Not assessed: prompt injection (no LLM on the order path by design), wallet/signing/nonce/chain-selection/bridge risks (no such code exists), RPC trust (not applicable), authentication/authorization (no service API), insecure deserialisation, shell execution paths in `tools/` and `scripts/*.sh`. These need a read of the unread modules.

## Relevance to a pivot

The *pattern* here (fail-closed gates, human-only kill-switch clear, predicate-not-actuator promotion gate, AST proof that the model cannot reach the venue) is the most reusable asset. External evidence that the problem is real: the Bybit exchange hack (~$1.5B, Feb 2025), the Drift (~$285M, Apr 2026) and KelpDAO (~$292M, Apr 2026) losses attributed to key/admin/verifier failures rather than contract bugs, and a reported prompt-injection drain of an agent wallet (~$150-200K, May 2026). All of these come from secondary sources via the research subagents; see `appendix/raw-ecosystems.md` and `appendix/raw-competitors.md` for URLs and caveats. Do not treat the dollar figures as verified.
