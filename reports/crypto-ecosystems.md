# Seven ecosystems for software integration

Date 2026-10-28. **Evidence quality: weak.** All primary data hosts (DefiLlama, L2Beat, EIP pages, Coinbase docs, x402.org) failed to resolve from the session. Every number comes from search-summary secondary sources and sources conflict (e.g. Ethereum TVL $38.2B in June vs $52.7B late September). Scores are judgement. Details and URLs: `appendix/raw-ecosystems.md`. Re-run after the network allowlist is opened.

## Context

Today the repo has zero on-chain code. "Compatibility with existing architecture" is therefore low for every candidate; the Bybit adapter is the only venue adapter. I rank by product value to a **permissions/execution-control sidecar**, not by token performance.

## Ranked shortlist

| # | Ecosystem | Why (evidence, secondary) | Distinct adapter? | Modules to change | Effort | Prioritise when |
|---|---|---|---|---|---|---|
| 1 | **Hyperliquid** (my ordering for this product) | Perp venue like Bybit; ~$178B 30-day perp volume (undated dashboard), HIP-3 markets $62B (May) / $115B (Jul 2026) per sources; TVL ~$1.4B. Closest to the carry/perp logic already in the repo (funding, margin, liquidation distance) | **Yes** (native API, agent wallets) | new `adapters/hyperliquid`, reuse carry risk/cost gates | Medium | First, if customers are perp-trading agent builders |
| 2 | **Base and Ethereum (EVM adapter, `chainId` parameter; also serves Arbitrum and BNB Chain)** | Ethereum TVL ~$52.7B, Base ~$5.9B, BSC ~$5.8B, Arbitrum ~$1.4B (~2026-09-21, via roundup); x402 on Base >100M cumulative transactions but small dollar volume (~$24M/30d official dashboard, wash trading suspected); ERC-8004 agent identity live Jan 2026 (Draft); Coinbase Agentic Wallets target EVM | Mostly one adapter | tx decoding, simulation hook, session-key/ERC-4337/7702 policy | Medium-High | Second; Base first for agent payments |
| 3 | **Solana** | TVL ~$6.2B; x402 reportedly leads daily count; Solana Agent Kit ~1.7k stars; Drift exploit (~$285M, Apr 2026) came from admin-key compromise | **Yes** (SVM, different tx model) | new adapter and decoder | High | Third |
| 4 | **BNB Chain** | EVM reuse; large retail/DEX activity; ERC-8004 deployed; BSC TVL ~$5.8B | No (EVM adapter) | chain config only | Low | After Base |
| 5 | **Arbitrum** | EVM reuse; deep DeFi/perps; weak agent narrative | No (EVM adapter) | chain config only | Low | Config-only add-on |
| 6 | **NEAR** | Intents >$30B cumulative (single report); function-call access keys are a native scoped-permission primitive | **Yes** | new adapter | Medium-High | Only if a customer asks |

(Rows 2, 4 and 5 are one EVM adapter. The research agent ranked Base #1, Ethereum #2, Hyperliquid #3, Solana #4, BNB #5, Arbitrum #6, NEAR #7; I move Hyperliquid first because it is the only venue that matches the repo's carry/perp logic, and I collapse Base+Ethereum into one row. That makes six rows for seven ecosystems.)

**Honest answer to "seven ecosystems"**: the seven best-evidenced are Base, Ethereum, Hyperliquid, Solana, BNB Chain, Arbitrum, NEAR (research agent ranking, which I accept), but that is **four engineering efforts** (Hyperliquid, EVM, Solana, NEAR). Bittensor is watch-only (128 subnets; a lab left in April 2026; Substrate tooling and a different use case). Tron is read-only stablecoin monitoring at best (~$93.9B stablecoins per a 2026-10-01 source).

## Scores (0-5, the research agent's judgement; copied from `appendix/raw-ecosystems.md` section 2)

Columns: Demand/dev, Agent relevance, Real activity, Wallet/tx infra, Security tooling and feasibility, Maintenance (5 = cheap), Differentiation/monetisation. The "compatibility with this repo" column was left blank by the agent; my view is that it is low (1) for everything except Hyperliquid (about 3, because its perps/funding concepts match the carry code) since the repo has no on-chain code.

| Ecosystem | Dem | Agent | Activity | Wallet | Sec | Maint | Diff |
|---|---|---|---|---|---|---|---|
| Ethereum | 5 | 4 | 5 | 5 | 5 | 3 | 3 |
| Base | 4 | 5 | 4 | 5 | 4 | 5 | 4 |
| Arbitrum | 3 | 2 | 3 | 4 | 4 | 5 | 2 |
| BNB Chain | 4 | 3 | 4 | 4 | 4 | 5 | 3 |
| Solana | 5 | 4 | 5 | 4 | 3 | 2 | 4 |
| Hyperliquid | 4 | 3 | 5 | 2 | 3 | 3 | 5 |
| NEAR | 2 | 4 | 3 | 3 | 3 | 2 | 3 |
| Bittensor | 2 | 3 | 2 | 2 | 2 | 1 | 2 |
| Tron | 4 | 1 | 4 | 3 | 3 | 2 | 3 |

Scores for Tron, Sui, TON, Polygon, Avalanche, Cosmos and Bitcoin L2s rest on thin or no gathered evidence. Treat all scores as ordering hints, not measurements.

## Not verified

Fees/revenue and active addresses per chain; 30-day DEX volume for most chains; stablecoin supply for Base and Arbitrum; Solana Agent Kit/ElizaOS/Virtuals/ERC-7715 adoption; MetaMask Agent Wallet (one mention); RPC and simulation tooling per chain; L2Beat stages; Hyperliquid API specifics.

## Evidence that would justify prioritising a chain

Three or more design-partner conversations naming that chain; an agent framework in their stack that already signs there; a measurable loss event on that chain involving unconstrained agent permissions.
