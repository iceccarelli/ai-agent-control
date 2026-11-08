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
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import evidence_chain as EC  # noqa: E402
import evidence_stats as ES  # noqa: E402
import statistical_power as SP  # noqa: E402
import strategy_verdict as SV  # noqa: E402

GAPS = (
    "the variant family count survives a restart only if the caller gives VariantLedger a path (append-only hash-chained JSONL); a deleted file plus sidecars is caught only with the caller's external anchors=",
    "AsOfView makes the future unreachable through its API; Python cannot stop deliberate reflection into the caller's own full arrays",
    "detect_lookahead catches leaks visible as a difference between full-history and truncated-history features; a leak through a label is for audit_split",
    "PointInTimeUniverse accepts only allow-listed source KINDS and a delisting inside the observed window (or a no-delisting attestation); the harness cannot verify either against the exchange. An attestation is checked only for SHAPE (bound to the observed window, at least two DISTINCT, non-placeholder 64-hex snapshot hashes): the hashes are not verified against any snapshot file, so a fabricated attestation of that shape passes. The evidence says so (universe_pit_basis, universe_pit_verified_against_exchange=False); attestation is a claim, not verification",
    "the selection log is verified by recomputing a best-train-mean selector; a different (documented) selection rule needs its own selector= and is only as honest as that function",
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
#: The ONLY source kinds accepted as point in time (documented kinds that record status history / dated snapshots). Anything else is not PIT.
PIT_SOURCE_KINDS = ("exchange_instruments_info_snapshots_with_status_history", "recorded_universe_snapshots")
_HEX64 = re.compile(r"[0-9a-f]{64}", re.ASCII)
MIN_ATTESTATION_SNAPSHOTS = 2
MIN_DISTINCT_HEX_CHARS = 8


class PointInTimeUniverse:
    """Membership as of timestamps: ``members = {symbol: {"listed_ms": int, "delisted_ms": int | None}}``. Active on [listed_ms, delisted_ms).

    Point in time requires ALL of: an allow-listed ``membership_source`` kind, ``includes_delisted``, an observed window
    [observed_from_ms, observed_to_ms], and either at least one delisting event dated inside that window or an explicit
    ``no_delistings_attestation = {"no_delistings_observed_in_window": True, "window_from_ms", "window_to_ms" (== the observed window),
    "snapshot_sha256": [>= 2 distinct 64-hex, ...]}``. The attestation's SHAPE is checked, not its truth (see GAPS)."""

    def __init__(self, members: Mapping[str, Mapping[str, Any]], *, membership_source: str, includes_delisted: bool,
                 observed_from_ms: Optional[int] = None, observed_to_ms: Optional[int] = None,
                 no_delistings_attestation: Optional[Mapping[str, Any]] = None) -> None:
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
        if (observed_from_ms is None) != (observed_to_ms is None):
            raise HarnessError("observed_from_ms and observed_to_ms must be given together")
        if observed_from_ms is not None:
            _int_ms(observed_from_ms, "observed_from_ms")
            _int_ms(observed_to_ms, "observed_to_ms")
            if observed_to_ms <= observed_from_ms:
                raise HarnessError("observed_to_ms must be after observed_from_ms")
        self.observed_from_ms, self.observed_to_ms = observed_from_ms, observed_to_ms
        self.no_delistings_attestation = dict(no_delistings_attestation) if isinstance(no_delistings_attestation, Mapping) else None
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

    def _attestation_ok(self) -> bool:
        a = self.no_delistings_attestation
        if not a or a.get("no_delistings_observed_in_window") is not True:
            return False
        for k, want in (("window_from_ms", self.observed_from_ms), ("window_to_ms", self.observed_to_ms)):
            v = a.get(k)
            if want is None or isinstance(v, bool) or not isinstance(v, int) or v != want:
                return False                                   # the attestation must be about THIS observed window, not any window
        h = a.get("snapshot_sha256")
        if not isinstance(h, (list, tuple)) or len(h) < MIN_ATTESTATION_SNAPSHOTS:
            return False
        if not all(isinstance(x, str) and _HEX64.fullmatch(x) and len(set(x)) >= MIN_DISTINCT_HEX_CHARS for x in h):
            return False                                       # shape only: a real SHA-256 is not '00..0' or 'cdcd..cd'
        return len(set(h)) == len(h)                           # one snapshot repeated is one snapshot

    @property
    def pit_basis(self) -> str:
        """What the point-in-time claim rests on. None of these bases is verified against the exchange."""
        if self.survivorship_problems():
            return "not_point_in_time"
        inside = [s for s, (_lo, hi) in self._m.items() if hi is not None and self.observed_from_ms <= hi <= self.observed_to_ms]
        return "self_declared_delisting_event_in_window" if inside else "attested_no_delistings_unverified"

    def survivorship_problems(self) -> List[str]:
        """Why this universe is NOT point in time. Empty = point in time (as far as its own source says)."""
        out: List[str] = []
        if self.membership_source in NON_PIT_SOURCES:
            out.append(f"MEMBERSHIP_SOURCE_IS_SURVIVOR_BASED:{self.membership_source}")
        if self.membership_source not in PIT_SOURCE_KINDS:
            out.append(f"MEMBERSHIP_SOURCE_NOT_ALLOWLISTED:{self.membership_source}")
        if not self.includes_delisted:
            out.append("SOURCE_DOES_NOT_RECORD_DELISTED_SYMBOLS")
        if self.observed_from_ms is None:
            out.append("OBSERVED_WINDOW_MISSING")
        else:
            inside = [s for s, (_lo, hi) in self._m.items() if hi is not None and self.observed_from_ms <= hi <= self.observed_to_ms]
            if inside and self.no_delistings_attestation is not None:
                out.append("ATTESTATION_CONTRADICTS_DELISTING_IN_WINDOW")
            elif not inside and not self._attestation_ok():
                out.append("NO_DELISTING_EVENT_IN_OBSERVED_WINDOW_AND_NO_VALID_ATTESTATION")
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

_FOLD_NONCE = os.urandom(16).hex()          # per process: only walk_forward_folds can stamp a Fold


def _fold_tag(train: Sequence[int], test: Sequence[int], n: int, horizon: int, embargo: int) -> str:
    return hashlib.sha256(json.dumps([_FOLD_NONCE, list(train), list(test), n, horizon, embargo]).encode()).hexdigest()


@dataclass(frozen=True)
class Fold:
    """A train/test split. Only ``walk_forward_folds`` stamps (n, label_horizon, embargo, tag): ``oos_evidence`` refuses a Fold that is hand-built or whose
    indices were edited afterwards, so a zero-embargo adjacent split cannot be re-audited against a horizon/embargo the caller chooses."""
    train: Tuple[int, ...]
    test: Tuple[int, ...]
    n: int = -1
    label_horizon: int = -1
    embargo: int = -1
    tag: str = field(default="", repr=False)

    def authentic(self) -> bool:
        return bool(self.tag) and self.tag == _fold_tag(self.train, self.test, self.n, self.label_horizon, self.embargo)


def walk_forward_folds(n: int, train_min: int, test_len: int, label_horizon: int, embargo: int) -> List[Fold]:
    """Expanding walk-forward (statistical_power.walk_forward), then AUDITED: every fold clean per ``audit_split`` and test blocks pairwise disjoint and
    chronological. ``embargo`` must be >= max(1, label_horizon): an embargo shorter than the label horizon lets training labels see test values."""
    if embargo < max(1, label_horizon):
        raise HarnessError(f"embargo ({embargo}) must be at least max(1, label_horizon={label_horizon})")
    folds = [Fold(tuple(a), tuple(b), n, label_horizon, embargo, _fold_tag(a, b, n, label_horizon, embargo))
             for a, b in SP.walk_forward(n, train_min, test_len, label_horizon, embargo)]
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
_PATH_HIGH_WATER: Dict[str, int] = {}


def normalise_family(family: Any) -> str:
    """The ONE key a family is counted under: NFKC, whitespace collapsed (incl. non-breaking), casefolded. Empty is refused."""
    if not isinstance(family, str):
        raise HarnessError("a variant ledger needs a family name")
    key = " ".join(unicodedata.normalize("NFKC", family).split()).casefold()
    if not key:
        raise HarnessError("a variant ledger needs a family name")
    return key


class VariantLedger:
    """Counts EVERY attempted variant of a research family, including ones rejected before they were ever tested (recorded with p = 1).

    Monotone and unresettable: there is no reset/clear/remove; the hash-chained ``TrialLedger`` underneath detects tampering; and a NEW VariantLedger for
    a family already seen in this process starts from that family's high-water mark. The family key is normalised ('fam', 'fam ', 'FAM' are one family).

    With ``path`` the ledger is persisted as an append-only, hash-chained JSONL file (``evidence_chain``): it is verified on load (a truncated, edited,
    reordered or torn file raises HarnessError), replayed, and every later record is appended, so the count survives a restart and only grows.
    ``anchors`` (``[{"n", "head"}]`` kept OUTSIDE the file) additionally catches a file+sidecar truncated together."""

    def __init__(self, family: str, *, path: Optional[str] = None, anchors: Optional[Sequence[Mapping[str, Any]]] = None) -> None:
        self.family = normalise_family(family)
        self.path = os.path.abspath(path) if path is not None else None
        self._tl = SP.TrialLedger()
        self._meta: List[Dict[str, Any]] = []          # per trial: what the ledger recorded ABOUT the series (digest, ess, independent obs, declared horizon)
        self._persisted = 0
        if self.path is None:
            self._carried = _FAMILY_HIGH_WATER.get(self.family, 0)
        else:
            self._carried = 0
            self._load(anchors)
        self._sync()

    def _load(self, anchors: Optional[Sequence[Mapping[str, Any]]]) -> None:
        if not (os.path.exists(self.path) or anchors):
            return
        v = EC.verify(self.path, anchors)
        if not v["ok"] or not v["fully_chained"]:
            raise HarnessError(f"variant ledger file failed verification: {[i['code'] for i in v['issues']] or 'unchained tail'}")
        lines, quarantined = EC.read_lines(self.path)
        if quarantined:
            raise HarnessError("variant ledger file has a torn or unparseable final line")
        for i, row in enumerate(EC.rows_of(lines)):
            if not isinstance(row, dict) or row.get("kind") != "variant" or row.get("seq") != i:
                raise HarnessError(f"variant ledger row {i} is malformed or out of sequence")
            if row.get("family") != self.family:
                raise HarnessError(f"variant ledger row {i} belongs to family {row.get('family')!r}, not {self.family!r}")
            try:
                self._tl.record(row["strategy_id"], row["params_hash"], row["window_id"], row["statistic"], pvalue=row["pvalue"], n_obs=row["n_obs"])
            except (KeyError, SP.StatisticalPowerError) as exc:
                raise HarnessError(f"variant ledger row {i} is not a valid trial: {exc}") from exc
            self._meta.append({k: row.get(k) for k in ("returns_sha256", "ess", "independent_observations", "horizon")})
        seen = _PATH_HIGH_WATER.get(self.path, 0)
        if self._tl.n_trials < seen:
            raise HarnessError(f"variant ledger file holds {self._tl.n_trials} variants but this process already saw {seen}: truncated")
        self._persisted = self._tl.n_trials

    def _persist(self) -> None:
        if self.path is None:
            return
        t = self._tl.trials[-1]
        EC.append(self.path, {"kind": "variant", "seq": self._persisted, "family": self.family, "strategy_id": t.strategy_id, "params_hash": t.params_hash,
                              "window_id": t.window_id, "statistic": t.statistic, "pvalue": t.pvalue, "n_obs": t.n_obs, **self._meta[-1]})
        self._persisted += 1
        _PATH_HIGH_WATER[self.path] = max(_PATH_HIGH_WATER.get(self.path, 0), self._persisted)

    def _sync(self) -> None:
        total = self._carried + self._tl.n_trials
        if total > _FAMILY_HIGH_WATER.get(self.family, 0):
            _FAMILY_HIGH_WATER[self.family] = total
        if self.path is not None:
            _PATH_HIGH_WATER[self.path] = max(_PATH_HIGH_WATER.get(self.path, 0), self._tl.n_trials)

    @property
    def n_variants(self) -> int:
        """Attempted variants in this family. Never below the family's high-water mark, whatever object asks."""
        self._tl.verify()
        return max(self._carried + self._tl.n_trials, _FAMILY_HIGH_WATER.get(self.family, 0))

    @property
    def carried_prior_variants(self) -> int:
        return self._carried

    def variant_names(self) -> Tuple[str, ...]:
        """Names of the variants that were actually TESTED (rejected ones are counted but cannot be selected)."""
        self._tl.verify()
        return tuple(sorted({t.strategy_id for t in self._tl.trials if t.window_id != "rejected"}))

    def best_index(self) -> int:
        pv = self._tl.pvalues()
        if not pv:
            raise HarnessError("no variants recorded")
        return min(range(len(pv)), key=lambda i: (pv[i], i))

    def best_trial(self) -> Any:
        return self._tl.trials[self.best_index()]

    def trial_meta(self, index: int) -> Mapping[str, Any]:
        """What the ledger itself recorded about trial ``index``'s series (None fields: recorded before this was tracked, or a rejected variant)."""
        return dict(self._meta[index]) if 0 <= index < len(self._meta) else {}

    def variant_horizon(self, name: str) -> Optional[int]:
        """The label horizon (rows a label looks forward = the variant's hold) DECLARED when the variant was recorded; the largest if recorded repeatedly."""
        hs = [m.get("horizon") for t, m in zip(self._tl.trials, self._meta) if t.strategy_id == name and t.window_id != "rejected"]
        hs = [h for h in hs if isinstance(h, int)]
        return max(hs) if hs else None

    def returns_digests(self, name: str) -> frozenset:
        return frozenset(m.get("returns_sha256") for t, m in zip(self._tl.trials, self._meta) if t.strategy_id == name and m.get("returns_sha256"))

    def anchor(self) -> SP.LedgerAnchor:
        return self._tl.anchor()

    def verify(self, anchor: Optional[SP.LedgerAnchor] = None) -> bool:
        return self._tl.verify(anchor)

    def record(self, name: str, params: Mapping[str, Any], returns: Sequence[float], *, window_id: str = "full",
               horizon: Optional[int] = None) -> Dict[str, Any]:
        """Count a variant AND test it. Returns its significance statistics. The ledger remembers a digest of the exact series, its ESS / independent
        observations (evidence takes those from here, never from the caller) and the variant's declared label ``horizon`` (rows a label looks forward;
        ``oos_evidence`` needs it to size the purge/embargo)."""
        if horizon is not None and (isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1):
            raise HarnessError(f"horizon must be a positive integer number of rows, got {horizon!r}")
        st = significance(returns, ci=False)
        self._tl.record(name, SP.params_hash(params), window_id, st["mean"] if st["mean"] is not None else 0.0, pvalue=st["p_one_sided"], n_obs=st["n"])
        self._meta.append({"returns_sha256": series_digest(returns), "ess": st["ess"], "independent_observations": st["independent_observations"], "horizon": horizon})
        self._persist()
        self._sync()
        st["variant"] = name
        return st

    def record_rejected(self, name: str, params: Mapping[str, Any], reason: str) -> None:
        """Count a variant dropped before/without testing (bad data, a failed sanity check). It enlarges the family with p = 1."""
        if not isinstance(reason, str) or not reason.strip():
            raise HarnessError("a rejected variant needs a reason")
        self._tl.record(name, SP.params_hash({**dict(params), "_rejected": reason}), "rejected", 0.0, pvalue=None, n_obs=0)
        self._meta.append({"returns_sha256": None, "ess": None, "independent_observations": None, "horizon": None})
        self._persist()
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


def series_digest(returns: Sequence[float]) -> str:
    """Digest of an exact return series (repr of each float: round-trips, so the same series gives the same digest on every run)."""
    return hashlib.sha256(",".join(repr(float(x)) for x in returns).encode()).hexdigest()


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


def _stamp(spec: Mapping[str, Any]) -> Dict[str, str]:
    """candidate_id + spec_digest of the exact specification this evidence is about (``counted`` refuses a PASS that does not match its spec)."""
    if not isinstance(spec, Mapping) or not isinstance(spec.get("candidate_id"), str) or not spec["candidate_id"]:
        raise HarnessError("evidence must be built against a candidate spec with a candidate_id")
    return {"candidate_id": spec["candidate_id"], "spec_digest": SV.spec_digest(spec)}


def _build(stage: str, *, stats: Mapping[str, Any], p_adjusted: float, alpha: float, n_variants: int, universe: PointInTimeUniverse, leak: LeakAudit,
           data_sha256: str, provenance: str, split_clean: bool, extra: Mapping[str, Any], spec: Mapping[str, Any]) -> SV.EvidenceItem:
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
               "universe_pit_basis": universe.pit_basis, "universe_pit_verified_against_exchange": False,
               "mean_net_per_settlement_bps": mean, "ess": stats["ess"], "n_raw": stats["n"], "guard_notes": notes}
    metrics.update(dict(extra))
    return SV.EvidenceItem(stage=stage, result=result, provenance=provenance, metrics=metrics, data_sha256=data_sha256, **_stamp(spec))


