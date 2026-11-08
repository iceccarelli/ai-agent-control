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

The manifest also records each table's newest row id and `PRAGMA user_version`, so `verify` fails a backup whose newest rows or schema version changed even if someone
re-hashed it (a manifest from before this check verifies with a warning, never silently). To INSTALL a restored database after an incident, **with the machine stopped**:

    python3 tools/state_backup.py promote --restored <NEW-EMPTY-DIR>/restored-state.db --live-db <LIVE-PATH> --confirm-owner

It refuses without `--confirm-owner`, if the restored file does not verify against the manifest beside it, if `-wal`/`-shm`/`-journal` exists next to the live file, or if
SQLite cannot take an exclusive lock on it right now. It RENAMES the old file to `<live>.replaced-<UTC>` (never deletes it), installs the copy by atomic rename, and verifies
the result. It does not stop or start anything: stopping the machine first, and checking the result before starting it, are yours.

Encrypt the file before it leaves the machine (`age -r <recipient> -o x.db.age x.db`, or
`openssl enc -aes-256-cbc -pbkdf2 -in x.db -out x.db.enc`) and keep the manifest beside it. A backup that
has not been restored once is a hope, not a backup. **No schedule is installed by the repository.**

## 15. Is the evidence trustworthy, and is it profitable? (read-only, offline)
Run on the recorder's data directory (or a copy; it never writes there):

    python3 tools/profitability_report.py --dir /data/market --history /data/monitor/monitor.jsonl --notional 100000 --hold-days 30

* **Integrity (separate states, no single flag):** every capacity row is validated against `capacity/2`; a row with no chain entry
  is never admitted and blocks the economics block; each file is graded ANCHORED_CURRENT / ANCHORED_LAGGING / STALE_ANCHOR /
  UNANCHORED / ANCHOR_CONFLICT by its LATEST anchor; and the sidecar chain is checked
  against the head copied into `monitor.jsonl` (a second copy on the same volume: external to the evidence directory, not to the machine, until you archive it off the machine). A broken chain in ANY chained file, an anchored file that lost its sidecars or rows, an anchored file that is gone,
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
  a validated edge. `PROSPECTIVE_MARKET_EDGE_SUPPORTED` is the gate a future pre-registration mechanism must satisfy (typed, finite claims,
  screens re-run on post-registration data alone, anchors held off the machine); no registration mechanism exists and this report supplies none,
  so it is unreachable from the CLI today.
* **Money:** two returns (on hedged notional, on fully allocated economic capital), the 10/15/20 %/yr
  screens labelled `PROVISIONAL_SCREENING_HURDLE_NOT_OWNER_APPROVED`, and an explicit list of UNKNOWN
  inputs (latency, the account's real fee tier, venue margin rules). Replace them with the owner's.
* **What a status means:** `PROSPECTIVE_MARKET_EDGE_SUPPORTED` is a statement about the PUBLIC market. `ECONOMICALLY_RECONCILED` needs account
  records (real fee schedule, reconciled account funding settlements, real fills with fee records, measured costs, verified
  margin rules) that nothing in this repository produces yet, so no run of this report can reach it; `EXECUTION_READY` additionally needs a
  separate owner authorisation record. No status authorises trading.
* **What it would take (6b) and what exposure survives (6c):** from the variance and dependence observed so far, the report prints how many
  settlements it would take to separate funding that earns the lowest screen from noise at the family-wise level (an illustrative
  position and an assumed 11.4 bps round trip: labelled ASSUMED). The exposure line is a research ceiling from an offline stress
  simulation (basis shock, funding reversal, gap, liquidity, correlated stackings) with ASSUMED venue rules and an unverified fee tier; it
  is allowed to be, and today is, ZERO, with every reason named. Levels above 1x are analysis only.
* **The other hypotheses (7):** basis convergence is measured as real EPISODES (entry, convergence or failure, adverse widening, net), trend and
  mean reversion are predeclared rules fed one price at a time against buy-and-hold, and all 16 predeclared variants are judged
  together (alpha 0.05/16). They are INSUFFICIENT on a few days of data by design; microstructure cannot be measured until imbalance is recorded and
  a cross-section until more than one symbol is. None is above RESEARCH_CANDIDATE.
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

## 18. Does the RUNNING recorder contain this code? (owner, read-only; merging a PR does not change a running machine)
A running Fly machine changes only when an image is built and deployed. Merging PR #217 changes the repository, not
`carry-drill`. To establish what is actually running, from a checkout of the commit you want to compare:

    python3 bot/tools/image_manifest.py expected > expected.txt        # reads only the repository
    python3 bot/tools/image_manifest.py command                         # prints ONE read-only command; run it yourself, save its output as actual.txt
    python3 bot/tools/image_manifest.py compare --expected expected.txt --actual actual.txt
    fly image show -a carry-drill                                       # record the registry image and digest beside the result
    fly status -a carry-drill                                           # machine id, version, restarts

`compare` reports per file MATCH / DIFFERENT / MISSING_ON_MACHINE for the recorder's whole static import closure (computed from the
entry points: recorder, monitor, supervisor and every module they import, including the book, wire, store and transport code) and a verdict. An image built before this change lacks
several of them, which shows as MISSING_ON_MACHINE: that is the expected answer, and it means the old recorder is still
running. The closure includes packages (`signals/`) and LAZY imports: the monitor imports the shadow engine inside a function, so the
trading-engine files that ship in the same image are compared too, and an engine-only edit therefore shows as DIFFERENT even though the
recorder's common path never runs it. Read such a DIFFERENT as "the image is not this commit", not as "the recorder is broken", and
decide the release separately. The drill image carries no commit label, so the tool compares bytes, not tags. **A recorder release is a
separate step from any trading-engine release and needs your explicit authorisation; nothing in this PR requests it.**

