# Competitive landscape: crypto-native AI-agent transaction security / policy / spend controls and agent governance

Accessed: 2026-10-28 (all URLs below accessed that date).

## Method and limits (read first)
- WebFetch could not resolve docs.cdp.coinbase.com, www.privy.io or www.turnkey.com (sandbox network allowlist). I therefore could NOT read vendor docs or pricing pages directly. Vendor claims below come from WebSearch result snippets, which cite vendor docs or third parties.
- `gh api` was blocked for non-attached repos. GitHub stars come from the GitHub MCP `search_repositories` (GitHub's own API, primary), read 2026-10-28.
- Labels: [GH-API] = verified from GitHub API; [VENDOR] = vendor's own marketing or docs, not independently tested; [3P] = third-party article or blog (often affiliate or competitor, lower trust); [ACAD] = academic paper.
- I did not run or test any product. Nothing here is "independently verified" in the sense of hands-on testing. The only independently checkable numbers are the GitHub stats.

## 1. Agent wallet / key-management / policy infrastructure

| Player | What exists (as found) | Evidence and adoption | Pricing found | Source |
|---|---|---|---|---|
| Coinbase CDP Server Wallets v2 + policies | Declarative policy rules checked at each signing, e.g. "transfer <= 2 ETH to allowlisted address" [VENDOR] | Coinbase dev blog on policy engine | Not found | https://www.coinbase.com/ru/developer-platform/discover/launches/policy-engine ; https://www.coinbase.com/llms.txt |
| Coinbase Agentic Wallets / Agentic Wallet CLI | Launched ~2026-02-11 per several news outlets [3P]; per-session caps and per-transaction limits, keys held in Coinbase infra [VENDOR docs + 3P]. One outlet said finer rules were "coming soon", so completeness at launch is unclear. | Runs on x402 | Not found | https://docs.cdp.coinbase.com/agentic-wallet/cli/welcome ; https://forklog.com/en/news/coinbase-unveils-wallet-infrastructure-for-ai-agents ; https://eco.com/support/en/articles/14845485-coinbase-agentic-wallets-explained |
| Coinbase AgentKit (OSS) | Framework; uses CDP Wallet API by default; spend permissions skill [VENDOR] | coinbase/agentkit: 1,341 stars, 890 forks, 460 open issues, pushed 2026-10-28 [GH-API]. Predecessor cdp-agentkit-nodejs archived (60 stars). | Free SDK; CDP usage pricing not found | https://github.com/coinbase/agentkit ; https://docs.cdp.coinbase.com/agent-kit/support/faq |
| x402 (HTTP 402 payments) | Open protocol, now under x402 Foundation / Linux Foundation; members reportedly incl. Google, Stripe, Visa, Mastercard, AWS, Cloudflare [3P] | x402-foundation/x402: 6,765 stars, 2,244 forks, 749 open issues [GH-API]. Volume claims conflict: "165M txns / ~$50M cumulative / 69k agents" (late Apr 2026) vs "~75M txns / ~$24M in 30 days to July" [3P, unaudited]. Treat as unverified. | Protocol free | https://github.com/x402-foundation/x402 ; https://blockeden.xyz/blog/2026/04/01/x402-protocol-http-402-ai-agent-commerce-stablecoin-payments/ ; https://www.paymentsjournal.com/?p=525316 |
| Google AP2 | Authorization layer with signed "mandates"; x402 integrated as settlement option [3P] | google-agentic-commerce/AP2: 3,253 stars; a2a-x402: 569 stars [GH-API] | Free spec | https://github.com/google-agentic-commerce/AP2 ; https://www.coinsdo.com/en/blog/x402-ap2-agent-payment-protocols-explained |
| Visa Trusted Agent Protocol (Oct 2025), Mastercard Agent Pay | Card-rail agent identity and tokens, consumer purchases, not on-chain [3P] | No adoption numbers found | n/a | https://blindpay.com/resources/more/agent-payment-protocols-compared.md |
| Stripe (owns Privy) | Machine payments preview using x402 on Base (Feb 2026) [3P]. Stripe agreed to buy Privy June 2025, terms undisclosed [3P]. Privy said to power wallet rails for Amazon Bedrock AgentCore Payments [3P, single source]. | Privy raised $15M (Ribbit, Mar 2025) before acquisition; "110M+ wallets" is a Stripe claim [VENDOR/3P] | see Privy | https://siliconangle.com/2025/06/11/stripe-acquires-crypto-wallet-infrastructure-provider-privy/ ; https://thedefiant.io/amazon-builds-ai-agent-payments-with-coinbase-and-stripe |
| Privy server/agent wallets | Policy engine: per-tx and windowed value caps, token/chain restrictions, recipient allowlists, function-level rules, m-of-n quorum approvals; EVM and SVM [VENDOR docs via search]. Root users bypass the policy engine. | See above | Third parties conflict: free tier 50K signatures/mo (agree); paid "from $299/mo" in one 3P source only; not confirmed on Privy's page | https://docs.privy.io/wallets/overview/solutions/agent-wallets ; https://privy.io/blog/turning-wallets-programmable-with-privy-policy-engine ; https://docs.sei.io/ai/agentic-wallets.md |
| Turnkey | Enclave-evaluated policy engine; DENY overrides ALLOW; root users bypass; Agentic Wallets/Payments product; example customer Spectral Labs (1:2 multisig agent wallet) [VENDOR] | Sacra [3P]: $12.5M strategic investment May 2026, total >$65M; customers incl. Bridge, Magic Eden, World, Alchemy, Polymarket (unverified); Turnkey blog "100M policies" claim [VENDOR] | Conflicting: free plan + enterprise (Turnkey FAQ); ~$0.0015/sig at enterprise (Sacra); "enterprise-only" (Crossmint, a competitor) | https://docs.turnkey.com/solutions/company-wallets/agentic-wallets ; https://sacra.com/c/turnkey ; https://www.turnkey.com/blog/100m-policies--turnkey-protecting-transactions |
| Fireblocks (+Dynamic) | Fireblocks acquired Dynamic announced 2025-10-23, ~ $90M reported, Dynamic ">50M accounts" [3P] | Fireblocks has an institutional policy engine historically; I did not retrieve agent-specific docs | Not found | https://cointelegraph.com/news/fireblocks-acquires-dynamic-wallet-institution ; https://www.theblock.co/post/375939/fireblocks-crypto-authentication-dynamic-web3 |
| Crossmint | Non-custodial agent wallets, spending caps, approved counterparties, validity windows [VENDOR/3P]. Raised $23.6M led by Ribbit (banner on its site). | Customers not verified | Not found | https://crossmint.com/products/wallets ; https://docs.crossmint.com/wallets/ |
| Skyfire | KYAPay identity+payment tokens (signed JWT), funding by card/ACH/USDC [VENDOR/3P] | Press releases only | Not found | https://www.skyfire.xyz/product ; https://www.businesswire.com/news/home/20241024532897/en/AI-Agents-Race-to-Join-Skyfire-Payments-Network |
| Safe + Zodiac Roles Modifier | On-chain role-based permissions (scoped by contract/function/params) for Safe; Roles v2 audited (G0, Omniscia). Used by ENS endowment/karpatkey for DAO treasuries. | zodiac-guard-scope: 43 stars [GH-API]. I found NO source showing Zodiac Roles used for AI agents (inference only: it fits). | Open source | https://github.com/gnosisguild/zodiac-modifier-roles ; https://docs.ens.domains/dao/proposals/5.12 |
| ERC-7715 / MetaMask Smart Accounts Kit | Wallet-granted scoped, time-limited permissions ("Advanced Permissions") [VENDOR docs] | Rhinestone session module only a passing mention. ZeroDev, Biconomy: NOT verified (no results). | Free/open | https://docs.metamask.io/smart-accounts-kit/concepts/advanced-permissions/ ; https://eco.com/support/en/articles/11953354-what-is-erc-7715-a-complete-guide-to-wallet-permissions |
| Solana Agent Kit (SendAI) | Toolkit, no policy engine found | sendaifun/solana-agent-kit: 1,726 stars, 889 forks, 78 issues [GH-API]. Squads: not researched. | Free | https://github.com/sendaifun/solana-agent-kit |
| ElizaOS | Agent framework, crypto-heavy | elizaOS/eliza: 19,691 stars, 5,835 forks, 712 issues [GH-API] | Free | https://github.com/elizaOS/eliza |
| OSS/small policy layers discovered | PolicyLayer (vendor of agent policy layer; its blogs argue enforcement must be outside the agent), WAIaaS (OSS, "21 policy types", default-deny, human approval above threshold) | Tiny/unknown adoption, self-described [3P/VENDOR] | unknown | https://policylayer.com/blog/why-agents-shouldnt-know-spending-limits ; https://dev.to/walletguy/complete-ai-agent-lockdown-21-policy-types-for-maximum-security-4a6f |

Not verified at all (no search results retrieved): Lit Protocol agent wallets, Squads, MoonPay agents, Biconomy, ZeroDev specifics, Sendai policy features, Mastercard/Visa adoption numbers.

## 2. Transaction simulation / screening
- Blockaid: $50M Series B (Ribbit lead, with GV, Variant, Cyberstarts), Feb 2025, ~$83M total [3P: SecurityWeek, https://www.securityweek.com/blockaid-raises-50-million-to-secure-blockchain-applications/amp/]. Scans, simulates and validates transactions.
- Hypernative: $40M Series B June 2025 (Ten Eleven, Ballistic) [3P: https://www.theblock.co/post/357627/crypto-security-startup-hypernative-series-b]; claims ~200 customers and >$100B protected [VENDOR via press, unverified]; "Guardian" simulates transactions before approval.
- No source found linking either to AI-agent products specifically. Forta, Tenderly, Chainalysis, TRM, Elliptic, Cyvers: NOT researched/verified (no results). Pricing for all: not found.

## 3. General agent governance / guardrails
- AWS Bedrock AgentCore Policy (Cedar rules on tool calls at Gateway): GA reported 2026-03 (AWS What's New, https://aws.amazon.com/de/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available/); June 2026 update adds Bedrock Guardrails support (https://dev.classmethod.jp/en/articles/20260617-amazon-bedrock-agentcore-policy-guardrails/); one write-up notes it did not support IAM-authenticated gateways as of 2026-03-12 [3P]. Pricing not retrieved.
- Microsoft Entra Agent ID: described as GA; extending Entra security features to agents reportedly needs Microsoft Agent 365 licence [3P: https://innfactory.de/en/cloud/azure/products/entra-agent-id/]. Not verified against Microsoft Learn.
- Okta / Auth0 for AI Agents: exists (Cross App Access based) [3P]; pricing/GA not found.
- NVIDIA NeMo Guardrails: 7,384 stars [GH-API, https://github.com/NVIDIA-NeMo/Guardrails]. Guardrails AI: 7,566 stars [GH-API]. Portkey Gateway: 13,287 stars, markets MCP gateway + 50+ guardrails [GH-API]. Invariant Labs OSS: 475 stars (repo updated 2026-10-24) [GH-API]; I could not confirm the reported Snyk acquisition.
- Acquisitions: Check Point bought Lakera (~$300M reported, closed Oct 2025) [3P]; Palo Alto closed Protect AI 2025-07-22 [3P]. Credo AI: no data found.
- Relevance: all are text/tool-call guardrails, none are crypto-aware (no spend velocity, chain, address semantics) as far as found. This is an inference from missing evidence.

## 4. Trading-bot frameworks (to judge the repo as a bot product)
Stars/forks [GH-API, 2026-10-28]:
- Freqtrade 56,071 stars (11,590 forks, 27 open issues, active) https://github.com/freqtrade/freqtrade
- NautilusTrader 30,981 (Rust-native engine) https://github.com/nautechsystems/nautilus_trader
- Hummingbot 20,755 (5,058 forks; topics include hyperliquid, ai-agents; hummingbot-api 152; gateway 262) https://github.com/hummingbot/hummingbot
- Jesse 8,719 https://github.com/jesse-ai/jesse
- Commercial [3P, conflicting, affiliate-heavy]: 3Commas free Starter, ~$22/$37/$75 per month by one source (others differ); Cryptohopper free "Pioneer" tier, paid plans ~$29.99+ (conflicting); Pionex has no subscription, fee ~0.05% per trade. https://hackernoon.com/top-10-best-ai-bot-trading-in-2026-features-and-pricing
- Bybit's own tool: "Arbitrage Trading" in the help center places paired spot/perp (USDT/USDC) orders; Bybit states it does not manage or close positions [VENDOR] https://www.bybit.com/km-KH/help-center/article/Arbitrage-Trading . Unofficial Bybit funding bots on GitHub/Whop exist (not evaluated).
- Ethena USDe is the scaled delta-neutral carry product. Supply figures conflict ($5.92B mid-Mar 2026 vs ~$4.61B on 2026-07-31) and yield compressed to ~high single digits or lower (BTC/ETH funding ~1.5-1.7% annualised in one report) [3P, unverified; check Ethena transparency dashboard / DeFiLlama]. Implication: carry edge is thin and crowded; a research bot would compete with free mature frameworks and with exchange-native tools.
- Not researched: backtest/perf claims of any bot, Hummingbot funding-arb scripts, Pionex/3Commas Bybit support.

## 5. Incidents and research
- Grok/Bankr (2026-05-04): attacker sent a Bankr membership NFT to Grok's linked wallet, then used Morse-code-encoded prompt injection; losses ~$150K-$200K in DRB tokens (sources disagree); ~80% reportedly returned (single source, Giskard). Commentary frames it as an over-broad permissions problem: nothing capped. No primary Bankr/xAI post-mortem found [3P]. https://cointelegraph.com/news/grok-bankr-wallet-free-nft-prompt-injection ; https://giskard.ai/knowledge/how-grok-got-prompt-injected-an-x-user-drained-150-000-from-an-ai-wallet ; https://oecd.ai/en/incidents/2026-05-04-4a73
- ElizaOS memory injection [ACAD]: Princeton/Sentient "AI Agents in Cryptoland" (arXiv 2503.16248; later retitled "Real AI Agents with Fake Memories"); memory poisoning made agents sign unauthorized transfers in a research setting; prompt-based defenses only partial. No confirmed real-world theft from it. https://arxiv.org/html/2503.16248v1
- AIXBT (Mar 2025): ~55.5 ETH (~$105K) reportedly drained via compromised dashboard/command queue, not model manipulation; not confirmed to a primary source [3P].
- Freysa (Nov 2024): a paid prompt-injection game (pool paid out), not an involuntary theft [3P].
- CrowdStrike claim of prompt-injection-linked crypto theft at 90+ orgs: unverified.
- Developer complaints: I found no Reddit/HN threads. Only observable signals: AgentKit has 460 open issues, x402 749 open issues [GH-API] (high backlog, not necessarily complaints); both Privy and Turnkey policy engines are bypassed by root users (a footgun documented by the vendors); a PolicyLayer-sourced argument that in-agent limits fail. Treat the last as marketing.

## 6. Feature comparison matrix
(Y = documented by vendor; ? = not verified; based on vendor docs via search, not tested)

| Product | Policy engine | Spend limits | Multi-chain | Human approval | Audit log | Kill switch | Self-host / OSS | Pricing |
|---|---|---|---|---|---|---|---|---|
| Coinbase CDP wallets / Agentic Wallets | Y | Y (session, per-tx) | EVM+Solana (? exact) | ? | ? | ? | No (SDK OSS, custody hosted) | not found |
| Privy | Y (granular) | Y | EVM, SVM | Y (quorum) | ? | ? (deny policy) | No | free 50K sig/mo; paid unconfirmed |
| Turnkey | Y (enclave, DENY>ALLOW) | Y | multi (? list) | Y (multisig/consensus) | ? | Y-ish (DENY circuit breaker) | No | conflicting |
| Crossmint | Y (caps, counterparties, windows) | Y | multi (?) | ? | ? | ? | No | not found |
| Fireblocks/Dynamic | Y (historic institutional) | ? | multi | ? | ? | ? | No | not found |
| Safe + Zodiac Roles | Y (on-chain) | partial (allowance modules) | EVM only | Y (multisig) | on-chain | Y (disable module) | Y | free |
| ERC-7715 / session keys | Y (scoped grants) | Y | EVM | per grant | on-chain | revoke | Y | free |
| Skyfire | Y (token limits) | Y | USDC/Base | ? | ? | ? | No | not found |
| Solana Agent Kit | N found | N | Solana | N | N | N | Y | free |
| AgentCore Policy / Entra Agent ID | Y (non-crypto) | N crypto-native | n/a | ? | Y | ? | No | not retrieved |
| NeMo / Guardrails AI / Portkey | content guardrails | N | n/a | ? | Portkey logs | ? | Y | free OSS; Portkey hosted unknown |
| Blockaid / Hypernative | simulation/risk | N | multi | N | ? | ? | No | not found |

## 7. Gaps in the market (inference, not verified)
Strongest competitors for the "policy at signing" function are well-funded custodial/enclave vendors (Coinbase, Privy/Stripe, Turnkey) whose policies run server-side where the agent cannot edit them. Open-source, self-hosted, exchange-API (CEX, e.g. Bybit) spend/risk controls with kill switch and audit log did not appear in any vendor result; all found products target on-chain wallets. That is an absence-of-evidence observation only.
