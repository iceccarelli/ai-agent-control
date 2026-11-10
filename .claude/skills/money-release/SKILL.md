---
name: money-release
description: The complete repository release checklist (correctness, CI on the exact head, security, frozen proof, image/import closure, execution safety, accounting identity, evidence, independent review). Refuses to call a release ready if any gate is missing.
---

# /money-release

A gate is PASS only with evidence on the EXACT head SHA. Pending, skipped, cancelled, or a different Python version is NOT a pass.

1. Narrow tests for every changed area, then the full buckets on Python 3.11 AND 3.12 (carry correctness, rest of suite), Supabase migrations on PostgreSQL, forward-shadow, production image build/import, autopilot-image, research-image, security workflow.
2. Local gates: `python3 bot/tools/secret_scan.py` (0 findings), `python3 bot/tools/frozen_proof.py` (ok), `python3 bot/tools/fly_stack.py --check` (0 problems), `git diff --check`, dependency audit and SBOM where the security workflow produces them.
3. Safety review of the diff: no gate, limit, lease, kill switch or authority check weakened; no new order-capable path outside the single-writer lease; no credential read or printed; no Mainnet path; new modules in the Docker COPY lists and structural tests; new tests in exactly one CI bucket.
4. Accounting identity: one net definition across replay, portfolio, ledger, control centre and capital book (`tests/test_ms_pipeline.py` filled-run test, `tests/test_ms_capital.py`).
5. Evidence: the decision record and lifecycle events pass the secret guard and are idempotent.
6. Independent adversarial read-only review of the integrated diff with no unresolved critical finding; no unresolved review thread.
7. PR text states what works, what is owner-only, what is proven economically (usually: nothing yet).

If any gate is missing, output `NOT READY: <gate>` and stop. Merge only when all pass and GitHub policy allows; never bypass branch protection or a failing check. After merge, verify the new `main` SHA and its CI.

Never mark a gate PASS on a pending, skipped or cancelled run, never weaken a test or a risk threshold to get green, and never deploy, change secrets or authority, or place an order as part of a release.
