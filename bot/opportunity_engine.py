"""An order-less registry and evaluator of Bybit trading hypotheses.

Every candidate is a ``StrategySpec``: identity and version, instrument/product, entry and
exit conditions, the market inputs it needs, funding and execution assumptions, holding period,
sizing, capital, risk limits, its research protocol and its acceptance/rejection criteria. An
evaluator turns the recorded evidence into one of eight statuses. They answer DIFFERENT questions, and a later one
never follows from an earlier one by accumulation of public data alone:

  INSUFFICIENT_EVIDENCE               not enough, or untrustworthy, evidence to say anything
  RESEARCH_CANDIDATE                  passes the screens on what was recorded (in sample); modelled costs only
  PROSPECTIVE_MARKET_EDGE_SUPPORTED   pre-registered, then the screens still pass on PUBLIC market data that arrived afterwards,
                                      with the integrity anchored and a stated headroom for the unpriced costs. It is a
                                      statement about the public market, NOT about any account's profit
  ECONOMICALLY_RECONCILED             the same edge reproduced from ACCOUNT records: the real fee schedule, product and account
                                      qualification, reconciled account funding settlements, real fills and fee records, every
                                      cost input priced from measurements, reconciled capital, verified venue margin rules
  EXECUTION_READY                     ECONOMICALLY_RECONCILED plus a separate, explicit owner execution authorisation record.
                                      Still not an order and not a permission
  ECONOMICALLY_UNATTRACTIVE           the net edge on economic capital misses even the lowest screen
  EXECUTION_UNATTRACTIVE              cost or fillability destroys the edge
  RISK_UNACCEPTABLE                   the risk limits are breached

A public funding print is not account cash. A modelled fill is not an execution. A predicted rebate is not a rebate received.

NO STATUS AUTHORISES TRADING, and nothing here can send an order: this module imports no
client, broker, store or configuration, a test pins that, and every spec carries
``order_path_connected = False``. `funding_carry_fade_v1` is FROZEN ABSENT: it is listed so its
state is explicit, it is never re-evaluated, and its rejection is not weakened.

The evaluators are deliberately conservative about what they accept: a stale recorder, a broken
evidence chain, a mixed stream, or fewer than 90 settlements (or too small an effective sample size) is INSUFFICIENT, and
PROSPECTIVE_MARKET_EDGE_SUPPORTED needs a registration timestamp that precedes the evidence it is judged on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

import evidence_stats as ES
import execution_model as EM
import funding_eval as FE
import strategy_measure as SM
import trade_economics as TE


class Status(str, Enum):
    RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ECONOMICALLY_UNATTRACTIVE = "ECONOMICALLY_UNATTRACTIVE"
    EXECUTION_UNATTRACTIVE = "EXECUTION_UNATTRACTIVE"
    RISK_UNACCEPTABLE = "RISK_UNACCEPTABLE"
    PROSPECTIVE_MARKET_EDGE_SUPPORTED = "PROSPECTIVE_MARKET_EDGE_SUPPORTED"
    ECONOMICALLY_RECONCILED = "ECONOMICALLY_RECONCILED"
    EXECUTION_READY = "EXECUTION_READY"


EXECUTED_BY_ENGINE = "EXECUTED_BY_ENGINE"            # paper/testnet only; live is locked
ORDER_LESS = "ORDER_LESS_RECOMMENDATION"
HYPOTHESIS = "RESEARCH_HYPOTHESIS"
FROZEN_ABSENT = "FROZEN_ABSENT"

MIN_SETTLEMENTS = 90                # 30 days of settlements (serially dependent: the ESS gate in funding_eval also applies)
PROSPECTIVE_SETTLEMENTS = 270       # 90 days after registration
#: A prospective market-edge claim must survive some UNPRICED cost: the modelled net must exceed the lowest screen by
#: at least this many bps of round-trip cost (a screening control, not a measurement of latency or impact).
MIN_UNKNOWN_COST_HEADROOM_BPS = 2.0
#: Account-level floors for ECONOMICALLY_RECONCILED (screening controls).
MIN_ACCOUNT_SETTLEMENTS = 90
MIN_ACCOUNT_ROUND_TRIPS = 3
RECONCILIATION_REQUIREMENTS = (
    "account_fee_schedule", "product_account_qualification", "account_funding_settlements", "fills_and_fee_records",
    "execution_and_operational_costs", "capital_and_collateral", "venue_margin_and_liquidation_rules",
    "account_economics")


@dataclass(frozen=True)
class StrategySpec:
    id: str
    version: int
    title: str
    origin: str
    product: Mapping[str, Any]
    entry_conditions: Sequence[str]
    exit_conditions: Sequence[str]
    required_inputs: Sequence[str]
    funding_assumptions: str
    expected_holding_days: Sequence[float]
    execution_assumptions: str
    sizing_rules: str
    capital_requirements: str
    risk_limits: Mapping[str, float]
    research_protocol: Mapping[str, Any]
    acceptance: Sequence[str]
    rejection: Sequence[str]
    order_path_connected: bool = False           # a test pins this False for every spec

    @property
    def key(self) -> str:
        return f"{self.id}/v{self.version}"


_BTC_LINEAR = {"venue": "bybit", "category": "linear", "symbol": "BTCUSDT", "settle": "USDT",
               "funding_interval_h": 8}
_RISK = {"min_liquidation_headroom_pct": 20.0, "max_single_settlement_loss_bps": 15.0,
         "min_fillable_share": 0.9, "max_round_trip_cost_bps": 15.0}
_COMMON_REJECT = ("insufficient or untrustworthy evidence", "net edge below the lowest screening hurdle",
                  "execution cost or fillability destroys the edge", "risk limits breached")

#: PREDECLARED variants. They are fixed here, before any evaluation, and are never tuned to the data. Every variant listed is
#: examined in every evaluation run, so a result is judged against the WHOLE family: the significance level of each interval is
#: ``FAMILY_ALPHA`` = 0.05 divided by the number of variants across ALL hypotheses (Bonferroni), not by the number that won.
PREDECLARED: Dict[str, Dict[str, Any]] = {
    "basis_convergence/v1": {"theta_in_cost_multiples": (1.5, 2.0, 3.0), "theta_out_fraction_of_in": 0.25, "max_hold_h": 24.0},
    "btc_trend_following/v1": {"ema_bars": ((12, 48), (48, 192), (96, 384))},
    "btc_mean_reversion/v1": {"zscore_bars": ((48, 2.0, 0.5), (96, 2.0, 0.5), (288, 2.5, 0.5))},
    "microstructure_imbalance/v1": {"horizon_snapshots": (1, 3, 12), "theta": 0.6},
    "regime_classifier/v1": {"vol_window_bars": 288, "min_history_bars": 576, "regimes": ("LOW", "MID", "HIGH")},
    "cross_sectional_funding/v1": {"top_k": 1, "hold_settlements": 21, "offset_min": 30},
}
FAMILY_TRIALS = (len(PREDECLARED["basis_convergence/v1"]["theta_in_cost_multiples"]) + len(PREDECLARED["btc_trend_following/v1"]["ema_bars"])
                 + len(PREDECLARED["btc_mean_reversion/v1"]["zscore_bars"]) + len(PREDECLARED["microstructure_imbalance/v1"]["horizon_snapshots"])
                 + len(PREDECLARED["regime_classifier/v1"]["regimes"]) + 1)
FAMILY_ALPHA = 0.05 / FAMILY_TRIALS
#: Floors below which a measured edge is not even described (screening controls, not proofs).
MIN_EPISODES = 30
MIN_SPAN_DAYS = 14                  # basis episodes
MIN_DIRECTIONAL_DAYS = 90           # trend / mean reversion: a quarter of bars, whatever the cadence
MIN_ALIGNED_SETTLEMENTS = 90

REGISTRY: Dict[str, StrategySpec] = {s.key: s for s in (
    StrategySpec(
        "carry_overlay_taker", 1, "BTC funding-carry overlay (short BTCUSDT linear vs owned BTC), taker",
        EXECUTED_BY_ENGINE, _BTC_LINEAR,
        ("production entry gate (carry_costs.entry_gate) passes", "net-benefit gate has an ALLOCATE verdict",
         "venue state flat and readable"),
        ("funding inverted for N prints", "margin or liquidation-distance floor", "inventory cover lost", "operator stop"),
        ("mainnet funding forecast and prints", "book depth", "fees", "account collateral and margin rules"),
        "funding persists; NOT assumed from the current forecast", (7.0, 90.0),
        "taker both legs of the perp round trip; spot is the client's existing BTC",
        "one lot under the $100 cap until reconciled forward cycles exist", "BTC inventory + margin + reserve",
        _RISK, {"registered": "pre-existing engine rule", "universe": "BTCUSDT linear",
                "validation": "reconciled forward cycles after all costs, versus holding BTC on the same capital",
                "multiple_testing": "no parameter search permitted"},
        ("lower bound of realised net APR on economic capital >= screening hurdle", "prospective confirmation"),
        _COMMON_REJECT),
    StrategySpec(
        "candidate_carry_depth", 1, "Depth-based carry entry recommendation (order-less)",
        ORDER_LESS, _BTC_LINEAR,
        ("forecast for the next settlement positive and not below history by the declared multiple",
         "depth cost of the declared size measured on the live book"),
        ("not an exit rule: it recommends entries only",), ("live book depth", "funding history", "fee tier"),
        "as carry_overlay_taker", (7.0, 90.0), "recommendation only; every real entry still passes the production gates",
        "n/a", "n/a", _RISK, {"registered": "2026-11-04", "universe": "BTCUSDT linear",
                              "validation": "forward only, independent, versus BTC hold", "multiple_testing": "none; one declared multiple"},
        ("adds net return over the production gate on prospective cycles",), _COMMON_REJECT),
    StrategySpec(
        "funding_carry_fade", 1, "Frozen funding-carry fade (FROZEN ABSENT)", FROZEN_ABSENT, _BTC_LINEAR,
        (), (), (), "n/a", (0.0, 0.0), "n/a", "n/a", "n/a", {},
        {"registered": "frozen", "note": "its historical rejection is preserved byte-for-byte; never re-evaluated here"},
        (), ("frozen absent",)),
    StrategySpec(
        "funding_carry_fade_btc", 1, "BTC-only directional funding-fade hypothesis (slice 57)", HYPOTHESIS,
        {**_BTC_LINEAR, "direction": "directional, not a hedge"},
        ("per the frozen rule in signals/funding_carry_fade_btc_v1.py",), ("time exit at the frozen horizon",),
        ("daily bars", "funding"), "n/a", (5.0, 5.0), "paper shadow only; the carry engine does not execute it",
        "n/a", "n/a", _RISK, {"registered": "EDGE.md section 39", "validation": "prospective OOS; the historical OOS used data already examined",
                              "multiple_testing": "family of frozen signals counted"},
        ("prospective OOS clearing the frozen gate on data that arrived after registration",), _COMMON_REJECT),
    StrategySpec(
        "basis_convergence", 1, "Spot/perpetual basis convergence", HYPOTHESIS, _BTC_LINEAR,
        ("basis outside a declared band measured only on past data",), ("basis back inside the band or time stop",),
        ("perp mark and spot mid with exchange timestamps", "fees", "depth"),
        "funding earned separately and never double-counted with basis", (1.0, 30.0),
        "both legs taker unless maker fill probability has been MEASURED", "n/a", "BTC inventory + margin + reserve",
        _RISK, {"registered": "2026-11-05 (this programme)", "universe": "BTCUSDT", "validation": "prospective only",
                "multiple_testing": "band chosen once, before evaluation"},
        ("basis capture exceeds round-trip cost with a lower CI bound above zero on independent episodes",), _COMMON_REJECT),
    StrategySpec(
        "cross_sectional_funding", 1, "Cross-sectional funding across eligible Bybit linear perpetuals", HYPOTHESIS,
        {"venue": "bybit", "category": "linear", "symbol": "ELIGIBLE_UNIVERSE (not yet recorded)"},
        ("symbol funding net of its own costs exceeds the BTC alternative",), ("rank reversal or time stop",),
        ("multi-symbol funding, depth and contract specs on the same clock",), "per-symbol interval respected", (7.0, 60.0),
        "per-symbol depth; displayed liquidity is not executable liquidity", "per-symbol caps and a concentration cap",
        "per-symbol hedge and margin", {**_RISK, "max_correlated_exposure_pct": 50.0},
        {"registered": "2026-11-05 (this programme)", "universe": "liquid linear perpetuals only; no arbitrary illiquid expansion",
         "validation": "prospective", "multiple_testing": "number of symbols is the number of tests"},
        ("a symbol survives costs, liquidity, concentration and tail screens on settlement-level data with an adequate effective sample size",), _COMMON_REJECT),
    StrategySpec(
        "btc_trend_following", 1, "BTC trend following (new version; needs untouched data)", HYPOTHESIS,
        {**_BTC_LINEAR, "direction": "directional"}, ("declared before evaluation",), ("declared before evaluation",),
        ("timestamp-safe bars",), "n/a", (1.0, 30.0), "to be measured", "to be declared", "to be declared", _RISK,
        {"registered": "not yet", "validation": "purged and embargoed; untouched holdout; block-bootstrap CI",
         "note": "the repository's historical data has already shaped design decisions and is not a fresh holdout"},
        ("prospective net edge over BTC hold after costs",), _COMMON_REJECT),
    StrategySpec(
        "btc_mean_reversion", 1, "BTC mean reversion (new version; needs untouched data)", HYPOTHESIS,
        {**_BTC_LINEAR, "direction": "directional"}, ("declared before evaluation",), ("declared before evaluation",),
        ("timestamp-safe bars",), "n/a", (0.1, 5.0), "to be measured", "to be declared", "to be declared", _RISK,
        {"registered": "not yet", "validation": "as btc_trend_following"},
        ("prospective net edge over BTC hold after costs",), _COMMON_REJECT),
    StrategySpec(
        "microstructure_imbalance", 1, "Order-book imbalance / short-horizon microstructure", HYPOTHESIS,
        {**_BTC_LINEAR, "horizon": "seconds to minutes"}, ("imbalance and spread conditions on validated books",),
        ("short time stop",), ("full-depth books with sequence ids",), "n/a", (0.0, 0.1),
        "adverse selection and queue position must be measured; displayed liquidity is not executable",
        "n/a", "n/a", _RISK, {"registered": "not yet", "validation": "replay of the hash-chained log, then prospective"},
        ("net edge after fees, adverse selection and latency on prospective data",), _COMMON_REJECT),
    StrategySpec(
        "maker_hybrid_execution", 1, "Maker-first and hybrid (post-only then taker chase) execution of the carry entry/exit", HYPOTHESIS,
        _BTC_LINEAR, ("measured fill probability, queue behaviour and adverse selection beat the taker round trip",),
        ("timeout then taker chase",), ("ACCOUNT_EXECUTION records of real maker attempts with fee records",), "n/a", (0.0, 0.1),
        "fill probability, queue position, partial fills, post-only rejection, adverse selection and legging risk must be MEASURED; "
        "a quote touch is not a fill and simulated fills are not executions",
        "n/a", "n/a", _RISK, {"registered": "not yet", "validation": "account execution records from a separately authorised, owner-run measurement"},
        ("maker or hybrid beats taker at the CONSERVATIVE bounds of measured fills",), _COMMON_REJECT),
    StrategySpec(
        "regime_classifier", 1, "Volatility / funding regime classifier (a modifier, not a strategy)", HYPOTHESIS,
        _BTC_LINEAR, ("uses only past observations",), (), ("timestamp-safe volatility and funding series",),
        "n/a", (0.0, 0.0), "n/a", "n/a", "n/a", {}, {"registered": "not yet", "validation": "timestamp-safe; cannot use future data"},
        ("changes the expected economics of a strategy out of sample",), _COMMON_REJECT),
)}


@dataclass
class Evaluation:
    key: str
    origin: str
    status: Optional[Status]
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    authorizes_trading: bool = False
    order_path_connected: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"strategy": self.key, "origin": self.origin, "status": None if self.status is None else self.status.value,
                "reasons": self.reasons, "metrics": self.metrics, "authorizes_trading": False,
                "order_path_connected": False}


def _insufficient(spec: StrategySpec, *why: str, **metrics: Any) -> Evaluation:
    return Evaluation(spec.key, spec.origin, Status.INSUFFICIENT_EVIDENCE, list(why), dict(metrics))


def evaluate_carry(spec: StrategySpec, ev: Mapping[str, Any]) -> Evaluation:
    """`ev` keys: validation_ok, chain_ok, reliability_verdict, funding_eval (funding_eval.evaluate output),
    economics (trade_economics.compute at the MEASURED costs and the realised mean funding LOWER bound),
    fillable_share, round_trip_cost_bps, prospective {registered_ms, first_evidence_ms, n_after_registration}."""
    reasons: List[str] = []
    if not ev.get("validation_ok"):
        reasons.append("evidence stream failed validation or holds no schema-v2 rows")
    if not ev.get("chain_ok"):
        reasons.append("evidence chain does not verify")
    if ev.get("reliability_verdict") != "HEALTHY":
        reasons.append(f"recorder is {ev.get('reliability_verdict')}: only HEALTHY evidence can support a screen")
    if ev.get("environment") != "mainnet":
        reasons.append(f"evidence environment is {ev.get('environment')!r}: only mainnet evidence can support a mainnet claim")
    fe = ev.get("funding_eval") or {}
    n = fe.get("joined", 0)
    ess = (fe.get("realised_bps") or {}).get("ess")
    if n < MIN_SETTLEMENTS:
        reasons.append(f"{n} settlements joined to a realised print; at least {MIN_SETTLEMENTS} needed")
    if fe.get("status") != "OK":
        reasons.extend(fe.get("status_reasons") or ["funding evaluation is not OK"])
    if (ev.get("economics") or {}).get("funding", {}).get("source") != "PUBLIC_REALISED":
        reasons.append("economics were not computed from the realised public funding print")
    if reasons:
        return _insufficient(spec, *reasons, settlements_joined=n, effective_sample_size=ess)
    econ = ev.get("economics") or {}
    cap = econ.get("capital") or {}
    risk = spec.risk_limits
    liq = (econ.get("liquidation") or {}).get("conservative_adverse_move_pct")
    worst = (fe.get("realised_bps") or {}).get("min")
    m = {"settlements_joined": n, "effective_sample_size": ess, "unpriced_inputs": list(econ.get("unknowns") or []),
         "annualised_pct_on_economic_capital":
         (econ.get("returns") or {}).get("annualised_pct_on_economic_capital"),
         "round_trip_cost_bps": ev.get("round_trip_cost_bps"), "fillable_share": ev.get("fillable_share"),
         "worst_realised_settlement_bps": worst, "liquidation_headroom_pct": liq}
    if not _num(liq):
        return Evaluation(spec.key, spec.origin, Status.RISK_UNACCEPTABLE, ["liquidation headroom is missing or not a finite number"], m)
    if liq < risk["min_liquidation_headroom_pct"]:
        return Evaluation(spec.key, spec.origin, Status.RISK_UNACCEPTABLE,
                          [f"liquidation headroom {liq:.1f}% < {risk['min_liquidation_headroom_pct']}%"], m)
    if cap.get("initial_margin_sufficient") is not True:
        return Evaluation(spec.key, spec.origin, Status.RISK_UNACCEPTABLE, ["initial margin not sufficient"], m)
    if not _num(worst):
        return Evaluation(spec.key, spec.origin, Status.RISK_UNACCEPTABLE, ["the worst realised settlement is missing or not finite"], m)
    if worst < -risk["max_single_settlement_loss_bps"]:
        return Evaluation(spec.key, spec.origin, Status.RISK_UNACCEPTABLE,
                          [f"a settlement paid {worst:.1f} bps against the position"], m)
    fs_, rt_ = ev.get("fillable_share"), ev.get("round_trip_cost_bps")
    if not (_num(fs_) and _num(rt_)) or fs_ < risk["min_fillable_share"] or rt_ > risk["max_round_trip_cost_bps"]:
        return Evaluation(spec.key, spec.origin, Status.EXECUTION_UNATTRACTIVE,
                          ["book-fillable share or round-trip cost missing, not finite, or outside the declared limits"], m)
    ret = m["annualised_pct_on_economic_capital"]
    lowest = min(TE.SCREENING_HURDLES_PCT)
    if not _num(ret) or ret < lowest:
        return Evaluation(spec.key, spec.origin, Status.ECONOMICALLY_UNATTRACTIVE,
                          [f"net {ret if not _num(ret) else round(ret, 2)}%/yr on economic capital (lower-bound funding) is below the lowest "
                           f"{TE.PROVISIONAL} screen of {lowest}%"], m)
    headroom = unknown_cost_headroom_bps(econ, lowest)
    m["unknown_cost_headroom_bps_round_trip"] = headroom
    gaps = reconciliation_gaps(ev, spec)
    m["reconciliation_gaps"] = gaps
    unpriced = ", ".join(m["unpriced_inputs"]) or "none"
    market_edge = _prospective_market_edge(ev, lowest, headroom)
    if market_edge is None:
        return Evaluation(spec.key, spec.origin, Status.RESEARCH_CANDIDATE,
                          [f"passes the {TE.PROVISIONAL} screens on MODELLED costs only (unpriced inputs: {unpriced}); "
                           "in-sample recorded evidence, not prospectively confirmed; authorises nothing",
                           f"not account profit: {len(gaps)} account-level requirement(s) are unmet"], m)
    if gaps:
        return Evaluation(spec.key, spec.origin, Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED,
                          ["registered before the evidence; the screens re-run on post-registration PUBLIC market data ONLY still pass, "
                           f"with {headroom:.1f} bps of round-trip headroom for the unpriced inputs ({unpriced})",
                           "this is a statement about the public market, NOT about any account's profit: public funding is not account "
                           "cash, a modelled fill is not an execution, a predicted rebate is not a rebate received",
                           "unmet for ECONOMICALLY_RECONCILED: " + "; ".join(gaps)], m)
    auth = _d(ev.get("execution_authorisation"))
    ref = auth.get("reference")
    if auth.get("owner_authorised") is True and _real_reference(ref) and auth.get("scope") == spec.key:
        return Evaluation(spec.key, spec.origin, Status.EXECUTION_READY,
                          ["economically reconciled from account records AND a separate owner execution-authorisation record "
                           f"({auth['reference']!r}) names this strategy. This engine authorises nothing: ready is not permission"], m)
    return Evaluation(spec.key, spec.origin, Status.ECONOMICALLY_RECONCILED,
                      ["every account-level requirement is met and the account-settled economics clear the lowest screen; "
                       "there is no separate execution-authorisation record, so this is NOT execution-ready"], m)


_PLACEHOLDER_TOKENS = frozenset(("placeholder", "todo", "tbd", "none", "null", "na", "dummy", "example", "sample", "fake", "lorem", "changeme",
                                 "unknown", "pending", "test", "testing", "tmp", "temp", "foo", "bar", "baz", "asdf", "qwerty", "reference", "ref"))


def _real_reference(ref: Any) -> bool:
    """A reference that could point at a record: at least 8 characters, at least 5 DISTINCT letters or digits, and NOT made only of the
    words people type when they have not got one ('TODO-TODO', 'pending-1', 'n/a n/a'). A placeholder word inside a real phrase
    ('sample-size-approval-77') is fine. This is a speed bump against typing 'xxxxxxxx', not proof that a record exists: the engine
    cannot open the owner's authorisation, and authorises nothing either way."""
    if not isinstance(ref, str):
        return False
    r = ref.strip()
    low = r.lower().replace("n/a", "na")
    words = [t for t in "".join(c if c.isalnum() else " " for c in low).split() if not t.isdigit()]
    return len(r) >= 8 and len({c for c in low if c.isalnum()}) >= 5 and bool(words) and not all(w in _PLACEHOLDER_TOKENS for w in words)


