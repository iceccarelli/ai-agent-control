# Runbooks

Short, for a person at a terminal during an incident. Each one says how to **recognise**
it, what state the system is **already in**, what to **do**, and what **never** to do.
Commands that change anything on Fly or on the exchange are the owner's to run; nothing
here is run by the repository.

Standing rules: the carry book is **PAPER / TESTNET only** until the promotion gate is
signed and live is deliberately armed (it is neither). A position, once held, is managed
through every failure below; entries are what stop. The kill switch is cleared by a
human, never by code. Never paste a key, secret, or `.env` into a chat, ticket or log.

Where "read the venue" appears, use the exchange's own web UI or a read-only API key, not
the bot's state, as the authority.

## 1. Venue unavailable (REST or WebSocket down, 403/geoblock, rate-limit ban)
- **Recognise:** `/health` → `healthy:false`, `carry.blocking_fault`, `market_feed.state` not
  RUNNING; logs `VENUE_POSITION_UNREADABLE`, `403`, `IP ban`, `Retry-After`.
- **Already in:** entries refused; a held pair keeps being watched through the venue-position
  read; reads stand down for the cooldown after a 403.
- **Do:** check Bybit status; check the egress IP is the one the API key is allowlisted for
  (`fly ips list --app carry-book`). A **403/geoblock is a refusal, not an obstacle**: do not
  route around it with a proxy, another region or another domain; record the exact response
  and stop.
- **Never:** restart in a loop (each restart re-reads the venue and can extend a ban).

## 2. Unknown order state (a request left, the answer was lost)
- **Recognise:** halt reason `LEG_OUTCOME_UNKNOWN`; order row `reconciliation_required`;
  startup says `unresolved carry order(s)`.
- **Already in:** HALTED, kill switch engaged, nothing sent on top of it. The order id is
  deterministic and is never re-sent under a new id.
- **Do:** read the venue's **order history and open orders** for the symbol, find the
  `orderLinkId` in the log line, and decide: filled / partly filled / not there. If it is
  **not there more than the absence window after the request** (60 s minimum, longer with the
  configured timeouts) recovery proves it never landed on the next start. If it filled, the
  position exists: go to runbook 3.
- **Never:** place a manual "fix-up" order before you know which of those it is.

## 3. Orphan position (venue holds a position the ledger does not)
- **Recognise:** `carry cold start HALTED: the venue holds a perp position … the ledger never
  recorded`, or `VENUE_POSITION_DRIFT`.
- **Already in:** HALTED, kill switch engaged; the ledger is **not** rewritten (the
  disagreement is the evidence).
- **Do:** read the venue position and open orders; decide with the owner whether to close it by
  hand on the exchange (reduce-only). Keep the DB file and the log. Only then clear the kill
  switch (runbook 12) and restart.
- **Never:** edit the ledger to match the venue.

## 4. Orphan or resting order
- **Recognise:** startup refuses on `open orders` the ledger did not expect; the carry
  broker lists a resting order at the venue.
- **Do:** cancel it **on the exchange** (cancels are always allowed) after reading it.
- **Never:** assume a resting reduce-only close is stale: it may be the close in progress.

## 5. Failed protection (a stop is missing or could not be set)
- **Recognise:** `naked_positions` non-empty in `/health`; PROTECT_ONLY on startup.
- **Already in:** unhealthy; PROTECT_ONLY sends nothing except protective actions.
- **Do:** set or verify the stop on the exchange; or close the position by hand (reduce-only).

## 6. Funding mismatch (estimate vs what the venue paid)
- **Recognise:** `funding_unbooked` in `/health`; a supersession entry in the journal.
- **Already in:** estimates are **never cash**; venue-settled rows supersede them by reversal
  (append-only). A row is booked once per `(id, ms, usd, symbol)`.
- **Do:** compare the journal's funding to the exchange's transaction log for the same window.
  A residual after supersession is an incident for a human, not a rounding.

## 7. Wallet mismatch (book vs wallet balances)
- **Recognise:** `ledger.reconcile()` returns `INCIDENT`; the perp-position difference is the
  strict one.
