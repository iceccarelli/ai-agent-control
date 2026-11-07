"""Research harness: the machinery that PRODUCES evidence for ``strategy_verdict`` and makes bad strategies die.

    python3 tools/research_harness.py --demo-cross-sectional-funding   # the committed 3-symbol funding history, run honestly

Offline, standard library only. It REUSES ``evidence_stats`` (ESS, block bootstrap) and ``statistical_power`` (Student-t tail, Holm/BH, the
hash-chained ``TrialLedger``, purged/embargoed walk-forward, split audit); it does not reimplement them. What it adds is the discipline around them:

  PointInTimeUniverse   membership with as-of dates. Asking about a symbol before its listing or after its delisting is an ERROR. A bare symbol list is
                        refused. A universe is "point in time" only if its source records delistings too: three hand-picked survivors never are.
  PITSeries / AsOfView  ``series.asof(t)`` returns a view that does not CONTAIN anything stamped after t, so reading the future is impossible, not discouraged.
                        ``detect_lookahead`` re-computes a feature on truncated history and compares: a leaking feature is caught.
  walk_forward_folds    purged + embargoed expanding walk-forward; train never touches test; test windows are disjoint (audited, raises otherwise).
  VariantLedger         every attempted variant is counted, rejected or not. There is no reset; a fresh object for the same family inherits the count.
  net_edge_bps          cost-aware net edge that REFUSES a zero/unlabelled fee or slippage unless the label is measured or venue_stated.
  historical_evidence / oos_evidence   build ``strategy_verdict.EvidenceItem`` with exactly the metrics ``counted()`` requires, computed, not asserted.

What this is NOT: a profitability claim. A PASS here is offline evidence about one dataset. Gaps are listed in ``GAPS``.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import evidence_stats as ES  # noqa: E402
import statistical_power as SP  # noqa: E402
import strategy_verdict as SV  # noqa: E402

GAPS = (
    "the variant family count is process-local: a NEW process starts at zero unless the caller anchors/persists the ledger (VariantLedger.anchor)",
    "AsOfView makes the future unreachable through its API; Python cannot stop deliberate reflection into the caller's own full arrays",
    "detect_lookahead catches leaks visible as a difference between full-history and truncated-history features; a leak through a label is for audit_split",
    "PointInTimeUniverse trusts the membership source's own claim that it records delistings; the harness cannot verify it against the exchange",
)

MS_8H = 8 * 3600 * 1000


class HarnessError(ValueError):
    pass


class LookAheadError(HarnessError):
    """Something tried to read a value stamped after the as-of time."""


class UniverseError(HarnessError):
    """A symbol was queried outside its membership interval."""


class SurvivorshipError(HarnessError):
    """A universe was built without as-of dates, or claims point-in-time with a survivor-only source."""


class CostError(HarnessError):
    """A cost input is missing, zero without measurement, unlabelled, unknown or not a number."""


def _int_ms(x: Any, name: str) -> int:
    if isinstance(x, bool) or not isinstance(x, int):
        raise HarnessError(f"{name} must be an integer millisecond timestamp, got {x!r}")
    return x


# ---------------------------------------------------------------- point-in-time universe

#: Sources whose membership list is built from what survived to today: never point in time.
NON_PIT_SOURCES = ("hand_picked", "hand_picked_survivors", "current_listing", "current_top_n", "survivors", "unknown")


class PointInTimeUniverse:
    """Membership as of timestamps: ``members = {symbol: {"listed_ms": int, "delisted_ms": int | None}}``. Active on [listed_ms, delisted_ms)."""

    def __init__(self, members: Mapping[str, Mapping[str, Any]], *, membership_source: str, includes_delisted: bool) -> None:
        if not isinstance(members, Mapping) or isinstance(members, (str, bytes)):
            raise SurvivorshipError("a universe needs {symbol: {listed_ms, delisted_ms}} with as-of dates; a bare symbol list has none and is refused")
        if not members:
            raise SurvivorshipError("an empty universe")
        if not isinstance(membership_source, str) or not membership_source.strip():
            raise SurvivorshipError("the universe must name where its membership records came from")
        self._m: Dict[str, Tuple[int, Optional[int]]] = {}
        for sym, rec in members.items():
            if not isinstance(rec, Mapping) or "listed_ms" not in rec:
                raise SurvivorshipError(f"{sym}: no listed_ms as-of date; a symbol without dates cannot be queried point-in-time")
            lo = _int_ms(rec["listed_ms"], f"{sym}.listed_ms")
            hi = rec.get("delisted_ms")
            if hi is not None:
                hi = _int_ms(hi, f"{sym}.delisted_ms")
                if hi <= lo:
                    raise HarnessError(f"{sym}: delisted_ms must be after listed_ms")
            self._m[str(sym)] = (lo, hi)
        self.membership_source = membership_source.strip()
        self.includes_delisted = bool(includes_delisted)

    @classmethod
    def from_symbol_list(cls, symbols: Sequence[str], **_k: Any) -> "PointInTimeUniverse":
        raise SurvivorshipError("refusing to build a universe from a symbol list with no as-of dates")

    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted(self._m))

    def active_at(self, t_ms: int) -> List[str]:
        t = _int_ms(t_ms, "t_ms")
        return sorted(s for s, (lo, hi) in self._m.items() if lo <= t and (hi is None or t < hi))

    def require_active(self, symbol: str, t_ms: int) -> None:
        t = _int_ms(t_ms, "t_ms")
        if symbol not in self._m:
            raise UniverseError(f"{symbol} is not in the universe")
        lo, hi = self._m[symbol]
        if t < lo:
            raise UniverseError(f"{symbol} queried at {t} before its listing at {lo}")
        if hi is not None and t >= hi:
            raise UniverseError(f"{symbol} queried at {t} at/after its delisting at {hi}")

    def survivorship_problems(self) -> List[str]:
        """Why this universe is NOT point in time. Empty = point in time (as far as its own source says)."""
        out: List[str] = []
        if self.membership_source in NON_PIT_SOURCES:
            out.append(f"MEMBERSHIP_SOURCE_IS_SURVIVOR_BASED:{self.membership_source}")
        if not self.includes_delisted:
            out.append("SOURCE_DOES_NOT_RECORD_DELISTED_SYMBOLS")
        if all(hi is None for _lo, hi in self._m.values()) and len(self._m) < 10:
            out.append("NO_MEMBER_EVER_DELISTED_IN_A_SMALL_UNIVERSE")
        return out

    @property
    def point_in_time(self) -> bool:
        return not self.survivorship_problems()


# ---------------------------------------------------------------- no-look-ahead series access

class AsOfView:
    """History up to and including ``cutoff_ms``. It holds ONLY those values: later ones are not in the object."""
    __slots__ = ("cutoff_ms", "_ts", "_vals")

    def __init__(self, cutoff_ms: int, ts: Tuple[int, ...], vals: Tuple[float, ...]) -> None:
        object.__setattr__(self, "cutoff_ms", cutoff_ms)
        object.__setattr__(self, "_ts", ts)
        object.__setattr__(self, "_vals", vals)

    def __setattr__(self, k: str, v: Any) -> None:
        raise AttributeError("an AsOfView is immutable")

    def __len__(self) -> int:
        return len(self._ts)

    def values(self) -> List[float]:
        return list(self._vals)

    def timestamps(self) -> List[int]:
        return list(self._ts)

    def last(self, n: int = 1) -> List[float]:
        return list(self._vals[-n:]) if n > 0 else []

    def at(self, ts: int) -> float:
        t = _int_ms(ts, "ts")
        if t > self.cutoff_ms:
            raise LookAheadError(f"read of ts={t} is after the as-of time {self.cutoff_ms}")
        try:
            return self._vals[self._ts.index(t)]
        except ValueError:
            raise KeyError(t) from None

    def __getitem__(self, i: int) -> float:
        return self._vals[i]


class PITSeries:
    """A timestamped series whose ONLY read path is ``asof(t)``."""

    def __init__(self, ts: Sequence[int], vals: Sequence[float]) -> None:
        if len(ts) != len(vals):
            raise HarnessError("ts and vals differ in length")
        t = [_int_ms(x, "timestamp") for x in ts]
        if any(b <= a for a, b in zip(t, t[1:])):
            raise HarnessError("timestamps must be strictly increasing")
        v = [float(x) for x in vals]
        if not all(math.isfinite(x) for x in v):
            raise HarnessError("non-finite value in series")
        self._ts, self._vals = tuple(t), tuple(v)

    def __len__(self) -> int:
        return len(self._ts)

    def asof(self, t_ms: int) -> AsOfView:
        t = _int_ms(t_ms, "t_ms")
        k = 0
        lo, hi = 0, len(self._ts)
        while lo < hi:                                     # bisect_right on the stamps
            mid = (lo + hi) // 2
            if self._ts[mid] <= t:
                lo = mid + 1
            else:
                hi = mid
        k = lo
        return AsOfView(t, self._ts[:k], self._vals[:k])


@dataclass(frozen=True)
class LeakAudit:
    passed: bool
    n_probes: int
    mismatches: Tuple[int, ...]


def detect_lookahead(feature_fn: Callable[[List[int], List[float]], List[float]], ts: Sequence[int], vals: Sequence[float], *,
                     probes: Optional[Sequence[int]] = None) -> LeakAudit:
    """``feature_fn(ts, vals)`` returns one feature per input row. A causal feature gives the same value at row i whether or not rows after i exist.
    Compare full-history output at i with the output on history truncated at i; any difference (or NaN where the other is not) is a leak."""
    t, v = list(ts), list(vals)
    full = feature_fn(t, v)
    if len(full) != len(v):
        raise HarnessError("feature_fn must return one value per input row")
    idx = list(probes) if probes is not None else list(range(0, len(v), max(1, len(v) // 50)))
    bad: List[int] = []
    for i in idx:
        part = feature_fn(t[:i + 1], v[:i + 1])
        a, b = full[i], part[-1]
        same = (a == b) or (isinstance(a, float) and isinstance(b, float) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)) or \
               (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))
        if not same:
            bad.append(i)
    return LeakAudit(not bad, len(idx), tuple(bad))


# ---------------------------------------------------------------- walk-forward with purge and embargo

@dataclass(frozen=True)
class Fold:
    train: Tuple[int, ...]
    test: Tuple[int, ...]


def walk_forward_folds(n: int, train_min: int, test_len: int, label_horizon: int, embargo: int) -> List[Fold]:
    """Expanding walk-forward (statistical_power.walk_forward), then AUDITED: every fold clean per ``audit_split`` and test blocks pairwise disjoint and
    chronological. ``embargo`` must be >= max(1, label_horizon): an embargo shorter than the label horizon lets training labels see test values."""
    if embargo < max(1, label_horizon):
        raise HarnessError(f"embargo ({embargo}) must be at least max(1, label_horizon={label_horizon})")
    folds = [Fold(tuple(a), tuple(b)) for a, b in SP.walk_forward(n, train_min, test_len, label_horizon, embargo)]
    seen_end = -1
    for k, f in enumerate(folds):
        a = SP.audit_split(list(f.train), list(f.test), n, label_horizon, embargo)
        if not a["clean"] or not a["chronological"]:
            raise HarnessError(f"fold {k} leaks: {a['reading']}")
        if f.test[0] <= seen_end:
            raise HarnessError(f"fold {k}: test window overlaps an earlier test window")
        seen_end = f.test[-1]
    return folds


# ---------------------------------------------------------------- the variant counter

_FAMILY_HIGH_WATER: Dict[str, int] = {}


class VariantLedger:
    """Counts EVERY attempted variant of a research family, including ones rejected before they were ever tested (recorded with p = 1).

    Monotone and unresettable: there is no reset/clear/remove; the hash-chained ``TrialLedger`` underneath detects tampering; and a NEW VariantLedger for
    a family already seen in this process starts from that family's high-water mark, so building a fresh object does not erase the search."""

    def __init__(self, family: str) -> None:
        if not isinstance(family, str) or not family.strip():
            raise HarnessError("a variant ledger needs a family name")
        self.family = family
        self._carried = _FAMILY_HIGH_WATER.get(family, 0)
        self._tl = SP.TrialLedger()
        self._sync()

    def _sync(self) -> None:
        total = self._carried + self._tl.n_trials
        if total > _FAMILY_HIGH_WATER.get(self.family, 0):
            _FAMILY_HIGH_WATER[self.family] = total

    @property
    def n_variants(self) -> int:
        """Attempted variants in this family. Never below the family's high-water mark, whatever object asks."""
        self._tl.verify()
        return max(self._carried + self._tl.n_trials, _FAMILY_HIGH_WATER.get(self.family, 0))

    @property
    def carried_prior_variants(self) -> int:
        return self._carried

    def anchor(self) -> SP.LedgerAnchor:
        return self._tl.anchor()

    def verify(self, anchor: Optional[SP.LedgerAnchor] = None) -> bool:
        return self._tl.verify(anchor)

    def record(self, name: str, params: Mapping[str, Any], returns: Sequence[float], *, window_id: str = "full") -> Dict[str, Any]:
        """Count a variant AND test it. Returns its significance statistics."""
        st = significance(returns, ci=False)
        self._tl.record(name, SP.params_hash(params), window_id, st["mean"] if st["mean"] is not None else 0.0, pvalue=st["p_one_sided"], n_obs=st["n"])
        self._sync()
        st["variant"] = name
        return st

    def record_rejected(self, name: str, params: Mapping[str, Any], reason: str) -> None:
        """Count a variant dropped before/without testing (bad data, a failed sanity check). It enlarges the family with p = 1."""
        if not isinstance(reason, str) or not reason.strip():
            raise HarnessError("a rejected variant needs a reason")
        self._tl.record(name, SP.params_hash({**dict(params), "_rejected": reason}), "rejected", 0.0, pvalue=None, n_obs=0)
        self._sync()

    def _refuse(self, *_a: Any, **_k: Any) -> None:
        raise HarnessError("the variant counter cannot be reset, cleared or reduced")

    reset = clear = remove = pop = decrement = _refuse

    def selection(self, *, alpha: float = 0.05, method: str = "holm") -> Dict[str, Any]:
        """The best variant judged against the WHOLE family: adjusted over n_variants (carried and unreported ones count as p = 1)."""
        if method not in ("holm", "bh"):
            raise HarnessError("method must be holm or bh")
        pv = self._tl.pvalues()
        if not pv:
            raise HarnessError("no variants recorded")
        n = self.n_variants
        adj = SP.adjust_pvalues(pv, method, alpha=alpha, n_tested=n)
        k = min(range(len(pv)), key=lambda i: (pv[i], i))
        t = self._tl.trials[k]
        return {"best_variant": t.strategy_id, "best_params_hash": t.params_hash, "best_p_unadjusted": pv[k], "best_p_adjusted": adj["adjusted"][k],
                "significant_after_adjustment": bool(adj["reject"][k]), "n_variants_tested": n, "method": method, "alpha": alpha,
                "n_rejected_of_family": adj["n_rejected"]}


