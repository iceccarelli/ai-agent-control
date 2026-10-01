# ASSET INTEGRATION AUDIT — ai-agent-control

**Audit basis:** live `main` tip `12a3b69bd81b8886bd905dd403385e12c923eba5`  
**Tip commit:** `ops: Stage B accrue 2026-09-27; promote shadow 4 trades / 49 bars`  
**Audit posture:** read-only engineering audit; no order, no live arming, no threshold change, no promotion performed.

---

## 1. Executive money thesis

1. **Today this repo is not a proven alpha asset.** It is a controlled forward-evidence + execution-safety stack whose alpha remains unproven.
2. The live forward record is **4 closed forward trades / 20 required** and **49 closed forward days / 180 required**.
3. The current forward artifact is explicitly `is_stage1_evidence=false` and `registration_eligible=false`; the gate still refuses live.
4. `FUND_ABS=0.0001`, the join rule, horizon, stop/TP geometry, schedule and $100 shadow cap are fingerprint-pinned and unchanged.
5. The four forward trades are real observations, but the sample is too small to establish an edge; no extrapolation is justified.
6. The strongest completed technical evidence is the **Bybit linear protective-stop testnet drill**: placement/read-back, restart survival, naked-position detection and margin/liquidation documentation are signed as Items 1–4.
7. **Mainnet execution remains unproven and is not part of this audit.** No recommendation here arms mainnet.
8. The execution core already has restart-safe `orderLinkId`, fill confirmation, partial-fill handling, stop read-back, kill-switch persistence and startup reconciliation.
9. The highest-value remaining engineering work is therefore **evidence integrity and execution observability**, not more signal features.
10. The fastest reusable commercial surfaces are the **venue/protective-stop verification service**, **risk/human-gate packet generator**, and **forward-shadow/promotion-gate control plane**.
11. Productization should stay behind the repo's own kill-or-keep rule for the trading strategy; service packaging can be designed now, but alpha marketing must not outrun the evidence.
12. Cash-compounding metric hierarchy: **closed forward evidence quality → venue execution reliability → repeatable infrastructure products**.

---

## 2. Architecture map

| Plane | Current path(s) | What it actually does | Money/evidence role |
|---|---|---|---|
| Control plane | `bot/tools/control_plane_tick.py`; `bot/tools/control_plane_loop.sh`; `bot/tools/session_tail.py`; `bot/promotion_gate.py` | Read-only orchestration, gate observation, invariant checks; explicitly cannot place orders, set `allows_live`, or promote shadow | Prevents operational shortcuts and makes gate state inspectable |
| Stage B accrual | `bot/tools/append_closed_corpus.py`; `bot/tools/append_spot_corpus.py`; `bot/tools/daily_forward_refresh.py`; `bot/scripts/stage_b_forward_accrual.sh` | Appends closed data only, dual-tree sync, scratch-only forward scoring, prints human promote hint | Builds the only forward evidence that counts |
| Human promotion | `bot/tools/promote_forward_shadow.py`; `docs/human/NO_GLUE_OPS.md` | Copies scratch result to `artifacts/forward_shadow_current.json` only with `--i-am-human --write` | Keeps accrual separate from human evidentiary promotion |
| Execution | `bot/trading_engine.py`; `bot/bybit_connection.py` | Gate → size → entry → fill confirm → verified stop → TP; emergency close + kill on bracket failure | Venue execution reliability and loss containment |
| State / accounting | `bot/persistence.py` | SQLite WAL, durable orders/positions/trades/execution quality, one-writer discipline, kill switch | Reconstructability after restart/crash |
| Signal / edge | `bot/signals/funding_carry_fade_v1.py`; `bot/signals/funding_carry_fade_btc_v1.py`; `bot/tools/slice76_forward_shadow.py`; `bot/EDGE.md` | Frozen `FUND_ABS=0.0001`, close-join, barrier eligibility, fixed schedule/caps, forward-only scoring | Preserves the experiment; prevents post-hoc rescue |
| Linear stop evidence | `bot/tools/linear_stop_venue_drill.py`; `bot/tests/test_linear_stop_venue_drill.py`; `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`; `bot/tools/sync_linear_stop_gate_evidence.py` | Testnet placement/read-back/restart/naked/margin evidence; syncs only the specific checklist item | Reusable execution-safety evidence |
| Forward gate | `bot/artifacts/slice59_promotion_gate.json`; `bot/tools/slice76_promotion_gate.py`; `bot/tools/sync_forward_shadow_gate_observation.py` | Stable gate path; current checklist is 3/8 complete; refuses until forward/human items close | Prevents alpha and operations claims from becoming live authority |
| Settlement / financing corpora | `bot/data/real_settlement_8h`; `bot/tools/fetch_settlement_klines.py`; `bot/tools/basis_at_settlement.py`; `bot/data/real_borrow`; `bot/tools/fetch_borrow_rates.py`; `bot/carry_costs.py`; `bot/tools/borrow_curve.py` | 8h settlement basis and OKX lending-rate floor | Turns carry economics from gross funding-only math into a measurable financing/cost surface |
| CI / host boundaries | `.github/workflows/stage-b-forward-accrual.yml`; `docs/human/NO_GLUE_OPS.md` | GitHub Actions accrual/commit path; factory/Codespaces operational alternatives; cloud coding agents are offline-only by charter | Reliability of overnight evidence collection; avoids two-writer/glue-ops failures |

