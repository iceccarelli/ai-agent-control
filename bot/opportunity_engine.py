"""An order-less registry and evaluator of Bybit trading hypotheses.

Every candidate is a ``StrategySpec``: identity and version, instrument/product, entry and
exit conditions, the market inputs it needs, funding and execution assumptions, holding period,
sizing, capital, risk limits, its research protocol and its acceptance/rejection criteria. An
evaluator turns the recorded evidence into one of six statuses:

  RESEARCH_CANDIDATE         passes the screens on what was recorded; not validated
  INSUFFICIENT_EVIDENCE      not enough, or untrustworthy, evidence to say anything
  ECONOMICALLY_UNATTRACTIVE  the net edge on economic capital misses even the lowest screen
  EXECUTION_UNATTRACTIVE     cost or fillability destroys the edge
  RISK_UNACCEPTABLE          the risk limits are breached
  PROSPECTIVELY_VALIDATED    pre-registered, then confirmed on data that arrived afterwards

NO STATUS AUTHORISES TRADING, and nothing here can send an order: this module imports no
client, broker, store or configuration, a test pins that, and every spec carries
``order_path_connected = False``. `funding_carry_fade_v1` is FROZEN ABSENT: it is listed so its
state is explicit, it is never re-evaluated, and its rejection is not weakened.

The evaluators are deliberately conservative about what they accept: a stale recorder, a broken
evidence chain, a mixed stream, or fewer than 90 settlements (or too small an effective sample size) is INSUFFICIENT, and
PROSPECTIVELY_VALIDATED needs a registration timestamp that precedes the evidence it is judged on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

import evidence_stats as ES
import trade_economics as TE


class Status(str, Enum):
    RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ECONOMICALLY_UNATTRACTIVE = "ECONOMICALLY_UNATTRACTIVE"
    EXECUTION_UNATTRACTIVE = "EXECUTION_UNATTRACTIVE"
    RISK_UNACCEPTABLE = "RISK_UNACCEPTABLE"
    PROSPECTIVELY_VALIDATED = "PROSPECTIVELY_VALIDATED"


EXECUTED_BY_ENGINE = "EXECUTED_BY_ENGINE"            # paper/testnet only; live is locked
ORDER_LESS = "ORDER_LESS_RECOMMENDATION"
HYPOTHESIS = "RESEARCH_HYPOTHESIS"
FROZEN_ABSENT = "FROZEN_ABSENT"

MIN_SETTLEMENTS = 90                # 30 days of settlements (serially dependent: the ESS gate in funding_eval also applies)
PROSPECTIVE_SETTLEMENTS = 270       # 90 days after registration


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
    if liq is not None and liq < risk["min_liquidation_headroom_pct"]:
        return Evaluation(spec.key, spec.origin, Status.RISK_UNACCEPTABLE,
                          [f"liquidation headroom {liq:.1f}% < {risk['min_liquidation_headroom_pct']}%"], m)
    if not cap.get("initial_margin_sufficient", False):
        return Evaluation(spec.key, spec.origin, Status.RISK_UNACCEPTABLE, ["initial margin not sufficient"], m)
    if worst is not None and worst < -risk["max_single_settlement_loss_bps"]:
        return Evaluation(spec.key, spec.origin, Status.RISK_UNACCEPTABLE,
                          [f"a settlement paid {worst:.1f} bps against the position"], m)
    if (ev.get("fillable_share") is None or ev["fillable_share"] < risk["min_fillable_share"]
            or ev.get("round_trip_cost_bps") is None or ev["round_trip_cost_bps"] > risk["max_round_trip_cost_bps"]):
        return Evaluation(spec.key, spec.origin, Status.EXECUTION_UNATTRACTIVE,
                          ["book-fillable share or round-trip cost outside the declared limits"], m)
    ret = m["annualised_pct_on_economic_capital"]
    lowest = min(TE.SCREENING_HURDLES_PCT)
    if ret is None or ret < lowest:
        return Evaluation(spec.key, spec.origin, Status.ECONOMICALLY_UNATTRACTIVE,
                          [f"net {ret if ret is None else round(ret, 2)}%/yr on economic capital (lower-bound funding) is below the lowest "
                           f"{TE.PROVISIONAL} screen of {lowest}%"], m)
    pro = ev.get("prospective") or {}
    post = pro.get("post_registration_annualised_pct_on_economic_capital")
    if (pro.get("registered_ms") and pro.get("first_evidence_ms") and pro["registered_ms"] < pro["first_evidence_ms"]
            and pro.get("n_after_registration", 0) >= PROSPECTIVE_SETTLEMENTS
            and pro.get("post_registration_ess", 0) >= PROSPECTIVE_SETTLEMENTS / 9.0
            and post is not None and post >= lowest):
        return Evaluation(spec.key, spec.origin, Status.PROSPECTIVELY_VALIDATED,
                          ["registered before the evidence; the screens re-run on post-registration settlements ONLY still pass; "
                           "authorises nothing"], m)
    unpriced = ", ".join(m["unpriced_inputs"]) or "none"
    return Evaluation(spec.key, spec.origin, Status.RESEARCH_CANDIDATE,
                      [f"passes the {TE.PROVISIONAL} screens on MODELLED costs only (unpriced inputs: {unpriced}); "
                       "recorded evidence, not prospectively validated; authorises nothing"], m)


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


def evaluate_basis(spec: StrategySpec, rows: Sequence[Mapping[str, Any]], *, round_trip_cost_bps: Optional[float]) -> Evaluation:
    st = basis_stats(rows)
    if st.get("n", 0) < 3 or st.get("independent_days", 0) < 14:
        return _insufficient(spec, f"{st.get('independent_days', 0)} independent days of basis recorded; at least 14 needed", **st)
    if round_trip_cost_bps is None:
        return _insufficient(spec, "no measured round-trip cost", **st)
    spread_capture = st["p95_bps"] - st["p05_bps"]
    if spread_capture <= round_trip_cost_bps:
        return Evaluation(spec.key, spec.origin, Status.ECONOMICALLY_UNATTRACTIVE,
                          [f"the 5-95% basis range ({spread_capture:.1f} bps) does not exceed the round-trip cost "
                           f"({round_trip_cost_bps:.1f} bps)"], st)
    return Evaluation(spec.key, spec.origin, Status.RESEARCH_CANDIDATE,
                      ["in-sample basis range exceeds cost; episodes, convergence time and adverse widening still unmeasured"], st)


def evaluate_cross_sectional(spec: StrategySpec, rows_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]]) -> Evaluation:
    syms = sorted(s for s, r in rows_by_symbol.items() if r)
    if len(syms) < 2:
        return _insufficient(spec, f"only {syms or 'no symbols'} recorded; a cross-section needs at least two on one clock",
                             symbols=syms)
    return _insufficient(spec, "a per-symbol funding/depth evaluation pipeline for a second instrument is not implemented; "
                               "recording more symbols is the prerequisite", symbols=syms)


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
            out.append(evaluate_basis(spec, bundle.get("rows") or [],
                                      round_trip_cost_bps=(bundle.get("carry") or {}).get("round_trip_cost_bps")).as_dict())
        elif key == "cross_sectional_funding/v1":
            out.append(evaluate_cross_sectional(spec, bundle.get("rows_by_symbol") or {}).as_dict())
        else:
            out.append(evaluate_unrun(spec).as_dict())
    return out
