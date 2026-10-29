# Revenue model and go-to-market

**Everything in this file is a hypothesis. There are no customers, no revenue and no pricing evidence for this product.** Competitor pricing could not be verified (see competitive analysis).

## Models considered

| Model | Fit | Notes |
|---|---|---|
| Open-source core + paid hosted/enterprise | Best fit if you want adoption; **needs a licence first** | Matches how the category adopts (Guardrails, Portkey gateway have 7-13k stars) |
| Self-hosted enterprise + support | Plausible for funds/prop shops that cannot hand over keys | Slow sales |
| Usage-based API | Poor early: hard to meter, small volumes | Later |
| Paid audits / integration consulting | **Fastest path to first cash**; builds evidence | Not scalable; keeps you a services firm |
| Premium integrations | After 2+ adapters exist | - |

## Testable pricing (hypotheses)

| Tier | Price | Who | What it must prove |
|---|---|---|---|
| Community | free | solo builders | adoption, stars, issues |
| Team | $500-1,500/month | small trading/agent teams | someone pays at all |
| Enterprise self-host | $20-60k/year | funds, exchanges' partners | procurement, support burden |
| Review/integration | $5-25k fixed | any | demand, case studies |

## Scenarios (12-month, illustrative; none is a forecast)

| | Conservative | Base | Upside |
|---|---|---|---|
| Paying customers | 5 | 25 | 80 |
| Avg monthly revenue per customer | $400 | $800 | $1,500 |
| MRR | $2,000 | $20,000 | $120,000 |
| ARR | $24,000 | $240,000 | $1,440,000 |
| Gross margin (assumed) | 85% | 85% | 80% |
| Gross profit (ARR x GM) | $20,400 | $204,000 | $1,152,000 |
| Monthly churn (assumed) | 3% | 3% | 2% |
| Operating costs (assumed) | infra+tools $18k, one review $15-30k | $120k infra/tools + part-time help | $900k incl. 4-5 hires |
| Cash required (assumed) | ~$50k | ~$250k | $600k-1M |
| CAC assumption | founder-led, ~0 cash | founder + content | outbound + partnerships |

Arithmetic: 5x$400=$2,000; x12=$24,000. 25x$800=$20,000; x12=$240,000. 80x$1,500=$120,000; x12=$1,440,000. Gross margin and costs are assumptions, not data. Revenue is not profit: the upside case still loses money if it hires ahead of revenue.

## First five paying customers

1. Make a list of 50 teams running agents with exchange API keys or perp-venue wallets (Hyperliquid/Bybit bot builders, small funds, agent frameworks, trading communities).
2. Offer a **paid 2-week "agent permission review"** ($2-5k): you review their agent's key scope, limits, kill switch, logging, and give a written report using this repo's gate checklist. This validates demand and buys access.
3. Convert reviews into a pilot of the sidecar.
4. Publish one honest post-mortem-style article on a documented agent loss, with the controls that would have limited it.
5. Evidence to collect before scaling: willingness to pay without discounting, time to integrate (<1 day), retention at 90 days, number of blocked actions the guard actually stopped, prospects who say "we'd build it ourselves".

## Kill criteria

If after 20 conversations fewer than 3 will pilot and none will pay, stop the governance direction. Do not pivot into trading signals: the repo's evidence says no timing edge.