### Corpora: who reads what

- **Primary + `_full` linear/funding:** Stage B append path; `daily_forward_refresh.py` / `slice76_forward_shadow.py` score the **append-only `_full`** BTC linear + funding trees.
- **Spot corpus:** updated in Stage B for data completeness and connector work; the forward pilot itself does not depend on spot.
- **Settlement 8h:** `basis_at_settlement.py` reads Binance perp/spot 8h bars; it is measurement infrastructure, not a live gate.
- **Borrow:** `borrow_curve.py` / `carry_costs.py` use the OKX public savings lending series as a **floor** on actual borrowing cost; this is not the trading venue's executable borrow series.

### Hard boundaries that must not mix

- Cloud coding agents: offline pytest, no venue network calls, no real-corpus writes (`NO_GLUE_OPS.md` items 9).
- Operational accrual host: the repo documents the factory Mac as the canonical write host; the workflow also explicitly names Codespaces as the proven alternative when GitHub-hosted egress is geo-blocked. Resolve that host declaration to one source of truth before permitting two schedulers.
- Human-only: `allows_live`, mainnet/live arming, kill-switch clear, forward-shadow promotion.
- Reviewer LLMs: advisory only; deterministic local rules own blockers and `allows_progress`.

---

## 3. Ranked integration backlog

**Severity meaning:** P0 = immediate money-loss risk if live; P1 = gate/evidence blocker; P2 = product/observability; P3 = polish.  
**Current state:** no active P0 while `allows_live=false`.

