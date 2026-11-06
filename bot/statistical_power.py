"""Statistical power, multiple-testing and pre-registration framework. Pure standard library.

The question this answers before any strategy is believed: HOW MUCH evidence would it take to tell a
real net edge from noise, given how variable and how serially dependent the observations are, how many
things were tried, and what a false discovery costs. Nothing here touches a venue, a file, the clock or
an unseeded random number generator.

  required_observations / achieved_power / minimum_detectable_edge
        Sample size, power and detectable edge for a mean with standard deviation ``sd``, with the
        variance inflated by the design effect of serial dependence (n_eff = n / deff; AR(1): deff =
        (1+rho)/(1-rho); or n_eff = ess_ratio * n; or deff = 1 + 2*sum(rho_k)), and with the significance
        level tightened for the number of hypotheses (bonferroni, holm, bh, none). ``method="normal"``
        is the closed form z test; ``method="t"`` uses the exact Student-t critical value and the exact
        noncentral-t power (numerical quadrature), with df = n_eff - 1.
  adjust_pvalues, TrialLedger, selection_adjusted_best, expected_max_null, deflated_sharpe
        Multiple testing. ``TrialLedger`` is append-only and hash-chained and counts EVERY trial, losers
        included; ``ledger.n_trials`` is what an adjustment uses, never the number of winners.
  false_discovery_cost, posterior_prob_true
        Bayes: P(real edge | significant) from the prior, alpha and power, and the expected value of
        acting on a significant result. p < 0.05 with low prior odds is still probably false.
  purged_embargoed_splits, walk_forward, holdout_split, audit_split
        Chronological splits whose training labels cannot overlap, or sit next to, the test block.
  EvaluationPlan, plan_digest, evaluate_under_plan
        Pre-registration. A frozen plan fixes strategy, parameters, evaluation window, metric, alpha,
        power, minimum edge, hypothesis count and seed BEFORE the data are read; the SHA-256 digest of
        its canonical encoding is what a caller pins. The evaluation refuses data outside or not
        covering the declared window, refuses a digest that does not match, records itself in a ledger,
        and returns one of three verdicts: INSUFFICIENT_POWER, NO_EDGE_DETECTED,
        EDGE_SUPPORTED_AFTER_ADJUSTMENT.

HONEST LABELLING. Every result dict carries ``classes``: CALCULATED (arithmetic or an exact identity
given the inputs), ESTIMATED (a statistic or approximation that could be wrong), ASSUMED (an input or a
modelling choice supplied by the caller). An unknown input is never replaced by a number: it raises
``UnknownInputError`` or the result is INSUFFICIENT with the reason.

KNOWN LIMITS (also stated in the results that depend on them)
  * NO PRODUCTION CALLER USES ``evaluate_under_plan`` yet; it is a framework, not a gate. Two limits found by independent review:
    (a) the window check proves the data reach both ENDS of the declared window, not that the INTERIOR is complete, so
    selectively dropping observations inside it is not refused (supply and check a declared cadence or count yourself, and
    treat the evidence chain as the guard on what rows exist); (b) the p-value uses an ESS-adjusted t with se = sd / sqrt(ESS), which
    over-rejects on short persistent series at the adjusted level (measured: 0.76 % against 0.31 % nominal at AR(0.8), n = 200,
    16 hypotheses, Holm), so require a larger ESS than ``MIN_ESS_FOR_SUPPORT`` before relying on it.
  * Sample sizes are conditional on the supplied sd and dependence. Understate either and the study is
    underpowered; the framework cannot know.
  * The AR(1) design effect (1+rho)/(1-rho) is the large-n value (slightly conservative at finite n); a
    negative rho is floored at deff = 1 (an observed negative lag-1 autocorrelation is more often a
    seasonal cycle than a free lunch).
  * Holm and BH sizing uses the strictest step (alpha/m, or k*alpha/m for k expected true edges): which
    rank the true edge will take is not known in advance.
  * ``expected_max_null`` and the deflated Sharpe ratio treat the N trials as independent nulls. Trials
    that are near-copies of one another make the true expected maximum lower, so this is conservative.
  * The plan digest binds the declared window, parameters and numbers. It cannot bind the CODE of the
    statistic function: the result records the function's name beside the declared metric and warns on a
    mismatch, but a determined analyst can still swap the function.
  * The verdict's p-value is an ESS-adjusted t test of the MEAN of the supplied values (statistic_fn is
    ``mean_statistic``) or, for any other statistic, a normal test whose standard error is read from
    the block-bootstrap interval. The ESS is the deliberately conservative one from ``evidence_stats``,
    so realised power at the stated n sits below the formula unless the sample was sized from the
    measured ESS ratio.
"""
from __future__ import annotations

import hashlib
import json
import math
import numbers
import statistics
from collections.abc import Mapping as _MappingABC
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import evidence_stats as _es

CALCULATED, ESTIMATED, ASSUMED = "CALCULATED", "ESTIMATED", "ASSUMED"
CLASSES = (CALCULATED, ESTIMATED, ASSUMED)

INSUFFICIENT_POWER = "INSUFFICIENT_POWER"
NO_EDGE_DETECTED = "NO_EDGE_DETECTED"
EDGE_SUPPORTED_AFTER_ADJUSTMENT = "EDGE_SUPPORTED_AFTER_ADJUSTMENT"
VERDICTS = (INSUFFICIENT_POWER, NO_EDGE_DETECTED, EDGE_SUPPORTED_AFTER_ADJUSTMENT)

ADJUST_METHODS = ("holm", "bonferroni", "bh", "none")
POWER_METHODS = ("normal", "t")
COVERAGE_SLACK_SPACINGS = 2          # first/last observation may sit this many median spacings inside the window
MIN_ESS_FOR_SUPPORT = 20.0           # below this many effective observations no edge is called supported: the ESS
                                     # estimate and the t approximation are unreliable (persistent short series over-reject)
META_KEYS = frozenset({"classes", "reading", "status", "reason", "warnings", "assumptions"})

_N = NormalDist()
_EULER_GAMMA = 0.5772156649015329
_MAX_REQUIRED_N = 1e12


# ------------------------------------------------------------------------------------------------
# errors
# ------------------------------------------------------------------------------------------------
class StatisticalPowerError(ValueError):
    """An input is invalid (wrong type, out of range, contradictory)."""


class UnknownInputError(StatisticalPowerError):
    """An input that must be stated was missing (None). Unknown inputs are never defaulted to a number."""


class LedgerError(RuntimeError):
    """The trial ledger is append-only and refuses the operation."""


class LedgerTamperError(LedgerError):
    """The ledger no longer matches its own hash chain or an external anchor: it was shortened or edited."""


class PlanError(StatisticalPowerError):
    """The evaluation plan or the data supplied under it is refused."""


class PlanDigestMismatch(PlanError):
    """The plan's digest differs from the digest the caller pinned in advance."""


class WindowViolation(PlanError):
    """The supplied data extend outside the declared evaluation window."""


class WindowCoverageError(WindowViolation):
    """The supplied data do not cover the declared window (a truncated or cherry-picked sub-window)."""


class DataIntegrityError(PlanError):
    """The supplied observations are malformed, unordered, duplicated or non-finite."""


# ------------------------------------------------------------------------------------------------
# small validators and helpers
# ------------------------------------------------------------------------------------------------
def _num(x: Any, name: str) -> float:
    if x is None:
        raise UnknownInputError(f"{name} is unknown (None): it is never defaulted to a number")
    if isinstance(x, bool) or not isinstance(x, numbers.Real):
        raise StatisticalPowerError(f"{name} must be a real number, got {type(x).__name__}")
    v = float(x)
    if not math.isfinite(v):
        raise StatisticalPowerError(f"{name} must be finite, got {x!r}")
    return v


def _pos(x: Any, name: str) -> float:
    v = _num(x, name)
    if v <= 0.0:
        raise StatisticalPowerError(f"{name} must be > 0, got {x!r}")
    return v


def _nonneg(x: Any, name: str) -> float:
    v = _num(x, name)
    if v < 0.0:
        raise StatisticalPowerError(f"{name} must be >= 0, got {x!r}")
    return v


def _open01(x: Any, name: str) -> float:
    v = _num(x, name)
    if not 0.0 < v < 1.0:
        raise StatisticalPowerError(f"{name} must be strictly between 0 and 1, got {x!r}")
    return v


def _int_min(x: Any, name: str, lo: int) -> int:
    if x is None:
        raise UnknownInputError(f"{name} is unknown (None): it is never defaulted to a number")
    if isinstance(x, bool) or not isinstance(x, numbers.Integral):
        raise StatisticalPowerError(f"{name} must be an integer, got {type(x).__name__}")
    v = int(x)
    if v < lo:
        raise StatisticalPowerError(f"{name} must be >= {lo}, got {x!r}")
    return v


def _choice(x: Any, name: str, allowed: Sequence[str]) -> str:
    if x is None:
        raise UnknownInputError(f"{name} is unknown (None)")
    if x not in allowed:
        raise StatisticalPowerError(f"{name} must be one of {tuple(allowed)}, got {x!r}")
    return x


def _zu(a: float) -> float:
    """z with P(Z > z) = a, for a in (0, 1); exact in the far tail."""
    return 0.0 - _N.inv_cdf(a)


def _labels(calculated: Iterable[str] = (), estimated: Iterable[str] = (), assumed: Iterable[str] = ()) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in calculated:
        out[k] = CALCULATED
    for k in estimated:
        out[k] = ESTIMATED
    for k in assumed:
        out[k] = ASSUMED
    return out


def _auto_classes(out: Mapping[str, Any], calculated: Iterable[str], inputs_class: str = ASSUMED,
                  input_keys: Iterable[str] = ("sd", "dependence")) -> Dict[str, str]:
    """Label EVERY non-meta key: CALCULATED if named, ``inputs_class`` for the measured-or-assumed inputs
    (sd, dependence), ASSUMED for any other caller-supplied input. Nothing is left unlabelled."""
    calc, inp = set(calculated), set(input_keys)
    return {k: (CALCULATED if k in calc else inputs_class if k in inp else ASSUMED)
            for k in out if k not in META_KEYS}


def _canon_number(x: Any) -> Any:
    """Numerically equal declared values must hash the same: an integral float becomes an int."""
    if isinstance(x, bool):
        return x
    if isinstance(x, float) and x.is_integer() and abs(x) < 2 ** 53:
        return int(x)
    return x