def unknown_cost_headroom_bps(econ: Mapping[str, Any], lowest_hurdle_pct: float) -> Optional[float]:
    """How many bps of ADDITIONAL round-trip cost (on the hedged notional) the modelled net can absorb before the
    annualised return on economic capital falls to the lowest screen. It is arithmetic on the modelled ledger, not an
    estimate of what latency or impact actually cost. None when the inputs are missing."""
    try:
        net = float(econ["net_pnl_usd"])
        cap = float(econ["capital"]["economic_capital_usd"])
        n = float(econ["capital"]["hedged_notional_usd"])
        hold = float(econ["position"]["hold_days"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(net) and cap > 0 and n > 0 and hold > 0):
        return None
    required = cap * (lowest_hurdle_pct / 100.0) * hold / 365.0
    return (net - required) / n * 1e4


EXTERNAL_ARCHIVE = "EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE"


def _prospective_market_edge(ev: Mapping[str, Any], lowest: float, headroom: Optional[float]) -> Optional[bool]:
    """Every claim is typed and finite: a string, a bool, a NaN or an infinity is not a registration time or a count. NOTHING in
    this repository registers a hypothesis or derives these figures from rows, and the report supplies none, so this is
    unreachable from the CLI today; it is the gate a future registration mechanism must satisfy, not evidence."""
    pro = _d(ev.get("prospective"))
    reg, first = pro.get("registered_ms"), pro.get("first_evidence_ms")
    n_after, ess, post = pro.get("n_after_registration"), pro.get("post_registration_ess"), pro.get("post_registration_annualised_pct_on_economic_capital")
    if (type(reg) is int and type(first) is int and 0 < reg < first
            and type(n_after) is int and n_after >= PROSPECTIVE_SETTLEMENTS
            and _num(ess) and ess >= PROSPECTIVE_SETTLEMENTS / 9.0
            and _num(post) and post >= lowest
            and integrity_grade(ev) == "ANCHORED"
            and _num(headroom) and headroom >= MIN_UNKNOWN_COST_HEADROOM_BPS):
        return True
    return None


def integrity_grade(ev: Mapping[str, Any]) -> str:
    """ANCHORED only when every chained evidence file has a current or lagging anchor that matches AND the anchors are held in
    an archive whose head matches a value the OWNER stored off the machine. Copies in ``monitor.jsonl`` sit on the recorder's own
    volume, so they grade ANCHORED_SAME_VOLUME_ONLY, which is not enough. Absent detail is NOT anchored."""
    i = _d(ev.get("integrity"))
    chained = ((i.get("files_total") if type(i.get("files_total")) is int else 0)
               - (i.get("files_legacy_pre_chain") if type(i.get("files_legacy_pre_chain")) is int else 0))
    if i.get("files_anchor_conflict") or i.get("files_missing_but_anchored"):
        return "CONFLICT"
    cur = i.get("files_anchored_current") if type(i.get("files_anchored_current")) is int else 0
    lag = i.get("files_anchored_lagging") if type(i.get("files_anchored_lagging")) is int else 0
    if chained > 0 and cur + lag == chained:
        # ANCHORED needs the ARCHIVE alone to anchor every chained file: anchors from both sources are merged for verification, so
        # a file that only monitor.jsonl (the recorder's own volume) anchors must not borrow the archive's location. A report that
        # does not say how many files the archive anchors (an older object) is not anchored.
        by_archive = i.get("files_anchored_by_archive")
        if i.get("anchor_location") == EXTERNAL_ARCHIVE and type(by_archive) is int and by_archive == chained:
            return "ANCHORED"
        return "ANCHORED_SAME_VOLUME_ONLY"
    return "NOT_FULLY_ANCHORED"


def _num(x: Any) -> bool:
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:                    # an int beyond float range (any JSON document can carry one) is not a usable number
        return False


def _names(x: Any) -> List[str]:
    """A list of names the caller claims, as strings; anything else is no names (the caller still sees that something was claimed)."""
    return [v for v in x if isinstance(v, str)] if isinstance(x, (list, tuple)) else []


def _d(x: Any) -> Mapping[str, Any]:
    """A container the caller claims is an object. Anything else is an EMPTY one, so every requirement that reads from it fails
    closed instead of raising."""
    return x if isinstance(x, Mapping) else {}


MAX_PLAUSIBLE_FEE_BPS = 100.0       # a per-side perpetual fee beyond 1 % is a unit error, not a tier


def reconciliation_gaps(ev: Mapping[str, Any], spec: Optional[StrategySpec] = None) -> List[str]:
    """Every account-level requirement that is NOT met, as readable strings; empty means the account evidence is complete.
    Nothing in this repository produces ``ev['account']``: until account reconciliation tooling exists and is run by the owner,
    every one of these is a gap, by design. A structurally complete object is necessary, NOT sufficient: its values are claims by
    whoever assembled it. So every field is type-strict (a bool is not an int, a string is not a number), bounded, and CROSS-CHECKED
    against the account ledger it is supposed to have produced; an unknown execution mode is treated as maker/hybrid, never as
    taker."""
    gaps: List[str] = []
    a = _d(ev.get("account"))
    econ = _d(ev.get("economics"))
    ae = _d(ev.get("account_economics"))
    comp = _d(ae.get("components_usd"))
    ledger_n = _d(ae.get("capital")).get("hedged_notional_usd")
    mode = _d(ev.get("execution")).get("mode")            # a MISSING mode is unknown, and unknown is treated as maker/hybrid
    fs = _d(a.get("fee_schedule"))

    def leg_bps(name: str) -> Optional[float]:
        v = comp.get(name)
        return (-v / ledger_n * 1e4) if (_num(v) and _num(ledger_n) and ledger_n > 0) else None

    def at_a_real_rate(bps: Optional[float], rates: Sequence[Any]) -> bool:
        return bps is not None and any(_num(r) and abs(bps - r) <= 1e-6 * max(1.0, abs(r)) for r in rates)

    # BOTH perpetual legs must have been charged at the account's TAKER rate. A leg at the maker rate would need the fill's liquidity
    # to be tied to the ledger (fee records vs fills), which nothing here can do, so it is a gap, not a pass: this check never lets a
    # status rise because a schedule HAS a lower rate. The spot legs of an overlay are zero; anything else cannot be checked against
    # a perpetual schedule.
    allowed = (fs.get("taker_bps"),)
    legs_ok = (at_a_real_rate(leg_bps("perp_entry_fee_usd"), allowed) and at_a_real_rate(leg_bps("perp_exit_fee_usd"), allowed)
               and comp.get("spot_entry_fee_usd") == 0 and comp.get("spot_exit_fee_usd") == 0)
    if not (fs.get("provenance") == "ACCOUNT_VERIFIED" and _num(fs.get("taker_bps")) and 0 < fs["taker_bps"] <= MAX_PLAUSIBLE_FEE_BPS
            and _num(fs.get("maker_bps")) and -MAX_PLAUSIBLE_FEE_BPS <= fs["maker_bps"] <= fs["taker_bps"]
            and _d(ae.get("fees")).get("verified") is True and legs_ok):
        gaps.append("account_fee_schedule: the account's real taker/maker rates are not read from the account, are implausible, or "
                    "differ from the fee the account ledger charged on EITHER perpetual leg, which must be the taker rate (published defaults are not them; a maker-rate "
                    "leg cannot be tied to a maker fill here; spot-leg "
                    "fees cannot be checked against a perpetual schedule)")
    q = _d(a.get("product_qualification"))
    if not (q.get("verdict") == "QUALIFIED" and q.get("environment") == "mainnet" and q.get("product") == "BTCUSDT linear"
            and isinstance(q.get("evidence_ref"), str) and len(q["evidence_ref"].strip()) >= 4):
        gaps.append("product_account_qualification: no QUALIFIED verdict for this account and product (EU is unqualified)")
    st = _d(a.get("funding_settlements"))
    ledger_settle = _d(ae.get("funding")).get("settlements")
    if not (st.get("source") == "ACCOUNT_SETTLED" and st.get("reconciled") is True and type(st.get("unreconciled")) is int
            and st["unreconciled"] == 0 and type(st.get("settlements_reconciled")) is int
            and st["settlements_reconciled"] >= MIN_ACCOUNT_SETTLEMENTS and st["settlements_reconciled"] == ledger_settle):
        gaps.append(f"account_funding_settlements: fewer than {MIN_ACCOUNT_SETTLEMENTS} ACCOUNT_SETTLED funding rows reconciled, any "
                    "unreconciled, or a count that differs from the account ledger (a public funding print is not account cash)")
    fl = _d(a.get("fills"))
    if not (fl.get("evidence_class") == "ACCOUNT_EXECUTION" and fl.get("reconciled") is True
            and all(type(fl.get(k)) is int for k in ("fills", "fee_records", "round_trips"))
            and fl["fills"] >= 2 * MIN_ACCOUNT_ROUND_TRIPS and fl["fee_records"] >= fl["fills"] and fl["round_trips"] >= MIN_ACCOUNT_ROUND_TRIPS
            and fl["round_trips"] <= fl["fills"] // 2):
        gaps.append("fills_and_fee_records: no reconciled ACCOUNT_EXECUTION fills with a fee record each, or more round trips than "
                    "the fills can form (a modelled fill is not an execution)")
    xc = _d(a.get("execution_costs"))
    prov = _d(xc.get("provenance"))
    needed = ("latency", "market_impact", "slippage")
    if mode != "taker":                      # maker, hybrid, MISSING, or any unrecognised value: fail closed
        needed += ("maker_fill_probability", "adverse_selection")
    unknowns = _names(econ.get("unknowns")) or _names(ae.get("unknowns"))
    if unknowns or econ.get("unknowns") or ae.get("unknowns") or any(prov.get(k) != "MEASURED" for k in needed):
        gaps.append("execution_and_operational_costs: " + (("unpriced inputs remain (" + ", ".join(unknowns or ["unreadable"]) + "); ")
                    if (econ.get("unknowns") or ae.get("unknowns")) else "")
                    + "not every input is MEASURED (" + ", ".join(k for k in needed if prov.get(k) != "MEASURED") + ")")
    cc = _d(a.get("capital"))
    if not (cc.get("reconciled") is True and cc.get("source") == "ACCOUNT_READ"):
        gaps.append("capital_and_collateral: balances and collateral are not reconciled against an account read")
    vr = _d(a.get("venue_rules"))
    if not (vr.get("verified") is True and isinstance(vr.get("source"), str) and len(vr["source"].strip()) >= 4):
        gaps.append("venue_margin_and_liquidation_rules: initial/maintenance margin, haircuts and liquidation rules are ASSUMED, not verified")
    lowest = min(TE.SCREENING_HURDLES_PCT)
    if not ((_d(ae.get("funding")).get("source") == "ACCOUNT_SETTLED" and _d(ae.get("fees")).get("verified") is True
             and not ae.get("unknowns") and _ledger_is_self_consistent(ae) and _ledger_return(ae) is not None and _ledger_return(ae) >= lowest)):
        gaps.append("account_economics: the ledger is not computed from ACCOUNT_SETTLED funding with verified fees and no unknown input, "
                    "does not recompute from its own components (net, return, margin sufficiency, liquidation headroom), "
                    f"or it does not clear the lowest {TE.PROVISIONAL} screen")
    return gaps


def _ledger_return(ae: Mapping[str, Any]) -> Optional[float]:
    """The annualised return on economic capital RECOMPUTED from the ledger's own net, capital and holding period, never the figure
    the object claims. None when any input is missing or not a usable number."""
    cap = _d(ae.get("capital")).get("economic_capital_usd")
    hold = _d(ae.get("position")).get("hold_days")
    net = ae.get("net_pnl_usd")
    if not (_num(cap) and cap > 0 and _num(hold) and hold > 0 and _num(net)):
        return None
    return net / cap * 365.0 / hold * 100.0


#: exactly the components ``trade_economics.compute`` writes: an unnamed extra line (``mystery_income_usd``) is a claim with no origin
_LEDGER_COMPONENTS = frozenset(("funding_income_usd", "basis_pnl_usd", "rebates_usd", "perp_entry_fee_usd", "perp_exit_fee_usd", "spot_entry_fee_usd",
                                "spot_exit_fee_usd", "spread_usd", "slippage_usd", "market_impact_usd", "latency_usd", "conversion_usd", "transfer_usd",
                                "custody_usd", "operational_usd", "borrow_usd"))
MAX_PLAUSIBLE_BASIS_BPS_OF_NOTIONAL = 100.0       # entry + exit basis on a BTC perp/spot pair: the largest 4h basis in the committed history is 23.5 bps


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def _ledger_is_self_consistent(ae: Mapping[str, Any]) -> bool:
    """A ledger that claims to have reconciled is RE-DERIVED from its own inputs, not read: its components are exactly the ones the engine
    writes and sum to its net; the funding line equals the notional times its own funding path; the economic capital equals inventory +
    posted margin + reserve; margin sufficiency and the liquidation headroom follow from its own collateral and notional; the settlement
    count matches the holding period; its claimed return equals the return recomputed from net, capital and hold; a rebate is not accepted
    (nothing here can tie one to a fill); and the basis P&L stays inside what a BTC perp/spot basis can be. Never raises."""
    try:
        comp = _d(ae.get("components_usd"))
        net = ae.get("net_pnl_usd")
        cap_d, pos_d, fund_d = _d(ae.get("capital")), _d(ae.get("position")), _d(ae.get("funding"))
        if set(comp) != _LEDGER_COMPONENTS or not all(_num(v) for v in comp.values()) or not _num(net):
            return False
        if ae.get("reconciles") is not True or not _close(math.fsum(float(v) for v in comp.values()), float(net)):
            return False
        n, pn = cap_d.get("hedged_notional_usd"), pos_d.get("notional_usd")
        inv, extra, reserve = cap_d.get("btc_inventory_usd"), cap_d.get("margin_posted_extra_usd"), cap_d.get("reserve_usd")
        ecap, avail, req = cap_d.get("economic_capital_usd"), cap_d.get("available_collateral_usd"), cap_d.get("required_initial_margin_usd")
        if not all(_num(v) for v in (n, pn, inv, extra, reserve, ecap, avail, req)) or n <= 0 or not _close(n, pn):
            return False
        if not _close(ecap, inv + extra + reserve):
            return False
        sum_bps, settlements, hold = fund_d.get("sum_bps"), fund_d.get("settlements"), pos_d.get("hold_days")
        if not (_num(sum_bps) and type(settlements) is int and _num(hold) and hold > 0):
            return False
        if not _close(comp["funding_income_usd"], n * sum_bps / 1e4) or abs(settlements - 3.0 * hold) > 1.0:
            return False
        if comp["rebates_usd"] != 0 or abs(comp["basis_pnl_usd"]) > n * MAX_PLAUSIBLE_BASIS_BPS_OF_NOTIONAL / 1e4:
            return False
        if cap_d.get("initial_margin_sufficient") is not True or not avail >= req:
            return False
        liq = _d(ae.get("liquidation")).get("conservative_adverse_move_pct")
        if not (_num(liq) and liq >= _RISK["min_liquidation_headroom_pct"] and liq <= avail / n * 100.0 + 1e-9):
            return False
        claimed = _d(ae.get("returns")).get("annualised_pct_on_economic_capital")
        recomputed = _ledger_return(ae)
        return _num(claimed) and recomputed is not None and math.isclose(claimed, recomputed, rel_tol=1e-6, abs_tol=1e-6)
    except (OverflowError, ValueError, TypeError, ArithmeticError):
        return False


def basis_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    xs = [r["basis_bps"] for r in rows if isinstance(r.get("basis_bps"), (int, float)) and math.isfinite(r["basis_bps"])]
    if len(xs) < 3:
        return {"n": len(xs)}
    s = sorted(xs)
    pick = lambda p: s[min(len(s) - 1, int(round(p * (len(s) - 1))))]      # noqa: E731
    rho = ES.autocorr(xs, 1)
    half = (-math.log(2.0) / math.log(rho)) if rho is not None and 0 < rho < 1 else None
    days = {int(r["at_ms"] // 86_400_000) for r in rows}
    return {"n": len(xs), "independent_days": len(days), "mean_bps": ES.mean(xs),
            "std_bps": math.sqrt(sum((x - ES.mean(xs)) ** 2 for x in xs) / (len(xs) - 1)),
            "p01_bps": pick(0.01), "p05_bps": pick(0.05), "p95_bps": pick(0.95), "p99_bps": pick(0.99),
            "lag1_autocorr_per_snapshot": rho, "half_life_snapshots": half}


def _evidence_reasons(ev: Mapping[str, Any]) -> List[str]:
    """The reasons the RECORDED evidence cannot support any screen (the same gates every evaluator applies first)."""
    reasons: List[str] = []
    if not ev.get("validation_ok"):
        reasons.append("evidence stream failed validation or holds no schema-v2 rows")
    if not ev.get("chain_ok"):
        reasons.append("evidence is not completely chained and consistent (an unchained row or a failed chain)")
    if ev.get("reliability_verdict") != "HEALTHY":
        reasons.append(f"recorder is {ev.get('reliability_verdict')}: only HEALTHY evidence can support a screen")
    if ev.get("environment") != "mainnet":
        reasons.append(f"evidence environment is {ev.get('environment')!r}: only mainnet evidence can support a mainnet claim")
    return reasons


def _grade(spec: StrategySpec, family: Sequence[Mapping[str, Any]], what: str, metrics: Dict[str, Any]) -> Evaluation:
    """Map the measured variants of ONE hypothesis to a status. ``family`` holds, per variant, ``mean`` and the interval
    (already at the FAMILY alpha) of the quantity that must be positive. A candidate needs a variant whose lower bound is
    above zero; a hypothesis whose every adequately sampled variant has a non-positive mean is unattractive; anything else
    cannot be told from zero. Never above RESEARCH_CANDIDATE: no registration or prospective path exists for these yet."""
    sampled = [f for f in family if f.get("ok")]
    metrics = dict(metrics, family_trials=FAMILY_TRIALS, family_alpha=FAMILY_ALPHA)
    if not sampled:
        return _insufficient(spec, f"no predeclared variant has enough {what} to be judged", **metrics)
    winners = [f for f in sampled if f["lo"] is not None and f["lo"] > 0]
    if winners:
        best = max(winners, key=lambda f: f["lo"])
        return Evaluation(spec.key, spec.origin, Status.RESEARCH_CANDIDATE,
                          [f"variant {best['label']} has a lower bound above zero at the family-wise level (alpha {FAMILY_ALPHA:.4f} for "
                           f"{FAMILY_TRIALS} predeclared variants across all hypotheses); in-sample, not prospectively confirmed; authorises nothing",
                           "net of the stated costs only; unpriced inputs and account-level facts are unresolved"], metrics)
    if all(f["mean"] is not None and f["mean"] <= 0 for f in sampled):
        return Evaluation(spec.key, spec.origin, Status.ECONOMICALLY_UNATTRACTIVE,
                          [f"every adequately sampled variant ({len(sampled)}) has a non-positive mean net of costs"], metrics)
    return _insufficient(spec, "a positive mean in at least one variant cannot be told from zero at the family-wise level", **metrics)


def evaluate_basis(spec: StrategySpec, rows: Sequence[Mapping[str, Any]], *, round_trip_cost_bps: Optional[float],
                   ev: Optional[Mapping[str, Any]] = None) -> Evaluation:
    """Measure real convergence episodes under each predeclared threshold. The 5-95 % range exceeding the cost is only an
    exploratory screen (``basis_stats``) and is reported, never graded."""
    st = basis_stats(rows)
    reasons = _evidence_reasons(ev) if ev is not None else []
    if round_trip_cost_bps is None:
        reasons.append("no measured round-trip cost")
    span = (rows[-1]["at_ms"] - rows[0]["at_ms"]) / 86_400_000.0 if len(rows) >= 2 else 0.0
    if span < MIN_SPAN_DAYS:
        reasons.append(f"{span:.1f} days of basis recorded; at least {MIN_SPAN_DAYS} needed")
    if reasons:
        return _insufficient(spec, *reasons, exploratory_screen=st)
    g = PREDECLARED[spec.key]
    fam = []
    for mult in g["theta_in_cost_multiples"]:
        tin = round_trip_cost_bps * mult
        res = SM.basis_episodes(rows, SM.BasisParams(tin, tin * g["theta_out_fraction_of_in"], g["max_hold_h"] * 3600.0, round_trip_cost_bps),
                                alpha=FAMILY_ALPHA)
        ci = res["net"]["ci"]
        fam.append({"label": f"theta_in={mult}x cost", "ok": res["episodes"] >= MIN_EPISODES, "mean": res["net"]["mean"],
                    "lo": ci.get("lo") if ci.get("status") == "OK" else None, "episodes": res["episodes"],
                    "failure_share": res["failure_share"], "adverse_widening_bps": res["adverse_widening_bps"],
                    "hold_hours": res["hold_hours"]})
    return _grade(spec, fam, f"episodes ({MIN_EPISODES} needed)", {"variants": fam, "exploratory_screen": st,
                                                                    "reading": "basis P&L only; funding earned during the hold is not added"})


def _mid_series(rows: Sequence[Mapping[str, Any]]) -> Tuple[List[float], float]:
    mids, ts = [], []
    for r in rows:
        m = r.get("perp_mid")
        if SM._fin(m) and m > 0:
            mids.append(float(m))
            ts.append(r["at_ms"])
    if len(ts) < 3:
        return mids, 0.0
    dts = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    med_s = dts[len(dts) // 2] / 1000.0
    return mids, (365.0 * 86400.0 / med_s if med_s > 0 else 0.0)


def _directional(spec: StrategySpec, ev: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], make_rule, variants, cost_bps: Optional[float]) -> Evaluation:
    reasons = _evidence_reasons(ev)
    mids, bars_per_year = _mid_series(rows)
    span = (rows[-1]["at_ms"] - rows[0]["at_ms"]) / 86_400_000.0 if len(rows) >= 2 else 0.0
    if span < MIN_DIRECTIONAL_DAYS:
        reasons.append(f"{span:.1f} days of price recorded; at least {MIN_DIRECTIONAL_DAYS} needed (already-examined history is not a fresh holdout)")
    if cost_bps is None:
        reasons.append("no measured one-way cost: a directional result without it is not an edge")
    if reasons:
        return _insufficient(spec, *reasons, days=span)
    fam = []
    for v in variants:
        rule = make_rule(v)
        warm = max(v[0] if isinstance(v[0], int) else 0, v[1] if len(v) > 1 and isinstance(v[1], int) else 0, 1)
        res = SM.run_directional(mids, rule, one_way_cost_bps=cost_bps, bars_per_year=bars_per_year, eval_start=warm * 3, alpha=FAMILY_ALPHA)
        if res.get("status") != "MEASURED":
            continue
        net, exc = res["net_per_bar"], res["excess_vs_hold_per_bar"]
        lo = net["ci"].get("lo") if net["ci"].get("status") == "OK" else None
        lo_x = exc["ci"].get("lo") if exc["ci"].get("status") == "OK" else None
        both = lo if (lo is not None and lo_x is not None and lo_x > 0) else (None if lo is None else min(lo, lo_x if lo_x is not None else -1.0))
        fam.append({"label": str(v), "ok": res["bars"] >= 5000, "mean": net["mean"], "lo": both, "net_annualised_pct": res["net_annualised_pct"],
                    "hold_annualised_pct": res["hold_annualised_pct"], "max_drawdown_pct": res["max_drawdown_pct"]})
    return _grade(spec, fam, "bars (5000 needed)", {"variants": fam, "days": span, "baseline": "buy-and-hold over the same window; "
                                                      "a variant must beat zero AND buy-and-hold"})


def evaluate_trend(spec: StrategySpec, ev: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], cost_bps: Optional[float]) -> Evaluation:
    return _directional(spec, ev, rows, lambda v: SM.EMACross(*v), PREDECLARED[spec.key]["ema_bars"], cost_bps)


def evaluate_mean_reversion(spec: StrategySpec, ev: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], cost_bps: Optional[float]) -> Evaluation:
    return _directional(spec, ev, rows, lambda v: SM.ZScoreReversion(*v), PREDECLARED[spec.key]["zscore_bars"], cost_bps)


def evaluate_microstructure(spec: StrategySpec, ev: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], cost_bps: Optional[float]) -> Evaluation:
    reasons = _evidence_reasons(ev)
    imb = [(r.get("book") or {}).get("imbalance") for r in rows]
    if not rows or any(not SM._fin(x) for x in imb):
        reasons.append("NOT_RECORDED: the capacity rows carry level counts, not top-of-book imbalance; the full-depth log holds it but "
                       "no extraction into evidence rows exists, so this hypothesis cannot be measured on recorded data")
    if cost_bps is None:
        reasons.append("no measured one-way cost")
    if reasons:
        return _insufficient(spec, *reasons)
    mids, _ = _mid_series(rows)
    fam = []
    g = PREDECLARED[spec.key]
    for h in g["horizon_snapshots"]:
        res = SM.imbalance_trades(imb, mids, horizon=h, theta=g["theta"], one_way_cost_bps=cost_bps, alpha=FAMILY_ALPHA)
        ci = res["net_bps_per_trade"]["ci"]
        fam.append({"label": f"horizon={h}", "ok": res["trades"] >= MIN_EPISODES, "mean": res["net_bps_per_trade"]["mean"],
                    "lo": ci.get("lo") if ci.get("status") == "OK" else None, "trades": res["trades"], "fill_assumption": res["fill_assumption"]})
    return _grade(spec, fam, f"trades ({MIN_EPISODES} needed)", {"variants": fam})


def evaluate_regime(spec: StrategySpec, ev: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], fe: Optional[Mapping[str, Any]]) -> Evaluation:
    reasons = _evidence_reasons(ev)
    if not fe or fe.get("status") != "OK":
        reasons.append("the funding evaluation is not OK: there is no trustworthy per-settlement series to condition")
    if reasons:
        return _insufficient(spec, *reasons)
    g = PREDECLARED[spec.key]
    mids, _ = _mid_series(rows)
    labels_row = SM.regime_labels(mids, window=g["vol_window_bars"], min_history=g["min_history_bars"])
    ts = [r["at_ms"] for r in rows if SM._fin(r.get("perp_mid")) and r["perp_mid"] > 0]
    labels, values = [], []
    for rec in FE.join(rows)["joined"]:
        cut = rec["settlement_ms"] - 30 * 60_000                       # the label known 30 minutes before the settlement
        idx = max((i for i, t in enumerate(ts) if t <= cut), default=None)
        labels.append(labels_row[idx] if idx is not None and idx < len(labels_row) else None)
        values.append(rec["realised_bps"])
    res = SM.regime_conditioning(labels, values, alpha=FAMILY_ALPHA)
    if res.get("status") != "MEASURED":
        return _insufficient(spec, res.get("reason", "regime conditioning not measurable"), **{k: v for k, v in res.items() if k != "reason"})
    if res["improves_out_of_sample"]:
        return Evaluation(spec.key, spec.origin, Status.RESEARCH_CANDIDATE,
                          ["conditioning on the chosen regime improved the second half at the family-wise level; a modifier: it changes the "
                           "carry's economics only if confirmed prospectively"], res)
    return _insufficient(spec, "no out-of-sample improvement from conditioning on a volatility regime was detected", **res)


