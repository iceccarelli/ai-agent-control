# Tokens research shortlist and startup valuation benchmarks

Retrieval date: 2026-10-28. Research candidates only, not buy recommendations, no return promises.

## Data-access status (read first)

- Part 1 live token data was NOT retrievable. WebFetch failed with DNS/network-allowlist errors for `api.coingecko.com` and `api.llama.fi`. CoinMarketCap, Token Terminal and Tokenomist were not tried, because the tool said to stop after a second host failed. These hosts must be allowed in the session network settings.
- WebSearch returned no sourced October 2026 prices for SOL/BNB or any of the other candidates. The only dated snapshots were stale (see below).
- So the Part 1 tables contain NO market cap, FDV, supply, unlock, volume or fee figures. Nothing was filled from memory. The tables are templates with "NOT FETCHED" cells.
- Part 2 figures come from WebSearch result summaries, which are secondary and were not verified against primary pages. Primary-source verification is still needed before any figure is relied on.

### Stale or unreliable crypto data points seen (do not use as current)
- A June 8, 2026 snapshot via search: ETH -43.8% YTD, BNB -30.4% YTD (cointelegraph.com/markets/if-2026-brings-alteason-will-eth-bnb-xrp-sol-and-doge-outperform; snippet only). It is useful only to say the 2026 market was weak by mid-year.
- Spring 2026: ETH about $2,353 with about $283.8B market cap (crypto.news ETH price prediction page). Also a March 30 article with ETH $2,049 and a $247.35B market cap. Both stale.
- Undated price widgets (SOL $90.44, BNB $616.68) are unverified and should be ignored.

## Part 1. Token research shortlist

### 1A. Data collection template (all cells NOT FETCHED as of 2026-10-28)

Intended sources per asset: CoinGecko or CMC (cap, FDV, supply, volume), DefiLlama (fees, revenue, DEX volume, TVL, stablecoins), Token Terminal (revenue, P/F), Tokenomist (unlocks), project docs (burn/buyback mechanics), Electric Capital or GitHub (developer activity).

| Asset | Mkt cap | FDV | Circ / max supply | Unlocks / emissions | 24h vol / liquidity | Fees / revenue | Accrual to token | Dev activity |
|---|---|---|---|---|---|---|---|---|
| ETH | NOT FETCHED | NOT FETCHED | NOT FETCHED | NOT FETCHED | NOT FETCHED | NOT FETCHED | NOT FETCHED | NOT FETCHED |
| SOL, BNB, NEAR, TAO, ARB | same | same | same | same | same | same | same | same |
| HYPE, SUI, AERO, VIRTUAL, LINK, TON, AVAX | same | same | same | same | same | same | same | same |

BASE has no token as far as this research can establish, but that was not re-verified today. Exposure to Base is therefore indirect, via ETH (L2 fees pay for L1 blob space) or via Base-native tokens such as AERO and VIRTUAL.

### 1B. Qualitative structural facts (analyst reasoning, NOT fetched today, must be verified)

These are conceptual statements about mechanism design. Every one needs confirmation against project docs before use.

- Network success is not the same as token value accrual. Activity can grow while the token captures little, for example when fees are paid in stablecoins or burned fees are offset by inflation. Check separately: (a) usage and fees, (b) what share reaches token holders via burn, buyback or staking yield, (c) net supply change after emissions and unlocks.
- ETH: fees are partly burned (EIP-1559), and issuance goes to stakers. Net supply change depends on L1 fee levels. L2s compress L1 fees, so check blob fees and net issuance.
- SOL: fees are partially burned, and inflation funds staking rewards. Check net issuance against revenue.
- BNB: a periodic burn mechanism exists (auto-burn). Concentration and centralization are risks tied to one exchange ecosystem. Regulatory exposure is linked to Binance.
- NEAR, AVAX, TON, SUI: L1 tokens with fee burns or staking. Check unlock schedules for SUI and the foundation share for TON.
- TAO: emissions-driven subnet economy. Check how much value comes from external demand rather than emissions.
- ARB: governance token. Whether protocol revenue accrues to ARB holders is a design and governance question. Check it before assuming any accrual.
- HYPE: reported to use fee-funded buybacks via the Assistance Fund. Verify the current mechanism and the team unlock schedule.
- AERO: ve-style fee and bribe distribution to lockers. Check the emissions schedule.
- VIRTUAL: agent-launchpad token. Heavily tied to AI-agent sentiment.
- LINK: oracle network. Check whether staking and fee mechanisms route real revenue to the token.

