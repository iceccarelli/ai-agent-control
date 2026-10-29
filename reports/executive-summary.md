# Executive summary — decision report

Repository: iceccarelli/ai-agent-control @ `84ba866`. Date: 2026-10-28. Read-only audit; no source file was changed and nothing was committed. Detailed files are in this directory.

## 1. The finding that changes the question

**The repository is not an AI-agent control product.** It is a Python **Bybit paper/testnet funding-carry research bot** with an unusually rigorous anti-self-deception framework (frozen-signal registry, null-controlled edge measurement, fail-closed risk gates, human-only kill-switch clear, promotion gate that cannot arm). It contains no on-chain code, no wallet or signing code, no agent-framework integration, no API for third parties. (`bot/README.md:1`, `git ls-files`, `repository-audit.md`.)

It also **has no demonstrated trading edge**, by its own evidence: every signal family is frozen ABSENT or INCONCLUSIVE against a 95.0 bar (`bot/project_status.py:122-175`); forward shadow has 4 of 20 trades and about -0.94 R on 4 trades (`bot/artifacts/cash_ledger.json`, `reviewer_verdict.json`); the promotion gate has 2 of 8 items complete (`artifacts/slice76_promotion_gate.json`). `DEPLOYMENT.md:3` says NOT READY for live capital.

Therefore the questions about "which coin wins", "seven chains" and company valuation rest on a premise the code does not support. I answered them as honestly as the evidence allows, and I flag where the answer is "not enough evidence".

## 2. Verified headline facts

- 926 tracked files; 40,269 lines of core Python (172,505 including tests/tools); 498 lines of C++; 145 commits over 6 days, 94 by Claude, 51 by the owner. No licence. No users.
- Full test suite executed: **18 failed, 8,533 passed, 386 skipped, 15 xpassed** (531 s). The failures I examined stem from missing `scipy`/`numpy` in the sandbox, not demonstrated logic bugs; I could not install packages to confirm for all 18.
- Strengths worth keeping: live arming gate (`config.py:694-760`), fail-closed kill switch (`risk_management.py:454-461`), AST test keeping LLMs off the order path (`tests/test_control_plane_boundary.py`).
- Weaknesses: referenced docs missing (EDGE.md, AGENT_CONTROL_PLANE.md, etc.); stale test counts; CI runs a subset with an admitted failing baseline; original Bybit keys flagged compromised and rotation unverified (`DEPLOYMENT.md:7-25`); tracked SQLite state.

## 3. Strongest evidence-supported thesis (hypothesis, unvalidated)

On-chain agent wallets already have policy engines from Coinbase, Privy (Stripe) and Turnkey, and generic agent policy from AWS/Microsoft. A **possible gap** is governance for agents that hold **exchange API keys or perp-venue wallets** (Bybit, Hyperliquid): venue-neutral gates, human-only kill switch, reconciliation and tamper-evident audit. I found no open-source product for that, but absence of evidence is not proof. The reusable asset is the *pattern* in this repo, not the Bybit-specific code. Investment thesis conditions: (1) at least 3 design partners who would pilot, (2) at least one pays, (3) a licence and clean hygiene, (4) an external security review before any production use.

## 4. Decision table

| Recommendation | Evidence | Benefit | Cost/effort | Risk | Confidence | Next validation step |
|---|---|---|---|---|---|---|
| Stop treating repo as an agent-control product; relabel honestly | `README.md`, file list | Avoids misleading buyers | 1 hour | none | High | Rename/describe |
| Add a licence | none present | Enables adoption/diligence | Hours | IP choice | High | Owner decides licence |
| Confirm key rotation | `DEPLOYMENT.md:7-25` | Closes the largest unknown security risk | Hours | none | High | Check Bybit console |
| Fix missing docs / hermetic CI | audit 1.5, 1.7 | Credible evidence trail | 1-2 weeks | low | High | Green full-suite CI |
| Validate agent-execution-guard demand before building | competitor research (gap is inference) | Avoids building something nobody pays for | 2-4 weeks of calls | time | Medium | 10 interviews, 3 pilots |
| Architecture: monorepo, shared core, 4 adapters (not 7 repos) | `architecture.md` | Lowest maintenance | Medium | premature platform | Medium | Extract gate interface first |
| Do not build trading-signal features | repo's own ABSENT verdicts | Saves months | - | - | High | - |
| Treat token list as research only | no live data obtained | Avoids false precision | - | - | High | Re-run with data access |