def evaluate_execution(spec: StrategySpec, ev: Mapping[str, Any]) -> Evaluation:
    """Maker and hybrid execution are SUPPORTED only by measured account fills; no assumed parameter changes that. Nothing in this
    repository produces account fills, so the default answer is: unsupported, taker only. If records are supplied the verdict comes
    from ``execution_model.recommend_mode`` at the conservative bounds of what they show."""
    recs = ev.get("account_execution_records")
    m = EM.measure_account_execution(recs)
    metrics = {"measured_section": {k: m[k] for k in ("status", "reasons", "minimums") if k in m}, "verdict": EM.MAKER_NOT_SUPPORTED}
    if m["status"] != "MEASURED":
        return _insufficient(spec, "maker and hybrid economics are UNSUPPORTED: " + ", ".join(m.get("reasons") or [m["status"]]),
                             **metrics, taker_remains_the_only_supported_mode=True,
                             need=f">= {m['minimums']['maker_attempts']} maker attempts, >= {m['minimums']['maker_fills']} fills over >= 24 h, with a fee record each")
    scen = ev.get("execution_scenario")
    if scen is None:
        return _insufficient(spec, "measured fills are present but no depth snapshot scenario was supplied to price them against the taker baseline", **metrics)
    rec = EM.recommend_mode(scen, recs)
    metrics["verdict"] = rec["verdict"]
    if rec["verdict"] == EM.MAKER_SUPPORTED_BY_MEASURED_FILLS:
        return Evaluation(spec.key, spec.origin, Status.RESEARCH_CANDIDATE,
                          ["hybrid beats taker at the conservative bounds of the measured fills; account-level reconciliation and prospective "
                           "confirmation are still required; authorises nothing"], {**metrics, "recommendation": rec})
    return Evaluation(spec.key, spec.origin, Status.ECONOMICALLY_UNATTRACTIVE if rec["verdict"] == EM.TAKER_ONLY else Status.INSUFFICIENT_EVIDENCE,
                      [f"verdict {rec['verdict']}: " + "; ".join(rec["reasons"])], {**metrics, "recommendation": rec})