def _require_family(spec: Mapping[str, Any], ledger: VariantLedger) -> None:
    """The ledger must count the SAME family the candidate registered (``multiple_testing_family``, else its candidate_id, as ``register_candidate`` does).
    Otherwise a best-of-N winner can be re-recorded in a fresh, differently named ledger with n_variants = 1."""
    want = normalise_family(spec.get("multiple_testing_family") or spec.get("candidate_id"))
    if ledger.family != want:
        raise HarnessError(f"the variant ledger counts family {ledger.family!r} but the candidate's multiple_testing_family is {want!r}")


def historical_evidence(*, stats: Mapping[str, Any], ledger: VariantLedger, spec: Mapping[str, Any], universe: PointInTimeUniverse, leak: LeakAudit,
                        data_sha256: str, alpha: float = 0.05, method: str = "holm", provenance: str = "offline_backtest",
                        extra: Optional[Mapping[str, Any]] = None) -> SV.EvidenceItem:
    """Historical_test evidence for the BEST variant of a family, judged against the whole family. ``n_variants_tested`` and the adjusted p come from the
    LEDGER (never a free integer), and ``stats`` must be the ledger's best variant's own statistics."""
    _require_family(spec, ledger)
    selection = ledger.selection(alpha=alpha, method=method)
    best = ledger.best_trial()
    if stats.get("n") != best.n_obs or stats.get("mean") is None or best.statistic is None or \
            not math.isclose(float(stats["mean"]), float(best.statistic), rel_tol=1e-9, abs_tol=1e-12):
        raise HarnessError("stats are not the ledger's best variant's statistics (n/mean differ from the recorded trial)")
    meta = ledger.trial_meta(ledger.best_index())
    if meta.get("ess") is None or meta.get("independent_observations") is None:
        raise HarnessError("the ledger holds no recorded ESS / independent observations for its best variant: re-record it so the evidence can be checked against the ledger")
    if stats.get("independent_observations") != meta["independent_observations"] or stats.get("ess") is None or \
            not math.isclose(float(stats["ess"]), float(meta["ess"]), rel_tol=1e-9, abs_tol=1e-12):
        raise HarnessError(f"stats claim independent_observations={stats.get('independent_observations')} ess={stats.get('ess')} but the ledger recorded "
                           f"{meta['independent_observations']} / {meta['ess']}")
    return _build("historical_test", stats=stats, p_adjusted=selection["best_p_adjusted"], alpha=selection["alpha"], n_variants=selection["n_variants_tested"],
                  universe=universe, leak=leak, data_sha256=data_sha256, provenance=provenance, split_clean=True, spec=spec,
                  extra={"best_variant": selection["best_variant"], **dict(extra or {})})


