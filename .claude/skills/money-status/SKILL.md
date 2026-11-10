---
name: money-status
description: One truthful state matrix for the trading asset: git/PR/CI, recorder and data health, research status, execution authority, and what is merged vs deployed vs owner-authorised vs economically proven. Use at the start of a session or before any release or decision.
---

# /money-status

Produce ONE matrix. Every cell is `VERIFIED <how>`, `OWNER-SUPPLIED <date>`, or `UNKNOWN`. Never infer.

1. Git: `git status --short --branch`, `git rev-parse HEAD`, `git fetch origin`, `git log -5 --oneline origin/main`, `git worktree list`, open PRs (list, with changed-file counts). Preserve dirty trees; never reset or clean.
2. CI on the EXACT head SHA: every workflow run and every job (carry-verify 8 jobs incl. Python 3.11 and 3.12 and Postgres, security, autopilot-image, research-image). Pending, cancelled or skipped is NOT a pass.
3. Read `docs/MONEY_MISSION.md` (sections 4 to 9) and compare each dated claim with live evidence; mark stale ones.
4. Data: recorder state from owner-supplied Fly output (`recorder_status.json`, `supervisor_status.json`, `monitor.jsonl` tail) or `UNKNOWN`. Dataset inventory from `python3 bot/tools/dataset_inventory.py` output or `UNKNOWN`.
5. Authority (read-only): `python3 bot/tools/ms_testnet_autopilot.py status`. Capability and grant present/absent by file metadata only. Credentials by PRESENCE only. Never print a value.
6. Economics: the last `/money-decision` and last research verdict with their dates.
7. Separate the columns: CODE MERGED / CI GREEN ON HEAD / DEPLOYED ON FLY / OWNER-AUTHORISED / VENUE REACHABLE / SESSION RECONCILED / ECONOMICALLY PROVEN. Testnet results are never owner profit.

Never: deploy, change Fly/Supabase/secrets, create or copy a grant or capability, place an order, enable Mainnet.
