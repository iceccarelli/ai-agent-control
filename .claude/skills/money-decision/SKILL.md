---
name: money-decision
description: Strictly READ-ONLY moment-of-decision economics (no grant, no capability, no lease, no order). Returns WAIT_FOR_DATA, SKIP_NO_OPPORTUNITY, REJECT_UNECONOMIC, INCONCLUSIVE or ELIGIBLE_FOR_OWNER_TESTNET_DECISION with the reproducible decision record.
---

# /money-decision

Run exactly this (needs only the dedicated Testnet credentials in the environment, used for public reads and the signed fee-rate READ; it creates no authority and sends no order):

```bash
cd bot && python3 tools/ms_testnet_autopilot.py evaluate [--symbol BTCUSDT] [--notional-usd 200] [--min-funding-bps N]
```

Exit code: 0 = a decisive answer (eligible / reject / skip), 3 = could not decide (wait for data / inconclusive).

Report from the JSON: market-data eligibility and `data_scope` (Testnet data is not Mainnet expectancy), the candidate and specification (`authority.candidate_id`), funding and seconds to settlement, the cost inputs with provenance (`edge.assumed_inputs`, `fee_tier`), expected gross and net bps and dollars (`edge`), the conservative scenario (the gate haircuts funding), `capital_required_usd`, the policy loss ceiling (a policy cap, not a model), `authority_gaps`, and `decision_record.decision_id`.

ELIGIBLE_FOR_OWNER_TESTNET_DECISION is a statement about economics, NOT authority: the order-capable loop still needs the owner's capability and grant, the executor lease, risk approval and a venue-flat proof. Optional evidence: add `--record-to <dir outside git>` to append the decision record once to the durable outbox.

Never run `loop`, `grant`, `ms_testnet_session.py execute`, `unattended_drill.py`, or anything that creates, copies or signs authority or places an order.
