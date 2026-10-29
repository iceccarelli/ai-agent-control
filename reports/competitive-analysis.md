# Competitive analysis

Research date 2026-10-28. **Limits**: the research agent could not fetch vendor docs or pricing pages (`docs.cdp.coinbase.com`, `privy.io`, `turnkey.com` failed DNS under the session network allowlist). Vendor claims below come from search snippets citing those docs or third parties. GitHub star counts come from the GitHub API (via the GitHub MCP search) and are the only independently checkable figures. Nothing was tested hands-on. Full tables and URLs: `appendix/raw-competitors.md`.

## Opportunity directions compared

| | A. General agent governance | B. Crypto-native agent permissions/execution controls | C. AI-powered crypto ops / trading |
|---|---|---|---|
| Problem severity | High, but being absorbed by platforms | High where agents hold funds; documented losses | Real, but users' pain is "make money", which no tool can promise |
| Alternatives | AWS Bedrock AgentCore Policy (Cedar, GA ~Mar 2026), Microsoft Entra Agent ID, Okta, NeMo Guardrails (~7.4k stars), Guardrails AI (~7.6k), Portkey gateway (~13.3k), Lakera (bought by Check Point), Protect AI (bought by Palo Alto) | Coinbase Agentic Wallets (Feb 2026), Privy (owned by Stripe), Turnkey, Fireblocks, Safe modules, Blockaid, Hypernative | Freqtrade (~56k stars), NautilusTrader (~31k), Hummingbot (~20.8k), Jesse (~8.7k), Bybit's own arbitrage tool, Ethena USDe |
| Competitive intensity | Very high; hyperscalers | High on-chain; **possible gap off-chain (CEX API-key agents)** [I] | Very high |
| Willingness to pay | Enterprise budgets, long cycles | Funds-at-risk buyers pay for security; unproven for small teams | Retail churns; no moat |
| Time to MVP from this code | Long (nothing reusable but ideas) | Medium for a CEX/perp sidecar; long for on-chain | Already there, but no edge |
| Security/regulatory burden | Medium | High (custody-adjacent) | High (advice/licensing, depending on jurisdiction) |
| Defensibility | Low | Medium if audit-grade evidence + integrations | Low |
| Fit with existing code | Low | **Medium** (pattern, not code) | Medium, but the repo's own evidence says no edge |

## Feature matrix (from the research; `?` = not verified)

| Product | Policy engine | Spend limits | Multi-chain | Human approval | Audit log | Kill switch | Self-host/OSS | Price |
|---|---|---|---|---|---|---|---|---|
| Coinbase Agentic Wallets / AgentKit | Yes, session & tx caps (off-chain) | Yes | EVM + Solana | ? | ? | ? | AgentKit OSS (~1.3k stars) | ? |
| Privy (Stripe) | Value caps, allowlists, m-of-n | Yes | EVM + Solana | Yes (quorum) | ? | ? | No | Free tier reported; "$299/mo" single-source |
| Turnkey | Deny-overrides-allow | Yes | Multi | Yes | ? | ? | No | Sources conflict |
| AWS AgentCore Policy | Cedar | n/a | n/a | ? | ? | ? | No | AWS pricing |
| This repo | Python gates, Bybit only | Notional caps | Bybit only | Human-only kill clear | Ledgers, hash manifests | Yes | Source available, **unlicensed** | n/a |

All three wallet vendors say root/admin users bypass their policy engines (competitor appendix). That is the structural weakness an independent governance layer could address.

## Categories

1. **Commodity**: per-transaction value caps, address allowlists, m-of-n approval for on-chain wallets (Coinbase, Privy, Turnkey all ship them).
2. **Not adequately solved (hypotheses)**: governance for agents that hold *exchange API keys* (Bybit/Binance/Hyperliquid), cross-venue budgets, independent audit evidence of what an agent did, human-only kill-switch semantics. I found no open-source self-hosted product for CEX API-key bots; absence of evidence, not proof.
3. **Gaps this code could realistically fill**: a Bybit/perp-venue sidecar with fail-closed gates, reconciliation, and a promotion-style checklist. Because the code is Bybit-specific, it covers one venue.
4. **Differentiation needing validation**: "model cannot reach the order path" proven by static analysis; evidence-first promotion gates; reconciliation of ledger to venue.

## Incidents (all secondary sources)

Grok/Bankr 2026-05-04: prompt injection plus a membership NFT granting elevated permissions drained ~$150-200K (sources disagree; no primary post-mortem). AIXBT March 2025: ~$105K via compromised dashboard, not model manipulation. ElizaOS memory-poisoning is academic (arXiv 2503.16248), no confirmed theft. Freysa was a paid injection game, not a theft.

## Could products make this unnecessary?

Yes. Coinbase, Privy and Turnkey already cover on-chain agent wallet policies; AWS covers generic tool-call policy. The only defensible space is what they do *not* cover, and that is unvalidated.