| ID | Severity | Area | Finding | Fix shape | Unlocks $ | Effort |
|---|---|---|---|---|---|---|
| P1-01 | P1 | Forward provenance | `bot/artifacts/forward_shadow_current.json` is current at 4/49 but records `git_commit=`\`13510f...-dirty\``, not a clean source commit. The evidence therefore cannot prove exactly which clean tree generated it. | Make promotion fail closed unless scratch carries: clean-tree provenance, exact HEAD, frozen constants fingerprint, observed-at, and corpus frontier; persist prior/new hashes. Add negative tests. | **(a)** makes each accepted observation reproducible; **(c)** enables paid evidence-grade audit reports. | 0.5–1 day |
| P1-02 | P1 | Human promotion integrity | `promote_forward_shadow.py` enforces the two human flags but does not validate freshness or frozen-constant identity before copying. Existing tests prove “no human flag = refuse”, not “stale/dirty/retuned scratch = refuse”. | Require expected constants fingerprint + frozen constant set + current corpus frontier; refuse stale `observed_at_utc`, dirty provenance, or mismatched `FUND_ABS`. Human-only promotion remains unchanged. | **(a)** prevents bad evidence from replacing good evidence; **(c)** strengthens the promotion-control product. | 1 day |
| P1-03 | P1 | OMS / execution | Current execution confirmation is REST-poll based: submit → `get_order` realtime/history → terminal status. There is no production private-WebSocket execution-event consumer or venue execution-event ledger found in the runtime tree. | Add an execution-event adapter/state machine with reconnect, sequence/order correlation, and REST reconciliation fallback; do not change trade authority. | **(b)** faster/stronger fill truth; **(c)** direct reusable OMS connector component. | 2–4 days |
| P1-04 | P1 | Position truth | Durable startup reconciliation exists and duplicate/lost-reply tests are strong, but a compact periodic venue-vs-local drift heartbeat is not part of the evidence packet. `observe_exits` is defensive, but the operator still needs multiple sources after an incident. | Add a deterministic reconciliation snapshot: local positions/orders, venue positions/orders, unknown/ orphan / reduced / stop state, one hashable report. | **(b)** reduces state-drift loss; **(c)** sellable reconciliation module. | 1–2 days |
| P1-05 | P1 | Gate accounting | `slice59_promotion_gate.json` is the canonical mutable gate path, now holding current 3/8 state, while numbered slice artifacts are historical snapshots. This is correct but non-obvious; reviewer code explicitly had to stop “highest slice wins” behavior. | Keep stable `GATE_PATH`, but add a machine-readable `gate_schema_version`, `current_slice`, source artifact hash and explicit “stable gate path” label. | **(a)** removes false-blocker/operator errors; **(c)** improves evidence packet interoperability. | 0.5 day |
| P1-06 | P1 | Host / CI boundary | `.github/workflows/stage-b-forward-accrual.yml` runs on `ubuntu-latest`; `NO_GLUE_OPS.md` says factory Mac is the only corpus-write host, while the workflow comments call Codespaces a proven egress alternative. This is a documented split-brain ops contract. | Declare one canonical scheduled writer and one manual failover host; add a single-writer lock/lease check across hosts before `--write`. | **(a)** prevents missed or double accrual; **(b)** fewer operational gaps. | 1 day |
| P1-07 | P1 | Financing | Borrow history bottoms at the OKX public-lending floor around 2021-12-14; the carry corpus reaches to 2019. The repo itself says this leaves the early financed period uncosted. A stale/hung OKX fetch can consume operator attention. | Add freshness/coverage status to the product surface and make financed-carry outputs explicitly “financing incomplete” until coverage is sufficient; add timeout/attention telemetry. | **(a)** avoids false financed-PnL certainty; **(c)** sells corpus-health/financing analytics. | 1–2 days |
| P1-08 | P1 | Settlement timing | The 8h settlement corpus exists and is refreshed, but settlement-basis measurement is not the Stage B forward gate. Current signal evidence is daily-close/funding-print based. | Build a read-only settlement reconciliation report that aligns 00/08/16 UTC perp/spot basis to every eligible observation. Do not feed it back into thresholds until separately re-declared. | **(a)** better economic attribution; **(c)** paid basis/settlement analytics. | 1–2 days |
| P2-01 | P2 | Incident observability | After a bad fill, evidence is distributed across SQLite execution-quality records, journal rows, order history, stop verification and session invariants. `session_tail.py` is a gate invariant checker, not an incident timeline. | Create one-shot `incident_packet` with orderLinkId/exchangeId, intended vs fill price, partials, venue status, stop status, local position, kill status, config fingerprint and artifacts. | **(b)** cuts post-fill diagnosis time; **(c)** clear paid “60-second incident pack” service. | 1 day |
| P2-02 | P2 | Promotion UX | Double-promotion is effectively a repeated file copy; identical promotion is not treated as an explicit idempotent no-op with provenance continuity. | Make identical hash = NO-OP; changed hash = explicit human-visible promotion event with previous/current hashes. | **(a)** cleaner evidence history; **(c)** stronger promotion service. | 0.5 day |
| P2-03 | P2 | Entry lifecycle | Maker fallback is deliberately cancel-then-reread-then-market, which is safe, but there is no generic amend/replace abstraction for an institutional OMS. | Normalize entry state transitions and optional amend capability behind the connector; measure maker reject, partial fill, cancel latency and taker slippage before optimizing. | **(b)** execution-quality evidence and lower avoidable slippage. | 1–2 days |
| P2-04 | P2 | Connectorization | Binance is a real/public data source in this repo, not an authenticated trade connector. Kraken trade/account integration is absent from the tree. | Build adapters behind the existing connector contract, with capability flags and fail-closed live arming. | **(b)** venue optionality; **(c)** connector kit product. | 3–7+ days |
| P2-05 | P2 | Trade evidence | Current shadow artifact contains detailed per-trade R and notional, but the live execution path's evidence is not yet normalized to the same schema. | Unify `ShadowTrade` and execution-quality schema for comparative reports without allowing live data to change shadow thresholds. | **(a)** apples-to-apples research/evidence; **(c)** portable control-plane analytics. | 1–2 days |

**Already adequately covered; do not “fix” what is not broken:** closed-bar arithmetic, open-bar exclusion, dual-tree append discipline, orderLinkId duplicate handling, stop verify false → emergency close/kill, naked-position detection, human-only promotion, and live-gate-before-network-call are already strongly represented by tests.