class SelectionLogError(HarnessError):
    """The per-fold selection log is missing, malformed, or not a function of each fold's train window."""


def train_only_best(train_returns: Mapping[str, Sequence[float]]) -> str:
    """The reference selector: highest mean return over the TRAIN window, ties to the greater name."""
    return max(train_returns, key=lambda k: (sum(train_returns[k]) / len(train_returns[k]), k))


def verify_selection_log(*, selection_log: Any, variant_returns: Mapping[str, Sequence[float]], folds: Sequence[Fold], oos_returns: Sequence[float],
                         tested_variants: Sequence[str], index_offset: int = 0,
                         selector: Callable[[Mapping[str, Sequence[float]]], str] = train_only_best,
                         recorded_digests: Optional[Mapping[str, Any]] = None) -> None:
    """Each fold's chosen variant must be what ``selector`` returns from the TRAIN returns alone, the log's train window must be the fold's, every chosen
    variant must be a counted (tested) variant, and ``oos_returns`` must be exactly the chosen variants' TEST-window returns, in order. Raises otherwise."""
    if not isinstance(selection_log, (list, tuple)) or len(selection_log) != len(folds) or not folds:
        raise SelectionLogError("selection_log must hold exactly one entry per fold")
    if not isinstance(variant_returns, Mapping) or not variant_returns:
        raise SelectionLogError("variant_returns (every variant's full return series) is required to recompute the selection")
    lengths = {len(v) for v in variant_returns.values()}
    if len(lengths) != 1:
        raise SelectionLogError("variant return series differ in length")
    rows = lengths.pop()
    unknown_names = [k for k in variant_returns if k not in set(tested_variants)]
    if unknown_names:
        raise SelectionLogError(f"variants {unknown_names} are not counted in the variant ledger")
    if recorded_digests is not None:
        for name, series in variant_returns.items():
            if series_digest(series[index_offset:]) not in recorded_digests.get(name, ()):
                raise SelectionLogError(f"variant {name!r}: the supplied return series is not the series the ledger recorded for it")
    expected: List[float] = []
    for k, (f, e) in enumerate(zip(folds, selection_log)):
        if not isinstance(e, Mapping) or e.get("fold") != k or not isinstance(e.get("chosen"), str) or e.get("chosen") not in variant_returns:
            raise SelectionLogError(f"fold {k}: malformed entry or unknown variant")
        if list(e.get("train_window") or ()) != [f.train[0], f.train[-1]]:
            raise SelectionLogError(f"fold {k}: logged train window {e.get('train_window')!r} is not the fold's [{f.train[0]}, {f.train[-1]}]")
        idx_tr = [index_offset + t for t in f.train]
        idx_te = [index_offset + t for t in f.test]
        if min(idx_tr) < 0 or max(idx_tr + idx_te) >= rows:
            raise SelectionLogError(f"fold {k}: indices outside the return series")
        recomputed = selector({n: [r[i] for i in idx_tr] for n, r in variant_returns.items()})
        if recomputed != e["chosen"]:
            raise SelectionLogError(f"fold {k}: chosen {e['chosen']!r} is not what the train window alone selects ({recomputed!r})")
        expected.extend(variant_returns[e["chosen"]][i] for i in idx_te)
    if list(oos_returns) != expected:
        raise SelectionLogError("oos_returns are not the chosen variants' test-window returns")


