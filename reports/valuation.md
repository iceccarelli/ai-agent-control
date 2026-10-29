# Valuation

Date 2026-10-28. Valuation is judgement under thin evidence. Benchmarks come from secondary summaries obtained by a research agent (details and URLs: `appendix/raw-tokens-valuation.md`); I did not open the primary pages for them.

## A. Current repository

### Facts used
- Production Python in `bot/*.py`: 40,269 lines [V]. All tracked Python including tests and tools: 172,505 lines [V]. C++: 498 lines [V].
- Git history: 145 commits over 6 days; 94 by Claude, 51 by the owner [V].
- Users: none evidenced. Revenue: none. Licence: none. Edge: none demonstrated (repo's own findings).

### Method 1: replacement cost (upper bound, not a price)
Assumption (unsourced rule of thumb): hand-written, tested, delivered code at 10-25 lines per developer-day.
- 40,269 / 25 = 1,611 days; 40,269 / 10 = 4,027 days.
- At ~230 working days a year: ~7 to ~17.5 person-years.
- At an assumed fully-loaded $150k per person-year: ~$1.0M to ~$2.6M nominal.
This is what it would cost to reproduce *by hand*. It is not what anyone would pay: the repo was produced in days with AI assistance, so the real replacement cost for a competent AI-assisted team is a small fraction of that, and much of the code (10 frozen failed signals, research harness for one strategy family) has no buyer use.

### Method 2: comparable transactions
No comparable sales of unlicensed single-venue research bots were found. Acquisitions found in AI/crypto security (Protect AI ~$650-700M est., Lakera ~$300M est., Privy undisclosed) were of companies with products, customers and teams. **They are not comparables for this repository.**

### Method 3: income
Revenue attributable today: $0. Income approach gives $0.

### Conclusion
**Code-only market value today: roughly $0-$30k** (my judgement), possibly higher to a buyer who wants the methodology (null-controlled edge measurement, frozen-signal registry, promotion-gate pattern) as a teaching or consulting asset. The nominal $1-2.6M reproduction figure is an upper bound that should not be quoted as value. Drivers that would raise it: a licence and clean IP chain, a hermetic green CI, documented rotation of credentials, a venue-neutral core, any paying user.

IP caution: 94 of 145 commits are AI-authored; ownership and licensing of AI-generated output varies by jurisdiction. Get counsel before a sale.

## B. Potential business (scenarios, not predictions)

Benchmarks (secondary): public software median ~3.2x implied ARR (Meritech, April 2026); private SaaS ~2.9-4.5x depending on source; cybersecurity M&A ~16x revenue (Finro/FE International, mid-2025; scale companies only); seed medians ~$18.4M pre-money (PitchBook-NVCA Q1 2026) and ~$21.6M post-money (Carta Q1 2026). Seed medians reflect team, market and round dynamics, not code.

| Stage | Evidence required | Downside | Base | Upside |
|---|---|---|---|---|
| Early OSS project | licence, stars, issues | $0 | $0-50k (asset) | $0.1-0.3M (acqui-hire interest, rare) |
| MVP with pilots | 3+ design partners, usage | $0.1M | $0.3-1M (mostly team value) | $2-5M seed on team/market |
| Initial recurring revenue ($24k ARR, conservative case) | paying customers | not ARR-valued | 0.1-0.3M | - |
| $240k ARR (base case) | 25 customers, retention data | 2x = $0.5M | 3-6x = $0.7-1.4M | 8x = $1.9M |
| $1.44M ARR (upside case) | 80 customers, 2% churn | 3x = $4.3M | 4-8x = $5.8-11.5M | 12x = $17.3M |
| Strategic infrastructure | multi-chain, enterprise logos, security record | - | - | only reachable at multi-million ARR; the ~16x cyber-M&A figure applies here, not earlier |

Arithmetic checks: $240k x 3 = $720k; x6 = $1.44M; x2 = $480k; x8 = $1.92M. $1.44M x 3 = $4.32M; x4 = $5.76M; x8 = $11.52M; x12 = $17.28M.

Adjustments not modelled: dilution (typically 15-25% per priced round, my assumption), customer concentration, security-incident liability (a fail-open bug in a funds-at-risk product is existential), regulatory exposure.

### What the current evidence supports
- It supports a modest asset value and a credible *engineering-discipline* signal.
- It does **not** support any company valuation: there is no customer, no validated problem, no product for the target market, and the named opportunity (agent governance) is not in the repo.
- The upside rows are mathematically possible, not probable. I have no evidence base to assign probabilities.