---

## 4. Stage B calibration plan — no cheating

### Current truth

- Forward window: **49 closed days**, strictly after `t1=2026-08-09`.
- Forward closed trades: **4**.
- Required: **20 closed trades + 180 closed days**.
- Remaining: **16 trades + 131 closed days**.
- Current data frontier: **2026-09-27 UTC**.
- If one complete closed UTC day is appended per calendar day with no gaps, the **180-day calendar floor is around 2027-02-05 UTC**. This is an earliest calendar boundary, not a trade-count prediction.
- Trade completion is event-driven; do not forecast its date.

### Daily loop

**Day 0 / now**
1. Verify clean working tree and the current gate state.
2. Run the read-only session invariant check.
3. Inspect the current shadow/gate/memo trio before accepting any overnight handoff.

**Each new closed UTC day**
1. Run the tracked Stage B accrual path.
2. Append only closed linear/funding/spot data through the append-only guards.
3. Score to persistent scratch only.
4. Check whether `forward_n_trades` or `closed_forward_bars` changed.
5. Never promote automatically.

**When the scratch counter changes**
1. Human compares scratch vs current shadow.
2. Human verifies unchanged constants fingerprint, corpus tip and clean provenance.
3. Human manually promotes only after inspecting the diff.
4. Human may commit the promoted artifact only after inspection; no automation does this.

**After each new forward trade closes**
- Recompute all monitors from the frozen trade list.
- M-1 needs 10 trades before it is informative.
- M-2 needs 5 trades inside its 90-day window.
- M-3 needs 10 trades.
- **M-4 requires 20 closed trades.** Its rule is fixed in `bot/shadow.py`: split closed trades into earlier/recent halves and report WARN only when earlier ≥ 0 and recent < 0. Recovery means the **recent-half mean is ≥ 0 under the unchanged thresholds and unchanged entry rule**, on genuinely new forward observations. It does not mean moving the threshold until it passes.

### What must never move

`FUND_ABS`, close-join rule, `STOP_ATR`, `TAKE_PROFIT_R`, `TAKE_PROFIT_ATR`, `HORIZON`, `ATR_PERIOD`, lockup, round-trip cost, one-entry-per-contiguous-run schedule, position/entry/day caps, barrier tail length, monitor thresholds, frozen Stage-1 folds.

The current slice explicitly records that these did not move; the first qualifying forward decision was reached **without lowering the threshold**.

### Gate close order

1. Forward observation reaches **20 trades / 180 days**.
2. M-4 is no longer WARN, or a human explicitly records an acceptance in the signed risk memo.
3. Risk memo is human-written/signed.
4. Live ACK is human-supplied under the independent config gate.
5. Human performs the final live-arming action under the existing safety rules.

No machine action may collapse those steps into one.

---

## 5. Gate accounting integrity

### Current 8-item gate

Current canonical gate: `bot/artifacts/slice59_promotion_gate.json`.

- `human_risk_memo_signed`: **open**, human-owned.
- `forward_shadow_clean`: **open**, 4/20 and 49/180.
- `m4_recent_half_accepted_or_recovered`: **open**.
- `kill_switch_drill_recorded`: **complete**.
- `linear_protective_stop_verified`: **open**. Briefly read complete after a
  sync from a signed checklist; reverted when an audit found none of the
  eight `bot/artifacts/linear_*.json` transcripts that checklist cited exist
  in this repository. See `artifacts/LINEAR_STOP_VENUE_GAP.md`.
- `notional_cap_within_policy`: **complete**, $100.
- `live_trading_ack_present`: **open**, `live_authorized=false`.
- `models_current_absent_or_contained`: **complete**.

Therefore **3/8 complete** and the gate must remain false.

### Existing sync tools

- `bot/tools/sync_forward_shadow_gate_observation.py`: may sync **only** `forward_shadow_clean`; it requires ≥20 trades and ≥180 days and never writes human items.
- `bot/tools/sync_linear_stop_gate_evidence.py`: may sync **only** `linear_protective_stop_verified`; it requires the four signed/recorded venue items, requires every artifact those items cite to exist on disk as readable JSON, and never writes M-4, live ACK or `allows_live`.

Human-only items stay human-only because their security property is the absence of machine authority, not merely a boolean schema field.

### Desync found