def evaluate_cross_sectional(spec: StrategySpec, ev: Mapping[str, Any], rows_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]]) -> Evaluation:
    syms = sorted(s for s, r in rows_by_symbol.items() if r)
    reasons = _evidence_reasons(ev)
    if len(syms) < 3:
        reasons.append(f"{len(syms)} instrument(s) recorded ({syms or 'none'}); the recorder is configured for one symbol, so a cross-section "
                       "needs a multi-instrument recording first (at least 3, each with its own depth and fee schedule)")
    if reasons:
        return _insufficient(spec, *reasons, symbols=syms)
    g = PREDECLARED[spec.key]
    res = SM.cross_sectional_funding(rows_by_symbol, baseline="BTCUSDT", k=g["top_k"], hold_settlements=g["hold_settlements"],
                                     offset_min=g["offset_min"], alpha=FAMILY_ALPHA)
    if res.get("status") != "MEASURED":
        return _insufficient(spec, res.get("reason", "not measurable"), symbols=syms)
    e = res["net_edge_bps_per_settlement_vs_baseline"]
    ic = res["rank_information_coefficient"]
    fam = [{"label": "top-1 vs BTC", "ok": res["aligned_settlements"] >= MIN_ALIGNED_SETTLEMENTS, "mean": e["mean"],
            "lo": e["ci"].get("lo") if e["ci"].get("status") == "OK" else None}]
    ev_out = _grade(spec, fam, f"aligned settlements ({MIN_ALIGNED_SETTLEMENTS} needed)", {"measured": res})
    if ev_out.status == Status.RESEARCH_CANDIDATE:
        ev_out.reasons.append(f"rank information coefficient {ic['mean']:.2f}: a high-funding instrument beats BTC whatever the forecast skill; "
                              "the coefficient says whether the FORECASTS add anything")
    return ev_out