def _canon_tree(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _canon_tree(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_canon_tree(v) for v in x]
    return _canon_number(x)


def _canon(obj: Any) -> str:
    return json.dumps(_canon_tree(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------
# Student t (exact) and noncentral t (quadrature)
# ------------------------------------------------------------------------------------------------
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 20000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < 3e-16:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


_T_AS_NORMAL_DF = 1e6


def student_t_sf(t: float, df: float) -> float:
    """P(T > t) for Student's t with ``df`` degrees of freedom (any real df > 0). Accurate in the tails."""
    df = _pos(df, "df")
    t = float(t)
    if t == 0.0:
        return 0.5
    if df >= _T_AS_NORMAL_DF:
        return _N.cdf(-t)
    x = df / (df + t * t)
    tail = 0.5 * _betainc(df / 2.0, 0.5, x)
    return tail if t > 0 else 1.0 - tail


def _t_pdf(t: float, df: float) -> float:
    return math.exp(math.lgamma((df + 1.0) / 2.0) - math.lgamma(df / 2.0) - 0.5 * math.log(df * math.pi)
                    - (df + 1.0) / 2.0 * math.log1p(t * t / df))


def student_t_isf(q: float, df: float) -> float:
    """The t with P(T > t) = q, for q in (0, 1)."""
    q = _open01(q, "q")
    df = _pos(df, "df")
    if q == 0.5:
        return 0.0
    if q > 0.5:
        return -student_t_isf(1.0 - q, df)
    z = _zu(q)
    if df >= _T_AS_NORMAL_DF:
        return z
    guess = z + (z ** 3 + z) / (4.0 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96.0 * df * df)
    lo, hi = 0.0, max(2.0 * guess, 1.0)
    for _ in range(400):
        if student_t_sf(hi, df) <= q:
            break
        lo, hi = hi, hi * 2.0
    x = min(max(guess, lo), hi)
    for _ in range(200):
        g = student_t_sf(x, df) - q
        if g > 0:
            lo = x
        else:
            hi = x
        pdf = _t_pdf(x, df)
        xn = x + g / pdf if pdf > 0 else 0.5 * (lo + hi)
        if not lo < xn < hi:
            xn = 0.5 * (lo + hi)
        if abs(xn - x) <= 1e-13 * max(1.0, abs(xn)):
            return xn
        x = xn
    return x


_QUAD_STEPS = 400


def noncentral_t_sf(tcrit: float, df: float, delta: float) -> float:
    """P(T' > tcrit) for the noncentral t with ``df`` degrees of freedom and noncentrality ``delta``.

    T' = (Z + delta) / S with S = sqrt(chi2_df / df). So P(T' > t) = E_S[ P(Z > t*S - delta) ], integrated
    over the chi density of S by composite Simpson on a range that holds all but ~1e-20 of its mass and
    renormalised. Exact to about 1e-9 for df >= 2; no table or approximation formula."""
    tcrit, delta, df = float(tcrit), float(delta), _pos(df, "df")
    if df >= _T_AS_NORMAL_DF:
        return _N.cdf(delta - tcrit)
    sig = 1.0 / math.sqrt(2.0 * df)
    lo = max(0.0, 1.0 - 10.0 * sig)
    hi = 1.0 + 10.0 * sig
    steps = _QUAD_STEPS
    h = (hi - lo) / steps
    lc = math.log(2.0) + (df / 2.0) * math.log(df / 2.0) - math.lgamma(df / 2.0)
    num = den = 0.0
    for i in range(steps + 1):
        s = lo + i * h
        if s <= 0.0:
            continue
        f = math.exp(lc + (df - 1.0) * math.log(s) - df * s * s / 2.0)
        w = 1.0 if i in (0, steps) else (4.0 if i % 2 else 2.0)
        num += w * f * _N.cdf(delta - tcrit * s)
        den += w * f
    return num / den if den > 0 else 0.0


# ------------------------------------------------------------------------------------------------
# serial dependence: design effect
# ------------------------------------------------------------------------------------------------
def _dependence(rho: Any, ess_ratio: Any, rho_sum: Any, rho_k: Any) -> Tuple[float, bool, str, Any]:
    """-> (design_effect >= 1, floored, kind, value). Exactly one spelling of the dependence is required."""
    given = [(k, v) for k, v in (("rho", rho), ("ess_ratio", ess_ratio), ("rho_sum", rho_sum), ("rho_k", rho_k))
             if v is not None]
    if not given:
        raise UnknownInputError("serial dependence is unknown: pass exactly one of rho= (AR(1) coefficient; "
                                "0.0 states independence), ess_ratio= (ESS/n), rho_sum= or rho_k= (autocorrelations)")
    if len(given) > 1:
        raise StatisticalPowerError("pass exactly one of rho, ess_ratio, rho_sum, rho_k; got "
                                    + ", ".join(k for k, _ in given))
    kind, val = given[0]
    if kind == "rho":
        r = _num(val, "rho")
        if not -1.0 < r < 1.0:
            raise StatisticalPowerError(f"rho must be inside (-1, 1) for a stationary AR(1), got {val!r}")
        raw, value = (1.0 + r) / (1.0 - r), r
    elif kind == "ess_ratio":
        e = _num(val, "ess_ratio")
        if not 0.0 < e <= 1.0:
            raise StatisticalPowerError(f"ess_ratio is ESS/n and must be in (0, 1], got {val!r}")
        raw, value = 1.0 / e, e
    elif kind == "rho_sum":
        s = _num(val, "rho_sum")
        raw, value = 1.0 + 2.0 * s, s
    else:
        if isinstance(val, (str, bytes)) or not hasattr(val, "__iter__"):
            raise StatisticalPowerError("rho_k must be a sequence of autocorrelations")
        ks = [_num(v, "rho_k element") for v in val]
        if not ks:
            raise UnknownInputError("rho_k is empty: the autocorrelations are unknown")
        if any(abs(v) > 1.0 for v in ks):
            raise StatisticalPowerError("an autocorrelation must be within [-1, 1]")
        raw, value = 1.0 + 2.0 * sum(ks), tuple(ks)
    deff = max(1.0, raw)
    return deff, raw < 1.0, kind, value


def design_effect(*, rho: Optional[float] = None, ess_ratio: Optional[float] = None,
                  rho_sum: Optional[float] = None, rho_k: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """Variance inflation of a sample mean from serial dependence: n_eff = n / design_effect."""
    deff, floored, kind, value = _dependence(rho, ess_ratio, rho_sum, rho_k)
    return {
        "status": "OK", "design_effect": deff, "ess_ratio": 1.0 / deff, "floored_at_one": floored,
        "dependence": {"kind": kind, "value": value},
        "reading": (f"design effect {deff:.4g}: each observation carries {1.0 / deff:.4g} independent draws"
                    + ("; a design effect below 1 was floored at 1 (no credit for negative dependence)" if floored else "")),
        "classes": _labels(calculated=("design_effect", "ess_ratio", "floored_at_one"), assumed=("dependence",)),
    }


# ------------------------------------------------------------------------------------------------
# significance level adjusted for the number of hypotheses, and the power function
# ------------------------------------------------------------------------------------------------
def _alpha_used(alpha: float, m: int, adjust: str, bh_n_true: Any) -> Tuple[float, str]:
    if adjust == "none":
        return alpha, "no multiplicity adjustment"
    if adjust in ("bonferroni", "holm"):
        note = ("Bonferroni alpha/m" if adjust == "bonferroni" else
                "Holm's strictest step alpha/m (which rank the true edge takes is unknown in advance, so the "
                "step-down gain is not relied on)")
        return alpha / m, note
    if bh_n_true is None:
        raise UnknownInputError("adjust='bh' sizing depends on how many true edges are expected: pass bh_n_true "
                                "(1 is the conservative single-winner case, giving alpha/m)")
    k = _int_min(bh_n_true, "bh_n_true", 1)
    if k > m:
        raise StatisticalPowerError("bh_n_true cannot exceed n_hypotheses")
    return min(alpha, k * alpha / m), (f"Benjamini-Hochberg rank-{k} threshold {k}*alpha/m (controls the false "
                                       "discovery rate, not the family-wise error)")


def _power(n_eff: float, sd: float, edge: float, a_used: float, two_sided: bool, method: str) -> float:
    lam = edge / sd * math.sqrt(n_eff)
    tail = a_used / 2.0 if two_sided else a_used
    if method == "normal":
        zc = _zu(tail)
        p = _N.cdf(lam - zc)
        if two_sided:
            p += _N.cdf(-lam - zc)
        return min(1.0, p)
    df = n_eff - 1.0
    tc = student_t_isf(tail, df)
    p = noncentral_t_sf(tc, df, lam)
    if two_sided:
        p += noncentral_t_sf(tc, df, -lam)
    return min(1.0, p)


def _solve_increasing(g: Callable[[float], float], lo: float, hi: float) -> float:
    """Root of an increasing g on [lo, hi] with g(lo) < 0 <= g(hi) (Illinois false position)."""
    a, b, ga, gb = lo, hi, g(lo), g(hi)
    side = 0
    c = b
    for _ in range(200):
        c = (a * gb - b * ga) / (gb - ga)
        gc = g(c)
        if abs(gc) < 1e-12 or abs(b - a) <= 1e-12 * max(1.0, abs(b)):
            return c
        if gc * gb > 0:
            b, gb = c, gc
            if side == -1:
                ga *= 0.5
            side = -1
        else:
            a, ga = c, gc
            if side == 1:
                gb *= 0.5
            side = 1
    return c


class _Unreachable(Exception):
    pass


def _required_neff(sd: float, edge: float, a_used: float, power: float, two_sided: bool, method: str) -> float:
    tail = a_used / 2.0 if two_sided else a_used
    if power <= tail:
        raise StatisticalPowerError("power must exceed the (adjusted) significance level: a test whose power is "
                                    "below its size detects nothing")
    n0 = ((_zu(tail) + _N.inv_cdf(power)) * sd / edge) ** 2             # closed form, one-sided and the main term of two-sided
    if n0 > _MAX_REQUIRED_N:
        raise _Unreachable
    if method == "normal" and not two_sided:
        return n0

    def g(n: float) -> float:
        return _power(n, sd, edge, a_used, two_sided, method) - power

    lo = 3.0 if method == "t" else max(1e-9, n0 * 0.25)
    if g(lo) >= 0:
        return lo
    tight = max(lo, n0 * 0.5)
    if tight > lo and g(tight) < 0:
        lo = tight
    hi = max(n0 * 1.5 + 10.0, lo * 2.0)
    while g(hi) < 0:
        hi *= 2.0
        if hi > _MAX_REQUIRED_N:
            raise _Unreachable
    return _solve_increasing(g, lo, hi)


def _ceil(x: float) -> int:
    return int(math.ceil(x - 1e-7))


def required_observations(sd: float, min_edge: float, *, alpha: float, power: float, n_hypotheses: int,
                          adjust: str = "holm", rho: Optional[float] = None, ess_ratio: Optional[float] = None,
                          rho_sum: Optional[float] = None, rho_k: Optional[Sequence[float]] = None,
                          two_sided: bool = False, bh_n_true: Optional[int] = None, method: str = "normal",
                          inputs_class: str = ASSUMED) -> Dict[str, Any]:
    """Observations needed to detect a mean of ``min_edge`` given per-observation standard deviation ``sd``.

    Returns the required EFFECTIVE n (independent draws) and the required RAW n (= effective * design
    effect), the adjusted alpha actually used, and a plain-language ``reading``. Exactly one of ``rho``
    (AR(1) coefficient; 0.0 = independent), ``ess_ratio`` (ESS / n), ``rho_sum`` or ``rho_k`` states the
    serial dependence. ``alpha`` is the one-sided level (two-sided if ``two_sided``) BEFORE adjustment for
    ``n_hypotheses``, which must count every strategy/parameter combination examined (use
    ``TrialLedger.n_trials``). ``adjust`` is 'holm' | 'bonferroni' | 'bh' | 'none'; 'bh' also needs
    ``bh_n_true``. ``method`` is 'normal' (known sd, closed form) or 't' (exact Student-t / noncentral t)."""
    sd_ = _pos(sd, "sd")
    edge = _pos(min_edge, "min_edge")
    alpha_ = _open01(alpha, "alpha")
    power_ = _open01(power, "power")
    m = _int_min(n_hypotheses, "n_hypotheses", 1)
    adjust = _choice(adjust, "adjust", ADJUST_METHODS)
    method = _choice(method, "method", POWER_METHODS)
    inputs_class = _choice(inputs_class, "inputs_class", CLASSES)
    two_sided = bool(two_sided)
    deff, floored, dep_kind, dep_value = _dependence(rho, ess_ratio, rho_sum, rho_k)
    a_used, a_note = _alpha_used(alpha_, m, adjust, bh_n_true)
    sides = "two-sided" if two_sided else "one-sided"
    base = {
        "method": method, "design_effect": deff, "design_effect_floored": floored,
        "dependence": {"kind": dep_kind, "value": dep_value}, "alpha_nominal": alpha_, "alpha_used": a_used,
        "alpha_note": a_note, "adjust": adjust, "n_hypotheses": m, "two_sided": two_sided,
        "power_target": power_, "sd": sd_, "min_edge": edge, "standardized_edge": edge / sd_,
    }
    calc = ("required_effective_n", "required_effective_n_exact", "required_raw_n", "required_raw_n_exact",
            "design_effect", "design_effect_floored", "alpha_used", "alpha_note", "standardized_edge")
    try:
        n_eff = _required_neff(sd_, edge, a_used, power_, two_sided, method)
    except _Unreachable:
        out = dict(base)
        out.update({"status": "INSUFFICIENT",
                    "reason": f"the required effective n exceeds {_MAX_REQUIRED_N:g}: this edge is not detectable in practice"})
        out["reading"] = out["reason"]
        out["classes"] = _auto_classes(out, calc, inputs_class)
        return out
    n_raw = n_eff * deff
    eff_i, raw_i = _ceil(n_eff), _ceil(n_raw)
    approx = " (exact Student-t critical value and exact noncentral-t power)" if method == "t" else " (normal approximation, known sd)"
    reading = (f"To detect a mean net edge of {edge:g} (sd {sd_:g}, standardised {edge / sd_:.3g}) with {sides} "
               f"alpha {alpha_:g} adjusted to {a_used:.3g} for {m} hypotheses ({adjust}) and power {power_:g} you need "
               f"{eff_i} effective observations = {raw_i} raw observations at design effect {deff:.3g}{approx}. "
               "These counts are only as good as the sd and the dependence supplied: understate either and the "
               "study is underpowered.")
    base.update({
        "status": "OK", "required_effective_n": eff_i, "required_effective_n_exact": n_eff,
        "required_raw_n": raw_i, "required_raw_n_exact": n_raw, "reading": reading,
        "assumptions": [f"serial dependence enters only through the design effect {deff:.4g}",
                        "the sample mean is approximately normal at the effective n",
                        a_note] + (["a design effect below 1 was floored at 1"] if floored else []),
    })
    base["classes"] = _auto_classes(base, calc, inputs_class)
    return base


def _resolve_n_eff(n: Any, n_kind: Any, deff_args: Tuple[Any, Any, Any, Any]) -> Tuple[float, Optional[float], Optional[float], bool, Any]:
    n_ = _pos(n, "n")
    kind = _choice(n_kind, "n_kind", ("raw", "effective"))
    if kind == "effective":
        if any(a is not None for a in deff_args):
            raise StatisticalPowerError("n_kind='effective' already accounts for dependence: do not also pass "
                                        "rho / ess_ratio / rho_sum / rho_k")
        return n_, None, None, False, None
    deff, floored, dep_kind, dep_value = _dependence(*deff_args)
    return n_ / deff, deff, n_, floored, {"kind": dep_kind, "value": dep_value}


def achieved_power(n: float, sd: float, edge: float, *, n_kind: str, alpha: float, n_hypotheses: int,
                   adjust: str = "holm", rho: Optional[float] = None, ess_ratio: Optional[float] = None,
                   rho_sum: Optional[float] = None, rho_k: Optional[Sequence[float]] = None,
                   two_sided: bool = False, bh_n_true: Optional[int] = None, method: str = "normal",
                   inputs_class: str = ASSUMED) -> Dict[str, Any]:
    """Power to detect a true mean of ``edge`` with ``n`` observations (``n_kind`` is 'raw' or 'effective').

    The inverse question of ``required_observations``. With ``n_kind='raw'`` the serial dependence must be
    stated; with 'effective' it is already inside ``n``."""
    sd_, edge_ = _pos(sd, "sd"), _pos(edge, "edge")
    alpha_ = _open01(alpha, "alpha")
    m = _int_min(n_hypotheses, "n_hypotheses", 1)
    adjust = _choice(adjust, "adjust", ADJUST_METHODS)
    method = _choice(method, "method", POWER_METHODS)
    inputs_class = _choice(inputs_class, "inputs_class", CLASSES)
    n_eff, deff, n_raw, floored, dep = _resolve_n_eff(n, n_kind, (rho, ess_ratio, rho_sum, rho_k))
    a_used, a_note = _alpha_used(alpha_, m, adjust, bh_n_true)
    out: Dict[str, Any] = {"method": method, "n_effective": n_eff, "n_raw": n_raw, "design_effect": deff,
                           "dependence": dep, "alpha_nominal": alpha_, "alpha_used": a_used, "alpha_note": a_note,
                           "adjust": adjust, "n_hypotheses": m, "two_sided": bool(two_sided), "sd": sd_, "edge": edge_}
    if method == "t" and n_eff < 3.0:
        out.update({"status": "INSUFFICIENT", "power": None,
                    "reason": "fewer than 3 effective observations: the t test has no usable degrees of freedom"})
        out["reading"] = out["reason"]
        out["classes"] = _auto_classes(out, ("power", "n_effective", "alpha_used", "alpha_note", "design_effect"), inputs_class)
        return out
    p = _power(n_eff, sd_, edge_, a_used, bool(two_sided), method)
    out.update({
        "status": "OK", "power": p,
        "reading": (f"With {n_eff:.4g} effective observations the chance of detecting a true mean of {edge_:g} "
                    f"(sd {sd_:g}) at adjusted alpha {a_used:.3g} is {p:.3f}. Conditional on the sd and dependence supplied."),
    })
    out["classes"] = _auto_classes(out, ("power", "n_effective", "alpha_used", "alpha_note", "design_effect"), inputs_class)
    return out


def minimum_detectable_edge(n: float, sd: float, *, n_kind: str, alpha: float, power: float, n_hypotheses: int,
                            adjust: str = "holm", rho: Optional[float] = None, ess_ratio: Optional[float] = None,
                            rho_sum: Optional[float] = None, rho_k: Optional[Sequence[float]] = None,
                            two_sided: bool = False, bh_n_true: Optional[int] = None, method: str = "normal",
                            inputs_class: str = ASSUMED) -> Dict[str, Any]:
    """The smallest true mean detectable with probability ``power`` from ``n`` observations."""
    sd_ = _pos(sd, "sd")
    alpha_ = _open01(alpha, "alpha")
    power_ = _open01(power, "power")
    m = _int_min(n_hypotheses, "n_hypotheses", 1)
    adjust = _choice(adjust, "adjust", ADJUST_METHODS)
    method = _choice(method, "method", POWER_METHODS)
    inputs_class = _choice(inputs_class, "inputs_class", CLASSES)
    two_sided = bool(two_sided)
    n_eff, deff, n_raw, floored, dep = _resolve_n_eff(n, n_kind, (rho, ess_ratio, rho_sum, rho_k))
    a_used, a_note = _alpha_used(alpha_, m, adjust, bh_n_true)
    tail = a_used / 2.0 if two_sided else a_used
    if power_ <= tail:
        raise StatisticalPowerError("power must exceed the (adjusted) significance level")
    out: Dict[str, Any] = {"method": method, "n_effective": n_eff, "n_raw": n_raw, "design_effect": deff,
                           "dependence": dep, "alpha_nominal": alpha_, "alpha_used": a_used, "alpha_note": a_note,
                           "adjust": adjust, "n_hypotheses": m, "two_sided": two_sided, "power_target": power_,
                           "sd": sd_}
    if method == "t" and n_eff < 3.0:
        out.update({"status": "INSUFFICIENT", "minimum_detectable_edge": None,
                    "reason": "fewer than 3 effective observations: the t test has no usable degrees of freedom"})
        out["reading"] = out["reason"]
        out["classes"] = _auto_classes(out, ("minimum_detectable_edge", "n_effective", "alpha_used", "alpha_note",
                                             "design_effect"), inputs_class)
        return out
    d0 = (_zu(tail) + _N.inv_cdf(power_)) * sd_ / math.sqrt(n_eff)
    if method == "normal" and not two_sided:
        mde = d0
    else:
        def g(e: float) -> float:
            return _power(n_eff, sd_, e, a_used, two_sided, method) - power_
        lo = d0 * 0.25
        while g(lo) >= 0 and lo > 1e-300:
            lo *= 0.25
        hi = d0 * 2.0
        while g(hi) < 0:
            hi *= 2.0
        mde = _solve_increasing(g, lo, hi)
    out.update({
        "status": "OK", "minimum_detectable_edge": mde, "minimum_detectable_edge_in_sd": mde / sd_,
        "reading": (f"With {n_eff:.4g} effective observations only a true mean of at least {mde:.4g} "
                    f"({mde / sd_:.3g} sd) is detected with probability {power_:g} at adjusted alpha {a_used:.3g}. "
                    "Smaller edges are not distinguishable from noise at this sample size."),
    })
    out["classes"] = _auto_classes(out, ("minimum_detectable_edge", "minimum_detectable_edge_in_sd", "n_effective",
                                         "alpha_used", "alpha_note", "design_effect"), inputs_class)
    return out


# ------------------------------------------------------------------------------------------------
# multiple testing
# ------------------------------------------------------------------------------------------------
def _pvalue_list(pvals: Any) -> List[float]:
    if pvals is None:
        raise UnknownInputError("pvals is unknown (None)")
    if isinstance(pvals, (str, bytes)) or not hasattr(pvals, "__iter__"):
        raise StatisticalPowerError("pvals must be a sequence of p-values")
    ps = [_num(p, "p-value") for p in pvals]
    if not ps:
        raise UnknownInputError("pvals is empty: there is nothing to adjust")
    if any(p < 0.0 or p > 1.0 for p in ps):
        raise StatisticalPowerError("a p-value must lie in [0, 1]")
    return ps


def _adjust_core(ps: List[float], m: int, method: str) -> List[float]:
    """Adjusted p-values for the supplied ``ps`` inside a family of ``m`` tests; the m - len(ps) unreported
    tests are counted with p = 1 (they can never reject and only enlarge the family)."""
    k = len(ps)
    if method == "none":
        return list(ps)
    if method == "bonferroni":
        return [min(1.0, m * p) for p in ps]
    order = sorted(range(k), key=lambda i: ps[i])
    adj = [0.0] * k
    if method == "holm":
        run = 0.0
        for rank, i in enumerate(order):                 # rank is 0-based: multiplier m - rank
            run = max(run, min(1.0, (m - rank) * ps[i]))
            adj[i] = run
        return adj
    run = 1.0                                            # bh: step-up from the largest, capped by the padded p = 1 tests
    for rank in range(k - 1, -1, -1):
        i = order[rank]
        run = min(run, min(1.0, m * ps[i] / (rank + 1)))
        adj[i] = run
    return adj


def adjust_pvalues(pvals: Sequence[float], method: str = "holm", *, alpha: float,
                   n_tested: Optional[int] = None) -> Dict[str, Any]:
    """Bonferroni / Holm / Benjamini-Hochberg adjusted p-values and rejection decisions at ``alpha``.

    ``n_tested`` is the size of the whole family: EVERY strategy and parameter combination examined, not only
    the winners whose p-values are supplied. The unreported ones are counted with p = 1. If it is None the
    family is exactly the supplied list, which is only right when that list is complete."""
    method = _choice(method, "method", ADJUST_METHODS)
    alpha_ = _open01(alpha, "alpha")
    ps = _pvalue_list(pvals)
    k = len(ps)
    m = k if n_tested is None else _int_min(n_tested, "n_tested", 1)
    if m < k:
        raise StatisticalPowerError(f"n_tested ({m}) is smaller than the number of p-values supplied ({k})")
    adj = _adjust_core(ps, m, method)
    reject = [a <= alpha_ for a in adj]
    ctrl = {"bonferroni": "family-wise error", "holm": "family-wise error", "bh": "false discovery rate",
            "none": "nothing (unadjusted)"}[method]
    return {
        "status": "OK", "method": method, "alpha": alpha_, "n_tested": m, "n_supplied": k, "n_padded_with_p1": m - k,
        "adjusted": adj, "reject": reject, "n_rejected": sum(reject), "controls": ctrl,
        "reading": (f"{method} at alpha {alpha_:g} over a family of {m} tests ({m - k} unreported counted with p=1): "
                    f"{sum(reject)} of {k} supplied rejected; controls {ctrl}."),
        "classes": _labels(calculated=("adjusted", "reject", "n_rejected", "n_tested", "n_supplied",
                                       "n_padded_with_p1", "controls", "method"), assumed=("alpha",)),
    }


class LedgerAnchor(NamedTuple):
    """An external receipt of a ledger's state: its length and head hash. Keep it OUTSIDE the ledger."""
    n: int
    head: str


@dataclass(frozen=True)
class Trial:
    index: int
    strategy_id: str
    params_hash: str
    window_id: str
    statistic: float
    pvalue: Optional[float]
    n_obs: Optional[int]
    plan_digest: Optional[str]
    prev_hash: str
    entry_hash: str


_GENESIS = _sha("statistical_power.TrialLedger.v1")


def _trial_hash(prev: str, index: int, strategy_id: str, params_hash: str, window_id: str, statistic: float,
                pvalue: Optional[float], n_obs: Optional[int], plan_digest: Optional[str]) -> str:
    return _sha(prev + "|" + _canon([index, strategy_id, params_hash, window_id, repr(statistic), repr(pvalue),
                                     n_obs, plan_digest]))


class TrialLedger:
    """Append-only, in-memory, hash-chained record of EVERY (strategy, parameters, window, statistic) examined.

    ``n_trials`` counts every record, winners and losers alike, and is what every adjustment uses. There is
    no deletion or edit API (each attempt raises ``LedgerError``). Python cannot forbid reaching into private
    attributes, so shortening the underlying list is made DETECTABLE instead: ``n_trials`` and ``verify`` raise
    ``LedgerTamperError`` when the entries no longer match the running count and head hash, and ``verify``
    given a previously exported ``anchor()`` also catches a ledger rebuilt shorter. Keep anchors outside the
    ledger (in the plan or the report) for that check to mean anything."""

    def __init__(self) -> None:
        self._entries: List[Trial] = []
        self._count = 0
        self._head = _GENESIS

    # -- writing ------------------------------------------------------------------------------
    def record(self, strategy_id: str, params_hash: str, window_id: str, statistic: float, *,
               pvalue: Optional[float] = None, n_obs: Optional[int] = None,
               plan_digest: Optional[str] = None) -> Trial:
        """Append one examined trial. ``pvalue`` is the one-sided p-value for a positive edge, or None if it was
        not computed (an uncomputed p-value is treated as 1 in adjustments, never as significant)."""
        self._check_light()
        for name, val in (("strategy_id", strategy_id), ("params_hash", params_hash), ("window_id", window_id)):
            if not isinstance(val, str) or not val:
                raise StatisticalPowerError(f"{name} must be a non-empty string")
        stat = _num(statistic, "statistic")
        pv = None if pvalue is None else _num(pvalue, "pvalue")
        if pv is not None and not 0.0 <= pv <= 1.0:
            raise StatisticalPowerError("pvalue must lie in [0, 1]")
        no = None if n_obs is None else _int_min(n_obs, "n_obs", 0)
        if plan_digest is not None and not isinstance(plan_digest, str):
            raise StatisticalPowerError("plan_digest must be a string")
        idx = self._count
        h = _trial_hash(self._head, idx, strategy_id, params_hash, window_id, stat, pv, no, plan_digest)
        trial = Trial(idx, strategy_id, params_hash, window_id, stat, pv, no, plan_digest, self._head, h)
        self._entries.append(trial)
        self._count += 1
        self._head = h
        return trial

    def _refuse(self, *_a: Any, **_k: Any) -> None:
        raise LedgerError("the trial ledger is append-only: entries cannot be removed, replaced or reordered; "
                          "use record() to add a trial")

    __delitem__ = __setitem__ = pop = remove = clear = insert = extend = append = sort = reverse = _refuse

    # -- reading ------------------------------------------------------------------------------
    def _check_light(self) -> None:
        if len(self._entries) != self._count:
            raise LedgerTamperError(f"ledger holds {len(self._entries)} entries but recorded {self._count}: "
                                    "entries were removed or inserted outside record()")
        if self._entries and self._entries[-1].entry_hash != self._head:
            raise LedgerTamperError("the last ledger entry does not match the head hash")

    @property
    def n_trials(self) -> int:
        self._check_light()
        return self._count

    @property
    def trials(self) -> Tuple[Trial, ...]:
        self._check_light()
        return tuple(self._entries)

    @property
    def head(self) -> str:
        self._check_light()
        return self._head

    @property
    def n_distinct(self) -> int:
        """Distinct (strategy_id, params_hash, window_id): a repeated look is another trial in ``n_trials``."""
        return len({(t.strategy_id, t.params_hash, t.window_id) for t in self.trials})

    def __len__(self) -> int:
        return self.n_trials

    def __iter__(self) -> Iterator[Trial]:
        return iter(self.trials)

    def __getitem__(self, i: int) -> Trial:
        return self.trials[i]

    def anchor(self) -> LedgerAnchor:
        return LedgerAnchor(self.n_trials, self._head)

    def verify(self, anchor: Optional[LedgerAnchor] = None) -> bool:
        """Recompute the whole hash chain. Raises ``LedgerTamperError`` on any mismatch, and, given an
        ``anchor``, if the ledger is shorter than it was or no longer extends it."""
        self._check_light()
        prev = _GENESIS
        at_anchor: Optional[str] = prev if anchor is not None and anchor.n == 0 else None
        for i, t in enumerate(self._entries):
            if t.index != i or t.prev_hash != prev:
                raise LedgerTamperError(f"ledger entry {i} is out of order or its link was altered")
            h = _trial_hash(prev, i, t.strategy_id, t.params_hash, t.window_id, t.statistic, t.pvalue, t.n_obs,
                            t.plan_digest)
            if h != t.entry_hash:
                raise LedgerTamperError(f"ledger entry {i} was edited after it was recorded")
            prev = h
            if anchor is not None and i + 1 == anchor.n:
                at_anchor = prev
        if prev != self._head:
            raise LedgerTamperError("the ledger head does not match its entries")
        if anchor is not None:
            if len(self._entries) < anchor.n:
                raise LedgerTamperError(f"the ledger was shortened: it held {anchor.n} trials when anchored and now "
                                        f"holds {len(self._entries)}")
            if at_anchor != anchor.head:
                raise LedgerTamperError("the ledger no longer extends the anchored history")
        return True

    def pvalues(self) -> List[float]:
        """Every trial's p-value, with a trial whose p-value was not computed counted as 1.0."""
        return [1.0 if t.pvalue is None else t.pvalue for t in self.trials]


def params_hash(parameters: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical encoding of a parameter mapping (key order and 1 vs 1.0 do not matter)."""
    return _sha(_canon(_thaw(_freeze(parameters))))


def _gauss_max_exact(n: int) -> float:
    """E[max of n iid N(0,1)] by Simpson integration of x * n * phi(x) * Phi(x)^(n-1)."""
    lo, hi, steps = -10.0, 10.0 + 2.0 * math.log(max(n, 2)), 20000
    h = (hi - lo) / steps
    tot = 0.0
    for i in range(steps + 1):
        x = lo + i * h
        f = x * n * _N.pdf(x) * _N.cdf(x) ** (n - 1)
        tot += f * (1.0 if i in (0, steps) else (4.0 if i % 2 else 2.0))
    return tot * h / 3.0


def expected_max_null(n_trials: int, *, null_sd: float = 1.0, method: str = "approx") -> float:
    """Expected maximum of ``n_trials`` independent null statistics with mean 0 and sd ``null_sd``.

    ``approx`` is the Bailey / Lopez de Prado form (1-g) Phi^-1(1-1/N) + g Phi^-1(1-1/(N e)) with g the
    Euler-Mascheroni constant; ``exact`` integrates the order-statistic density. One trial has expected
    maximum 0. A winner must beat THIS, not zero: it is what the best of N pure-noise candidates looks like."""
    n = _int_min(n_trials, "n_trials", 1)
    sd = _pos(null_sd, "null_sd")
    method = _choice(method, "method", ("approx", "exact"))
    if n == 1:
        return 0.0
    if method == "exact":
        return sd * _gauss_max_exact(n)
    return sd * ((1.0 - _EULER_GAMMA) * _N.inv_cdf(1.0 - 1.0 / n) + _EULER_GAMMA * _N.inv_cdf(1.0 - 1.0 / (n * math.e)))


def deflated_sharpe(sharpe: float, *, n_obs: int, n_trials: int, sharpe_variance: float, skew: float,
                    kurtosis: float) -> Dict[str, Any]:
    """Deflated Sharpe ratio (Bailey and Lopez de Prado, 2014).

    ``sharpe`` is the selected candidate's PER-PERIOD Sharpe ratio over ``n_obs`` periods; ``sharpe_variance``
    the variance of the per-period Sharpe ratios across ALL ``n_trials`` trials (winners and losers);
    ``kurtosis`` is the raw kurtosis (3 for a normal). SR0 = sqrt(V) * E[max of N standard normals] is the
    Sharpe ratio pure luck is expected to reach; the result ``dsr`` is the probability that the true Sharpe
    ratio exceeds SR0 (a value near 1 is evidence, near 0.5 is what noise looks like). Skew and kurtosis are
    required, not defaulted."""
    sr = _num(sharpe, "sharpe")
    t = _int_min(n_obs, "n_obs", 3)
    n = _int_min(n_trials, "n_trials", 1)
    var = _nonneg(sharpe_variance, "sharpe_variance")
    g3 = _num(skew, "skew")
    g4 = _num(kurtosis, "kurtosis")
    emax = expected_max_null(n)
    sr0 = math.sqrt(var) * emax
    denom2 = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    base = {"sharpe": sr, "n_obs": t, "n_trials": n, "sharpe_variance": var, "skew": g3, "kurtosis": g4,
            "expected_max_null_z": emax, "sr0": sr0}
    cl_in = _labels(assumed=("skew", "kurtosis"), calculated=("n_trials", "n_obs", "expected_max_null_z"),
                    estimated=("sharpe", "sharpe_variance", "sr0"))
    if denom2 <= 0.0:
        base.update({"status": "INSUFFICIENT", "dsr": None, "psr": None,
                     "reason": "the Sharpe-ratio variance term 1 - skew*SR + (kurt-1)/4*SR^2 is not positive for these moments",
                     "classes": cl_in})
        base["reading"] = base["reason"]
        return base
    scale = math.sqrt(t - 1.0) / math.sqrt(denom2)
    dsr = _N.cdf((sr - sr0) * scale)
    psr = _N.cdf(sr * scale)
    base.update({
        "status": "OK", "dsr": dsr, "psr_vs_zero": psr,
        "reading": (f"Best of {n} trials: expected luck-only Sharpe SR0={sr0:.4g}; observed {sr:.4g}. "
                    f"Probability the true Sharpe exceeds the luck level: {dsr:.3f} (vs {psr:.3f} if the "
                    f"{n} trials were ignored). Assumes independent null trials, so it is conservative for correlated ones."),
    })
    cl_in.update(_labels(estimated=("dsr", "psr_vs_zero")))
    base["classes"] = cl_in
    return base


def selection_adjusted_best(ledger: TrialLedger, *, alpha: float, adjust: str = "holm",
                            statistic_kind: str = "other", n_obs: Optional[int] = None,
                            skew: Optional[float] = None, kurtosis: Optional[float] = None,
                            assume_normal_returns: bool = False) -> Dict[str, Any]:
    """Judge the best candidate in a ledger against the whole search, not against itself.

    Best = smallest p-value (ties: larger statistic, then earlier). Reports its p-value ADJUSTED for ALL
    ``ledger.n_trials`` trials (a trial without a p-value counts as p=1), the expected maximum of N null
    z-statistics against which its own z must be judged, and, when ``statistic_kind='sharpe'`` (the ledger's
    ``statistic`` is the per-period Sharpe ratio over ``n_obs`` periods), the deflated Sharpe ratio using the
    variance of all trial Sharpe ratios. Skew and kurtosis must be given, or ``assume_normal_returns=True``
    must say out loud that returns are taken as normal (skew 0, kurtosis 3)."""
    if not isinstance(ledger, TrialLedger):
        raise StatisticalPowerError("selection_adjusted_best needs a TrialLedger (a plain list cannot prove it is complete)")
    alpha_ = _open01(alpha, "alpha")
    adjust = _choice(adjust, "adjust", ADJUST_METHODS)
    kind = _choice(statistic_kind, "statistic_kind", ("sharpe", "other"))
    ledger.verify()
    trials = ledger.trials
    n = len(trials)
    if n == 0:
        return {"status": "INSUFFICIENT", "reason": "the ledger is empty: nothing was examined",
                "reading": "the ledger is empty: nothing was examined", "n_trials": 0,
                "classes": _labels(calculated=("n_trials",))}
    with_p = [t for t in trials if t.pvalue is not None]
    out: Dict[str, Any] = {"n_trials": n, "n_with_pvalue": len(with_p), "n_distinct": ledger.n_distinct,
                           "ledger_head": ledger.head, "alpha": alpha_, "adjust": adjust}
    cl = _labels(calculated=("n_trials", "n_with_pvalue", "n_distinct", "ledger_head"), assumed=("alpha", "adjust"))
    if not with_p:
        best = max(trials, key=lambda t: (t.statistic, -t.index))
        out.update({"status": "INSUFFICIENT", "reason": "no trial in the ledger carries a p-value, so no selection-adjusted p-value exists",
                    "best": _trial_dict(best)})
        cl["best"] = CALCULATED
        out["classes"] = cl
        out["reading"] = out["reason"]
        return out
    best = min(with_p, key=lambda t: (t.pvalue, -t.statistic, t.index))
    adj = adjust_pvalues(ledger.pvalues(), adjust, alpha=alpha_, n_tested=n)
    best_adj = adj["adjusted"][best.index]
    z_best = _zu(min(max(best.pvalue, 1e-300), 1.0 - 2.0 ** -53))
    emax = expected_max_null(n)
    survives = bool(adj["reject"][best.index])
    beats = z_best > emax
    out.update({
        "status": "OK", "best": _trial_dict(best), "best_pvalue_unadjusted": best.pvalue,
        "best_pvalue_adjusted": best_adj, "survives_adjustment": survives, "n_rejected_in_family": adj["n_rejected"],
        "z_best": z_best, "expected_max_null_z": emax, "best_beats_noise_max": beats, "deflated_sharpe": None,
    })
    cl.update(_labels(calculated=("best", "best_pvalue_unadjusted", "best_pvalue_adjusted", "survives_adjustment",
                                  "n_rejected_in_family", "z_best"), estimated=("expected_max_null_z", "best_beats_noise_max")))
    warnings: List[str] = []
    if kind == "sharpe":
        stats = [t.statistic for t in trials]
        t_obs = n_obs if n_obs is not None else best.n_obs
        if t_obs is None:
            out["deflated_sharpe"] = {"status": "INSUFFICIENT", "reason": "n_obs for the Sharpe ratios was not supplied",
                                      "classes": {}}
        elif n < 2:
            out["deflated_sharpe"] = {"status": "INSUFFICIENT", "reason": "the variance of trial Sharpe ratios needs at least 2 trials",
                                      "classes": {}}
        else:
            if skew is None and kurtosis is None and assume_normal_returns:
                g3, g4 = 0.0, 3.0
                warnings.append("skew 0 and kurtosis 3 were ASSUMED (normal returns) at the caller's request")
            elif skew is None or kurtosis is None:
                g3 = g4 = None
            else:
                g3, g4 = skew, kurtosis
            if g3 is None:
                out["deflated_sharpe"] = {"status": "INSUFFICIENT", "classes": {},
                                          "reason": "skew and kurtosis were not supplied and assume_normal_returns is False"}
            else:
                var = statistics.variance(stats)
                out["deflated_sharpe"] = deflated_sharpe(best.statistic, n_obs=t_obs, n_trials=n,
                                                         sharpe_variance=var, skew=g3, kurtosis=g4)
    cl["deflated_sharpe"] = ESTIMATED
    out["warnings"] = warnings
    out["reading"] = (f"Best of {n} trials has p={best.pvalue:.3g}; adjusted for all {n} ({adjust}) it is {best_adj:.3g}"
                      f" ({'survives' if survives else 'does NOT survive'} alpha {alpha_:g}). Its z={z_best:.3f} versus an "
                      f"expected best-of-{n}-noise z of {emax:.3f}"
                      f" ({'above' if beats else 'NOT above'} luck).")
    out["classes"] = cl
    return out


def _trial_dict(t: Trial) -> Dict[str, Any]:
    return {"index": t.index, "strategy_id": t.strategy_id, "params_hash": t.params_hash, "window_id": t.window_id,
            "statistic": t.statistic, "pvalue": t.pvalue, "n_obs": t.n_obs}


# ------------------------------------------------------------------------------------------------
# the cost of a false discovery
# ------------------------------------------------------------------------------------------------
def posterior_prob_true(prior: float, alpha: float, power: float) -> float:
    """P(real edge | significant) = power*prior / (power*prior + alpha*(1-prior)). ``alpha`` should be the
    level actually applied to this test (the multiplicity-adjusted one)."""
    pr, a, pw = _open01(prior, "prior"), _open01(alpha, "alpha"), _open01(power, "power")
    return pw * pr / (pw * pr + a * (1.0 - pr))


def false_discovery_cost(*, loss_if_false: float, gain_if_true: float, prior: Optional[float] = None,
                         alpha: Optional[float] = None, power: Optional[float] = None,
                         prob_false: Optional[float] = None) -> Dict[str, Any]:
    """Expected value of acting on a significant result, and how likely that result is to be false.

    Bayes: P(true | significant) = power*prior / (power*prior + alpha*(1-prior)); so with a prior of 1% a
    p<0.05 result at 80% power is false about 86% of the time. If an externally estimated P(false | significant)
    is passed as ``prob_false`` it is used instead (and prior/alpha/power are then optional). Money terms are
    in any one consistent unit: ``loss_if_false`` is the loss from acting on a false edge, ``gain_if_true`` the
    gain from acting on a real one."""
    loss = _nonneg(loss_if_false, "loss_if_false")
    gain = _nonneg(gain_if_true, "gain_if_true")
    out: Dict[str, Any] = {"loss_if_false": loss, "gain_if_true": gain}
    cl = _labels(assumed=("loss_if_false", "gain_if_true"))
    if prob_false is not None:
        pf = _num(prob_false, "prob_false")
        if not 0.0 <= pf <= 1.0:
            raise StatisticalPowerError("prob_false must lie in [0, 1]")
        out["prob_false_source"] = "SUPPLIED"
        cl["prob_false_source"] = CALCULATED
        out["prob_false_given_significant"] = pf
        cl["prob_false_given_significant"] = ASSUMED
        for name, val in (("prior", prior), ("alpha", alpha), ("power", power)):
            if val is not None:
                out[name] = _open01(val, name)
                cl[name] = ASSUMED
    else:
        if prior is None or alpha is None or power is None:
            raise UnknownInputError("prior, alpha and power are all required to compute P(false | significant) "
                                    "(or pass prob_false directly)")
        ppv = posterior_prob_true(prior, alpha, power)
        pf = 1.0 - ppv
        pr, a, pw = float(prior), float(alpha), float(power)
        out.update({"prior": pr, "alpha": a, "power": pw, "prob_false_source": "BAYES",
                    "prob_false_given_significant": pf, "prior_odds": pr / (1.0 - pr),
                    "likelihood_ratio_significant": pw / a,
                    "posterior_odds_true": pw / a * pr / (1.0 - pr)})
        cl.update(_labels(assumed=("prior", "alpha", "power"),
                          calculated=("prob_false_source", "prior_odds", "likelihood_ratio_significant",
                                      "posterior_odds_true"), estimated=("prob_false_given_significant",)))
    ppv = 1.0 - pf
    ev = ppv * gain - pf * loss
    out.update({
        "status": "OK", "posterior_prob_true": ppv, "expected_value": ev,
        "expected_false_discovery_cost": pf * loss, "expected_true_discovery_gain": ppv * gain,
        "breakeven_posterior_prob_true": (loss / (loss + gain)) if (loss + gain) > 0 else None,
        "positive_expected_value": ev > 0.0,
    })
    cl.update(_labels(calculated=("posterior_prob_true", "expected_value", "expected_false_discovery_cost",
                                  "expected_true_discovery_gain", "breakeven_posterior_prob_true",
                                  "positive_expected_value")))
    out["reading"] = (f"A significant result is real with probability {ppv:.3f} and false with probability {pf:.3f}. "
                      f"Acting on it is worth {ev:+.4g} in expectation (expected false-discovery cost {pf * loss:.4g}, "
                      f"expected gain {ppv * gain:.4g}). The probabilities are only as good as the prior, which is a judgement.")
    out["classes"] = cl
    return out


# ------------------------------------------------------------------------------------------------
# chronological splits
# ------------------------------------------------------------------------------------------------
class InsufficientSplitError(StatisticalPowerError):
    """The purge and embargo leave no training observations."""


def purged_embargoed_splits(n: int, k_folds: int, label_horizon: int, embargo: int) -> List[Tuple[List[int], List[int]]]:
    """Purged, embargoed K-fold over ``n`` time-ordered observations.

    Observation i's label looks forward over indices i .. i+label_horizon. Fold f's test block is a
    contiguous run [a, b). Training drops the ``label_horizon`` indices BEFORE a (their label window would
    reach into the test block) and the ``embargo`` indices from b onward (serial dependence into the
    test block's tail). Every index list is ascending; folds are in time order and their test blocks
    partition 0..n-1. Set ``embargo >= label_horizon`` as well to keep training values out of the test labels
    (``audit_split`` reports that exposure)."""
    n_ = _int_min(n, "n", 2)
    k = _int_min(k_folds, "k_folds", 2)
    h = _int_min(label_horizon, "label_horizon", 0)
    e = _int_min(embargo, "embargo", 0)
    if k > n_:
        raise StatisticalPowerError("k_folds cannot exceed n")
    base, extra = divmod(n_, k)
    out: List[Tuple[List[int], List[int]]] = []
    a = 0
    for f in range(k):
        b = a + base + (1 if f < extra else 0)
        train = list(range(0, max(0, a - h))) + list(range(min(n_, b + e), n_))
        if not train:
            raise InsufficientSplitError(f"fold {f}: purge {h} and embargo {e} leave no training observations")
        out.append((train, list(range(a, b))))
        a = b
    return out


def walk_forward(n: int, train_min: int, test_len: int, label_horizon: int, embargo: int) -> List[Tuple[List[int], List[int]]]:
    """Expanding-window walk-forward. Each fold trains on 0 .. s-gap-1 and tests on s .. s+test_len-1, where the
    gap is ``label_horizon + embargo`` (the purge, plus a buffer for serial dependence; nothing follows a test
    block inside its own fold, so the embargo is applied as extra distance before it). Every fold's training set
    has at least ``train_min`` observations; test blocks tile forward without overlap."""
    n_ = _int_min(n, "n", 2)
    tm = _int_min(train_min, "train_min", 1)
    tl = _int_min(test_len, "test_len", 1)
    h = _int_min(label_horizon, "label_horizon", 0)
    e = _int_min(embargo, "embargo", 0)
    gap = h + e
    s = tm + gap
    out: List[Tuple[List[int], List[int]]] = []
    while s + tl <= n_:
        out.append((list(range(0, s - gap)), list(range(s, s + tl))))
        s += tl
    if not out:
        raise InsufficientSplitError("n is too small for one walk-forward fold with this train_min, test_len, purge and embargo")
    return out


def holdout_split(n: int, holdout_frac: float, label_horizon: int, embargo: int) -> Tuple[List[int], List[int]]:
    """Train / final holdout. The holdout is the LAST ceil(holdout_frac * n) observations; training stops
    ``label_horizon + embargo`` before it (the embargo is applied as extra distance, since nothing follows the
    holdout, which is never looser than the usual after-the-block embargo)."""
    n_ = _int_min(n, "n", 2)
    frac = _open01(holdout_frac, "holdout_frac")
    h = _int_min(label_horizon, "label_horizon", 0)
    e = _int_min(embargo, "embargo", 0)
    hold = max(1, int(math.ceil(frac * n_ - 1e-9)))
    start = n_ - hold
    train_end = start - h - e
    if train_end < 1:
        raise InsufficientSplitError("the holdout, purge and embargo leave no training observations")
    return list(range(0, train_end)), list(range(start, n_))


def audit_split(train_idx: Sequence[int], test_idx: Sequence[int], n: int, label_horizon: int,
                embargo: int) -> Dict[str, Any]:
    """Count the ways a split leaks. ``clean`` needs: both lists ascending and unique, inside 0..n-1,
    disjoint; no training label window [j, j+label_horizon] touching a test index; no training index within
    ``embargo`` after a test index. ``train_values_inside_test_labels`` counts training indices that fall inside
    some test label window (zero when the embargo is at least the label horizon)."""
    n_ = _int_min(n, "n", 1)
    h = _int_min(label_horizon, "label_horizon", 0)
    e = _int_min(embargo, "embargo", 0)
    tr, te = list(train_idx), list(test_idx)
    asc = lambda xs: all(a < b for a, b in zip(xs, xs[1:]))
    in_range = all(isinstance(i, int) and 0 <= i < n_ for i in tr + te)
    sorted_unique = asc(tr) and asc(te)
    out: Dict[str, Any] = {"status": "OK", "n_train": len(tr), "n_test": len(te), "ascending_unique": sorted_unique,
                           "in_range": in_range}
    if not (sorted_unique and in_range):
        out.update({"disjoint": None, "label_overlap": None, "embargo_violations": None,
                    "train_values_inside_test_labels": None, "chronological": False, "clean": False,
                    "reading": "the index lists are not ascending, unique and in range"})
    else:
        test_set = set(te)
        label_zone = bytearray(n_ + 1)      # training j whose label window [j, j+h] reaches a test index
        embargo_zone = bytearray(n_ + 1)
        inside_zone = bytearray(n_ + 1)
        for i in te:
            for j in range(max(0, i - h), i + 1):
                label_zone[j] = 1
            for j in range(i + 1, min(n_, i + e + 1)):
                embargo_zone[j] = 1
            for j in range(i + 1, min(n_, i + h + 1)):
                inside_zone[j] = 1
        disjoint = not (set(tr) & test_set)
        label_overlap = sum(1 for j in tr if label_zone[j])
        emb = sum(1 for j in tr if embargo_zone[j])
        inside = sum(1 for j in tr if inside_zone[j])
        out.update({"disjoint": disjoint, "label_overlap": label_overlap, "embargo_violations": emb,
                    "train_values_inside_test_labels": inside,
                    "chronological": bool(tr and te and (max(tr) < min(te))),
                    "clean": bool(disjoint and label_overlap == 0 and emb == 0)})
        out["reading"] = ("no training label window touches the test block and the embargo is respected"
                          if out["clean"] else
                          f"LEAKS: {label_overlap} training labels overlap the test block, {emb} training indices sit "
                          f"inside the embargo, disjoint={disjoint}")
    out["classes"] = _labels(calculated=tuple(k for k in out if k not in META_KEYS and k != "classes"))
    return out


# ------------------------------------------------------------------------------------------------
# pre-registration
# ------------------------------------------------------------------------------------------------
class FrozenParams(_MappingABC):
    """Immutable, hashable, order-independent mapping of declared parameters (nested containers frozen)."""

    __slots__ = ("_d", "_items")

    def __init__(self, data: Mapping[str, Any]) -> None:
        if any(not isinstance(k, str) for k in data):
            raise PlanError("parameter names must be strings")
        d = {k: _freeze(v) for k, v in data.items()}
        object.__setattr__(self, "_d", d)
        object.__setattr__(self, "_items", tuple(sorted(d.items())))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("FrozenParams is immutable")

    def __getitem__(self, k: str) -> Any:
        return self._d[k]

    def __iter__(self) -> Iterator[str]:
        return iter(k for k, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(self._items)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, FrozenParams) and self._items == other._items

    def __repr__(self) -> str:
        return f"FrozenParams({dict(self._items)!r})"


def _freeze(x: Any) -> Any:
    if isinstance(x, FrozenParams):
        return x
    if isinstance(x, _MappingABC):
        if any(not isinstance(k, str) for k in x):
            raise PlanError("parameter names must be strings")
        return FrozenParams(x)
    if isinstance(x, (list, tuple)):
        return tuple(_freeze(v) for v in x)
    if x is None or isinstance(x, (str, bool, int)):
        return x
    if isinstance(x, float):
        if not math.isfinite(x):
            raise PlanError("parameters must be finite numbers")
        return x
    raise PlanError(f"parameter values must be JSON-like (str, number, bool, None, list, mapping), got {type(x).__name__}")


def _thaw(x: Any) -> Any:
    if isinstance(x, FrozenParams):
        return {k: _thaw(v) for k, v in x.items()}
    if isinstance(x, tuple):
        return [_thaw(v) for v in x]
    return x


@dataclass(frozen=True)
class EvaluationPlan:
    """Everything that must be fixed BEFORE the data are read. Frozen: a field cannot be changed afterwards,
    and ``plan_digest`` changes if any field differs, so a changed window is a different plan."""
    strategy_id: str
    version: str
    parameters: Mapping[str, Any]
    evaluation_window: Tuple[float, float]
    metric: str
    alpha: float
    power_target: float
    min_edge: float
    n_hypotheses_declared: int
    seed: int
    adjust: str = "holm"

    def __post_init__(self) -> None:
        for name in ("strategy_id", "version", "metric"):
            v = getattr(self, name)
            if v is None:
                raise UnknownInputError(f"{name} is unknown (None)")
            if not isinstance(v, str) or not v.strip():
                raise PlanError(f"{name} must be a non-empty string")
        if self.parameters is None:
            raise UnknownInputError("parameters is unknown (None); pass an empty mapping if there are none")
        if not isinstance(self.parameters, _MappingABC):
            raise PlanError("parameters must be a mapping")
        object.__setattr__(self, "parameters", FrozenParams(self.parameters))
        w = self.evaluation_window
        if w is None:
            raise UnknownInputError("evaluation_window is unknown (None)")
        if not isinstance(w, (tuple, list)) or len(w) != 2:
            raise PlanError("evaluation_window must be a (start, end) pair")
        start, end = _num(w[0], "window start"), _num(w[1], "window end")
        if not start < end:
            raise PlanError("evaluation_window must satisfy start < end")
        object.__setattr__(self, "evaluation_window", (w[0], w[1]))
        _open01(self.alpha, "alpha")
        _open01(self.power_target, "power_target")
        _pos(self.min_edge, "min_edge")
        _int_min(self.n_hypotheses_declared, "n_hypotheses_declared", 1)
        _int_min(self.seed, "seed", 0)
        _choice(self.adjust, "adjust", ADJUST_METHODS)

    @property
    def window_id(self) -> str:
        return _canon([self.evaluation_window[0], self.evaluation_window[1]])

    @property
    def params_hash(self) -> str:
        return _sha(_canon(_thaw(self.parameters)))


def plan_digest(plan: EvaluationPlan) -> str:
    """SHA-256 hex of the canonical encoding of every declared field. Key order and 5 vs 5.0 do not change it;
    any other change (including the window) does."""
    if not isinstance(plan, EvaluationPlan):
        raise PlanError("plan_digest needs an EvaluationPlan")
    body = {
        "schema": "statistical_power.EvaluationPlan.v1", "strategy_id": plan.strategy_id, "version": plan.version,
        "parameters": _thaw(plan.parameters), "evaluation_window": [plan.evaluation_window[0], plan.evaluation_window[1]],
        "metric": plan.metric, "alpha": plan.alpha, "power_target": plan.power_target, "min_edge": plan.min_edge,
        "n_hypotheses_declared": plan.n_hypotheses_declared, "seed": plan.seed, "adjust": plan.adjust,
    }
    return _sha(_canon(body))


def mean_statistic(values: Sequence[float]) -> float:
    return sum(values) / len(values)


mean_statistic.metric_name = "mean"            # type: ignore[attr-defined]


def _read_observations(obs: Any) -> Tuple[List[float], List[float]]:
    if obs is None:
        raise UnknownInputError("observations is unknown (None)")
    if isinstance(obs, (str, bytes)) or not hasattr(obs, "__iter__"):
        raise DataIntegrityError("observations must be an iterable of (timestamp, value) pairs")
    ts: List[float] = []
    vs: List[float] = []
    for k, row in enumerate(obs):
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise DataIntegrityError(f"observation {k} is not a (timestamp, value) pair")
        t, v = row
        for what, x in (("timestamp", t), ("value", v)):
            if isinstance(x, bool) or not isinstance(x, numbers.Real) or not math.isfinite(float(x)):
                raise DataIntegrityError(f"observation {k} has a non-finite or non-numeric {what}: {x!r}")
        ts.append(float(t))
        vs.append(float(v))
    for k in range(1, len(ts)):
        if not ts[k] > ts[k - 1]:
            raise DataIntegrityError(f"timestamps must be strictly increasing; observation {k} breaks the order")
    return ts, vs


def _check_window(plan: EvaluationPlan, ts: List[float]) -> Dict[str, Any]:
    start, end = float(plan.evaluation_window[0]), float(plan.evaluation_window[1])
    if not ts:
        raise WindowCoverageError("no observations were supplied for the declared window")
    outside = [t for t in ts if not start <= t < end]
    if outside:
        raise WindowViolation(f"{len(outside)} observation(s) lie outside the declared window [{start:g}, {end:g}) "
                              f"(first offender {outside[0]:g}): the window was fixed before the data and cannot move")
    info: Dict[str, Any] = {"window_start": start, "window_end": end, "first_timestamp": ts[0], "last_timestamp": ts[-1]}
    if len(ts) >= 3:
        spacing = statistics.median([b - a for a, b in zip(ts, ts[1:])])
        slack = COVERAGE_SLACK_SPACINGS * spacing
        if ts[0] - start > slack or end - ts[-1] > slack:
            raise WindowCoverageError(
                f"the data cover [{ts[0]:g}, {ts[-1]:g}] but the declared window is [{start:g}, {end:g}) "
                f"(median spacing {spacing:g}): a truncated or cherry-picked sub-window is not the declared evaluation")
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        info["median_spacing"] = spacing
        info["max_gap_in_spacings"] = max(gaps) / spacing if spacing > 0 else None
    return info


def _sample_sd(vals: Sequence[float]) -> float:
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def evaluate_under_plan(plan: EvaluationPlan, observations_with_timestamps: Iterable[Tuple[float, float]],
                        statistic_fn: Callable[[Sequence[float]], float], *, expected_digest: str,
                        ledger: TrialLedger, bootstrap_B: int = 2000) -> Dict[str, Any]:
    """Evaluate pre-registered data under a plan. Refuses (raises) instead of adapting.

    Refusals: ``PlanDigestMismatch`` if ``expected_digest`` (pinned in advance) is not the plan's digest;
    ``WindowViolation`` if any timestamp lies outside the declared half-open window [start, end);
    ``WindowCoverageError`` if the data do not reach both ends of it; ``DataIntegrityError`` for malformed,
    unordered, duplicated or non-finite observations. A refused call examines nothing and records nothing.

    Otherwise the evaluation is recorded in ``ledger`` and the result carries the statistic, a block-bootstrap
    interval from ``evidence_stats`` (block length from the ESS), the required vs available observations,
    the achieved power for the plan's ``min_edge``, the p-value ADJUSTED for ``max(n_hypotheses_declared,
    ledger.n_trials)`` hypotheses, and a verdict:

      EDGE_SUPPORTED_AFTER_ADJUSTMENT  adjusted p <= alpha AND the sample could detect min_edge at the planned power
      NO_EDGE_DETECTED                 adequately powered, and the adjusted test did not reject
      INSUFFICIENT_POWER               everything else, including a significant result from an underpowered
                                       sample (it would be inflated), too few observations, or no variance

    The test is one-sided (H0: mean <= 0). ``statistic_fn`` is ``mean_statistic`` for the ESS-adjusted t test
    of the mean; any other statistic gets a normal test with the standard error read from the block-bootstrap
    interval (labelled ESTIMATED, and bounded by the same ESS)."""
    if not isinstance(plan, EvaluationPlan):
        raise PlanError("plan must be an EvaluationPlan")
    if not isinstance(ledger, TrialLedger):
        raise PlanError("ledger must be a TrialLedger: an evaluation that is not recorded is refused")
    if not callable(statistic_fn):
        raise PlanError("statistic_fn must be callable")
    digest = plan_digest(plan)
    if not isinstance(expected_digest, str) or expected_digest.strip().lower() != digest:
        raise PlanDigestMismatch("the plan's digest does not match the expected digest pinned in advance: the plan "
                                 "(or its evaluation window) is not the one that was registered")
    boot_b = _int_min(bootstrap_B, "bootstrap_B", 40)
    ts, vals = _read_observations(observations_with_timestamps)
    win = _check_window(plan, ts)
    ledger.verify()

    n = len(vals)
    is_mean = statistic_fn is mean_statistic
    stat = statistic_fn(list(vals))
    if isinstance(stat, bool) or not isinstance(stat, numbers.Real) or not math.isfinite(float(stat)):
        raise DataIntegrityError(f"statistic_fn returned a non-finite or non-numeric value: {stat!r}")
    stat = float(stat)
    fn_name = getattr(statistic_fn, "metric_name", getattr(statistic_fn, "__name__", None))
    warnings: List[str] = []
    if fn_name != plan.metric:
        warnings.append(f"statistic_fn is named {fn_name!r} but the plan declares metric {plan.metric!r}: the digest "
                        "binds the declared label, not the code")

    ci = _es.block_bootstrap_ci(vals, None if is_mean else statistic_fn, B=boot_b, seed=plan.seed)
    ess = _es.conservative_ess(vals) if n >= 2 else None
    sd = _sample_sd(vals) if n >= 2 else 0.0
    constant = n >= 2 and (sd <= 0.0 or (max(vals) - min(vals)) <= 1e-12 * max(1.0, abs(sum(vals) / n)))
    unusable: Optional[str] = None
    if n < _es.MIN_FOR_CI:
        unusable = f"only {n} observations (fewer than {_es.MIN_FOR_CI})"
    elif constant:
        unusable = "the series is constant: there is no variance to test against"
    elif ess is None or ess < 3.0:
        unusable = f"effective sample size {ess} is below 3: too few independent draws for any test"
    elif ci.get("status") != "OK":
        unusable = f"the bootstrap interval is unavailable ({ci.get('reason', ci.get('status'))})"

    pval: Optional[float] = None
    se: Optional[float] = None
    sd_eff: Optional[float] = None
    if unusable is None:
        assert ess is not None
        if is_mean:
            se = sd / math.sqrt(ess)
            pval = student_t_sf(stat / se, ess - 1.0)
            sd_eff = sd
        else:
            width = float(ci["hi"]) - float(ci["lo"])
            z975 = _zu(0.025)
            se = width / (2.0 * z975)
            if se <= 0.0:
                unusable = "the bootstrap interval has zero width: no standard error"
            else:
                pval = _N.cdf(-stat / se)
                sd_eff = se * math.sqrt(ess)
    if pval is not None:
        pval = min(1.0, max(pval, 1e-300))

    trial = ledger.record(plan.strategy_id, plan.params_hash, plan.window_id, stat, pvalue=pval, n_obs=n,
                          plan_digest=digest)
    n_trials = ledger.n_trials
    m_used = max(plan.n_hypotheses_declared, n_trials)
    repeats = sum(1 for t in ledger.trials if t.plan_digest == digest)
    if repeats > 1:
        warnings.append(f"this plan has now been evaluated {repeats} times: every look is a trial and was counted")
    if n_trials > plan.n_hypotheses_declared:
        warnings.append(f"the ledger holds {n_trials} trials, more than the {plan.n_hypotheses_declared} declared: "
                        "the adjustment uses the larger")

    out: Dict[str, Any] = {
        "plan_digest": digest, "metric": plan.metric, "strategy_id": plan.strategy_id, "version": plan.version,
        "statistic": stat, "n_observations": n, "ci": ci, "window": win,
        "alpha": plan.alpha, "power_target": plan.power_target, "min_edge": plan.min_edge, "adjust": plan.adjust,
        "n_hypotheses_declared": plan.n_hypotheses_declared, "n_hypotheses_used": m_used,
        "ledger": {"trial_index": trial.index, "n_trials": n_trials, "n_distinct": ledger.n_distinct,
                   "head": ledger.head, "evaluations_of_this_plan": repeats},
        "inference": "ESS-adjusted one-sided t test of the mean" if is_mean else
                     "one-sided normal test, standard error read from the block-bootstrap interval",
        "warnings": warnings,
    }
    cl = _labels(calculated=("plan_digest", "statistic", "n_observations", "window", "n_hypotheses_used",
                             "n_hypotheses_declared", "ledger", "metric", "strategy_id", "version"),
                 estimated=("ci", "inference"), assumed=("alpha", "power_target", "min_edge", "adjust"))

    if unusable is not None:
        out.update({
            "status": "INSUFFICIENT", "verdict": INSUFFICIENT_POWER, "reason": unusable,
            "effective_n_available": ess, "sd": sd if n >= 2 else None, "p_value": None, "adjusted_p_value": None,
            "required": None, "achieved_power": None,
            "reading": f"INSUFFICIENT_POWER: {unusable}. The evaluation was recorded as a trial with no p-value.",
        })
        cl.update(_labels(estimated=("effective_n_available", "sd", "p_value", "adjusted_p_value", "required",
                                     "achieved_power", "verdict")))
        out["classes"] = cl
        return out

    assert ess is not None and sd_eff is not None and se is not None and pval is not None
    ess_ratio = min(1.0, ess / n)
    method = "t" if is_mean else "normal"
    fam = ledger.pvalues()
    padded_adj = adjust_pvalues(fam, plan.adjust, alpha=plan.alpha, n_tested=m_used)
    adj_p = padded_adj["adjusted"][trial.index]
    significant = adj_p <= plan.alpha
    bh_k = 1 if plan.adjust == "bh" else None
    req = required_observations(sd_eff, plan.min_edge, alpha=plan.alpha, power=plan.power_target, n_hypotheses=m_used,
                                adjust=plan.adjust, ess_ratio=ess_ratio, bh_n_true=bh_k, method=method,
                                inputs_class=ESTIMATED)
    pw = achieved_power(ess, sd_eff, plan.min_edge, n_kind="effective", alpha=plan.alpha, n_hypotheses=m_used,
                        adjust=plan.adjust, bh_n_true=bh_k, method=method, inputs_class=ESTIMATED)
    floor_binds = ess < MIN_ESS_FOR_SUPPORT
    powered_by_n = req.get("status") == "OK" and ess + 1e-9 >= req["required_effective_n_exact"]
    powered = powered_by_n and not floor_binds
    z_obs = _zu(min(max(pval, 1e-300), 1.0 - 2.0 ** -53))     # a p-value of exactly 1 (a mean far BELOW zero) must report a very negative z, not crash
    emax = expected_max_null(m_used)
    need = req.get("required_effective_n")
    floor_note = (f" Below {MIN_ESS_FOR_SUPPORT:g} effective observations the ESS estimate and the t approximation are "
                  "unreliable, so no edge is called supported on so few independent draws." if floor_binds else "")
    if floor_binds:
        warnings.append(f"only {ess:.1f} effective observations (< {MIN_ESS_FOR_SUPPORT:g}): the effective sample size "
                        "is itself poorly estimated and p-values from a short persistent series over-reject")
    if significant and powered:
        verdict, why = EDGE_SUPPORTED_AFTER_ADJUSTMENT, (
            f"adjusted p {adj_p:.3g} <= alpha {plan.alpha:g} over {m_used} hypotheses, and {ess:.1f} effective observations "
            f"meet the {need} needed to detect min_edge {plan.min_edge:g} at power {plan.power_target:g}")
    elif significant:
        verdict, why = INSUFFICIENT_POWER, (
            f"adjusted p {adj_p:.3g} <= alpha {plan.alpha:g}, but only {ess:.1f} effective observations against "
            f"{need} needed for power {plan.power_target:g}: a significant result from a sample "
            "that could not reliably detect min_edge is inflated (winner's curse) and is not promoted." + floor_note)
    elif powered:
        verdict, why = NO_EDGE_DETECTED, (
            f"adjusted p {adj_p:.3g} > alpha {plan.alpha:g} with {ess:.1f} effective observations (>= {need} "
            f"needed): an adequately powered test found no edge of size min_edge {plan.min_edge:g}")
    else:
        verdict, why = INSUFFICIENT_POWER, (
            f"adjusted p {adj_p:.3g} > alpha {plan.alpha:g} and only {ess:.1f} effective observations against "
            f"{need} needed: the sample cannot separate min_edge {plan.min_edge:g} from noise." + floor_note)
    if is_mean and ci.get("status") == "OK" and ci["lo"] <= 0.0 < stat and significant:
        warnings.append("the conservative evidence_stats interval for the mean still includes zero although the "
                        "ESS-adjusted test rejects")
    if stat < plan.min_edge:
        warnings.append(f"the point estimate {stat:.4g} is below min_edge {plan.min_edge:g}: a statistically positive "
                        "edge may still be economically meaningless")
    out.update({
        "status": "OK", "verdict": verdict, "reason": why, "effective_n_available": ess, "ess_ratio": ess_ratio,
        "design_effect": 1.0 / ess_ratio, "sd": sd, "standard_error": se, "t_or_z_statistic": stat / se,
        "p_value": pval, "adjusted_p_value": adj_p, "significant_after_adjustment": significant,
        "required": req, "available_raw_n": n, "required_raw_n": req.get("required_raw_n"),
        "required_effective_n": req.get("required_effective_n"), "adequately_powered": powered,
        "achieved_power": pw.get("power"), "z_observed": z_obs, "expected_max_null_z": emax,
        "z_exceeds_expected_noise_max": z_obs > emax,
        "reading": (f"{verdict}: {why}. Statistic {stat:.4g} (block-bootstrap interval "
                    + (f"[{ci['lo']:.4g}, {ci['hi']:.4g}]" if ci.get("status") == "OK" else "unavailable")
                    + f"); {n} raw observations carry {ess:.1f} effective ones."),
    })
    cl.update(_labels(estimated=("effective_n_available", "ess_ratio", "design_effect", "sd", "standard_error",
                                 "t_or_z_statistic", "p_value", "adjusted_p_value", "significant_after_adjustment",
                                 "required", "required_raw_n", "required_effective_n", "adequately_powered",
                                 "achieved_power", "z_observed", "expected_max_null_z", "z_exceeds_expected_noise_max",
                                 "verdict"), calculated=("available_raw_n",)))
    out["classes"] = cl
    return out


__all__ = [
    "CALCULATED", "ESTIMATED", "ASSUMED", "CLASSES", "VERDICTS", "INSUFFICIENT_POWER", "NO_EDGE_DETECTED",
    "EDGE_SUPPORTED_AFTER_ADJUSTMENT", "StatisticalPowerError", "UnknownInputError", "LedgerError", "LedgerTamperError",
    "PlanError", "PlanDigestMismatch", "WindowViolation", "WindowCoverageError", "DataIntegrityError",
    "InsufficientSplitError", "student_t_sf", "student_t_isf", "noncentral_t_sf", "design_effect",
    "required_observations", "achieved_power", "minimum_detectable_edge", "adjust_pvalues", "TrialLedger", "Trial",
    "LedgerAnchor", "params_hash", "expected_max_null", "deflated_sharpe", "selection_adjusted_best",
    "posterior_prob_true", "false_discovery_cost", "purged_embargoed_splits", "walk_forward", "holdout_split",
    "audit_split", "FrozenParams", "EvaluationPlan", "plan_digest", "mean_statistic", "evaluate_under_plan",
]