`bot/docs/promotion/RISK_MEMO_MICRO_LIVE_DRAFT.md` still describes **48/180** while the current gate and forward artifact are **49/180**. This is a documentation truth gap, not a live-authority breach, because the runtime gate reads the canonical gate artifact and remains false.

Also note the stable filename `slice59_promotion_gate.json`: this is intentional runtime design, but the name is easy to mistake for an old snapshot. The reviewer explicitly guards against globbing to the highest slice number.

---

## 6. Execution and transaction-quality audit

### A. Order lifecycle

**Implemented well**
- `orderLinkId` is deterministic, persisted and restart-safe (`bot/bybit_connection.py::build_order_link_id`).
- Lost submit replies are resolved by duplicate-ID lookup rather than a blind second order; covered by `tests/test_reconcile_orphans_and_duplicates.py`.
- Entry fill confirmation checks realtime then history.
- Maker path cancels before rereading cumulative quantity, then submits only the residual.
- Unknown fill state does **not** trigger an automatic second entry.

**Gap**
- Runtime is REST-poll based, not private-event-stream based.
- Generic amend/replace is not a first-class OMS abstraction.
- Execution attempts are measurable, but a normalized lifecycle timeline is not yet packaged.

**Cash effect:** better fill truth reduces duplicate-order and reconciliation risk; the same lifecycle primitives can become a connector/OMS service.

### B. Protective stops

The current separation is correct:
- linear/inverse/option-style stop semantics use Bybit `/v5/position/trading-stop`;
- spot stop semantics use conditional orders rather than the linear trading-stop endpoint;
- `TradingEngine._protect` places the stop, then calls `verify_stop`;
- failure to verify causes emergency close + kill switch;
- stop moves are place-new-first, cancel-old-second.

**What is proven:** signed **testnet** Items 1–4, including restart survival and naked detection.

**What is not proven:** mainnet venue behavior. No mainnet execution is authorized or recommended by this audit.

**Margin/liquidation:** the signed testnet evidence documents the proposed notional and observed wallet/margin fields, but `liqPrice` can be empty in the venue response. Treat liquidation distance as “observed/unknown” when the venue does not return a usable price; never synthesize one.

**Cash effect:** this is the strongest current sellable execution-safety evidence and directly supports a venue-drill service.

### C. Position truth / drift

`StateStore` is durable (WAL + `synchronous=FULL`), enforces one writer, and stores positions/orders/trades/execution quality. Startup reconciliation chases unknown orders and refuses an orphan venue position rather than silently adopting it.

Remaining gap: no single periodic drift artifact joins local vs venue state for every symbol and stop in one machine-readable snapshot.

### D. Risk

Current controls include:
- fixed $100 shadow notional;
- position and aggregate exposure checks;
- correlation buckets;
- leverage-aware risk fractions;
- persistent breaker/kill-switch state;
- independent live-authorized configuration;
- promotion gate checked before the first network call for live-armed startup.

This is enough to keep the current process fail-closed while live remains dark. The missing layer is not a larger cap; it is stronger evidence that every live-side state transition is observable and reconciled.

### E. Clock / settlement

Bybit client has explicit server-time synchronization and skew correction; the forward dataset is UTC daily and excludes the current open candle.

The economic carry stack, however, has an **8h funding/settlement clock** while the Stage B forward decision is expressed at daily close with the last funding print at-or-before that close. The 8h settlement corpus is therefore best used now as an attribution/reconciliation layer, not as a new decision input.

### F. Financing

`fetch_borrow_rates.py` explicitly describes its series as the OKX **public lender rate**, therefore a floor on true borrower cost, not the executable borrow cost of the trading venue.

The venue's public history starts around **2021-12-14**, while the carry corpus begins in 2019. The early financed period is therefore incomplete rather than imputed. A stale/hung fetch must remain non-blocking to Stage B but should be observable as attention debt.

### G. Multi-venue connector matrix

| Capability | Bybit | Binance | Kraken |
|---|---|---|---|
| Public market data | **Yes** | **Yes** | **No runtime connector found** |
| Public funding/corpus | **Yes / linear market path** | **Yes / corpus path** | **No** |
| Auth account read | **Yes** | **No native authenticated client found** | **No** |
| Auth trading | **Yes, current BybitClient** | **No** | **No** |
| Protective stop verification | **Yes on testnet; signed** | **No** | **No** |
| Unified USDT margin in current code | **Linear path reads UNIFIED account** | N/A in current trade code | N/A |
| Private WebSocket execution feed | **Not found in runtime** | **No** | **No** |
| IP/egress proof | Host-dependent; REST path exists | GH Actions can see HTTP 451 | Not implemented |
| Live arming | **Explicitly blocked today** | Not implemented | Not implemented |