def evaluate_btc_fade(spec: StrategySpec, slice57: Optional[Mapping[str, Any]]) -> Evaluation:
    m: Dict[str, Any] = {}
    reasons = ["the historical OOS used data that had already been examined, so it is not a fresh holdout",
               "the carry engine does not execute this signal; it is a directional paper shadow"]
    if slice57:
        fr = slice57.get("HOW_FRAGILE_THIS_RESULT_IS") or {}
        oos = slice57.get("oos") or {}
        q = slice57.get("q1_q2") or {}
        m = {"n_oos": oos.get("n_trades"), "n_floor": (fr.get("n_margin") or {}).get("floor"),
             "m1_margin_replicates": (fr.get("m1_margin") or {}).get("margin_in_replicates"),
             "q1_mean_net_r": (q.get("q1") or {}).get("mean_net_r"), "q2_mean_net_r": (q.get("q2") or {}).get("mean_net_r"),
             "top3_share_of_total_r": q.get("top_3_trades_share_of_total_r")}
        reasons.append(f"cleared the gate by {m['m1_margin_replicates']} of 1500 replicates and by "
                       f"{(m['n_oos'] or 0) - (m['n_floor'] or 0)} trade(s); the second half mean "
                       f"({m['q2_mean_net_r']:.3f} R) is a fraction of the first ({m['q1_mean_net_r']:.3f} R)"
                       if all(isinstance(m.get(k), (int, float)) for k in ("m1_margin_replicates", "n_oos", "n_floor", "q1_mean_net_r", "q2_mean_net_r"))
                       else "thin margins reported in its own artefact")
    else:
        reasons.append("the slice 57 artefact was not found in this environment (it is not shipped in the drill image): "
                       "its fragility metrics were NOT loaded")
    return Evaluation(spec.key, spec.origin, Status.INSUFFICIENT_EVIDENCE, reasons, m)