### 1C. Shortlist of 7 for further research (selection logic, not a ranking, no price view)

Selection criteria: (1) a plausible, checkable fee-to-token link, (2) relevance to the business thesis, i.e. agents, payments and wallets, (3) diversity of risk types. The data to confirm this selection was not retrievable, so treat it as a hypothesis list.

1. **ETH**
   - Why: base settlement layer. Largest asset in the set with the deepest stack of wallet and agent infrastructure (Privy, Turnkey, Safe all target EVM).
   - Invalidates if: L2 activity keeps fees near zero, net issuance stays positive, and ETH loses market share of stablecoin and RWA settlement.
   - Monitor: L1 fees and burn, net issuance, blob fees, staking ratio, stablecoin supply on L1 versus L2, ETF flows.
2. **SOL**
   - Why: high activity, payments and stablecoin focus, and agents (Skyfire-style payments) can run on it.
   - Invalidates if: fee revenue stays small relative to inflation-funded staking rewards, or major outages recur.
   - Monitor: real economic value (fees plus MEV tips) versus issuance, validator concentration, outage history, unlock-free supply status.
3. **BNB**
   - Why: a clearly defined burn and fee mechanism, and large exchange-linked demand.
   - Invalidates if: regulatory action against the issuing exchange, or burn slows as activity falls.
   - Monitor: quarterly burn amounts, BNB Chain fees, regulatory dockets, exchange concentration.
4. **HYPE**
   - Why: a candidate where protocol revenue is reported to flow to the token via buybacks, which is the cleanest accrual case to test.
   - Invalidates if: the buyback share is lower than reported, team unlocks overwhelm buybacks, or perp volume migrates.
   - Monitor: buyback versus unlock value, open interest, revenue, venue competition, security and oracle incidents.
5. **LINK**
   - Why: cross-chain infrastructure, with possible demand from tokenization and agent data feeds.
   - Invalidates if: staking and fee mechanics fail to route revenue to the token, or competing oracles take share.
   - Monitor: CCIP and oracle revenue, staking participation, reserve outflows.
6. **TAO**
   - Why: the main AI-native network token, thematically adjacent to AI agents.
   - Invalidates if: subnet demand is mainly emission-driven and external paying usage does not appear.
   - Monitor: external revenue per subnet versus emissions, subnet concentration, halving effects, exchange liquidity.
7. **NEAR**
   - Why: positions itself around agents and chain abstraction, and the thesis can be checked on fees and active users.
   - Invalidates if: usage is incentive-driven and falls when incentives stop.
   - Monitor: fee revenue, daily active accounts, inflation versus burn, developer counts.

Considered but not shortlisted (reason is a data gap, not a view): ARB (accrual unclear), SUI (unlock overhang to verify), AERO and VIRTUAL (Base-ecosystem beta with high sentiment risk), TON (concentration and platform dependence), AVAX.

### 1D. Valuation methodology (to use only after data is fetched)

No asset is labeled undervalued or overvalued here. If the data later becomes available, use:

- P/F (FDV or market cap divided by annualized fees) and P/R (divided by annualized revenue to token holders).
- Fee yield to holders = (annualized burned value + buybacks + distributed fees) / market cap.
- Net dilution = annual emissions and unlocks as a percent of circulating supply.

Illustrative arithmetic with HYPOTHETICAL inputs only (these are not real data):
- If annualized fees = $100M and market cap = $2B, then P/F = 20x. If 50% of fees reach holders, holder yield = $50M / $2B = 2.5%. If annual emissions are 5% of circulating supply, net dilution (5%) exceeds yield (2.5%), so the net holder return from fees alone is negative before price effects.

Scenario frames (qualitative):
- Bull: usage rises, the accrual share rises, and emissions are absorbed by real demand.
- Base: usage grows slowly, accrual is stable, and the dilution of unlocks roughly offsets fee yield.
- Bear: usage falls or migrates, accrual is cut or regulated, and unlock supply meets thin liquidity. Add security events and concentration shocks.