def oos_evidence(*, oos_returns: Sequence[float], ledger: VariantLedger, spec: Mapping[str, Any], universe: PointInTimeUniverse, leak: LeakAudit,
                 folds: Sequence[Fold], data_sha256: str, selection_log: Sequence[Mapping[str, Any]],
                 variant_returns: Mapping[str, Sequence[float]], index_offset: int = 0,
                 selector: Callable[[Mapping[str, Sequence[float]]], str] = train_only_best, alpha: float = 0.05,
                 provenance: str = "offline_walk_forward", extra: Optional[Mapping[str, Any]] = None) -> SV.EvidenceItem:
    """Out-of-sample evidence from STITCHED test-window returns. The p-value is adjusted over the ledger's whole family (which must be the spec's).

    The folds must be ones ``walk_forward_folds`` produced (a hand-built or edited ``Fold`` is refused). Label horizon, embargo and n are NOT caller
    numbers: they are read from the folds' stamp and must cover every variant's DECLARED horizon (``VariantLedger.record(..., horizon=)``) with
    ``embargo >= max(1, horizon)``. Each variant's supplied returns must be the series the ledger recorded, and the ``selection_log`` is RECOMPUTED from
    the train windows (SelectionLogError otherwise)."""
    _require_family(spec, ledger)
    if not folds or not all(isinstance(f, Fold) and f.authentic() for f in folds):
        raise HarnessError("folds must be exactly those produced by walk_forward_folds (a hand-built or edited Fold is refused)")
    params = {(f.n, f.label_horizon, f.embargo) for f in folds}
    if len(params) != 1:
        raise HarnessError("folds were produced with different n / label_horizon / embargo")
    n, label_horizon, embargo = params.pop()
    if embargo < max(1, label_horizon):
        raise HarnessError(f"embargo ({embargo}) must be at least max(1, label_horizon={label_horizon})")
    if not isinstance(variant_returns, Mapping) or not variant_returns:
        raise SelectionLogError("variant_returns (every variant's full return series) is required to recompute the selection")
    declared: Dict[str, int] = {}
    for name in variant_returns:
        h = ledger.variant_horizon(name)
        if h is None:
            raise HarnessError(f"variant {name!r} has no declared label horizon in the ledger: record it with horizon= (its hold, in rows)")
        declared[name] = h
    if label_horizon < max(declared.values()):
        raise HarnessError(f"the folds purge a label horizon of {label_horizon} rows but a variant declares a horizon of {max(declared.values())}")
    rows = len(next(iter(variant_returns.values())))
    if rows - index_offset != n:
        raise HarnessError(f"the folds index {n} rows but the variant returns hold {rows - index_offset} after index_offset")
    verify_selection_log(selection_log=selection_log, variant_returns=variant_returns, folds=folds, oos_returns=oos_returns,
                         tested_variants=ledger.variant_names(), index_offset=index_offset, selector=selector,
                         recorded_digests={name: ledger.returns_digests(name) for name in variant_returns})
    st = significance(oos_returns)
    clean = all(SP.audit_split(list(f.train), list(f.test), n, label_horizon, embargo)["clean"] for f in folds)
    m = ledger.n_variants
    adj = SP.adjust_pvalues([st["p_one_sided"]], "holm", alpha=alpha, n_tested=m)["adjusted"][0]
    return _build("oos_test", stats=st, p_adjusted=adj, alpha=alpha, n_variants=m, universe=universe, leak=leak, data_sha256=data_sha256, provenance=provenance,
                  split_clean=clean, spec=spec, extra={"n_folds": len(folds), "selection_log_verified": True, "label_horizon": label_horizon, "embargo": embargo,
                                                       **dict(extra or {})})


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