## 19. External anchor archive (owner; moves the chain head off the recorder machine)
Chain heads copied into `monitor.jsonl` sit on the same volume as the evidence. To make a rewrite detectable by someone who
controls that volume, keep the heads where the machine cannot reach:

    fly ssh console -a carry-drill -C "tail -n 400 /data/monitor/monitor.jsonl" > monitor_tail.jsonl     # read-only fetch, no secret in it
    python3 bot/tools/anchor_archive.py append --history monitor_tail.jsonl --archive anchors.jsonl        # on YOUR machine
    python3 bot/tools/anchor_archive.py latest --archive anchors.jsonl                                     # prints the archive head

Do this at least daily (the monitor's history is bounded). Store the printed **archive head** (one 64-hex value) and its entry
count somewhere the recorder machine cannot reach: a password-manager note, a mail to yourself, a private repository.
`append` refuses, and writes nothing, if a file's anchor goes BACKWARDS (the evidence was truncated) or the same row count
reappears with a different head (the chain was rewritten). Then, on a copy of `/data/market` on your machine:

    python3 bot/tools/profitability_report.py --dir <copy> --history monitor_tail.jsonl --anchor-archive anchors.jsonl \
        --archive-head <the value you stored> --archive-entries <its count>

The report then says `EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE` only if the archive is exactly what you recorded (or has
only grown). When it has grown, only the entries inside the prefix you stored count as anchored by the archive (`anchored by the ARCHIVE
itself: N of M chained files` in the report; the grade is ANCHORED only when that equals M): entries appended later are not pinned by you.
The tool cannot see where you keep the value: that remains your claim, and it is printed as one.

The report decides which anchored day-files that are gone were "removed" and which were merely "pruned" from the capacity retention YOU
state (`--capacity-retention-days`, default 365, the recorder's own default; 0 = the recorder keeps everything). It is never read from
the evidence directory, because whoever can delete a day of evidence can also edit a file next to it. If you run the recorder with a
shorter retention, pass the same number here or every legitimately pruned day will block the economics.

A chain sidecar written by an EARLIER revision of this branch (an adoption entry whose hash lacks the mark) does not verify: the entry
is indistinguishable from an ordinary one with the flag flipped onto it, and an anchor cannot tell them apart. Nothing deployed has ever
written one (`evidence_chain.py` first appears in this change), so there is nothing to migrate; a development file would be excluded,
visibly, as SIDECAR_INCONSISTENT.

An automated version of the archive step (a scheduled job that runs the read-only fetch and appends to a private repository) is possible
but needs your decision about where the secrets for it live; it is not part of this PR.

## 20. The prospective evaluation of the BTC funding carry (owner; opens 2026-11-07, closes 2027-11-07)
This is the only evidence that can change the verdict (`REJECT_OR_SUSPEND`, capital ZERO). Nothing in this section authorises capital, and a
good-looking number before the window closes is not a result. Everything below is offline; the one network step is yours.

1. **Save the public responses as they come.** On a machine you control, request
   `GET https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&startTime=<ms>&endTime=<ms>&limit=200`
   (at most 200 rows a page, ordering unspecified: page by `endTime`) and save each response UNEDITED. Keep them.
   Or let `python3 tools/funding_capture.py --start-ms <ms> --out-dir <NEW dir>` do exactly that loop: one unauthenticated GET per page to the registry host, no redirects,
   each body written byte-for-byte (never overwritten, mode 0600) plus a `manifest.json` with every page's SHA-256 and the gaps on the 8-hour grid (reported, never filled). It is
   never run by tests or CI; you run it. Its pages feed step 2 unchanged.
2. **Make the registered input.** `python3 tools/prospective_prints.py --in page1.json --in page2.json ... --out prints.jsonl`. It converts the
   venue's fraction to basis points itself (never type a unit), refuses anything that is not a clean BTCUSDT print on the 8-hour grid (a symbol that
   moves to a different funding interval is refused: the plan is an 8-hour plan), merges overlapping pages, never overwrites `--out`, and reports gaps
   without filling them. Use `--out` in a new file each time.
3. **Progress, without evaluating:** `python3 tools/carry_prospective.py --funding prints.jsonl`. This records nothing and cannot evaluate.
4. **Evaluate after the window closes (2027-11-07); the closed-window evaluation is the official look, and every look is journaled and counted:**
   `python3 tools/trial_journal.py evaluate --funding prints.jsonl --journal <path on a LOCAL disk> --first-look --allow-unanchored` for the first look,
   then record the printed `ANCHOR TO RECORD NOW` line **off the machine that holds the journal, in a record someone with write access to that machine
   cannot alter** (a printed page, a mail to another person, a commit in another organisation's repository). On every later look pass the anchor with the
   HIGHEST count from that record (`--anchor-n/--anchor-head`, or `--anchor-file` from a copy outside the journal's directory). Be clear about what the code
   can and cannot do: it only refuses an anchor file in the journal's own directory, which is not a trust boundary; a stale anchor verifies a truncated
   journal; and anyone who can write the journal can delete it and rebuild a valid one. The custody is the procedure, not the code.
5. **Read the whole report, not the verdict.** `selection_sensitivity` is NOT a worst case: if settlements are missing, compare `breakeven_fill_bps` with
   the lowest funding a settlement can legally print (you supply it with `--venue-funding-floor-bps`; this repository holds no verified figure). A
   verdict that is "supported" only because of which settlements are present is flagged `DO NOT RELY`. `INSUFFICIENT_POWER` is "not enough data", not "no edge".
6. **Do not change the plan, the coverage floor, the hurdles or the prints after looking.** A different data set is a data revision
   (`--accept-data-revision`), it is recorded permanently in the chain, and it is a different test.

### Owner read-out (offline)
`python3 tools/owner_console.py [--funding prints.jsonl] [--backup FILE]` prints, in one place and without a request or a credential value: whether real money is armed
(`config.is_live_authorized`), the prospective evaluation's progress and the age of the newest print, what capital is (zero until a strategy has an ALLOCATE verdict),
four separate eligibility questions (research / simulated / testnet / owner-authorised), a backup's verification, and the actual-vs-modelled P&L lines. Each section has
one status word: VERIFIED, ESTIMATED, PROVISIONAL, STALE or UNAVAILABLE. UNAVAILABLE is "nothing supplied", never "fine". It exits 3 if the environment arms live.

## Backups: what is not done
Nothing here schedules a backup, stores one off-volume, or has restored one. That needs the owner.

## The testnet drill's own stops
The armed drill (`tools/drill.py --arm`, and the unattended runner that calls it) now refuses, before the first order: a hedge-mode account (the adapter sends no
`positionIdx`; a broker that cannot read the mode also refuses when armed), an existing **kill file** (`touch <run root>/DRILL_KILL` also stops a running drill at its next gate;
the scratch database it runs on cannot hold a kill switch), and a second armed entry on the same UTC day **across processes**
(`--entry-ledger`, required with `--arm`; the unattended runner uses `<run root>/drill_entries.jsonl`; a slot is spent when claimed and not returned if the run fails).
The unattended runner therefore makes at most one armed drill per UTC day. The drill is still off unless you enable it, and nothing here enables it.

## The market-data log: seal, back up, restore-check (owner-run; offline)
The recorder's `md-YYYYMMDD.db` segments are hash-chained inside each file, but nothing outside a file remembered how far it got: deleting the newest records of a segment passed
`MarketStore.verify()` and the writer resumed at the shortened end (reproduced). `tools/md_seal.py` is the outside memory and the tested copy. It opens segments read-only and never
writes into the recorder's directory:

    python3 tools/md_seal.py seal          --dir <MD_DIR> --manifest <OTHER-VOLUME>/manifest.jsonl     # run at least daily; append-only, chained
    python3 tools/md_seal.py check         --dir <MD_DIR> --manifest <OTHER-VOLUME>/manifest.jsonl     # TRUNCATED / MISSING / ALTERED against EVERY earlier seal
    python3 tools/md_seal.py backup        --dir <MD_DIR> --to <OFF-VOLUME-DIR> --receipts <OTHER-VOLUME>/receipts.jsonl --manifest <OTHER-VOLUME>/manifest.jsonl   # refuses if `check` fails; copies only sealed closed days
    python3 tools/md_seal.py restore-check --backup <OFF-VOLUME-DIR> --receipts <OTHER-VOLUME>/receipts.jsonl --manifest <OTHER-VOLUME>/manifest.jsonl   # every sealed closed day needs a matching receipt

`backup` copies only closed days (before today UTC), re-hashes and re-opens each copy and writes a receipt only if it recomputes; a failed copy is set aside as `.REJECTED`, nothing is
overwritten or deleted. A backup is not proven until `restore-check` passes. Keep the manifest and receipts off the recorder's volume and record the printed head somewhere the
recorder's host cannot write (a printed page, another account): the code cannot protect a manifest its own writer can rewrite. The recorder's default retention deletes depth-log
segments after 2 days; do not rely on retention for evidence you have not backed up and restore-checked. Nothing here schedules itself; the schedule and the storage are yours.

**Back up BEFORE retention (operational consequence).** `python3 tools/md_seal.py check|seal ... --receipts <OTHER-VOLUME>/receipts.jsonl` excuses a sealed day that is no longer in the
directory ONLY if all hold: it is older than the oldest segment still present; the receipts verify and hold a receipt for it whose last chain value equals the sealed one; and the oldest
remaining segment's `chain_start` equals that receipt's last chain (a run of pruned days must chain unbroken). `pruned.jsonl` is written by the recorder and is not trusted for this. A day
pruned by the recorder's `--retention-days` BEFORE it was backed up stays a loud failure: `check` says `MISSING ... NOT backed up`, and `seal` and `backup` refuse, because evidence that was
never copied has vanished and nobody should be able to launder that by re-sealing. Therefore: run `seal`, then `backup`, then `restore-check` daily, and keep `--retention-days` longer than
that cycle; pass `--receipts` to `check` and `seal` once retention has started pruning. **A receipt is a claim about a copy, not the copy:** with `--receipts` alone the excuse is receipt-based only, and `check` prints a note saying so; run `restore-check` alongside, or pass `--backup <OFF-VOLUME-DIR>` to `check`/`seal` so each excused day's copy must exist there with the receipted sha256 (missing or different = not excused). If a day was pruned unbacked-up, investigate (is a copy elsewhere?); the manifest keeps reporting it (there is deliberately no automatic way to retire a sealed day: start a new
manifest only as a recorded owner decision). `check` also reports any `md-YYYYMMDD.db` dated after today (UTC) as FUTURE and `seal` refuses it: a fabricated far-future file must not decide
which segment is the newest. The seal manifest, receipts and backup may not be at, under, or a symlink into the recorder's directory.

## The market collector: one run at a time; seal, back up, restore-check (owner-run; offline)
`tools/market_collector.py run-once|seal|check|backup|restore-check|repair-log` take an exclusive non-blocking `flock` on `<out-root>/.lock` for the whole operation (the kernel drops it if the
process dies). A second concurrent `run-once` returns `REFUSED_LOCKED` (no state touched; the first cycle is never renamed to `.INCOMPLETE`); `seal`, `backup` and `repair-log` refuse the same way
with exit 2: retry after the running one finishes. An unexpected capture error mid-cycle is reported as `ABORTED` and the unfinished directory is set aside on the next run.

    python3 tools/market_collector.py seal          --root <ROOT> --manifest <OTHER-VOLUME>/cseal.jsonl
    python3 tools/market_collector.py backup        --root <ROOT> --to <OFF-VOLUME-DIR> --receipts <OTHER-VOLUME>/creceipts.jsonl --manifest <OTHER-VOLUME>/cseal.jsonl
    python3 tools/market_collector.py restore-check --backup <OFF-VOLUME-DIR> --receipts <OTHER-VOLUME>/creceipts.jsonl --manifest <OTHER-VOLUME>/cseal.jsonl

`backup` is bound to the seal manifest and `cycles.jsonl` like `md_seal backup`: it refuses when the root fails its seal or the log does not verify; only sealed, recorded, self-consistent cycles are
copied and each copy must equal the sealed hashes. A cycle skipped because it fails its own manifest, is not in `cycles.jsonl` or is not sealed makes `ok` false (seal first, then back up).
`restore-check` needs a receipt for every sealed cycle. Manifest and receipts may not be at, under or a symlink into the collector's root.

## The entry authority: why an opening order is refused, and what you must do (owner)
Every order that can ADD exposure now passes one authority inside `BybitClient._request` (`entry_authority.py`), whoever the caller is: the carry book, `place_order`, the drills, the
linear-stop drill, the unattended runner. A client with no authority refuses (`NO_ENTRY_AUTHORITY`). A reduce-only exit is not gated by it; a spot sell is an exit only if the venue's own position and wallet reads show the short perp stays covered (spot left >= 98% of the short), otherwise it is refused `WRONG_DIRECTION` (unreadable = refused: close the long by hand, it is price risk; a stripped hedge is a naked short). The production book installs
one that checks, in order: the venue, the persistent kill switch (read now; unreadable = engaged), the instrument (only the carry's legs: perp Sell, spot Buy; anything else `UNKNOWN_INSTRUMENT`/`WRONG_DIRECTION`), the order type (exact `Market`/`Limit`; a Market order with a price is refused; a Market order is sized at the mark), your approved risk policy with MEASURED inputs (`risk_policy.py`), the order's notional cap,
and a durable per-`orderLinkId` reservation (an identical resend passes, a different order under the same id is refused, at most 2 distinct unresolved entries).

**Nothing is approved by default, so today the book refuses to open on testnet or mainnet.** The refusal reasons name what is missing: `NO_APPROVED_POLICY` until you write a policy file and
point `RISK_POLICY_PATH` at it (JSON with every `risk_policy.Policy` field and `"approved_by_owner": true`; strict: the string "true" does not count, an unknown or missing field is "no policy");
then `MISSING_INPUT:<name>` for every limit whose value the running book does not yet measure (only `operational_fault` is measured today; see `tools/risk_policy_proposal.py` for the
unmeasured list). That is the intended fail-closed state: do not work around it by editing limits to match what is measured. A loss/hedge/fault breach latches the strategy SUSPENDED in
`risk_latch.json` beside the state database; it survives restarts and clears only by `RiskLatch.clear(strategy, reason=..., at_ms=...)`, which records why. The reservation ledger
(`entry_reservations.jsonl`, same directory) is evidence: do not delete it to "unblock" an entry. Paper and shadow runs send no orders, so they are unaffected.

**Reservation ledger limits (stated, not hidden).** `ReservationStore.resolve()` is **not called anywhere in production**; nothing marks a reservation done. The "at most 2 distinct unresolved
entries" bound is therefore a 15-minute WINDOW over `entry_reservations.jsonl`, not a count of truly open entries, and an old `orderLinkId` stays reserved forever (a different body under it is a
conflict; an identical resend is allowed). An `orderLinkId` must be exactly `[A-Za-z0-9_-]{1,36}` (no whitespace, never stripped) and "identical" means the whole canonical body (category, symbol, side,
qty, orderType, price, reduceOnly, timeInForce). Rows written by an older version lack the type/price fields and so never equal a new order: a resend of such an id is refused, not guessed.
**Writes on any client** are limited to an allow-list (`bybit_connection.WRITE_ENDPOINT_ALLOWLIST`: order create/cancel/cancel-all, position trading-stop/set-leverage, and the demo faucet on the demo venue
only) and a strict canonical path; set-leverage is also bounded (linear, integer 1..10 and, on a client with an authority, no more than `DEFAULT_LEVERAGE` and the approved policy's `max_leverage`, buy == sell, the carry's perp symbol); trading-stop is limited to the exact body the client builds (linear, perp symbol, plain-decimal `stopLoss`, `tpslMode` Full, `positionIdx` 0); cancel needs an `orderId`/`orderLinkId`, cancel-all needs a symbol, and with an authority both are bound to the carry's two instruments. A new write endpoint is a reviewed code change, not a flag.
An order-create body is a CLOSED list of keys (`entry_authority.ORDER_BODY_KEYS`; anything else `UNKNOWN_BODY_FIELD`, a spot Buy with `isLeverage` is a refused margin borrow) and a resend is "identical" only if the whole body matches. `reduceOnly` counts as an exit only as the boolean `true`, exact lowercase `linear`/`inverse`, and (with an authority) only as a perp BUY on the carry's own perp symbol; any other reduce-only order is refused `EXIT_NOT_BOUND:*`. The authority sizes a Market order from the book's snapshot mark only if that snapshot is under 30 s old and perp and spot agree within 5%; otherwise the order is refused (`NO_PRICE_FOR_NOTIONAL`).

**Operator tools that build their own client cannot open an order any more (intended; the authority is not weakened for them).** `tools/linear_stop_venue_drill.py`, `tools/close_testnet_experiment.py` (default client factory),
`tools/testnet_conformance_run.py` and `tools/drill.py` each construct a bare `BybitClient` with no entry authority, so every opening order they send is refused `NO_ENTRY_AUTHORITY` before the transport (reduce-only exits, cancels,
set-leverage and the stop body still go through, now shape-checked). A testnet run of one of them is therefore an OWNER-run session, and needs all of: (1) an owner-written, approved policy file (`RISK_POLICY_PATH`) with the measured inputs the
book supplies, (2) the tool to install `entry_authority.build_for_book(...)` on its client with the carry's perp/spot symbols, a state directory and a mark provider (a code change per tool, reviewed by you; none has been made), and (3) the usual testnet-only environment. Until then
those tools can run their read-only and exit steps and will report `ENTRY_REFUSED: NO_ENTRY_AUTHORITY` on the first opening order.

## Owner attestation key and registry anchor custody

Two secrets decide whether `strategy_verdict.py` can say QUALIFIED or OWNER_PROFIT_EVIDENCED, and both must live where this repository, its volume and the app host cannot reach.

**The key (`OWNER_ATTESTATION_KEY`).** At least 32 characters with at least 12 distinct characters (a repeated or short string is refused as no key). Generate it on your own machine (for example 32 random bytes, base64), keep it in your own password manager. It never goes in the repository, the Fly volume, Fly secrets, a `.env` the app reads, CI, or a chat. `tools/owner_console.py` reports it as set/unset only and raises a finding if it is set in an environment that looks like the app host (any `BYBIT_*` variable present): if you see that finding, remove the key there and treat it as exposed (generate a new one; old attestations stop verifying, which is correct).

**Attesting evidence (on your machine only).**
`OWNER_ATTESTATION_KEY=... python3 tools/strategy_verdict.py attest --candidate cand.json --evidence evidence.jsonl --registry hypothesis_registry.json --out attested.jsonl`
It signs only observed-stage items (prospective, execution, owner result), and only if `counted()`'s own preconditions hold: bound to this candidate and the REGISTERED spec digest, registered before the observation started, a window not shorter than registered, not in the future. Evidence the engine would refuse anyway is refused at signing. The signature covers the evidence content including `recorded_ms`. Add the signed lines to the registry with `record_evidence`.

**The anchor.** The registry seal is keyless: anyone who can write the file can delete a FAIL and reseal. The anchor is the head digest you hold outside it. Print it with `python3 tools/hypothesis_registry.py --print-anchor`, write it on paper or in an offline note, and store it away from the repository and volume. After EVERY deliberate append (a registration, evidence, an amendment) print and store the new anchor, and keep the old one only as history; pass the current one as `--anchor` when you evaluate. Without it the verdict cannot exceed PROSPECTIVE.

**A changed spec is a new candidate.** The registry stores the spec digest and the cost-chain declarations (fees, slippage, funding, capacity, execution, holding period, eligibility, OOS protocol, parameter grid) at registration. A spec whose digest differs gets INSUFFICIENT_DATA with `SPEC_DIFFERS_FROM_REGISTERED(<what changed>)`. To change anything legitimately, register a NEW candidate id/version and start its ladder again. A registration from before digests were stored reports `REGISTERED_SPEC_DIGEST_MISSING`: its observed evidence is not counted, so it stays below QUALIFIED; re-register it as a new version.

**What they prove, and what they do not.** The key proves that the key holder signed these claims about this candidate and spec. The anchor proves the registry still has the history it had when you recorded the anchor. Neither proves the claims are true: a key holder can sign a falsehood, and nothing here independently checks the exchange, the fills or the account. QUALIFIED means "consistent claims you signed on a registry you anchored", not "verified profit".