### 1E. Cross-asset risk themes
- Regulatory: securities classification, exchange-linked tokens (BNB), enforcement and MiCA treatment.
- Concentration: foundation and insider holdings, validator and exchange concentration.
- Security: bridge and smart-contract exploits, outages, key-management failures.
- Liquidity: smaller tokens can show thin books, so apparent volume may overstate exit liquidity.

## Part 2. Valuation benchmarks (business side)

All figures come from WebSearch result summaries on 2026-10-28 and were not checked against primary pages unless stated. Treat as leads.

### 2A. Comparable deals and rounds

| Company | Event | Reported figure | Status of evidence |
|---|---|---|---|
| Privy | Acquired by Stripe, announced 2025-06-11 | Price undisclosed. Prior valuation reportedly $230M (Bloomberg, March 2025). Last round $15M led by Ribbit. CB Insights total raised $41.3M | Press report on the valuation, not a company disclosure. Closing date conflicting in sources |
| Lakera | Acquired by Check Point, announced 2025-09-16, closed 2025-10-22 | About $300M estimated (Globes, others). One source cites $201.8M | Not officially disclosed, sources conflict |
| Protect AI | Acquired by Palo Alto Networks, announced 2025-04-28, closed 2025-07-22 | Estimated $650-700M (Globes). Another report "$500M+" | Not officially disclosed |
| Invariant Labs | Acquired by Snyk, 2025-06-24 | Undisclosed. ETH Zurich spin-off from 2024; became Snyk Labs | Price unknown, so no multiple |
| Turnkey | Series B, 2025-06-09, $30M led by Bain Capital Crypto | Valuation undisclosed. Series A $15M (April 2024). Sacra: total over $65M plus a $12.5M strategic investment reported 2026-05-14 | Valuation not found. A date conflict exists in one aggregator |
| Blockaid | Series B, 2025-02-18, $50M led by Ribbit, with GV | Total funding $83M (CB Insights). Valuation not found | Round confirmed, valuation unknown |
| Hypernative | Series B, 2025-06-10, $40M (BusinessWire) | Valuation not found | Round confirmed |
| Skyfire | Seed $8.5M (2024); other aggregators show different amounts | The "$9.5M with a16z" claim was NOT confirmed. Windows Central snippet mentions $13M, undated | Conflicting and unclear. Do not cite a figure without a primary source |
| Stripe (context) | Valuation $159B, TechCrunch 2026-02-24 | Context for buyer capacity | Reported |
| Fireblocks, Safe, Coinbase wallet products | NOT researched in this run | n/a | Gap |

Takeaways: AI-security acquirers paid several hundred million dollars for companies roughly 1-3 years old (Protect AI, Lakera). Those are strategic deals with undisclosed terms, and the selection is biased toward the winners. Wallet infrastructure rounds were $15-50M with valuations mostly not public.

### 2B. ARR and revenue multiples (secondary sources)

- Public software: Meritech Software Pulse (April 9, 2026 issue) reports a median of about 3.2x implied ARR, and 11.7x for the top 10. Later issues (2026-07-02, 07-17, 07-31) exist at meritech.substack.com but their headline figures were not captured.
- Private SaaS: ranges vary by source. FE International gives about 2-7x ARR. Fungies gives a median of about 4.5x. Iconic, citing Aventis, gives medians of 2.9x (2024), 3.8x (2025) and about 3.1x (March 2026).
- AI-native: Acquiry says 10-20x ARR for growth above 50%. Fungies cites a median of 12.5x. Both are vendor or advisory blogs, so the evidence is thin.
- L40, citing PitchBook: 2026 disclosed tech deals excluding 20x+ outliers averaged 6.2x EV/revenue, and sub-$1B deals 3.9x.
- Cybersecurity: FE International cites Finro (mid-2025): public median 7.8x revenue, private startups 15.2x, M&A median 16.3x. Treat as a rough indicator only.
- Seed valuations: PitchBook-NVCA (Q1 2026) median US seed pre-money about $18.4M (AI $18.7M, non-AI $18.0M). Carta round benchmarking page (Q1 2026) median seed post-money $21.6M. Other blogs cite Carta at about $24M post-money (Q4 2025, a secondary claim). Definitions differ (pre vs post, PitchBook vs Carta), so they are not directly comparable. Carta's own 2026 report was not opened.

