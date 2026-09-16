# ROADMAP — from a $100 ruler to something an institution would fund

Written 2026-09-05 against tradingbot_slice76 + patches 0001–0006.
This document answers "how do I make this a billion-dollar asset" the only
way the tree's own rules (MASTER_PROMPT §4) allow: by stating what is true,
what is missing, and the order in which the missing things must be proven.

## 0. Where the money actually is (and is not)

| Claim | Status on this tree | Evidence |
|---|---|---|
| Plumbing works | Mostly. Now deployable (0001), gate-enforced in code (0002), reconciles against the venue (0003), sees exits (0004). | 4,953 baseline tests + 39 new |
| Timing works | **One historical pass, two forward losses.** Stage-1 OOS n=41, M1≈95.1/M2=96 against pre-declared bars; forward n=2, mean net R −0.836; monitors INSUFFICIENT_DATA. | artifacts/slice5x–7x, SLICE78_CERTIFICATION |
| Live works | **No. Gate still 2/8.** Both blockers this row named are retired and the gate did not move by one item: the linear simulator exists (`LinearSimulatedExchange`, 0059) and `connector_check` prints `BYBIT_TESTNET_OK` from this host. Neither is a gate item. `linear_protective_stop_verified` needs four observations of a VENUE that no simulator can supply, and six of the eight items are human acts by design. | slice77_promotion_gate.json, artifacts/connector_check.json |
| Capacity | **Unmeasured.** The rule fades daily-bar funding extremes on one symbol. Its capacity is bounded by how much BTC-perp notional can enter at the next daily open without moving the funding print it keys on. $100 tells you nothing about $100k; $100k tells you nothing about $100M. | none — this is the biggest unknown |

A "billion-dollar" trading asset is not a bot. It is (a) a set of edges with
measured capacity summing to a book that can carry that capital, (b) an
execution and risk stack that survives audit, (c) a legal/operational wrapper
(fund, custody, counterparties) that lets outside capital in. This tree is a
credible start on (b), a disciplined *method* for (a) with zero cleared
edges at meaningful capacity, and nothing of (c).

## 1. Sequence (each stage gates the next; none can be skipped)

### Stage A — finish the shell (weeks, engineering only)
1. ~~**Linear simulator**~~ **DONE 0059.** `LinearSimulatedExchange`: position
   accounting, 8-hourly funding accrual from a real series when supplied,
   continuous mark-to-market, a liquidation that can precede the stop, and the
   position-attached stop written by `/v5/position/trading-stop` and read back
   through the real `BybitClient.verify_stop`. 25 tests.
   **It does NOT complete the gate item `linear_protective_stop_verified`.**
   That item's four conditions are statements about a VENUE — a stop placed and
   read back from Bybit, surviving a process restart, a naked position detected,
   margin behaviour documented at the proposed notional — and no simulator can
   make any of them true. The checklist's simulator-gap box is filled in; its
   four venue boxes and its signature block are untouched.
   Still outstanding: **no walk-forward has been re-run through it**, so there
   is a linear simulator and still no linear number.
2. ~~**Run the measured rule in the runtime.**~~ **DONE 0062**, as
   `BOOK_MODE=shadow`. Live providers over `get_klines('D')` and a new
   `get_funding_history_fractions`; the 5-bar HORIZON time exit the runtime
   genuinely lacked (`HORIZON` appeared only in the signal module and in
   research tools, so a position ran to its stop or target and never timed
   out — a cousin of the measured rule, not the rule). `shadow_is_permitted`
   returns True on this tree, so the attach is live rather than decorative,
   and a real run reads 299 linear daily bars and 200 funding prints.

   **Three deliberate departures from the line above:**
   * it attaches on `BOOK_MODE=shadow`, NOT automatically when
     `cleared_edge_signal` names the rule. The repo's own reasoning for carry
     applies: "selecting carry is a deliberate operator act rather than
     something that happens by upgrade." An automatic attach would change what
     a running process trades on the next deploy.
   * `MarketStrategy` is NOT deleted and is still the `directional` default.
     Deleting it is a separate change with its own blast radius.
   * it refuses unless `CATEGORY=linear`. The rule was measured on linear
     daily bars and the funding endpoint does not exist on spot; BTC spot and
     perp closes are close enough that substituting one would be invisible.

   **It changes what the shell RUNS, not what it may do.** Paper only, one
   position, one entry per calendar day, 100.00 USD, every existing gate
   unchanged, and the promotion gate still reads 2 of 8.
3. **Testnet from a host that can reach Bybit.** `connector_check` must say
   BYBIT_TESTNET_OK before Track D. Kill-switch drill, recorded. Stop
   verification, recorded. That closes three human gate items.
   **The verdict is BYBIT_TESTNET_OK from this host (0059).** Not an inference
   from a reachability probe of my own — `tools/connector_check.py` run
   unmodified prints `BYBIT_TESTNET_OK | CORPUS_PATH_OK | CARRY_READS_OK`, over
   public endpoints, with no credentials. That retires "unreachable from every
   host used so far", which was the stated blocker on this item.
   **It closes none of the three human gate items.** The drill and the stop
   verification are acts a person performs and records; this only removes the
   environmental excuse for not performing them.
4. Websocket private stream for fills (the auth frame already exists at
   bybit_connection.py `ws_auth_message`; nothing consumes it). Polling
   `observe_exits` is correct but slow.

### Stage B — earn the forward number (months, calendar-bound)
- 20 closed forward trades AND 180 forward days on data that did not exist
  when the rule was frozen. At ~2 entries per 22 days observed so far, 20
  trades is roughly 7 months. Nothing shortens this. Not more compute, not
  a better prompt, not a second symbol.
- If forward M1/M2 fail under the unchanged thresholds, the family is
  demoted automatically (§2.7). The programme then has a shell and no edge,
  which is an honest place to be.

### Stage C — capacity and a second thesis (only after B clears)
- Measure capacity: replay the rule's entries against real L2 (data/orderbook
  exists) with a market-impact model; find the notional at which expected
  edge_bps − impact_bps crosses zero. That number, not $100, is the ruler.
- A second, information-set-disjoint family via NEW_SIGNAL_INTAKE.md
  (liquidations, options skew, cross-exchange basis, inventory). Each family
  gets its own pre-declared OOS cut, control, and capacity figure.
- Portfolio layer: risk budget across families, not per-trade caps. This is
  where `position_sizing.py`'s Kelly helper stops being a helper.

### Stage D — institutional wrapper (legal/ops, parallel to C)
- Segregated custody, exchange sub-accounts per family, dual-control on key
  rotation, immutable audit log off-box (the SQLite journal is on-box).
- Independent risk sign-off separate from the builder (the gate's
  `human_risk_memo_signed` already assumes this person exists).
- Fund structure and a track record auditable by a third party. Track record
  starts at Stage B's first forward day; nothing before it counts.

## 2. What "billion" would require that nothing here provides
- Multiple uncorrelated edges each with capacity ≥ $10M/day of tradable
  notional, or one very large one. A daily-bar BTC funding fade is neither.
- Multi-venue execution with smart routing and prime-broker-style netting.
- A team: builder, independent risk, ops/on-call, compliance.

## 3. What NOT to do (from §4, restated for this stage)
- Do not raise the $100 cap in code. Raise it in a signed memo after Stage B.
- Do not add ETH/SOL "because BTC looked good". SOL's control was −0.13 M2
  with a CI excluding zero on the wrong side (project_status narration).
- Do not fine-tune anything on the two forward losers.
- Do not present the operator loop as autonomy. It fetches, hashes, refuses.
