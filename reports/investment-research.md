# Seven crypto assets for further research (separate from integrations)

Date 2026-10-28. **This is a research-candidate list, not investment advice, not a ranking of expected returns, and no asset is called undervalued.**

## Data limitation

Live market data could not be retrieved: `api.coingecko.com` and `api.llama.fi` failed DNS under the session network policy, and web search returned no sourced October 2026 prices. Market cap, FDV, supply, unlocks, volume and fees are therefore **NOT FETCHED** for every asset. The only dated data point obtained: a June 2026 snippet reporting ETH down 43.8% and BNB down 30.4% year to date, and ETH around $2.0-2.35K in spring 2026 (secondary source). Nothing was filled from memory. See `appendix/raw-tokens-valuation.md`.

## Candidate list (hypotheses to research)

| Asset | Why research | What would invalidate | Monitor |
|---|---|---|---|
| ETH | Largest smart-contract settlement and stablecoin base; fee burn links network use to token | Fee burn stays small after L2 scaling; stablecoin share migrates | Net issuance, fees, L2 fee capture |
| SOL | High agent/x402 transaction count; large retail and payments activity | Outages, validator concentration, inflation not offset by fee demand | Fees, active addresses, stake concentration |
| BNB | Exchange-linked chain, burn schedule | Regulatory action against its parent ecosystem; burn mechanics changed | Burn rate, regulatory news |
| HYPE | Perp venue with large volume; relevant to funding-carry | Volume share loss, validator/governance concentration, unlock overhang | Open interest, buyback/revenue use, unlock schedule |
| LINK | Oracle/cross-chain infrastructure used by agents | Competing oracles, weak token value accrual | Staking, service revenue |
| TAO | Direct AI-network exposure | Subnet concentration, emissions dilution, a major lab left in Apr 2026 | Emissions, subnet revenue, validator concentration |
| NEAR | Intents volume, AI positioning | Intents volume proves not sticky; token accrual weak | Intents volume, fee share |

## Method to use once data exists (nothing computed here)

1. Network success is not token value: for each asset write down the exact mechanism that moves fees to holders (burn, buyback, staking yield) and verify it in primary docs.
2. Compute fee yield = annualised protocol fees / market cap, and FDV/circulating ratio, from DefiLlama/Token Terminal/CoinGecko; adjust for unlock schedule from Tokenomist.
3. Scenarios must be explicit-assumption arithmetic; otherwise write "insufficient evidence".
4. Re-run this document after allowing the data hosts. Until then there is no basis for a ranking.

## Important separation

Owning these assets is not company revenue. A token's performance and the repo's value are unrelated. Also note the repo's own research found no timing edge on BTC/ETH/SOL; that is a reason to be sceptical of trading-signal claims in general.