- **Honest limit:** `reconcile()` observes only and is **not run on a timer by the bot**; it is
  run by the report tools. For a client-owned wallet an absolute USDT/BTC comparison is only
  meaningful against a declared slice, which is an owner decision not yet made.
- **Do:** run `python3 tools/carry_report.py`; compare with the exchange; do not adjust the journal.

## 8. Disk full (the Fly volume)
- **Recognise:** `market_feed` store fault `below the … reserve`; SQLite `disk is full` in logs.
- **Already in:** the market-data feed stops writing at its free-space reserve
  (`MARKET_FEED_MIN_FREE_MB`, default 256), **entries stop, a held position is still managed**;
  it resumes by itself with a marked `store_gap`. The ledger is not the feed: if the **ledger**
  cannot write, the book halts with the exposure written down.
- **Measured:** ~1.5 GB per day of feed. A 1 GB volume cannot hold a day. Either extend the
  volume (owner: `fly volumes extend`), set `MARKET_FEED_RETENTION_DAYS`, or leave the feed off.
- **Never:** delete files under the state directory by hand; `trading_state.db*` is the ledger.

## 9. DB corruption (SQLite integrity)
- **Recognise:** `PersistenceError`, `database disk image is malformed`, startup refuses.
- **Do:** stop the machine (owner), copy the volume's files **off the volume first**
  (`fly ssh sftp`), then `sqlite3 copy.db "PRAGMA integrity_check"` **on the copy**. Restore from the
  last verified backup only with the owner's say-so. Recovery never runs on the original.
- **Honest limit:** no automated off-volume encrypted backup exists yet (see "Backups").

## 10. Fly restart / crash loop
- **Recognise:** `fly status` shows restarts; `/health` flaps.
- **Already in:** restart policy `on-failure` ×10; cold start re-reads the venue twice and
  refuses unless both agree; a staged close in flight resumes from its durable log.
- **Do:** `fly logs --app carry-book`; compare `/health` → `fingerprint.code_sha256` and
  `build_commit` with the reviewed commit.
- **Never:** scale to two machines; `fly deploy` without `--strategy immediate --ha=false`.

## 11. Volume loss
- **Recognise:** the machine boots with an empty state directory.
- **Already in:** an empty ledger over a venue that holds a position **HALTs** at cold start
  (it does not open a second hedge).
- **Do:** treat as runbook 3 with no ledger: reconstruct from the exchange's history, with the owner.

## 12. Manual kill switch / clearing it
- **Engage:** the book engages it on every unsafe state. A human can stop the machine
  (`fly machine stop`, owner) — it holds no position of its own; the venue does.
- **Clear (human only):** after you understand why it engaged and the venue is in a state you
  accept, on a copy of the process that owns the DB:
  `StateStore.clear_kill_switch_by_human("HUMAN_CLEARED_KILL_SWITCH")`. The code never calls it.

## 13. API key revoked / leaked
- **Do:** revoke in the Bybit console first, then `fly secrets unset` the old names (owner), issue a
  new key with the **least** permission and an IP allowlist, set it, redeploy. Rotate immediately
  if a secret ever appears in a log, a file or a chat.

## 14. Market recorder down (alert `recorder_down`)
- **Recognise:** Slack/watch alert "the market recorder not recording"; `recorder_state` in the
  last history lines is ABSENT, STALE, UNREADABLE or FAULT (`fly ssh console -a carry-drill -C
  "tail -n 3 /data/monitor/monitor.jsonl"`; the recorder's own file is `/data/market/recorder_status.json`).
- **Already in:** the supervisor restarts the recorder with backoff (`/data/market/supervisor_status.json`
  shows restarts); the monitor and the shadow book are unaffected; entries are not involved (read-only).
- **FAULT with "reserve":** the volume is below the 512 MB reserve. Extend it (owner:
  `fly volumes extend <id> -s <GB>`) or lower `--recorder-retention-days` in the image CMD.
- **Never:** delete files under `/data/market` by hand; the log is hash-chained and prunes itself by day.

## Backups (not yet automated)
A safe backup is an **online SQLite backup** (`sqlite3 trading_state.db ".backup 'copy.db'"`
taken from inside the machine), encrypted, stored **off** the volume, with a restore
rehearsed on a throwaway machine. None of that is done by the repository; it needs the owner.
