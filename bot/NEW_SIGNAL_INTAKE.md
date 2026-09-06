# NEW SIGNAL — INTAKE FORM (FILLED) — PATH B ULTIMATE

**Status: HUMAN-FILLED — this text must be committed before any Slice-57 code.**

**Signal name:** `funding_carry_fade_btc_v1`

**Program rule:** From this intake forward, the only research outcome that
matters is whether `ProjectStatus.cleared_edge_signal` becomes
`funding_carry_fade_btc_v1` under the OOS gate below. Freeze-only work,
paper-doc work, and “almost positive” narrative are out of scope for this slice.

---

## Relationship to frozen research

| Name | Status | Rule |
|---|---|---|
| `funding_carry_fade_v1` | FROZEN ABSENT (11th line) | Multi-symbol ≥2 rule failed. **Do not reopen.** |
| Slice-55 BTC 97.0/97.5 | Prior exploratory evidence | **Must not** set `cleared_edge_signal` |
| `funding_carry_fade_btc_v1` | NEW product | BTC-only + **OOS primary clear** |

This is **not**:
- a FUND_ABS retune
- a post-hoc drop of ETH/SOL after they lost
- a promotion of the old 3-symbol run under a new label

---

## Thesis (one paragraph)

On BTCUSDT USDT-margined linear perpetual, extreme funding reflects crowded
carry. When funding is sufficiently rich (longs pay shorts) or cheap (shorts
pay longs), a short-horizon **fade** on **BTC only** is the candidate
inefficiency after costs. Portability to ETH/SOL is **explicitly out of scope**.
This is a single-name product definition, not a claim that the multi-coin rule
worked.

---

## Material difference

| Prior family | Difference |
|---|---|
| `funding_carry_fade_v1` (frozen ABSENT) | Universe BTC+ETH+SOL; POSITIVE needed ≥2 symbols at M1/M2≥95 |
| `funding_carry_fade_btc_v1` | Universe **BTCUSDT only**; clear gate = **pre-declared OOS** (not full-sample) |
| Eleven OHLC/funding freezes | Trigger remains funding_rate; no OHLC path filters |

---

## Universe (frozen)

- **BTCUSDT only**
- Price barriers: `data/real_linear_1d` BTC linear daily (synthetic: false)
- Trigger: `data/real_funding` BTC 8h funding (synthetic: false)
- ETH/SOL: **not measured**, not registered, not “optional diagnostics” for clear

If either corpus is missing or ineligible → **STOP**. Do not invent bars.

---

## Constants (frozen — no grid, no “small improvement”)

| Constant | Value | Notes |
|---|---|---|
| FUND_ABS | **0.0001** | Same as slice 55; **not** lowered |
| STOP_ATR | **1.5** | Wilder ATR(14) at signal bar |
| TAKE_PROFIT_R | **1.0** | Fade target |
| HORIZON | **5** | Daily bars |
| ENTRY | **next_open** | Open of t+1 |
| one-trade-per-run | **yes** | Contiguous-run scheduler |
| LOCKUP | **1** | As existing engine |
| ROUND_TRIP_BPS | **25** | Trading friction |
| Funding in PnL | **yes** | Charge funding over hold on observed and surrogates |

**Join (frozen):**  
`f[t] =` last funding print with `funding_time ≤ close_time(t)`.  
Missing → no setup. **No forward-fill. No future funding.**

---

## Entry rule (frozen)

At daily bar t close:

- if `f[t] ≥ +FUND_ABS` → **SHORT_SETUP**
- if `f[t] ≤ −FUND_ABS` → **LONG_SETUP**
- else → no setup

No extra filters (no vol, no trend, no time-of-day, no equity filter).

---

## PRIMARY CLEAR GATE — the only thing that may set `cleared_edge_signal`

### Principle

| May set clear? | Source |
|---|---|
| **NO** | Slice-55 BTC summary JSON |
| **NO** | Full-sample diagnostic replay on all history |
| **YES only if** | **OOS late-50% window** passes **every** clause below |

### Fold calendar (must exist before any OOS score)

1. Load BTC linear daily index (eligible series only).  
2. Sort by time. Let `t0 = first bar time`, `t1 = last bar time`.  
3. `t_mid =` timestamp at **50% of bar count** (by index position, not by return).  
4. **Early (burn-in / non-registration):** bars with time in `[t0, t_mid)`  
5. **Late (OOS / registration):** bars with time in `[t_mid, t1]`  

Write **before any OOS edge/control percentile**:

`artifacts/funding_carry_fade_btc_v1_folds.json`

Must include at least:

- `t0`, `t_mid`, `t1` (ISO UTC)
- bar counts early / late
- sha256 of the BTC linear file and funding file used
- statement: cuts derived from timestamps/index only; not from returns or trade list

**FORBIDDEN:** moving `t_mid` after seeing `n_OOS`, M1, M2, or mean R.

### OOS evaluation set

- Only trades whose **signal bar** (or entry decision bar, as implemented consistently
  with slice 55) falls in the **late** window.
- Same constants, same one-trade-per-run, same costs.

### OOS pass rule — ALL must hold

| # | Clause | Fail label if violated |
|---|---|---|
| 1 | `n_OOS ≥ 40` | INCONCLUSIVE (do not move cut) |
| 2 | Control VALID on OOS (slice-55-class directed/skill control; 0% or ≤5% incompletes per frozen control clauses; abort if INVALID) | no M1/M2 claim |
| 3 | **M1 ≥ 95.0** | ABSENT |
| 4 | **M2 ≥ 95.0** | ABSENT |
| 5 | Mean net R on OOS **> 0** | ABSENT |

If **all** hold:

```text
ProjectStatus.cleared_edge_signal = "funding_carry_fade_btc_v1"