def _load_spec(name: str) -> Dict[str, Any]:
    with open(os.path.join(BOT, "research", "candidates", name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def cross_sectional_funding_demo(data_dir: str, *, family: Optional[str] = None, alpha: float = 0.05, spec: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Run the whole harness over the committed Binance 3-symbol funding history. Returns the evidence items, the selection, the universe audit and numbers.
    The universe is three hand-picked survivors: the harness marks it NOT point in time and the evidence says so."""
    files = {s: os.path.join(data_dir, "real_funding_full", "funding", fn) for s, fn in FUNDING_FILES.items()}
    raw = {s: load_funding(p) for s, p in files.items()}
    grid = sorted(set.intersection(*[set(r) for r in raw.values()]))
    series = {s: PITSeries([g * MS_8H for g in grid], [raw[s][g] for g in grid]) for s in raw}
    universe = PointInTimeUniverse({s: {"listed_ms": grid[0] * MS_8H} for s in raw}, membership_source="hand_picked_survivors", includes_delisted=False)
    spec = spec if spec is not None else _load_spec("cross_sectional_funding")
    family = family if family is not None else str(spec.get("multiple_testing_family") or spec["candidate_id"])
    vl = VariantLedger(family)             # the evidence builders refuse a ledger whose family is not the spec's multiple_testing_family
    per_variant: Dict[str, List[float]] = {}
    for L in LOOKBACKS:
        for H in HOLDS:
            name = f"L{L}_H{H}"
            per_variant[name] = _variant_returns(grid, series, raw, L, H)
            vl.record(name, {"lookback": L, "hold": H}, per_variant[name][max(LOOKBACKS):], horizon=H)
    n = len(grid)
    # causal-feature audit of the signal's building block (trailing mean) on one real series
    def trailing_mean(ts: List[int], v: List[float]) -> List[float]:
        return [sum(v[max(0, i - 8):i + 1]) / len(v[max(0, i - 8):i + 1]) for i in range(len(v))]
    leak = detect_lookahead(trailing_mean, [g * MS_8H for g in grid[:600]], [raw["BTCUSDT"][g] for g in grid[:600]])
    sha = sha256_files(sorted(files.values()))
    sel = vl.selection(alpha=alpha, method="holm")
    best = sel["best_variant"]
    hist = historical_evidence(stats=significance(per_variant[best][max(LOOKBACKS):]), ledger=vl, spec=spec, universe=universe, leak=leak, data_sha256=sha, alpha=alpha,
                               extra={"funding_leg_only": True, "gross_mean_note": "price legs unhedged and unmeasured; not a P&L claim"})
    # walk-forward: re-select the best variant on each train window, trade it on the next test window
    h_max, emb = max(HOLDS), max(HOLDS)
    start = max(LOOKBACKS)
    folds = walk_forward_folds(n - start, train_min=1095, test_len=365, label_horizon=h_max, embargo=emb)
    oos: List[float] = []
    chosen: List[str] = []
    log: List[Dict[str, Any]] = []
    for k, f in enumerate(folds):
        tr0, tr1 = start + f.train[0], start + f.train[-1] + 1
        pick = max(per_variant, key=lambda k: (sum(per_variant[k][tr0:tr1]) / (tr1 - tr0), k))
        chosen.append(pick)
        log.append({"fold": k, "chosen": pick, "train_window": [f.train[0], f.train[-1]]})
        oos.extend(per_variant[pick][start + f.test[0]:start + f.test[-1] + 1])
    oos_ev = oos_evidence(oos_returns=oos, ledger=vl, spec=spec, universe=universe, leak=leak, folds=folds,
                          data_sha256=sha, alpha=alpha, selection_log=log, variant_returns={k: v[:] for k, v in per_variant.items()}, index_offset=start,
                          extra={"selected_per_fold": chosen})
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
    spec = _load_spec("cross_sectional_funding")
    r = cross_sectional_funding_demo(a.data_dir, spec=spec)
    v = SV.evaluate(spec, [r["historical"], r["oos"]])
    print(json.dumps({"universe_point_in_time": r["universe_point_in_time"], "problems": r["survivorship_problems"], "selection": r["selection"],
                      "historical": {"result": r["historical"].result, "metrics": dict(r["historical"].metrics)},
                      "oos": {"result": r["oos"].result, "metrics": dict(r["oos"].metrics)}, "verdict": v.as_dict()}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