Roadmap is **Bybit → Binance authenticated trade → Kraken**, but every connector must expose capability flags, never assume “exchange connected” means “safe to trade”, and must inherit the same human live-arm boundary.

---

## 7. Test pyramid: what is locked vs missing

### Locked now

- `tests/test_live_promotion_check.py`: live-armed startup refuses before first network call when promotion gate is false.
- `tests/test_promote_forward_shadow.py`: missing human/write flags refuse; automation cannot invoke promotion.
- `tests/test_control_plane_never_promotes.py`: control-plane source/mission cannot promote shadow.
- `tests/test_linear_stop_venue_drill.py`: fake fail-closed branches plus real simulated BybitClient/venue mechanics.
- `tests/test_reconcile_orphans_and_duplicates.py`: duplicate orderLinkId after lost reply, unknown-order chase, orphan venue position refusal.
- `tests/test_stage_b_bars.py` and slice-specific tests: open-bar exclusion, frozen barrier eligibility, gap recomputation, no threshold rescue, no false forward observation.
- `bot/tools/session_tail.py`: read-only invariants for gate false / FUND_ABS / cap / live-authorized assignment.
- Reviewer is import-isolated and deterministic; LLM output is advisory only (`bot/tools/reviewer_verdict.py`).

### Missing negative tests

1. **Allows-live immutability:** add a regression test that scans non-test runtime code and fails if any code path writes the live authorization state, not just the current control-plane charter.
2. **Promotion provenance refusal:** mutate `FUND_ABS`, constants fingerprint, observed-at freshness and clean-tree provenance in scratch and assert promotion refuses.
3. **Stop-evidence forgery:** mutate/replace a venue drill artifact or checklist evidence reference and assert the sync path cannot convert unverifiable evidence into a completed item without the human step remaining intact.
4. **Corpus write safety at the CLI boundary:** explicitly run the write mode against a fixture containing an open bar and assert the open bar is absent after the write; code-level guards already exist, but the end-to-end CLI contract should be pinned.
5. **Execution stream gap:** once a private event adapter exists, test reconnect, duplicate events, out-of-order events and REST fallback.
6. **Host boundary:** add a contract test that a second scheduled host cannot become a concurrent corpus writer without first acquiring the same lease/lock.

---

## 8. Top 10 corner cases / adversarial pass

| # | Scenario | Reproduction hint | Expected fail-closed behavior | Money relevance |
|---|---|---|---|---|
| 1 | **IP rotation / HTTP 401/403 vs 451** | Fake/fixture transport returns 401, 403, 451 at the connector boundary; run Stage B spot path separately from linear/funding. | 401/403 are permanent; 451 may soft-fail only in the exact already-tip-current spot case. Never auto-switch venue. | Prevents unsafe rerouting and protects overnight evidence continuity. |
| 2 | **Insufficient margin / Unified BTC-only equity** | Test linear account with low USDT/equity or only BTC inventory; exercise `get_equity` + risk gates + order path. | Unknown/insufficient equity blocks; no invented collateral and no order. | Prevents sizing from phantom buying power. |
| 3 | **GH Actions 451 vs Codespaces/factory success** | Run the same Stage B script on GH-hosted runner and the proven egress host. | Spot-only harmless geo refusal can soft-fail when already current; real data gaps abort. | Converts host variance into a controlled reliability feature instead of silent missing evidence. |
| 4 | **Stale scratch promoted** | Keep an older `state/forward_shadow_scratch/forward.json`; call `promote_forward_shadow.py --i-am-human --write`. | **Current code accepts this if the human flags are supplied.** This is P1-01/P1-02. | Bad evidence can overwrite the evidence head; fix before treating promotion as audit-grade. |
| 5 | **Dirty FUND_ABS / retuned scratch** | Alter scratch `constants.FUND_ABS` or fingerprint while leaving counters plausible; human-promote fixture. | Promotion should refuse; **current promotion tool does not prove this invariant**. | Directly protects against “minting” trades/evidence by threshold changes. |
| 6 | **Stop placed but verify=false** | In the existing fake client, set `attach_ok=True, verify_live=False`; exercise `TradingEngine._protect`. | Emergency close + kill switch; never report protected. | Demonstrates actual capital-protection behavior. |
| 7 | **Naked check wrong category (spot vs linear)** | Run stop/naked tests with `category=spot` and `category=linear`; force a missing stop. | Spot uses conditional-order semantics; linear uses position stop semantics; category mismatch refuses rather than guessing. | Prevents “stop existed” false positives on the wrong product. |
| 8 | **Partial fill / PostOnly reject / reduce-only reject** | Fake maker partial, then cancellation; fake PostOnly rejection; fake reduce-only emergency close rejection. | Residual only is sent after cancel + reread; reduce-only failure leaves an explicit human-action state and kill where applicable. | Measures real transaction cost and avoids duplicate exposure. |
| 9 | **Clock skew across funding / daily / 8h** | Inject positive/negative server skew; force Bybit 10002; compare daily close join with 00/08/16 settlement records. | Resync on exchange timestamp error; stale/ambiguous timing should block or remain measurement-only, never alter the frozen signal. | Protects attribution and prevents time-boundary lookahead. |
| 10 | **Agent gate-forgery + borrow hang / attention tax** | Ask reviewer/builder path to “complete” human items; separately hang OKX borrow fetch past its timeout. | LLM remains advisory; no signature/live bit is machine-set. Borrow timeout is visible and cannot rewrite Stage B authority. | Preserves human governance and keeps operator attention on money-critical events. |