def evaluate_unrun(spec: StrategySpec) -> Evaluation:
    need = ", ".join(spec.required_inputs) or "its declared inputs"
    return _insufficient(spec, f"registered as a hypothesis; not evaluated: needs {need} and untouched prospective data; "
                               f"protocol: {spec.research_protocol.get('validation', 'to be declared')}")


def frozen(spec: StrategySpec) -> Dict[str, Any]:
    return {"strategy": spec.key, "origin": FROZEN_ABSENT, "status": None, "authorizes_trading": False,
            "order_path_connected": False, "reasons": ["frozen absent: never re-evaluated; its historical rejection stands"]}


def evaluate_all(bundle: Mapping[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    carry = bundle.get("carry") or {}
    rt = carry.get("round_trip_cost_bps")
    one_way = None if rt is None else rt / 2.0         # the overlay's round trip is perpetual-only: half of it is one trade
    for key, spec in REGISTRY.items():
        if spec.origin == FROZEN_ABSENT:
            out.append(frozen(spec))
        elif key == "carry_overlay_taker/v1" or key == "candidate_carry_depth/v1":
            e = evaluate_carry(spec, bundle.get("carry") or {})
            if key == "candidate_carry_depth/v1":
                # its own depth-benefit entry rule is not evaluated against recorded evidence, so no screen is claimed
                # for it; the shared carry evidence is shown for reference only
                ref = None if e.status is None else e.status.value
                e = Evaluation(spec.key, spec.origin, Status.INSUFFICIENT_EVIDENCE,
                               ["order-less recommendation: its own depth-benefit entry rule is not evaluated against recorded "
                                "evidence, so no screen is claimed for it",
                                f"for reference, the same carry evidence gives the always-on overlay: {ref}"],
                               {**e.metrics, "carry_overlay_status": ref})
            out.append(e.as_dict())
        elif key == "funding_carry_fade_btc/v1":
            out.append(evaluate_btc_fade(spec, bundle.get("slice57")).as_dict())
        elif key == "basis_convergence/v1":
            out.append(evaluate_basis(spec, bundle.get("rows") or [], round_trip_cost_bps=carry.get("round_trip_cost_bps"), ev=carry).as_dict())
        elif key == "cross_sectional_funding/v1":
            out.append(evaluate_cross_sectional(spec, carry, bundle.get("rows_by_symbol") or {}).as_dict())
        elif key == "btc_trend_following/v1":
            out.append(evaluate_trend(spec, carry, bundle.get("rows") or [], one_way).as_dict())
        elif key == "btc_mean_reversion/v1":
            out.append(evaluate_mean_reversion(spec, carry, bundle.get("rows") or [], one_way).as_dict())
        elif key == "microstructure_imbalance/v1":
            out.append(evaluate_microstructure(spec, carry, bundle.get("rows") or [], one_way).as_dict())
        elif key == "maker_hybrid_execution/v1":
            out.append(evaluate_execution(spec, carry).as_dict())
        elif key == "regime_classifier/v1":
            out.append(evaluate_regime(spec, carry, bundle.get("rows") or [], carry.get("funding_eval")).as_dict())
        else:
            out.append(evaluate_unrun(spec).as_dict())
    return out