### 2C. Pre-revenue open-source code and acqui-hires

- Public data is thin. No source found addressed acquisitions of open-source projects specifically. For a pre-revenue project, any valuation rests on the team, the adoption (stars, downloads, users), and what a buyer would have to pay to rebuild it.
- Acqui-hire per engineer: secondary blogs cite $1-3M for standard engineers (range $0.25-5M), $500K-2M in another, $200K in an a16z example, and $5-10M+ for senior AI researchers. The headline number is typically split with investors and creditors and retention pools, and is not what engineers receive. Sources are consultancy blogs, so use as an anecdotal range only.
- Practical use: a one-to-three-person team with unproven code and no revenue sits at the low end of acqui-hire reasoning, and buyers price it mostly as recruiting-cost substitution.

### 2D. Reproduction-cost methods

- COCOMO II (calibrated on 161 projects per the sources) estimates effort in person-months from size in KLOC and cost drivers. It excludes test drivers and generated code in size counting. Lines of code alone are a weak predictor.
- Method: cost = estimated person-months x fully loaded monthly cost. Divide by KLOC for a cost per line.
- Fully loaded engineer cost: NOT FOUND in this run. No reputable 2026 US/EU figure was retrieved, so none is given. Obtain from a current compensation survey or a government labor statistics source before computing.
- Illustrative formula only (hypothetical inputs): organic-mode COCOMO effort = 2.4 x KLOC^1.05 person-months. For 20 KLOC that gives about 2.4 x 23.2 = about 56 person-months. At a hypothetical loaded cost of X per month, reproduction cost = 56 x X. AI-assisted development may cut actual effort well below the model, which was calibrated on pre-AI projects.
- Caution: reproduction cost sets a floor for what a buyer would pay for code alone. It is not a market value. Value depends on adoption, revenue and strategic fit.

## Where evidence is thin
- All live token data (blocked network hosts).
- Valuations for Turnkey, Blockaid, Hypernative and Skyfire; and Fireblocks, Safe and Coinbase wallet products were not researched.
- Primary-source verification of all deal figures.
- Fully loaded engineer cost; open-source asset valuations.

## Sources (from search results)
- https://thepaypers.com/fintech/news/stripe-to-acquire-privy-to-further-scale-web3-capabilities
- https://www.theblock.co/post/357803/payment-giant-stripe-to-buy-crypto-wallet-firm-privy-report
- https://www.cbinsights.com/company/privy-1/financials
- https://techcrunch.com/2026/02/24/stripes-valuation-soars-74-to-159-billion/
- https://en.globes.co.il/en/article-1001521951 (Lakera)
- https://en.globes.co.il/en/article-1001507476 (Protect AI)
- https://nand-research.com/research-note-check-point-to-acquire-lakera-for-end-to-end-ai-security/
- https://siliconangle.com/2025/06/24/snyk-acquires-invariant-labs-expand-ai-agent-security-capabilities/
- https://www.theblock.co/post/357445/former-coinbase-employees-raise-30-million-series-b-crypto-infrastructure-startup-turnkey
- https://sacra.com/c/turnkey
- https://www.cbinsights.com/company/blockaid/financials
- https://www.businesswire.com/news/home/20250610162307/en (Hypernative)
- https://seedtable.com/companies/skyfire-systems/funding-rounds/seed-2024-08
- https://meritech.substack.com/p/meritech-software-pulse-31-july-2026
- https://www.feinternational.com/blog/saas-valuation-multiples
- https://www.feinternational.com/blog/cybersecurity-business-valuation
- https://iconic.co/blog/saas-multiples/
- https://www.l40.com/insights/saas-market-update
- https://carta.com/sg/en/data/round-benchmarking-tool/
- https://waveup.com/blog/seed-round-benchmarks/
- https://waveup.com/blog/acquihire/
- https://www.cbinsights.com/research/acquihire
- https://www.datacamp.com/tutorial/cocomo-model