---

## 9. Product surfaces extractable from this stack

**These are product wedges, not permission to market the strategy as proven. The repo's own `NO_GLUE_OPS.md` keeps productization behind kill-or-keep for the trading strategy.**

| Rank | Product | Buyer | MVP using existing code | Time-to-first-dollar | Reuse | Compliance load |
|---|---|---|---|---|---|---|
| 1 | **Venue Drill / Protective-Stop Verification Service** | Small funds, prop teams, agent-trading developers, exchange-integrators | Bybit testnet drill + stop receipt + restart/naked/margin checklist + PDF/JSON evidence pack | **Fastest** | **Very high** | **Lower** if audit/tooling only; much higher if it executes or manages assets |
| 2 | **Risk-Memo / Human-Gate Packet Generator** | Quant teams, internal risk/compliance teams, AI-agent operators | Deterministic gate snapshot + evidence index + memo template + hashable artifacts | **Fast** | **Very high** | **Lower–medium** as documentation/control software |
| 3 | **Forward-Shadow / Promotion-Gate Control Plane** | Developers operating trading agents | Frozen-threshold forward ledger, provenance guard, human promotion workflow, alerts | **Fast–medium** | **Very high** | **Medium** if it remains monitoring/control infrastructure |
| 4 | **Corpus Health + Settlement-Basis Analytics** | Research desks, carry/financing analysts | Freshness, gaps, settlement basis, financing-coverage report | Medium | High | Lower |
| 5 | **Multi-Exchange Fail-Closed Connector Kit** | Agent builders / trading-system vendors | Bybit first, then Binance authenticated trade, then Kraken; capability matrix + safety gates | Slowest | Medium–high | **Highest** of the five |

The economic point is not “AI trading” branding. The reusable asset is **evidence, execution control, provenance and fail-closed integration**.

---

## 10. Explicit NON-GOALS

- **No** `allows_live` automation.
- **No** mainnet execution in this mission.
- **No** forged ACCEPTED / M-4 / human signatures.
- **No** `FUND_ABS` retune, edge-threshold rescue or schedule rescue.
- **No** reopening frozen Stage-1 research.
- **No** counting flags, setups, bars, funding prints or entries as closed forward trades.
- **No** replacing missing borrow history with invented historical rates.
- **No** treating testnet proof as mainnet proof.
- **No** autonomous venue switching after 401/403/451.
- **No** LLM authority over deterministic blockers.
- **No** “green CI = edge proven”.
- **No** dashboard theater before evidence/execution quality.
- **No** live capital/custody service architecture in the current gate state.

---

## 11. NEXT_HUMAN — only the next 3 compounding actions

Use the tracked operational host and current `$REPO`:

1. `cd $REPO/bot && .venv/bin/python tools/session_tail.py`  
   Confirm gate false, `FUND_ABS=0.0001`, $100 cap and no live-authorization writers.

2. `cd $REPO/bot && ./scripts/stage_b_forward_accrual.sh`  
   Accrue the next closed data, produce the persistent scratch score, and inspect whether counters moved.