## 5. Answers

- **Strongest opportunity supported by evidence**: a venue-neutral, fail-closed execution guard for agents with exchange or perp-venue credentials, **if** customers validate it. Not trading, not a token.
- **Why it might win**: the discipline in this code is real (fail-closed idioms, adversarial tests, honest negative results); wallet vendors' policy engines can be bypassed by root users per their own docs; documented agent losses point to permission failures.
- **Biggest reason it fails**: platform vendors (Coinbase, Stripe/Privy, Turnkey, AWS, Microsoft) cover most of the surface, and the niche may be too small or buyers may build it themselves. Close second: a solo founder owning a funds-at-risk security product.
- **Build first**: licence, doc/CI hygiene, gate-interface extraction, Bybit+MCP demo, then Hyperliquid.
- **Seven ecosystems** (weak data): Hyperliquid, Base, Ethereum, Solana, BNB Chain, Arbitrum, NEAR — but that is four engineering efforts (Hyperliquid, EVM, Solana, NEAR). See `crypto-ecosystems.md`. Seven assets for token research (not recommendations, no live data fetched): ETH, SOL, BNB, HYPE, LINK, TAO, NEAR.
- **Architecture**: monorepo with shared core and separately versioned adapters; never seven repos.
- **Current code value**: roughly $0-30k as a code asset (judgement); nominal hand-written reproduction cost $1.0-2.6M is an upper bound under stated assumptions, not a price.
- **Business value**: no supportable valuation today. Illustrative scenarios: $240k ARR x 3-6 = $0.7-1.4M; $1.44M ARR x 4-8 = $5.8-11.5M; each requires customers that do not exist yet.
- **Next 48 hours**: (1) confirm Bybit keys rotated; (2) decide a licence; (3) decide whether this repo stays a research bot or becomes the seed of an agent-guard product; (4) list 20 prospects and book 5 calls; (5) open network access to DefiLlama/CoinGecko/vendor docs if you want the market sections upgraded from secondary to primary sources.

## 6. Top risks and mitigations

1. No demonstrated edge → do not pitch trading. 2. Platform absorption → pick the CEX/perp niche, verify. 3. Fail-open bug in funds path → conformance suite, external review. 4. Unlicensed/AI-authored IP → licence and counsel. 5. Credential history → rotate and document. 6. Solo-founder bandwidth → one adapter at a time. 7. Regulatory (custody/advice) → keep it a control layer that never holds funds. 8. Market-data errors in my research → re-verify with primary sources. 9. Scope creep into 7 chains → four adapters, customer-gated. 10. Broken evidence trail → restore docs.

## 7. Evidence gaps and questions for you

- Were the original Bybit keys revoked, and when?
- Where are `EDGE.md`, `AGENT_CONTROL_PLANE.md`, `PROMOTION_GATE_MICRO_LIVE.md`, `PAPER_AGENT_CERTIFICATION.md`?
- Do you want an agent-governance business, or is this a personal trading research lab? The answer changes everything.
- Licence preference and who owns IP of AI-authored commits?
- Unread code: `trading_engine.py`, `carry_engine.py`, `persistence.py`, `bybit_connection.py`, `tools/`. Approve a deeper module audit?
- Primary-source market data was blocked; approve network access to re-run Phases 3-4.

## Files
`repository-audit.md`, `security-review.md`, `architecture.md`, `competitive-analysis.md`, `crypto-ecosystems.md`, `investment-research.md`, `product-roadmap.md`, `business-model.md`, `valuation.md`, `sources.md`, `appendix/raw-*.md`.

I am stopping here and waiting for your approval before changing anything.