# ---------------------------------------------------------------- significance (reuses ESS + bootstrap + t tail)

def significance(returns: Sequence[float], *, alpha: float = 0.05, ci: bool = True) -> Dict[str, Any]:
    """One-sided test of mean > 0 with an ESS-adjusted standard error. Serial dependence shrinks the sample: ``ess`` is the conservative ESS from
    evidence_stats, ``independent_observations`` is its floor. Fewer than 8 points, ESS < 3, or zero variance: p = 1 (no claim)."""
    xs = [float(x) for x in returns]
    n = len(xs)
    out: Dict[str, Any] = {"n": n, "mean": None, "sd": None, "ess": None, "independent_observations": 0, "t": None, "p_one_sided": 1.0, "ci": None}
    if n < ES.MIN_FOR_CI:
        return out
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    ess = ES.conservative_ess(xs) or 1.0
    out.update({"mean": m, "sd": sd, "ess": ess, "independent_observations": int(math.floor(min(float(n), ess)))})
    if sd <= 0 or ess < 3.0:
        return out
    t = m / (sd / math.sqrt(ess))
    out["t"] = t
    out["p_one_sided"] = SP.student_t_sf(t, ess - 1.0)
    if ci:                                                  # the bootstrap is the expensive part: the ledger skips it for every variant, builders ask for the winner's
        out["ci"] = ES.block_bootstrap_ci(xs, alpha=alpha * 2)
    return out


