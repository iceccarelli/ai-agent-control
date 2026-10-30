# Security and safety review

Status as of 2026-10-29, `main` at `2f8a29a`. Basis: static reading of the order path from process start to order placement, plus offline tests. No exchange or deployed service was contacted, no secret was read, and no database contents were inspected. This is not a third-party audit.

## Controls verified in code and by tests

| Control | Where |
|---|---|
| Live trading needs `USE_TESTNET=0`, `PAPER_TRADING=0`, `LIVE_TRADING_ACK="I_UNDERSTAND"` and both credentials; a typo in a safety flag is read as not live. `is_live_authorized` re-derives the verdict from those fields and requires it to agree with the stored flag, so a `dataclasses.replace` cannot leave a stale "armed" | `bot/config.py` (`_resolve_live_gate`, `is_live_authorized`), `tests/test_live_gate_recomputed.py` |
| Mainnet exposure endpoints (`/v5/order/create`, `/v5/position/set-leverage`) refuse at the transport unless live is authorised; carry orders also pass `carry_orders_permitted_for` and the promotion gate | `bot/bybit_connection.py` `_request`, `bot/main.py` |
| A non-machine promotion-gate item counts only if its flag is a JSON true AND the SHA-256 of its exact entry is in a separate approvals file named by `PROMOTION_APPROVALS_FILE` (outside the repo, written by `tools/promotion_approve.py`). A commit cannot flip it; editing the entry after approval voids it; a missing or malformed file approves nothing | `bot/promotion_gate.py`, `tests/test_promotion_approvals.py` |
| A kill switch that is unreadable blocks; an engaged one blocks; clearing needs a human token. A state DB that loses its kill-switch row reopens **engaged**, and a live-armed process opens a new or zero-byte DB engaged | `bot/persistence.py` `StateStore`, `tests/test_state_store_fail_closed_reopen.py` |
| Size, exposure, liquidation and margin gates refuse NaN, missing or unreadable numbers instead of counting them as zero or safe | `bot/risk_management.py`, `bot/carry_engine.py`, `tests/test_risk_management.py`, `tests/test_liquidation_unknown_reason_fail_closed.py` |
| No LLM module is importable on the order path | `tests/test_control_plane_boundary.py` |
| CI holds no trading credential; the production image is built and imported offline, read-only, non-root | `.github/workflows/carry-verify.yml` |
| The set of files that may read the process environment is pinned by an AST test, and no order-path module may read it at all | `tests/test_environment_read_surface.py` |
| `carry-watch.yml` is off unless a repository variable is set, runs only from `main`, reads one file, and each secret is set only on the one step that uses it (never at job level) | `.github/workflows/carry-watch.yml`, `tests/test_watch_workflow.py` |

## Open findings

| # | Finding | Severity | Next step |
|---|---|---|---|
| S1 | The original Bybit credentials were in the imported source; `bot/DEPLOYMENT.md` says treat them as compromised. The repository cannot show whether they were rotated | High until confirmed | Owner confirms in the Bybit console |
| S2 | `bot/state/trading_state.db` (which holds the kill-switch table) is tracked in git and is also an operator's working database. A checkout, revert or rebase of that file can rewind or reset kill-switch state | Medium | See D1 in `improvement-plan.md` |
| S3 | `setup-flyctl@master` in `carry-watch.yml` is not pinned to a commit. It no longer sees either secret, but it can still change under you | Low | Pin to a commit SHA |
| S5 | The kill switch is enforced in the risk gate, the carry tick and (for entries only) at the carry broker's `entry_gate`. A caller that goes straight to `BybitClient.place_order` still bypasses it; it cannot simply be added at the transport, because exits and the market close after a bracket failure share `/v5/order/create` | Medium by design | Distinguish entries from reduce-only exits at the client before adding a transport check |
| S6 | On a mainnet-pointed but unarmed process, cancel, cancel-all and `/v5/position/trading-stop` are not in `EXPOSURE_ENDPOINTS`. Refusing a cancel would be unsafe, so this is deliberate | Low | Keep, and state it |
| S8 | `Config.BYBIT_BASE_URL` maps the demo venue to the testnet URL and nothing reads it today | Low | Delete it or derive it from `bybit_connection.VENUE_REST` |
| S9 | Safety claims cite documents that are absent (`AGENT_CONTROL_PLANE.md`, `EDGE.md`, the certification document) | Medium | Restore or remove the citations |
| S10 | The drill's evidence hash chain cannot show a cut tail unless the producer's head hash and record count are supplied. The drill now records them and the testnet workflow passes them; other producers do not yet | Medium | Record head hash and count wherever a capture is produced |

## Not assessed

Shell execution paths in `tools/` and `scripts/*.sh`, deserialisation of untrusted files, a hard-coded secret scan beyond a regex, and any threat model. Wallet, signing and chain risks do not apply: the codebase has no such code.
