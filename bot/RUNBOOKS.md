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

## Backups
`tools/state_backup.py` is the tooling; scheduling and storage are the owner's. It opens the ledger
**read-only**, takes SQLite's online backup (safe beside the writer), checks the copy, and writes a
manifest (SHA-256 and per-table row COUNTS, never contents). It refuses an out-dir on the source's own
directory: a backup beside the ledger on the same volume is not a backup.

    # on the machine, to a scratch dir, then copy OFF the volume (owner):
    python3 tools/state_backup.py backup  --db <PATH-TO-LEDGER> --out-dir <DIR>
    python3 tools/state_backup.py verify  --backup <DIR>/<file>
    # the rehearsal that proves it restores, on a disposable machine/dir (never the live one):
    python3 tools/state_backup.py restore --backup <FILE> --into <NEW-EMPTY-DIR>

Encrypt the file before it leaves the machine (`age -r <recipient> -o x.db.age x.db`, or
`openssl enc -aes-256-cbc -pbkdf2 -in x.db -out x.db.enc`) and keep the manifest beside it. A backup that
has not been restored once is a hope, not a backup. **No schedule is installed by the repository.**

## 15. Is the evidence trustworthy, and is it profitable? (read-only, offline)
Run on the recorder's data directory (or a copy; it never writes there):

    python3 tools/profitability_report.py --dir /data/market --history /data/monitor/monitor.jsonl --notional 100000 --hold-days 30

* **Integrity:** every capacity row is validated against `capacity/2`; the sidecar chain is checked
  against the head copied into `monitor.jsonl` (a second copy on the same volume: external to the evidence directory, not to the machine, until you archive it off the machine). A broken chain in ANY file,
  a mixed stream, a regression, a duplicate or more than 1 % rejected rows withholds the economics block entirely; the report says so.
* **Dependence:** unique settlements, not snapshots, are the unit, and settlements are themselves serially dependent:
  a claim needs the effective sample size (>= 30) as well as the count (>= 30 joined, >= 90 for a candidate).
  `INSUFFICIENT` is the honest answer below the floor; a constant funding history has no variance to test and stays INSUFFICIENT.
* **How far to trust the interval (measured, not assumed):** the interval is the widest of a block bootstrap and three
  analytic intervals, and a claim also needs the effective sample size (the LOWER of an autocorrelation estimate and a
  batch-means estimate) to be >= 30. Among series the gate accepts, simulation gives the true mean inside the interval
  about 0.88-0.99 of the time for first-order persistence up to 0.8, and the lower bound above the truth about 3 %.
  Two things it cannot do at ~100 settlements: identify a slow persistent level hidden under noise (measured coverage
  0.7-0.85 there) and predict a level shift. So the economics carry the LEAST favourable of the window's and the most
  recent third's lower bounds, a drift below the window is flagged, and a `RESEARCH_CANDIDATE` is a research flag, never
  a validated edge: only `PROSPECTIVELY_VALIDATED` (screens re-run on post-registration data alone) is evidence of one.
* **Money:** two returns (on hedged notional, on fully allocated economic capital), the 10/15/20 %/yr
  screens labelled `PROVISIONAL_SCREENING_HURDLE_NOT_OWNER_APPROVED`, and an explicit list of UNKNOWN
  inputs (latency, the account's real fee tier, venue margin rules). Replace them with the owner's.
* **Never** read it as realised cash: no cash settlement is read from the venue by this tool.

## 16. Operational verification (owner, read-only; run before and after any deploy decision)
`carry-drill` and `carry-book` are separate apps. Do not assume either is up; look.

    fly status -a carry-drill                      # machine state, restarts, image
    fly machine status <id> -a carry-drill         # image digest, last start, checks
    fly volumes list -a carry-drill                # size, attached machine
    fly ssh console -a carry-drill -C "df -h /data"                         # disk reserve
    fly ssh console -a carry-drill -C "cat /data/market/recorder_status.json"
    fly ssh console -a carry-drill -C "cat /data/market/supervisor_status.json"
    fly ssh console -a carry-drill -C "tail -n 3 /data/monitor/monitor.jsonl"
    gh run list --workflow carry-watch --limit 5   # alert freshness

Record: image digest and build commit (the `/health` fingerprint), restarts since last look, free MB vs the
512 MB reserve, recorder state, newest capacity row age, and whether the newest chain head matches the
monitor's copy. The 365-day capacity retention is separate from the 2-day market-event retention; do not
shorten one to fix the other. Do not scale the authoritative book horizontally.

## 17. GitHub branch protection (owner; this repository does not change settings)
On `main`: require a pull request; require these status checks (names as the Actions UI shows them):
`carry-correctness (3.11)`, `carry-correctness (3.12)`, `rest-of-suite (3.11)` and `(3.12)` as listed in
the run, `production-image`, `shadow-runtime-image`, `secret-scan`, `dependency-audit`; require
up-to-date branches; require code-owner review; disallow force pushes and deletions. Do not allow
administrators to bypass if the owner wants the gate to bind the owner too. Read the exact check names from
a recent run before saving the rule: a required check that never reports blocks every merge.

## Backups: what is not done
Nothing here schedules a backup, stores one off-volume, or has restored one. That needs the owner.
