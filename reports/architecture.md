# Architecture recommendation

## Question: expand this repo, seven repos, shared core + packages, or monorepo?

| Criterion | A. Expand this repo as is | B. Seven repos | C. Shared core + adapter packages | D. Monorepo, separately versioned |
|---|---|---|---|---|
| Code duplication | Low now, rises | High | Low | Low |
| Security consistency | Mixed with bot code | Poor (seven copies of the gate) | Good | Good |
| Test burden | One huge suite (8.9k tests, 9 min) | Seven | Core tested once + conformance per adapter | Same as C, one CI |
| Dependency isolation | Poor (numpy/scipy bot deps leak) | Good | Good | Good |
| Independent releases | No | Yes | Yes | Yes |
| Community / discoverability | Weak (name misleads, no license) | Fragmented | OK | Best early |
| Maintenance for a solo founder | Medium | **Worst** | Medium | **Lowest overhead** |
| Enterprise packaging | Hard | Hard | Good | Good |

**Recommendation: D now, C's boundaries inside it.** One new monorepo (or a new top-level `core/` + `adapters/` in a fresh repository named for the actual product), packages versioned independently, one CI. **Do not create seven repositories**, and only four adapters are genuinely distinct anyway: EVM (Ethereum, Base, Arbitrum, BNB Chain), Solana, NEAR, Hyperliquid. Keep the existing Bybit bot as its own repo or as `examples/bybit-carry`; do not entangle it.

**Do this only after customer validation** (see roadmap). Today's code does not justify a platform build. No rewrite is recommended: the evidence says the gating *ideas* are sound; what is missing is a venue-agnostic interface, which is a small extraction, not a rewrite.

## Smallest extraction that unlocks the most

From `risk_management.py` (`RiskDecision`, `_block`/`_allow`, gate list at ~line 1097) and `promotion_gate.py`, extract:

1. `GateDecision {allow|block, reason_code, evidence}` plus a `Gate` protocol: `check(intent) -> GateDecision`, fail-closed wrapper that converts any exception into `block`.
2. `ActionIntent` — a venue-neutral description (venue, chain, instrument/contract, side, notional in USD, counterparty/allowlist key, agent id, session id).
3. `KillSwitch` interface with human-only `clear` and tamper-evident storage.
4. `AuditLog` — append-only, hash-chained (the bot already hash-manifests artifacts: `slice*_sha256_manifest.txt`).
5. A conformance test kit: "any adapter must block on exception / NaN / unstated field / stale data", derived from the existing `test_*_fail_closed*` tests.

## Proposed layout (proposal, not existing files)

```
core/
  policy-engine/      intents, gates, deny-overrides rules (Cedar-style optional)
  permissions/        agent identities, scopes, budgets, human-approval workflow
  risk/               notional/slippage/velocity caps, cross-venue budget ledger
  audit/              hash-chained log, export, tamper check
adapters/
  cex-bybit/          wraps existing bybit_connection (reuse)
  hyperliquid/
  evm/                Ethereum, Base, Arbitrum, BNB via chainId param
  solana/
  near/
integrations/
  agent-frameworks/   MCP server/gateway, LangGraph, ElizaOS, AgentKit shim
  wallets/            Privy/Turnkey/Coinbase policy mirrors (verify, not replace)
tests/
  unit/ integration/ security/ conformance/
docs/ examples/
```

## Design rules

- **Stable adapter interface**: `decode(raw_tx_or_order) -> ActionIntent`, `simulate(intent) -> SimResult`, `submit(approved_intent) -> Receipt`. Adapters never decide policy.
- **Enforcement boundary**: policy runs *outside* the agent process (sidecar or hosted), and the signer only accepts requests carrying a signed `Approval`. Mirrors how Coinbase/Privy/Turnkey run policy server-side or in an enclave (secondary-source claim; see competitor appendix).
- **Cross-chain budget**: one ledger denominated in USD with per-agent, per-venue, per-day caps; reservation before submission, reconciliation after (the bot's cash-ledger reconcile discipline).
- **Failure behaviour**: fail closed; kill switch cleared only by a human credential; unknown fields block (existing idiom).
- **Versioning**: SemVer per package; adapter declares `core_api >=X <Y`; conformance suite must pass for a release.
- **Security review**: no adapter ships without chain-specific decoding tests on real transaction fixtures and an external review of the signing path.