3. **Only when the run prints a real `HUMAN_PROMOTE_HINT` and the human has inspected the scratch diff:**  
   `cd $REPO/bot && .venv/bin/python tools/promote_forward_shadow.py --from state/forward_shadow_scratch/forward.json --i-am-human --write`  
   Otherwise: wait for the next closed day; do not manufacture progress.

---

## 12. NEXT_CLAUDE

**One PR-sized mission, no live behavior changes:**

> **P1-01/P1-02 — Harden forward-shadow promotion provenance.**  
> Modify `bot/tools/promote_forward_shadow.py` and its tests so promotion fails closed unless the source scratch is a clean, hashable observation of the frozen program/data contract: exact frozen constants fingerprint, `FUND_ABS=0.0001`, unchanged join/schedule/caps, valid `is_forward_observation`, current/declared corpus frontier, and clean source provenance. Identical content becomes an explicit no-op; changed content emits previous/current hashes. Preserve the existing `--i-am-human --write` requirement and do not touch `allows_live`, mainnet, thresholds, or any live-authority field.
>
> Required tests: stale scratch refusal, dirty provenance refusal, dirty `FUND_ABS` refusal, fingerprint mismatch refusal, exact-repeat no-op, and human-flag fail-closed behavior.

---

## 13. Audit stop packet

- **HEAD:** `12a3b69bd81b8886bd905dd403385e12c923eba5`
- **Tip:** Stage B accrue 2026-09-27; promote shadow **4 trades / 49 bars**
- **Forward:** 4 / 20 trades; 49 / 180 closed days
- **allows_live:** **false**
- **Gate:** 4 / 8 complete
- **Linear protective stop:** complete=true; Items 1–4 signed testnet
- **RISK_MEMO:** §4 not accepted; §7 unsigned
- **Borrow:** public OKX lender-rate floor; historical coverage does not reach the 2019 carry start; stale/hung fetch remains an ops-health issue
- **Settlement:** 8h corpus refreshed through 2026-09-27; measurement-only in current Stage B path
- **Top 5 P0/P1 IDs:** P1-01 provenance; P1-02 promotion guard; P1-03 execution event stream; P1-04 reconciliation packet; P1-06 host/single-writer boundary
- **Product top 3 by time-to-dollar:** Venue Drill / Protective-Stop Verification; Risk-Memo / Human-Gate Packet Generator; Forward-Shadow / Promotion-Gate Control Plane
- **NEXT_HUMAN:** session_tail → Stage B accrual → human-only promote only on inspected hint
- **NEXT_CLAUDE:** P1-01/P1-02 promotion provenance hardening
- **ORDERS_FROM_ME:** 0

---

## Primary evidence paths

- `bot/artifacts/forward_shadow_current.json`
- `bot/artifacts/slice59_promotion_gate.json`
- `bot/docs/promotion/RISK_MEMO_MICRO_LIVE_DRAFT.md`
- `bot/docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`
- `bot/tools/promotion_gate.py`
- `bot/tools/promote_forward_shadow.py`
- `bot/tools/sync_forward_shadow_gate_observation.py`
- `bot/tools/sync_linear_stop_gate_evidence.py`
- `bot/tools/stage_b_forward_accrual.sh`
- `bot/tools/daily_forward_refresh.py`
- `bot/tools/append_closed_corpus.py`
- `bot/trading_engine.py`
- `bot/bybit_connection.py`
- `bot/persistence.py`
- `bot/risk_management.py`
- `bot/shadow.py`
- `bot/tools/basis_at_settlement.py`
- `bot/tools/fetch_borrow_rates.py`
- `bot/carry_costs.py`
- `bot/tools/reviewer_verdict.py`
- `bot/tools/session_tail.py`
- `bot/docs/human/NO_GLUE_OPS.md`
- `bot/tests/test_live_promotion_check.py`
- `bot/tests/test_promote_forward_shadow.py`
- `bot/tests/test_control_plane_never_promotes.py`
- `bot/tests/test_reconcile_orphans_and_duplicates.py`
- `bot/tests/test_linear_stop_venue_drill.py`
- `bot/tests/test_stage_b_bars.py`
- `.github/workflows/stage-b-forward-accrual.yml`

**Conclusion:** the compounding asset is the control/evidence/execution layer. The current alpha should be treated as an unproven experiment with genuine forward observations, not as a live trading product. The next engineering dollar belongs in provenance hardening, execution-event/reconciliation evidence, and repeatable safety services—not in finding more ways to create trades.