# ---------------------------------------------------------------- costs

_REAL_ZERO_LABELS = ("measured", "venue_stated")
_COST_LABELS = SV.PROVENANCE_LABELS


def net_edge_bps(gross_edge_bps: float, costs: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Net edge per round trip in bps. ``costs`` MUST carry ``fee_bps`` and ``slippage_bps`` as {"value", "provenance"}; may carry others (``funding_bps`` ...).
    Refuses: a missing component, a non-number, a negative, an unlabelled or 'unknown' label, and a ZERO unless labelled measured/venue_stated."""
    if isinstance(gross_edge_bps, bool) or not isinstance(gross_edge_bps, (int, float)) or not math.isfinite(gross_edge_bps):
        raise CostError("gross edge must be a finite number")
    if not isinstance(costs, Mapping):
        raise CostError("costs must be a mapping")
    for req in ("fee_bps", "slippage_bps"):
        if req not in costs:
            raise CostError(f"missing cost component {req}")
    total = 0.0
    labels: Dict[str, str] = {}
    for k, e in costs.items():
        if not isinstance(e, Mapping) or "value" not in e or e.get("provenance") not in _COST_LABELS:
            raise CostError(f"{k}: needs {{value, provenance in {_COST_LABELS}}}")
        v, lab = e["value"], e["provenance"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise CostError(f"{k}: value is not a finite number")
        if v < 0:
            raise CostError(f"{k}: a negative cost (a rebate) must be modelled as its own labelled positive edge, not a negative cost")
        if lab == "unknown":
            raise CostError(f"{k}: provenance 'unknown' - unknown is not zero and not a number")
        if v == 0 and lab not in _REAL_ZERO_LABELS:
            raise CostError(f"{k}: zero cost labelled {lab!r}; a zero must be measured or venue_stated")
        total += float(v)
        labels[k] = lab
    return {"gross_edge_bps": float(gross_edge_bps), "total_cost_bps": total, "net_edge_bps_after_costs": float(gross_edge_bps) - total,
            "unknown_cost_inputs": 0, "labels": labels}


# ---------------------------------------------------------------- data hash

def sha256_files(paths: Sequence[str]) -> str:
    """SHA-256 of a file (one path: exactly the file's digest, as ``data_sha256`` requires); of several, the digest of their sorted 'basename:sha' lines."""
    if not paths:
        raise HarnessError("no input data files named")
    digs = []
    for p in paths:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digs.append((os.path.basename(p), h.hexdigest()))
    if len(digs) == 1:
        return digs[0][1]
    return hashlib.sha256("\n".join(f"{n}:{d}" for n, d in sorted(digs)).encode()).hexdigest()


# ---------------------------------------------------------------- evidence builders

def _guards(universe: PointInTimeUniverse, leak: LeakAudit, split_clean: bool = True) -> Tuple[bool, bool, List[str]]:
    probs = universe.survivorship_problems()
    uses_future = not (leak.passed and split_clean)
    notes = list(probs) + ([] if leak.passed else [f"LOOKAHEAD_DETECTED_AT_ROWS:{list(leak.mismatches)[:5]}"]) + ([] if split_clean else ["SPLIT_LEAKS"])
    return (not probs), uses_future, notes


def _build(stage: str, *, stats: Mapping[str, Any], p_adjusted: float, alpha: float, n_variants: int, universe: PointInTimeUniverse, leak: LeakAudit,
           data_sha256: str, provenance: str, split_clean: bool, extra: Mapping[str, Any]) -> SV.EvidenceItem:
    pit, uses_future, notes = _guards(universe, leak, split_clean)
    n_ind = int(stats["independent_observations"])
    mean = stats["mean"]
    significant = p_adjusted <= alpha and mean is not None and mean > 0
    enough = n_ind >= SV.MIN_OBSERVATIONS[stage]
    guards_ok = pit and not uses_future
    if significant and guards_ok and enough:
        result = SV.PASS
    elif not significant and enough:
        result = SV.FAIL                       # adequately sized and still not significant after correction: a kill
    else:
        result = SV.INCONCLUSIVE               # significant-but-biased, or too little data to say anything either way
    metrics = {"independent_observations": n_ind, "n_variants_tested": int(n_variants), "multiple_testing_adjusted": True,
               "uses_future_information": bool(uses_future), "universe_point_in_time": bool(pit), "alpha": alpha, "p_adjusted": p_adjusted,
               "mean_net_per_settlement_bps": mean, "ess": stats["ess"], "n_raw": stats["n"], "guard_notes": notes}
    metrics.update(dict(extra))
    return SV.EvidenceItem(stage=stage, result=result, provenance=provenance, metrics=metrics, data_sha256=data_sha256)


def historical_evidence(*, stats: Mapping[str, Any], selection: Mapping[str, Any], universe: PointInTimeUniverse, leak: LeakAudit, data_sha256: str,
                        provenance: str = "offline_backtest", extra: Optional[Mapping[str, Any]] = None) -> SV.EvidenceItem:
    """Historical_test evidence for the BEST variant of a family, judged against the whole family (``VariantLedger.selection``)."""
    return _build("historical_test", stats=stats, p_adjusted=selection["best_p_adjusted"], alpha=selection["alpha"], n_variants=selection["n_variants_tested"],
                  universe=universe, leak=leak, data_sha256=data_sha256, provenance=provenance, split_clean=True,
                  extra={"best_variant": selection["best_variant"], **dict(extra or {})})


def oos_evidence(*, oos_returns: Sequence[float], ledger: VariantLedger, universe: PointInTimeUniverse, leak: LeakAudit, folds: Sequence[Fold], n: int,
                 label_horizon: int, embargo: int, data_sha256: str, alpha: float = 0.05, provenance: str = "offline_walk_forward",
                 extra: Optional[Mapping[str, Any]] = None) -> SV.EvidenceItem:
    """Out-of-sample evidence from STITCHED test-window returns. The p-value is adjusted over the ledger's whole family (the selection procedure was one of
    the family's attempts). The folds are re-audited here; a leaking fold makes ``uses_future_information`` True."""
    st = significance(oos_returns)
    clean = bool(folds) and all(SP.audit_split(list(f.train), list(f.test), n, label_horizon, embargo)["clean"] for f in folds)
    m = ledger.n_variants
    adj = SP.adjust_pvalues([st["p_one_sided"]], "holm", alpha=alpha, n_tested=m)["adjusted"][0]
    return _build("oos_test", stats=st, p_adjusted=adj, alpha=alpha, n_variants=m, universe=universe, leak=leak, data_sha256=data_sha256, provenance=provenance,
                  split_clean=clean, extra={"n_folds": len(folds), **dict(extra or {})})


# ---------------------------------------------------------------- the demonstration: cross-sectional funding on 3 hand-picked survivors

FUNDING_FILES = {"BTCUSDT": "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz", "ETHUSDT": "BINANCE_LINEAR_ETH_USDT_FUNDING.csv.gz",
                 "SOLUSDT": "BINANCE_LINEAR_SOL_USDT_FUNDING.csv.gz"}
DEMO_COSTS = {"fee_bps": {"value": 5.5, "provenance": "assumed"}, "slippage_bps": {"value": 2.0, "provenance": "assumed"}}
LOOKBACKS = (3, 9, 21, 63, 189)
HOLDS = (1, 3, 9)


def load_funding(path: str) -> Dict[int, float]:
    """{8h-grid index: funding rate in bps}. Row timestamps carry ms jitter (…001/…005), so they snap to the 8h grid."""
    out: Dict[int, float] = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[int(round(int(r["funding_time_ms"]) / MS_8H))] = float(r["funding_rate"]) * 1e4
    return out


def _variant_returns(grid: List[int], series: Mapping[str, PITSeries], rates: Mapping[str, Mapping[int, float]], lookback: int, hold: int) -> List[float]:
    """Net bps per settlement for: every ``hold`` settlements rank symbols by mean funding over the last ``lookback`` settlements (read ONLY through
    ``asof(previous settlement)``), short the highest, long the lowest. Realised per settlement: funding received on the short minus funding paid on the
    long. Each leg change costs close+open = 2 trades * (fee + slippage). FUNDING LEG ONLY: the price legs are not hedged and not measured here."""
    per_trade = DEMO_COSTS["fee_bps"]["value"] + DEMO_COSTS["slippage_bps"]["value"]
    ret: List[float] = [0.0] * len(grid)
    held: Optional[Tuple[str, str]] = None
    cur: Optional[Tuple[str, str]] = None
    for i, g in enumerate(grid):
        cost = 0.0
        if i % hold == 0 and i >= 1:
            cutoff = grid[i - 1] * MS_8H
            means = {s: sum(v.asof(cutoff).last(lookback)) / max(1, len(v.asof(cutoff).last(lookback))) for s, v in series.items()}
            short, long_ = max(means, key=lambda s: (means[s], s)), min(means, key=lambda s: (means[s], s))
            new = (short, long_)
            if held is None:
                cost = 4 * per_trade
            else:
                cost = (2 * per_trade) * ((held[0] != new[0]) + (held[1] != new[1]))
            held = new
        cur = held
        if cur is None:
            continue
        ret[i] = (rates[cur[0]][g] - rates[cur[1]][g]) - cost
    return ret


def cross_sectional_funding_demo(data_dir: str, *, family: str, alpha: float = 0.05) -> Dict[str, Any]:
    """Run the whole harness over the committed Binance 3-symbol funding history. Returns the evidence items, the selection, the universe audit and numbers.
    The universe is three hand-picked survivors: the harness marks it NOT point in time and the evidence says so."""
    files = {s: os.path.join(data_dir, "real_funding_full", "funding", fn) for s, fn in FUNDING_FILES.items()}
    raw = {s: load_funding(p) for s, p in files.items()}
    grid = sorted(set.intersection(*[set(r) for r in raw.values()]))
    series = {s: PITSeries([g * MS_8H for g in grid], [raw[s][g] for g in grid]) for s in raw}
    universe = PointInTimeUniverse({s: {"listed_ms": grid[0] * MS_8H} for s in raw}, membership_source="hand_picked_survivors", includes_delisted=False)
    vl = VariantLedger(family)
    per_variant: Dict[str, List[float]] = {}
    for L in LOOKBACKS:
        for H in HOLDS:
            name = f"L{L}_H{H}"
            per_variant[name] = _variant_returns(grid, series, raw, L, H)
            vl.record(name, {"lookback": L, "hold": H}, per_variant[name][max(LOOKBACKS):])
    n = len(grid)
    # causal-feature audit of the signal's building block (trailing mean) on one real series
    def trailing_mean(ts: List[int], v: List[float]) -> List[float]:
        return [sum(v[max(0, i - 8):i + 1]) / len(v[max(0, i - 8):i + 1]) for i in range(len(v))]
    leak = detect_lookahead(trailing_mean, [g * MS_8H for g in grid[:600]], [raw["BTCUSDT"][g] for g in grid[:600]])
    sha = sha256_files(sorted(files.values()))
    sel = vl.selection(alpha=alpha, method="holm")
    best = sel["best_variant"]
    hist = historical_evidence(stats=significance(per_variant[best][max(LOOKBACKS):]), selection=sel, universe=universe, leak=leak, data_sha256=sha,
                               extra={"funding_leg_only": True, "gross_mean_note": "price legs unhedged and unmeasured; not a P&L claim"})
    # walk-forward: re-select the best variant on each train window, trade it on the next test window
    h_max, emb = max(HOLDS), max(HOLDS)
    start = max(LOOKBACKS)
    folds = walk_forward_folds(n - start, train_min=1095, test_len=365, label_horizon=h_max, embargo=emb)
    oos: List[float] = []
    chosen: List[str] = []
    for f in folds:
        tr0, tr1 = start + f.train[0], start + f.train[-1] + 1
        pick = max(per_variant, key=lambda k: (sum(per_variant[k][tr0:tr1]) / (tr1 - tr0), k))
        chosen.append(pick)
        oos.extend(per_variant[pick][start + f.test[0]:start + f.test[-1] + 1])
    oos_ev = oos_evidence(oos_returns=oos, ledger=vl, universe=universe, leak=leak, folds=folds, n=n - start, label_horizon=h_max, embargo=emb,
                          data_sha256=sha, alpha=alpha, extra={"selected_per_fold": chosen})
    return {"family": family, "grid_settlements": n, "universe_point_in_time": universe.point_in_time, "survivorship_problems": universe.survivorship_problems(),
            "selection": sel, "historical": hist, "oos": oos_ev, "n_folds": len(folds), "leak_audit": leak,
            "best_gross_mean_bps": sum(per_variant[best][start:]) / (n - start), "oos_n": len(oos), "data_sha256": sha}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Research harness demo. Offline.")
    ap.add_argument("--demo-cross-sectional-funding", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(BOT, "data"))
    a = ap.parse_args(argv)
    if not a.demo_cross_sectional_funding:
        ap.print_help()
        return 2
    spec = json.load(open(os.path.join(BOT, "research", "candidates", "cross_sectional_funding.json"), encoding="utf-8"))
    r = cross_sectional_funding_demo(a.data_dir, family="cli_demo_cross_sectional_funding")
    v = SV.evaluate(spec, [r["historical"], r["oos"]])
    print(json.dumps({"universe_point_in_time": r["universe_point_in_time"], "problems": r["survivorship_problems"], "selection": r["selection"],
                      "historical": {"result": r["historical"].result, "metrics": dict(r["historical"].metrics)},
                      "oos": {"result": r["oos"].result, "metrics": dict(r["oos"].metrics)}, "verdict": v.as_dict()}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
