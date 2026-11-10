---
name: money-next
description: Pick the single highest-value economic bottleneck left in the repository and implement the repository-side fix test-first, instead of producing another audit.
---

# /money-next

1. Run `/money-status` first. List the open items in `docs/MONEY_MISSION.md` section 9 ("Open, repository-side").
2. Rank by: expected net bps per unit of economic capital unlocked or protected; evidence quality; capital survival; time to cash. State why the winner beats the runner-up in two sentences. Reject a task that does not improve net economics, evidence quality, capital survival, execution efficiency or reliability.
3. Name the measurable uncertainty it resolves and the result that would make you REJECT the work.
4. Reproduce the fault with a failing test or an end-to-end run, then implement the smallest coherent fix in the module that already owns it (no competing implementation), add boundary and adversarial tests, run the narrow tests and the full relevant bucket, `python3 bot/tools/secret_scan.py`, `python3 bot/tools/frozen_proof.py`, `python3 bot/tools/fly_stack.py --check`.
5. Commit to the ONE mission PR branch (never a new PR per task). Update the execution log in `docs/MONEY_MISSION.md` (the single status system).

Never weaken a risk gate, authority check, lease, kill switch or test to make progress. Never touch `signals/funding_carry_fade_v1.py`.
