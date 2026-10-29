# Product roadmap

Premise: nothing here should be built before the **validation gate** (10 customer-discovery calls, at least 3 design partners willing to run a pilot). Effort numbers are my estimates for one experienced engineer; they are not measured. RICE uses Reach (accounts/quarter), Impact (0.25-3), Confidence (%), Effort (person-weeks); all inputs are guesses until validated.

## Stage 1 — Immediate fixes (weeks 0-2, no new product)

| Item | Problem solved | Effort | Priority |
|---|---|---|---|
| Add a `LICENSE` (owner decision: Apache-2.0 / BUSL / proprietary) | Unlicensed code cannot be adopted or diligenced | 0.1 wk | P0 |
| Confirm Bybit keys from the original source are revoked; record the date | `DEPLOYMENT.md:7-25` | 0.1 wk | P0 |
| Restore or remove references to missing docs (EDGE.md, AGENT_CONTROL_PLANE.md, TEST_REPORT.md, certification doc) | broken evidence trail | 0.5 wk | P0 |
| Make the full suite hermetic (declare scipy/sklearn as test deps or skip cleanly; fix subprocess interpreter) and run it in CI | 18 failures here; CI runs a subset | 1 wk | P1 |
| Untrack `bot/state/trading_state.db`; update stale counts in docs | hygiene | 0.3 wk | P2 |
| Rename/describe the repo honestly (it is a Bybit carry research bot) | name/product mismatch | 0.1 wk | P1 |

## Stage 2 — MVP (days 30-90): one thing, validated

Working hypothesis [H]: **a self-hosted "agent execution guard" for agents that hold exchange API keys or perp-venue wallets**: a sidecar that enforces notional/velocity/venue/instrument limits, requires human approval above thresholds, holds a human-only kill switch, and writes a tamper-evident audit log.

| Feature | Customer problem | Impact | Effort | RICE (rough) | Validation |
|---|---|---|---|---|---|
| Venue-neutral gate interface extracted from `risk_management.py` | Reusable core | enabler | 3 wk | n/a | design review |
| Policy file (YAML/Cedar-like) with deny-overrides | Configurable limits | High | 3 wk | R20 x I2 x C50% / 3 = 6.7 | pilots |
| Bybit + Hyperliquid adapters | The two venues named by prospects | High | 4 wk | R20 x I2 x C40% / 4 = 4 | pilots |
| MCP/HTTP interface so any agent framework calls it | Distribution | High | 2 wk | R30 x I2 x C60% / 2 = 18 | usage |
| Hash-chained audit log + export | Compliance/forensics | Medium | 2 wk | R15 x I1 x C60% / 2 = 4.5 | prospect asks |
| Human approval via Slack/web | Control | Medium | 2 wk | R15 x I1 x C50% / 2 = 3.8 | prospect asks |

Security risk: this product becomes part of a funds-at-risk path. A bug that fails open is worse than no product. Needs the conformance suite and an external review before any production pilot.

## Stage 3 — Expansion (months 3-6)

EVM adapter (Base first, then Ethereum, Arbitrum, BNB via `chainId`); LangGraph/ElizaOS/AgentKit shims; dashboards; multi-tenant control plane; SOC2-style evidence (only if enterprise prospects ask).

## Stage 4 — Long term (months 6-12+)

Solana and NEAR adapters; cross-venue budget ledger; managed hosted tier; insurance/attestation partnerships; transaction simulation vendor integrations.

## Do NOT build yet

Token or trading strategies, an "AI alpha" feature, a hosted custody product, a seven-chain launch, a mobile app, a marketplace, or a token. The repo's own research closed timing-skill research as ABSENT; adding trading features contradicts the evidence.

## Plans

- **30 days**: Stage 1; 10 discovery calls; write a one-page positioning; decide licence.
- **60 days**: extract the gate core; build Bybit adapter demo with MCP; 3 design partners signed (free pilot).
- **90 days**: Hyperliquid adapter; first paid pilot ($500-1,500/month, hypothesis); third-party review scheduled.
- **12 months**: EVM adapter, hosted tier, decide on seed fundraising or bootstrapped path based on pilot retention.
