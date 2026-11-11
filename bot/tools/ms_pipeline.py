"""ms_pipeline: the multi-symbol system as ONE working pipeline, REPLAY / PAPER ONLY. It owns the seams between the independently built modules and nothing else.

    run(series_or_history_root, *, spec, candidate_id, spec_digest, replay_cfg, artifacts_dir, risk_policy=None, mode="REPLAY", ...) -> PipelineResult

What runs, in the order it runs (every step is the REAL module; nothing is stubbed):
  history      ``candidate_readiness.load_history_root`` (verified datasets only) OR injected ``ReplaySeries`` / ``SymbolSeries`` (labelled ``INJECTED_TEST_DATA``, never MEASURED)
  scan         ``ms_scanner.scan`` behind an adapter: the instruments are viewed AT the decision time (status from the lifecycle bounds), the snapshots are the replay world's
               point-in-time snapshots, spec / candidate / digest are bound
  allocate     ``ms_portfolio.allocate`` (inside ``ms_replay.run_replay``) with ``ms_risk.risk_assess`` and a policy that satisfies ms_risk's CLOSED schema
  authority    ``ms_authority.MultiSymbolAuthority`` sits in the loop: the plan is registered (risk-assessed again on fresh state) BEFORE it is submitted and EVERY leg body that would
               be sent is passed through ``authorize(body, venue=...)``; a refusal means the order never reaches the venue. The authority is bound to an in-process SIMULATION transport
  execute      ``ms_execution.ExecutionEngine`` + ``SimVenue`` driven by ``ms_fillmodel`` (maker fills need trade-through; fees are charged; funding is booked at the print time by side)
  account      ``ms_accounting.Ledger`` behind ``LedgerAdapter`` (the replay's ledger contract: record_fill / record_funding / mark / pnl()['net'] / record_slippage)
  artifacts    the six control-centre JSON files in EXACTLY the control_centre schema, plus ``pipeline_summary.json`` (not read by the control centre) with the full report

Nothing real is ever sent: there is no client, no credential, no network and no wall clock in this module (``generated_ms`` and the PAPER clock are injected).

Honesty rules enforced here (each has a test and a mutation):
  * artifact bases: replay results SIMULATED; opportunities ESTIMATED unless EVERY item is derived from measured/stated inputs (then CALCULATED); the competition file SIMULATED when its
    numbers come from replay round trips, else ESTIMATED. An item's provenance is the WEAKEST of its inputs; injected data can never be MEASURED (capped at ASSUMED).
  * unknown stays unknown: a ledger component that is unknown makes the artifact's net ``null`` (the control centre shows MISSING), never zero.
  * the pipeline never reports SUFFICIENT_EVIDENCE: a replay is not evidence of execution.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_authority as EA  # noqa: E402
import ms_accounting as MACC  # noqa: E402
import ms_authority as MAUTH  # noqa: E402
import ms_execution as EX  # noqa: E402
from ms_fillmodel import Tick as FM_Tick  # noqa: E402
import ms_portfolio as PF  # noqa: E402
import ms_reconcile as RC  # noqa: E402
import ms_replay as RP  # noqa: E402
import ms_risk as MR  # noqa: E402
import ms_scanner as SC  # noqa: E402
import ms_types as T  # noqa: E402

SCHEMA = "pipeline_summary/v1"
DATA_INJECTED = "INJECTED_TEST_DATA"
DATA_HISTORY = "VERIFIED_HISTORY_ROOT"
MODES = ("REPLAY", "PAPER")                   # what this pipeline (and its CLI) runs; TESTNET is a separate, owner-invoked session tool and is refused here; there is NO mainnet mode anywhere
STATUS_CODES = ("REJECTED_CANDIDATE", "INADEQUATE_HISTORY", "NO_OPPORTUNITY", "NO_ALLOCATION", "RISK_REFUSAL", "NO_FILL", "UNKNOWN_OR_UNRECONCILED", "INADEQUATE_SAMPLE", "SIMULATED_EXECUTION")
#: the competition readiness levels at which candidate_competition itself ranks a candidate in its top tier (candidate_competition._tier == PROMISING); nothing below may allocate
PROMOTED_READINESS = ("PROMISING", "PROSPECTIVE", "EXECUTION_READY", "OWNER_PROFIT_EVIDENCED")
BLOCKING_VERDICTS = ("REJECTED", "UNTESTED", "INSUFFICIENT_DATA")
#: which scanner kind each registered candidate family runs as. A family not listed (microstructure, cross-venue, regime conditioning) is NOT runnable by this pipeline.
CANDIDATE_KIND = {"cash_and_carry": "SPOT_PERP_CARRY", "spot_perp_basis": "SPOT_PERP_CARRY", "hedgeable_majors_basis_convergence": "SPOT_PERP_CARRY",
                  "cross_sectional_funding": "PERP_FUNDING_SPREAD", "cross_interval_funding_dispersion": "PERP_FUNDING_SPREAD", "market_neutral_relative_value": "PERP_FUNDING_SPREAD"}
POLICY_VENUE = "testnet"                      # the policy's registered SANDBOX classification; this pipeline never connects anywhere
AUTHORITY_STRATEGY = "multi_symbol_funding_carry_replay"
ARTIFACT_FILES = ("replay_report.json", "portfolio_state.json", "risk_state.json", "ledger_pnl.json", "competition.json", "opportunities.json")
SUMMARY_FILE = "pipeline_summary.json"
RECONCILE_FILE = "reconcile_report.json"           # schema reconcile_report/v1 (the result of ms_reconcile; the control centre reads it)
MAX_ITEMS = 500
MIN_INDEPENDENT = RP.MIN_INDEPENDENT_FOR_ANY_CLAIM
DAY_MS = 86_400_000
_CC_BASIS_BY_PROV = {"MEASURED": "CALCULATED", "VENUE_STATED": "CALCULATED", "DERIVED": "CALCULATED", "MODELLED": "ESTIMATED", "ASSUMED": "ESTIMATED", "UNKNOWN": "ESTIMATED"}


class PipelineRefused(ValueError):
    """The pipeline refuses to run: a malformed spec / policy, an unverified history root, a venue that is not a sandbox, a missing injected clock. Nothing ran."""


# ======================================================================== identity, data digest, revision, allocation gate, history requirement (pure helpers)

def git_revision(cwd: Optional[str] = None, reader: Optional[Callable[..., Any]] = None) -> str:
    """``git rev-parse HEAD`` of the checkout this code lives in (through ``provenance.git_commit``, the repo's one git reader; this module spawns nothing itself), or the string
    'UNKNOWN'. Never invented: any failure, a sentinel, or anything that is not a 40-hex lower-case object name is 'UNKNOWN'."""
    try:
        if reader is None:
            import provenance as _prov
            reader = _prov.git_commit
        out = str(reader(cwd or BOT, mark_dirty=False)).strip()
        return out if re.fullmatch(r"[0-9a-f]{40}", out) else "UNKNOWN"
    except Exception:                                                       # noqa: BLE001 - a revision is never worth failing a run for; the answer is then 'UNKNOWN'
        return "UNKNOWN"


def data_digest_of(parts: Mapping[str, str]) -> str:
    """The run's ``data_digest``: sha256 over the canonical JSON of the verified inputs' manifest digests (an absent part is the explicit string 'ABSENT', which is part of the hash, so a
    run with and without a collector never share a digest). 'UNKNOWN' when nothing is known (injected data, no parts)."""
    if not parts or all(v == "UNKNOWN" for v in parts.values()):
        return "UNKNOWN"
    return hashlib.sha256(json.dumps({str(k): str(v) for k, v in sorted(parts.items())}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def stamp_identity(arts: Dict[str, Dict[str, Any]], ident: T.RunIdentity) -> Dict[str, Dict[str, Any]]:
    """EVERY artifact carries the run's identity so a consumer can prove the files belong together."""
    for a in arts.values():
        a["identity"] = ident.as_dict()
    return arts


@dataclass(frozen=True)
class AllocationGate:
    """May this candidate allocate capital (even simulated)? Decided from the candidate_competition row ONLY; closed: anything not positively promoted is a refusal."""
    allowed: bool
    reason: str
    verdict: str
    readiness_level: str
    candidate_id: str

    def as_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "verdict": self.verdict, "readiness_level": self.readiness_level, "candidate_id": self.candidate_id}


def evaluate_gate(row: Optional[Mapping[str, Any]], *, candidate_id: str) -> AllocationGate:
    """REJECTED / UNTESTED / INSUFFICIENT_DATA never allocate unless the competition's own ranking put the candidate in its top tier (readiness PROMISING or above). A missing row, a row of
    another candidate or an unknown verdict is a refusal, never a pass."""
    if not isinstance(row, Mapping) or not row:
        return AllocationGate(False, "COMPETITION_ROW_MISSING: no candidate_competition verdict for this candidate; nothing may allocate on an unknown verdict", "UNKNOWN", "UNKNOWN", candidate_id)
    verdict, level = str(row.get("verdict", "UNKNOWN")), str(row.get("readiness_level", "UNKNOWN"))
    why = "; ".join(_short(x, 160) for x in list(row.get("rejection_reasons") or [])[:2])
    if row.get("candidate") != candidate_id:
        return AllocationGate(False, f"COMPETITION_ROW_IS_FOR_ANOTHER_CANDIDATE: {_short(row.get('candidate'), 80)}", verdict, level, candidate_id)
    if verdict not in BLOCKING_VERDICTS:
        return AllocationGate(False, f"COMPETITION_VERDICT_UNRECOGNISED: {_short(verdict, 40)}", verdict, level, candidate_id)
    if verdict == "REJECTED":
        return AllocationGate(False, "COMPETITION_VERDICT_REJECTED" + (": " + why if why else ""), verdict, level, candidate_id)
    if level not in PROMOTED_READINESS:
        return AllocationGate(False, f"COMPETITION_VERDICT_{verdict}_AT_READINESS_{_short(level, 40)}: below PROMISING a candidate does not allocate" + (": " + why if why else ""), verdict, level, candidate_id)
    return AllocationGate(True, f"competition readiness {level} (verdict {verdict}); simulated allocation only", verdict, level, candidate_id)


def history_requirement(*, formation_days: float, hold_days: float, min_rows: int, first_ms: Optional[int], last_ms: Optional[int], end_ms: int) -> Dict[str, Any]:
    """The span backfill_plan.required_window says ``min_rows`` independent holding rows need (warmup + formation + rows x hold) against the history the window can actually use."""
    import backfill_plan as BP
    w = BP.required_window(None, 1, max(1, int(math.ceil(formation_days))), max(1, int(math.ceil(hold_days))), int(min_rows), 7)
    avail = None if (first_ms is None or last_ms is None) else max(0.0, (min(int(end_ms), int(last_ms)) - int(first_ms)) / DAY_MS)
    need = float(w["needed_days"])
    return {"required_days": need, "available_days": avail, "shortfall_days": None if avail is None else max(0.0, need - avail), "adequate": avail is not None and avail >= need,
            "components": w["components"], "min_holding_rows": int(min_rows), "formula": w["formula"], "source": "backfill_plan.required_window"}


def competition_row_for(candidate_id: str, *, history_root: str, universe_dir: str, spot_dir: Optional[str], mark_interval: Optional[str], ledger_dir: str,
                        unreserved_exploratory: bool = False) -> Optional[Dict[str, Any]]:
    """The candidate's row of the REAL candidate_competition over the same verified inputs (its persistent multiple-testing ledger, and the reserved-holdout ledger, live in ``ledger_dir``).
    None when the competition has no row for this candidate id."""
    import candidate_competition as CCMP
    rep = CCMP.run_competition(data_root=history_root, history_root=history_root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval, ledger_dir=ledger_dir,
                               unreserved_exploratory=unreserved_exploratory)
    for r in rep["results"]:
        if r.get("candidate") == candidate_id:
            return dict(r)
    return None


class StepClock:
    """PAPER mode's injected clock for the CLI: simulated time that advances one step per call from the window start. Never the wall clock."""

    def __init__(self, start_ms: int, step_ms: int) -> None:
        self.start_ms, self.step_ms, self.n = int(start_ms), int(step_ms), 0

    def __call__(self) -> int:
        t = self.start_ms + self.n * self.step_ms
        self.n += 1
        return t


@dataclass(frozen=True)
class LoadedHistory:
    """A history root that went through the VERIFIED loader (``verify_inputs``): the series, the loader's info and the manifest digests of every input part."""
    series: Sequence[Any]
    info: Mapping[str, Any]
    parts: Mapping[str, str]


def _manifest_digest(path: str) -> Optional[str]:
    try:
        with open(os.path.join(path, "manifest.json"), "rb") as fh:
            d = json.loads(fh.read().decode("utf-8")).get("manifest_digest")
        return d if isinstance(d, str) and d else None
    except (OSError, ValueError, AttributeError):
        return None


def input_parts(info: Mapping[str, Any], spot_dir: Optional[str], collector_digest: Optional[str]) -> Dict[str, str]:
    """Manifest digests of the VERIFIED input parts (from the loader's own info, which only exists after verification). A part that is genuinely absent is 'ABSENT'; a present part whose
    digest cannot be read is 'UNKNOWN' (and the data_digest then says so for that part)."""
    ds = info.get("datasets") or {}
    fund = (ds.get("funding") or {}).get("manifest_digest")
    marks = sorted(k for k in ds if k != "funding")
    mark = (ds[marks[0]] or {}).get("manifest_digest") if marks else None
    uni = (info.get("universe") or {}).get("manifest_sha256")
    h = info.get("holdout") if isinstance(info.get("holdout"), Mapping) else {}
    holdout = f"RESERVED:{h.get('cut_ms')}" if h.get("status") == "RESERVED" else str(h.get("status") or "NOT_GUARDED")        # a cut run and a full-window run read different data: the identity says which
    return {"history_funding": fund or "UNKNOWN", "history_mark": mark or "UNKNOWN", "universe": uni or "UNKNOWN",
            "spot": "ABSENT" if spot_dir is None else (_manifest_digest(spot_dir) or "UNKNOWN"), "collector": collector_digest or "ABSENT", "holdout": holdout}


def verify_inputs(history_root: str, *, universe_dir: str, spot_dir: Optional[str], mark_interval: Optional[str], holdout_ledger: Any = None,
                  unreserved_exploratory: bool = False) -> LoadedHistory:
    """The ONLY way a history root enters the pipeline: ``history_holdout.load`` over ``candidate_readiness.load_history_root`` (re-verifies every dataset, the universe capture and the
    spot snapshot, and cuts the series at the reserved-holdout boundary)."""
    series, label, info = load_series(history_root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval, holdout_ledger=holdout_ledger,
                                      unreserved_exploratory=unreserved_exploratory)
    assert label == DATA_HISTORY
    return LoadedHistory(tuple(series), info, input_parts(info, spot_dir, None))


def _series_span(series: Sequence[Any]) -> Tuple[Optional[int], Optional[int]]:
    firsts, lasts = [], []
    for x in series:
        for seq in (getattr(x, "mark", None), getattr(x, "funding", None)):
            if seq:
                firsts.append(seq[0][0])
                lasts.append(seq[-1][0])
    return (min(firsts), max(lasts)) if firsts else (None, None)


# ======================================================================== ledger adapter (ms_replay's ledger contract over ms_accounting.Ledger)

class LedgerAdapter:
    """``ms_replay`` expects ``record_fill(fill, category=)`` / ``record_funding`` / ``mark(symbol, ts, price)`` (spot marks arrive as ``SPOT:<symbol>``) / ``pnl()['net']`` (a FLOAT) and an
    optional ``record_slippage``. ``ms_accounting.Ledger`` keys spot by category, has no slippage hook and returns ``net=None`` when any component is unknown. This adapter:
      * translates the ``SPOT:`` mark key into ``category='spot'`` and books every fill of a (group, symbol, category) position under ONE leg (exits have their own order leg ids);
      * declares ``declare_no_funding`` for a perp symbol at its FIRST fill: the replay books EVERY settlement print it crosses, so 'no record yet' is a stated fact (provenance DERIVED), not an
        unknown; a recorded print always takes precedence over the declaration;
      * returns ``net`` as a float for the replay's equity (the sum of the KNOWN components) together with ``net_complete`` / ``flags``. The artifacts write ``null`` whenever ``net_complete``
        is False: an unknown component is never presented as a number there;
      * states THE net definition (the same as ``ms_replay.LocalLedger`` and ``ms_replay.build_report``, which asserts it):
            price_leg = ms_accounting's price leg (actual fill prices) + taker slippage     (AT MID: the taker walk is inside the fill prices, it is added back here ...)
            net       = price_leg + funding - fees - taker slippage - adverse selection      (... and charged once as slippage; adverse selection is a post-fill markout that is in NO
                                                                                              fill price, so ms_accounting's own net omits it and it is deducted here, once)
        ``ledger`` (the raw ms_accounting dict) is passed through untouched: ITS net is not the displayed net.
    """

    def __init__(self, mark_prov: str = "MEASURED", exit_fee_bps_perp: float = 0.0, exit_fee_bps_spot: float = 0.0) -> None:
        if mark_prov not in T.PROVS:
            raise ValueError(f"unknown provenance {mark_prov!r}")
        self.exit_fee_bps = {"linear": float(exit_fee_bps_perp), "spot": float(exit_fee_bps_spot)}
        self.books: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]] = {}      # (symbol, category) -> (best bid, best ask) of the latest mark that carried a book
        self.ledger = MACC.Ledger()
        self.ledger.set_exit_fee_bps(float(exit_fee_bps_perp), float(exit_fee_bps_spot))
        self.mark_prov = mark_prov
        self.slip = {"adverse": 0.0, "taker": 0.0}
        self.slip_by_symbol: Dict[str, Dict[str, float]] = {}
        self.marks: Dict[Tuple[str, str], float] = {}
        self._declared: set = set()

    def record_fill(self, fill: T.Fill, category: str = "linear") -> bool:
        # POSITION identity, not order identity: the engine's exit orders carry NEW leg ids (``x...``), ms_accounting keys a leg on (group, leg_id). Booked under the entry's own id an exit
        # would sit as a second OPEN leg, the pair would never be flat and nothing could ever reconcile. (group, symbol, category) names the position; the fill id stays unique.
        fill = dataclasses.replace(fill, leg_id=f"{category}:{fill.symbol}")
        ok = self.ledger.record_fill(fill, category=category)
        if category == "linear" and fill.symbol not in self._declared:
            self._declared.add(fill.symbol)
            self.ledger.declare_no_funding(fill.symbol, "NO_SETTLEMENT_PRINT_BOOKED_YET_BY_THE_REPLAY", "DERIVED")
        return ok

    def record_funding(self, symbol: str, ts_ms: int, amount_usd: Optional[float], prov: str) -> bool:
        return self.ledger.record_funding(symbol, int(ts_ms), amount_usd, prov)

    def record_slippage(self, usd: float, kind: str, symbol: Optional[str] = None) -> None:
        k = "adverse" if kind == "adverse" else "taker"
        self.ledger.record_slippage(float(usd), k, symbol)                       # the ledger's canonical_net charges them (each once)
        self.slip[k] += float(usd)
        d = self.slip_by_symbol.setdefault(symbol or "", {"adverse": 0.0, "taker": 0.0})
        d[k] += float(usd)

    def mark(self, symbol: str, ts_ms: int, price: float, bid: Optional[float] = None, ask: Optional[float] = None) -> None:
        """Mark a position. ``bid`` / ``ask`` (optional) are the best prices of the book at the mark: an OPEN leg is then valued at what CLOSING it would yield (a long at the bid, a
        short at the ask, less the modelled taker exit fee), never at mid / mark. Without a book the leg stays valued at the mark."""
        if symbol.startswith("SPOT:"):
            sym, cat = symbol[5:], "spot"
        else:
            sym, cat = symbol, "linear"
        self.ledger.mark(sym, int(ts_ms), price, category=cat, prov=self.mark_prov, bid=bid, ask=ask)
        self.marks[(sym, cat)] = float(price)
        if bid is not None and ask is not None and 0 < float(bid) <= float(ask) and math.isfinite(float(ask)):
            self.books[(sym, cat)] = (float(bid), float(ask))
        else:
            self.books.pop((sym, cat), None)

    def _mid_fallback(self, ledger_pnl: Mapping[str, Any]) -> bool:
        """True when an OPEN leg has no book at its latest mark: it is valued at the mark, and that is flagged, never silent."""
        for key, r in ledger_pnl["per_leg"].items():
            if abs(r["qty"]) < 1e-12:
                continue
            cat, sym = key.split("/", 1)[1].split(":", 1)
            if self.books.get((sym, cat)) is None:
                return True
        return False

    def _exit_adjustment(self, ledger_pnl: Mapping[str, Any], symbol: Optional[str] = None) -> float:
        """What valuing the OPEN legs at the exit side of the book (plus the modelled taker exit fee) changes versus the mark the ledger used. 0.0 for a leg with no book."""
        adj = 0.0
        for key, r in ledger_pnl["per_leg"].items():
            q = r["qty"]
            if abs(q) < 1e-12:
                continue
            cat, sym = key.split("/", 1)[1].split(":", 1)
            if symbol is not None and sym != symbol:
                continue
            bk, mk = self.books.get((sym, cat)), self.marks.get((sym, cat))
            if bk is None or mk is None:
                continue
            px = bk[0] if q > 0 else bk[1]
            adj += (px - mk) * q - abs(q) * px * self.exit_fee_bps[cat] / 1e4
        return adj

    def symbol_pnl(self, symbol: str) -> Optional[Dict[str, Any]]:
        """One symbol's rollup on the SAME definition (gross at mid, minus fees, taker slippage and adverse selection); None when the symbol has no ledger leg."""
        lp = self.ledger.pnl()
        r = lp["per_symbol"].get(symbol)
        if r is None:
            return None
        sl = self.slip_by_symbol.get(symbol, {"adverse": 0.0, "taker": 0.0})
        pl = None if r["price_leg"] is None else r["price_leg"] + sl["taker"] + self._exit_adjustment(lp, symbol)
        gross = None if pl is None or r["funding"] is None else pl + r["funding"]
        net = None if gross is None or r["fees"] is None else gross - r["fees"] - sl["taker"] - sl["adverse"]
        return {"price_leg": pl, "gross": gross, "fees": r["fees"], "taker_slippage": sl["taker"], "adverse_selection": sl["adverse"], "net": net}

    def pnl(self) -> Dict[str, Any]:
        p = self.ledger.pnl()
        known = lambda k: p[k] if p[k] is not None else 0.0                      # noqa: E731 - only for the replay's equity number; net_complete tells the truth
        taker, adverse = self.slip["taker"], self.slip["adverse"]
        price_actual = p["price_leg"] if p["price_leg"] is not None else (p["realised_price"] or 0.0)
        price_leg = price_actual + taker                                          # AT MID
        funding, fees = known("funding"), known("fees")
        adj = self._exit_adjustment(p)
        mid_fb = self._mid_fallback(p)
        price_leg += adj
        net = price_leg + funding - fees - taker - adverse
        canon = self.ledger.canonical_net()
        if canon["net"] is not None:                                              # a COMPLETE ledger: THE net is ms_accounting's canonical_net (one definition; reconcile compares against it)
            price_leg, funding, fees, adj, net = canon["price_leg"], canon["funding"], canon["fees"], canon["exit_adjustment"], canon["net"]
            mid_fb = canon["valuation"] == "MID_FALLBACK"
        complete = p["net"] is not None and canon["net_complete"]                 # an unknown component OR an integrity conflict (a duplicate fill whose content differs ...) is INCOMPLETE
        unreal = p["unrealised_price"]
        unreal = None if unreal is None else unreal + adj                         # the value of closing what is open NOW (exit side of the book, less the taker exit fee)
        return {"price_leg": price_leg, "funding": funding, "fees": fees, "slippage": taker + adverse, "adverse_selection": adverse, "taker_slippage": taker,
                "realised": net - (unreal or 0.0), "unrealised": unreal, "unrealised_price": p["unrealised_price"], "exit_adjustment": adj, "net": net, "net_subtotal": net, "net_complete": complete, "flags": list(p["flags"]) + [f for f in canon["flags"] if f not in p["flags"] and f.startswith(MACC.INTEGRITY_FLAG_PREFIXES)] + (["MID_FALLBACK"] if mid_fb else []), "valuation": "MID_FALLBACK" if mid_fb else "EXIT_SIDE", "status": p["status"],
                "prov": p["prov"], "ledger": p}


# ======================================================================== risk policy (ms_risk's CLOSED schema, derived from the symbols actually in the series)

def derive_risk_policy(symbols: Sequence[str], *, candidate_id: str, spec_digest: str, portfolio: PF.PortfolioPolicy, equity_usd: float,
                       aggregate_notional_usd: Optional[float] = None, exec_policy: Optional[EX.ExecPolicy] = None, venue: str = POLICY_VENUE,
                       max_daily_loss_usd: Optional[float] = None, max_drawdown_frac: Optional[float] = None) -> Dict[str, Any]:
    """A conservative ms_risk policy: an EXPLICIT allow-list of the symbols in the series (never a wildcard), the portfolio policy's own limits with 5% headroom for the price drift between
    sizing (mark) and sending (touch), and a SMALL explicit aggregate ceiling (default half of equity: a hedged pair counts once). The venue names a registered SANDBOX; nothing connects."""
    if not symbols or len(set(symbols)) != len(symbols):
        raise PipelineRefused("RISK_POLICY_NEEDS_A_NON_EMPTY_UNIQUE_SYMBOL_LIST")
    eq = float(equity_usd)
    if not math.isfinite(eq) or eq <= 0:
        raise PipelineRefused("EQUITY_NOT_POSITIVE")
    agg = float(aggregate_notional_usd) if aggregate_notional_usd is not None else 0.5 * eq
    leg_cap = min(portfolio.max_symbol_notional_usd, portfolio.max_symbol_frac * eq, portfolio.max_symbol_leverage * eq, agg)
    ex = exec_policy or EX.ExecPolicy()
    head = 1.05
    out = {"policy_version": portfolio.policy_version, "candidate_id": candidate_id, "spec_digest": spec_digest, "venue": venue, "allowed_symbols": sorted(symbols),
            "allowed_sides": {"linear": ["Buy", "Sell"], "spot": ["Buy", "Sell"]}, "allowed_order_types": ["Limit", "Market"],
            "max_notional_per_symbol_usd": 2.0 * leg_cap * head, "max_aggregate_notional_usd": agg, "max_gross_exposure_usd": 2.0 * agg * head,
            "max_net_exposure_usd": max(portfolio.max_net_frac * eq, 0.01 * eq) * head, "max_active_groups": int(portfolio.max_groups), "max_unhedged_usd": leg_cap * head,
            "max_leverage": max(1.0, portfolio.max_aggregate_leverage * head), "max_market_age_ms": int(min(portfolio.max_market_age_ms, ex.max_market_age_ms)),
            "max_account_age_ms": int(min(portfolio.max_account_age_ms, ex.max_account_age_ms)), "execution_styles": [portfolio.style]}
    if max_daily_loss_usd is not None:                                       # hard loss limits (ms_risk, fail-closed): the state must then carry equity / day-start / peak or every entry is rejected
        out["max_daily_loss_usd"] = float(max_daily_loss_usd)
    out["max_drawdown_frac"] = float(max_drawdown_frac if max_drawdown_frac is not None else portfolio.max_drawdown_frac)
    return out


# ======================================================================== the authority in the replay loop

class SimulatedTransport:
    """The ONLY transport an authority is bound to here: an in-process simulation that carries no request. ``IN_PROCESS_SIMULATION`` is declared at CLASS level (ms_authority's test)."""
    IN_PROCESS_SIMULATION = True

    def request(self, *a: Any, **k: Any) -> Any:                            # pragma: no cover - never called: the replay's SimVenue is the order sink
        raise RuntimeError("the pipeline's simulated transport carries no request")


def body_from_request(req: EX.OrderRequest) -> Dict[str, Any]:
    """The venue-request body the leg WOULD be sent as (Bybit v5 field names), built only from the leg the engine journalled. The authority judges exactly this dict."""
    leg = req.leg
    b: Dict[str, Any] = {"category": leg.category, "symbol": leg.symbol, "side": leg.side, "orderType": leg.order_type, "qty": leg.qty, "orderLinkId": req.link_id}
    if leg.order_type == "Limit":
        b["price"] = leg.price
        b["timeInForce"] = "PostOnly" if leg.post_only else leg.time_in_force
    elif leg.time_in_force:
        b["timeInForce"] = leg.time_in_force
    if leg.reduce_only:
        b["reduceOnly"] = True
    return b


class AuthorityGates:
    """``ms_replay.run_replay(gates=...)``: registers every plan with the authority BEFORE it is submitted and authorises EVERY order the engine sends (entry, resend, exit). The authority
    reads the account state and market snapshots of the CURRENT replay step through the probe (never a stale copy), with the injected clock. A refusal is returned as a reason: the
    transport answers REJECTED and the order never reaches the venue."""

    def __init__(self, policy: Mapping[str, Any], *, state_dir: str, venue: EX.SimVenue, start_ms: int) -> None:
        self.policy = dict(policy)
        self.venue = venue
        self.start_ms = int(start_ms)
        self.probe: Optional[RP.WorldProbe] = None
        self._current_group: Optional[str] = None
        self._registered: set = set()
        self._settled: set = set()
        self._resolved: set = set()
        self._released: set = set()
        self.exit_releases: List[Tuple[str, str]] = []
        self.authorized: List[str] = []
        self.refusals: List[Tuple[str, Tuple[str, ...]]] = []
        self.plan_refusals: List[Tuple[str, Tuple[str, ...]]] = []
        self.transport = SimulatedTransport()
        self.ledger_getter: Optional[Callable[[], Any]] = None
        self._halter: Optional[Tuple[int, Any]] = None
        self.rc_steps = 0
        self.rc_history: Dict[str, Dict[str, Any]] = {}
        self.rc_last: Optional[RC.ReconcileReport] = None
        self.rc_error: Optional[str] = None
        try:
            self.authority = MAUTH.MultiSymbolAuthority(policy=self.policy, transport=self.transport, state_dir=state_dir, strategy=AUTHORITY_STRATEGY, state_provider=self._state,
                                                        snapshot_provider=self._snapshots, kill_switch_reader=lambda: (False, "replay: no kill switch engaged"), clock=self._clock,
                                                        hedge_reader=self._hedge)
        except MAUTH.AuthorityConfigError as exc:
            raise PipelineRefused(f"AUTHORITY_REFUSED_THE_POLICY: {exc}") from exc
        if self.authority.mode != "SIMULATED":                              # belt and braces: nothing but a simulation is ever wired here
            raise PipelineRefused("AUTHORITY_NOT_IN_SIMULATED_MODE")

    # -- providers (read the present replay step)
    def bind(self, probe: RP.WorldProbe) -> None:
        self.probe = probe

    def _now(self) -> int:
        return self.probe.now() if self.probe is not None else self.start_ms

    def _clock(self) -> float:
        return (self._now() + 0.5) / 1000.0                               # +0.5ms: int(seconds*1000) lands on the replay's millisecond, never one below it (a 'future' market stamp)

    def _state(self) -> Dict[str, Any]:
        if self.probe is None:
            raise RuntimeError("no probe bound")
        st = self.probe.risk_state()
        cur = self._current_group
        eng = self.probe.engine()
        g = eng.groups.get(cur) if cur else None
        if g is not None and g.state not in (T.GroupState.CLOSED, T.GroupState.FAILED) and isinstance(st.get("open_groups"), int):
            st["open_groups"] = max(0, st["open_groups"] - 1)           # the group being sent is not 'another' active group
        return st

    def _snapshots(self) -> Dict[str, T.MarketSnapshot]:
        if self.probe is None:
            raise RuntimeError("no probe bound")
        return self.probe.snapshots()

    def _hedge(self, symbol: str) -> Tuple[float, float]:
        pos = self.venue.positions()                                       # the venue's OWN position read, never fill arithmetic
        return max(0.0, -pos.get(f"linear:{symbol}", 0.0)), max(0.0, pos.get(f"spot:{symbol}", 0.0))

    # -- the gates ms_replay calls
    def on_plan(self, plan: T.OrderGroupPlan) -> Any:
        d = self.authority.register_group(plan)
        if d.approved:
            self._registered.add(plan.group_id)
        else:
            self.plan_refusals.append((plan.group_id, tuple(d.reasons)))
        return d

    def release_dead_exits(self, gid: str) -> None:
        """The authority reserves exit QUANTITY cumulatively per group / symbol / category (ms_authority ``release_exit``): an exit that was REJECTED, CANCELLED or expired unfilled gave
        nothing back until told. The engine retries exits under fresh link ids, so without this every retry past ~1.02x the entry quantity is refused and the position can never close.
        Called before each exit is authorised (the retry is sent in the same step as the rejection) and after every step. Only the UNFILLED remainder is released; the fill stays consumed."""
        if self.probe is None:
            return
        g = self.probe.engine().groups.get(gid)
        if g is None:
            return
        for l in g.exit_legs():
            if l.status in (EX.REJECTED, EX.CANCELLED) and l.link not in self._released:
                self._released.add(l.link)
                left = l.plan_qty - l.filled
                if left > 0 and self.authority.release_exit(l.link, format(left.normalize(), "f")):
                    self.exit_releases.append((l.link, format(left.normalize(), "f")))

    def _exit_reserved(self, link: str) -> bool:
        try:
            return bool(self.authority.exit_reserved(link))
        except Exception:                                                  # noqa: BLE001 - unknown means "do not release"
            return True

    def on_order(self, req: EX.OrderRequest) -> Optional[str]:
        self._current_group = req.group_id
        if self.probe is not None and req.link_id.split("-", 1)[-1].startswith("x"):
            self.release_dead_exits(req.group_id)
        is_exit = req.link_id.split("-", 1)[-1].startswith("x")
        held_before = bool(is_exit and self._exit_reserved(req.link_id))     # an earlier call's reservation (a live order may stand behind it) is not this call's to give back
        try:
            body = body_from_request(req)
            self.authority.authorize(body, venue=self.policy["venue"])
        except EA.EntryRefused as exc:
            self.refusals.append((req.link_id, tuple(exc.reasons)))
            if is_exit and not held_before and self._exit_reserved(req.link_id):
                self.authority.release_exit(req.link_id)                    # THIS call reserved the quantity and a later gate refused: give it back (never on CONFLICT / RELEASED / a duplicate)
            return ",".join(exc.reasons)[:300]
        except Exception as exc:                                           # noqa: BLE001 - an authority that cannot decide refuses
            self.refusals.append((req.link_id, (f"AUTHORITY_ERROR:{type(exc).__name__}",)))
            return f"AUTHORITY_ERROR:{type(exc).__name__}"
        finally:
            self._current_group = None
        self.authorized.append(req.link_id)
        return None

    def on_step(self, now: int) -> None:
        """Tell the authority what the account now shows: a FILLED entry leg stops being a pending reservation; a CLOSED / FAILED group stops counting as active."""
        if self.probe is None:
            return
        eng = self.probe.engine()
        for gid in sorted(self._registered):
            g = eng.groups.get(gid)
            if g is None:
                continue
            for l in g.entry_legs():
                if l.status == EX.FILLED and (gid, l.plan.leg_id) not in self._settled:
                    self._settled.add((gid, l.plan.leg_id))
                    self.authority.settle_leg(gid, l.plan.leg_id)
            self.release_dead_exits(gid)
            if g.state in (T.GroupState.CLOSED, T.GroupState.FAILED) and gid not in self._resolved:
                self._resolved.add(gid)
                self.authority.resolve_group(gid)
        self.reconcile_step(now)

    def reconcile_step(self, now: int) -> None:
        """ms_reconcile at the end of EVERY step: the venue's own position read is the reference; engine, portfolio and ledger must agree with it, and the authority's reservations with the
        engine. A disagreement HALTS new exposure (``HaltOnDisagreement.for_engine``); nothing is overwritten. Exits and the lifecycle continue."""
        eng = self.probe.engine()
        led = self.ledger_getter() if self.ledger_getter is not None else None
        try:
            vs = self.venue.venue_state()
            read = {"positions": vs["positions"], "complete": vs["complete"]}
        except (TimeoutError, OSError, KeyError):
            read = None
        snaps = self.probe.snapshots()
        marks = {**{k: s.mark for k, s in snaps.items()}, **{"SPOT:" + k: (s.spot_mark or s.mark) for k, s in snaps.items()}}
        pf = self.probe.portfolio_state() if self.probe.portfolio_state is not None else None
        views = RC.collect_views(engine=eng, portfolio=pf, ledger=led, authority=self.authority, venue_read=read, now_ms=now, portfolio_marks=marks)
        rep = RC.reconcile(views)
        if self._halter is None or self._halter[0] != id(eng):               # a restarted engine is a new object with its own halts
            self._halter = (id(eng), RC.HaltOnDisagreement.for_engine(eng))
        self._halter[1](rep, now)
        self.rc_steps += 1
        self.rc_last = rep
        for f in rep.findings:
            h = self.rc_history.setdefault(f.reason(), {"code": f.code, "key": f.key, "views": list(f.views), "detail": f.detail, "first_ms": now, "n_steps": 0})
            h["last_ms"], h["n_steps"] = now, h["n_steps"] + 1

    def stats(self) -> Dict[str, Any]:
        refused: Dict[str, int] = {}
        for _link, reasons in self.refusals:
            for r in reasons:
                k = r.split(":", 1)[0]
                refused[k] = refused.get(k, 0) + 1
        plan_refused: Dict[str, int] = {}
        for _gid, reasons in self.plan_refusals:
            for r in reasons:
                k = r.split(":", 1)[0]
                plan_refused[k] = plan_refused.get(k, 0) + 1
        return {"mode": self.authority.mode, "policy_digest": self.authority.policy_digest, "venue": self.policy["venue"], "orders_authorised": len(self.authorized),
                "orders_refused": len(self.refusals), "exit_reservations_released": len(self.exit_releases), "order_refusal_codes": dict(sorted(refused.items())), "groups_registered": len(self._registered),
                "groups_refused_at_registration": len(self.plan_refusals), "registration_refusal_codes": dict(sorted(plan_refused.items())), "transport": "IN_PROCESS_SIMULATION"}


# ======================================================================== scanner adapter

class ScanRecorder:
    """The scan_fn handed to the replay: ms_scanner.scan with the spec / candidate / digest bound, instruments viewed at the decision time. Records every scan (for opportunities.json and
    the competition status) and which opportunity produced which group (the group id is derived exactly as ms_portfolio derives it)."""

    def __init__(self, spec: Mapping[str, Any], candidate_id: str, spec_digest: str, params: Optional[Mapping[str, Any]]) -> None:
        self.spec, self.candidate_id, self.spec_digest, self.params = dict(spec), candidate_id, spec_digest, dict(params or {})
        self.scans: List[Tuple[int, List[T.Opportunity]]] = []
        self.by_group: Dict[str, T.Opportunity] = {}
        self.suppress = False                                              # True: the scan is recorded (and shown) but NOTHING is handed to the allocator (the allocation gate said no)

    @staticmethod
    def instrument_at(inst: T.Instrument, t_ms: int) -> T.Instrument:
        """Status AT t from the lifecycle bounds (ms_types: 'for history use lifecycle bounds'): a contract delivered by t is Closed, one not yet launched is PreLaunch; an explicitly
        non-Trading recorded status with no bound stays as recorded. The scanner then refuses to open anything that is not Trading."""
        if inst.delivery_ms is not None and t_ms >= inst.delivery_ms:
            return dataclasses.replace(inst, status="Closed")
        if inst.launch_ms is not None and t_ms < inst.launch_ms:
            return dataclasses.replace(inst, status="PreLaunch")
        return inst

    def __call__(self, now: int, snaps: Mapping[str, T.MarketSnapshot], instruments: Mapping[str, T.Instrument]) -> List[T.Opportunity]:
        insts = {s: self.instrument_at(i, now) for s, i in instruments.items()}
        opps = SC.scan(insts, snaps, spec=self.spec, candidate_id=self.candidate_id, spec_digest=self.spec_digest, decision_ms=now, params=self.params)
        self.scans.append((now, list(opps)))
        if self.suppress:
            return []
        for o in opps:
            if o.legs and not o.exclusions:
                gid = PF.derive_group_id(o.candidate_id, o.spec_digest, o.decision_ms, [s for s, _ in o.legs])
                self.by_group[gid] = o
        return opps


# ======================================================================== loading

def _hedge_of(spot_listed: Optional[bool]) -> str:
    return {True: "HEDGEABLE_ON_BYBIT", False: "PERP_ONLY"}.get(spot_listed, "HEDGE_UNKNOWN")


def _instrument_for(x: RP.ReplaySeries, cfg: RP.ReplayConfig) -> T.Instrument:
    if x.instrument is not None:
        inst = x.instrument
        if inst.status == "Closed" and inst.delivery_ms is not None:         # a contract that is Closed NOW traded before its delivery: the lifecycle bound carries the closure
            inst = dataclasses.replace(inst, status="Trading")
        return inst
    return T.Instrument(x.symbol, None, "Trading", x.launch_ms, x.delivery_ms, x.interval_min, x.base_coin or x.symbol, "USDT", cfg.default_tick, cfg.default_qty_step,
                        cfg.default_min_qty, cfg.default_max_qty, cfg.default_max_qty, cfg.default_min_notional, "10", x.symbol, _hedge_of(x.spot_listed))


def load_series(source: Any, *, universe_dir: Optional[str], spot_dir: Optional[str], mark_interval: Optional[str], holdout_ledger: Any = None,
                unreserved_exploratory: bool = False) -> Tuple[List[Any], str, Dict[str, Any]]:
    """(series, data_label, info). A string is a market_history root and goes through the VERIFIED loader; anything else is a sequence of injected series (INJECTED_TEST_DATA)."""
    if isinstance(source, LoadedHistory):
        info = dict(source.info)
        ds = info.get("datasets") or {}
        if not ds or not all(isinstance(v, Mapping) and v.get("verified") is True for v in ds.values()) or not source.series:
            raise PipelineRefused("LOADED_HISTORY_IS_NOT_A_VERIFIED_LOAD")
        return list(source.series), DATA_HISTORY, info
    if isinstance(source, str):
        if not universe_dir:
            raise PipelineRefused("UNIVERSE_DIR_REQUIRED_FOR_A_HISTORY_ROOT")
        import history_holdout as HH                                      # lazy: heavy, and only the verified path needs it
        import research_harness as RH
        if holdout_ledger is None:
            raise PipelineRefused("HOLDOUT_LEDGER_REQUIRED: a history root is only read through the reserved-holdout registry (history_holdout.default_registry_path()) or the explicit history_holdout.UNGUARDED")
        try:
            series, info = HH.load(source, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval, ledger_path=holdout_ledger,
                                   reader="ms_pipeline", unreserved_exploratory=unreserved_exploratory)
        except RH.HarnessError as exc:
            raise PipelineRefused(f"HISTORY_REFUSED: {exc}") from exc
        if not series:
            raise PipelineRefused("HISTORY_ROOT_HAS_NO_USABLE_SYMBOL")
        return list(series), DATA_HISTORY, dict(info)
    series = list(source)
    if not series:
        raise PipelineRefused("NO_SERIES")
    return series, DATA_INJECTED, {"survivorship": "SURVIVORS_ONLY", "excluded": []}


def _stale_bound(orig: Any, r: RP.ReplaySeries) -> Optional[int]:
    iv = getattr(orig, "mark_interval_ms", None)
    if not (isinstance(iv, int) and iv > 0):
        gaps = sorted(b[0] - a[0] for a, b in zip(r.mark, r.mark[1:]) if b[0] > a[0])
        iv = gaps[len(gaps) // 2] if gaps else None                        # the median spacing: one missing print does not move it
    return 2 * iv if iv else None


def _to_replay_series(series: Sequence[Any], cfg: RP.ReplayConfig) -> List[RP.ReplaySeries]:
    out: List[RP.ReplaySeries] = []
    seen = set()
    for x in series:
        r = x if isinstance(x, RP.ReplaySeries) else RP.from_symbol_series(x)
        if r.symbol in seen:
            raise PipelineRefused(f"DUPLICATE_SYMBOL:{r.symbol}")
        seen.add(r.symbol)
        if r.mark_stale_ms is None:                                         # a mark older than two mark intervals is UNKNOWN at the decision (the loader's own staleness rule)
            r = dataclasses.replace(r, mark_stale_ms=_stale_bound(x, r))
        out.append(dataclasses.replace(r, instrument=_instrument_for(r, cfg)))
    return sorted(out, key=lambda s: s.symbol)


# ======================================================================== artifacts

def _num(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _sum(*xs: Optional[float]) -> Optional[float]:
    return None if any(v is None for v in xs) else float(sum(xs))                # type: ignore[arg-type]


def _write_json(path: str, obj: Mapping[str, Any]) -> None:
    raw = json.dumps(obj, sort_keys=True, indent=1, allow_nan=False, ensure_ascii=True) + "\n"
    data = raw.encode("utf-8")
    if len(data) > 1_048_576:
        raise PipelineRefused(f"ARTIFACT_TOO_LARGE:{os.path.basename(path)}")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _short(s: Any, n: int = 300) -> str:
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in str(s))[:n]


def _hedge_label(hedges: Sequence[str]) -> str:
    hs = set(hedges)
    if hs and hs == {"HEDGEABLE_ON_BYBIT"}:
        return "HEDGEABLE_ON_BYBIT"
    if "PERP_ONLY" in hs:
        return "PERP_ONLY"
    return "HEDGE_UNKNOWN"


def _prov_cap(prov: str, label: str) -> str:
    """Injected data is never MEASURED: the weakest of the item's provenance and ASSUMED."""
    return T.weakest(prov, "ASSUMED") if label == DATA_INJECTED else prov


def _basis_from_provs(provs: Sequence[str]) -> str:
    return _CC_BASIS_BY_PROV[T.weakest(*provs)] if provs else "ESTIMATED"


@dataclass
class PipelineResult:
    mode: str
    data_label: str
    result: RP.ReplayResult
    report: Dict[str, Any]
    summary: Dict[str, Any]
    files: Dict[str, str]
    ledger: LedgerAdapter
    recorder: ScanRecorder
    gates: AuthorityGates
    policy: Dict[str, Any]
    artifacts: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _group_kinds(res: RP.ReplayResult, rec: ScanRecorder) -> Dict[str, str]:
    return {gid: rec.by_group[gid].kind for gid in res.groups if gid in rec.by_group}


def _build_artifacts(res: RP.ReplayResult, led: LedgerAdapter, rec: ScanRecorder, series: Sequence[RP.ReplaySeries], policy: Mapping[str, Any], gates: AuthorityGates,
                     report: Mapping[str, Any], *, label: str, generated_ms: int, spec: Mapping[str, Any], candidate_id: str, universe_scope: str,
                     gate: Optional[AllocationGate] = None, evidence_basis: Optional[Mapping[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    cfg = res.cfg
    p = led.pnl()
    L = p["ledger"]
    complete = bool(p["net_complete"])
    hedge_of = {s.symbol: (s.instrument.spot_hedge if s.instrument else "HEDGE_UNKNOWN") for s in series}
    kinds = _group_kinds(res, rec)
    kind_default = str(spec.get("kind", "SPOT_PERP_CARRY"))
    common = {"generated_ms": int(generated_ms), "data_label": label, "evidence_basis": dict(evidence_basis or {"data": label, "execution": "MODELLED"})}

    # ---- replay_report
    gross = p["price_leg"] + p["funding"] if complete else None             # AT MID: net = gross - fees - slippage (taker walk + adverse selection)
    fees = L["fees"]
    net = p["net"] if complete else None
    sym_rows = []
    sym_groups: Dict[str, List[str]] = {}
    for gid, g in sorted(res.groups.items()):
        for s in g.symbols:
            sym_groups.setdefault(s, []).append(gid)
    for sym in sorted(sym_groups):
        r = led.symbol_pnl(sym)
        kind = kinds.get(sym_groups[sym][0], kind_default)
        sym_rows.append({"symbol": sym, "kind": kind, "spot_hedge": hedge_of.get(sym, "HEDGE_UNKNOWN"), "n_groups": len(sym_groups[sym]),
                         "gross_pnl_usd": r["gross"] if r else 0.0, "fees_usd": r["fees"] if r else 0.0, "net_pnl_usd": r["net"] if r else 0.0})     # no ledger leg = no fill: a stated zero
    states: Dict[str, int] = {}
    for st in res.states.values():
        k = st.value if hasattr(st, "value") else str(st)
        states[k] = states.get(k, 0) + 1
    replay_report = {"schema": "replay_report/v1", "basis": "SIMULATED", **common, "universe_scope": universe_scope, "period_start_ms": cfg.start_ms, "period_end_ms": cfg.end_ms,
                     "n_groups": len(res.states), "groups_by_state": dict(sorted(states.items())), "gross_pnl_usd": gross, "fees_usd": fees, "net_pnl_usd": net, "symbols": sym_rows,
                     "slippage_usd": p["slippage"] if complete else None, "taker_slippage_usd": p["taker_slippage"] if complete else None,
                     "adverse_selection_usd": p["adverse_selection"] if complete else None,
                     "net_definition": "net = gross (price leg at mid + funding) - fees - slippage (taker walk + adverse selection), each modelled cost once"}

    # ---- ledger_pnl (realised / unrealised split; a simulated ledger is never realised cash)
    # realised_gross_usd is the realised PRICE result at mid ALONE: funding is its own line (funding_usd). The one definition, stated identically in replay_report and in the control centre:
    #   realised_net = realised_gross + funding - fees - slippage (taker walk + adverse selection). The previous value ALSO contained the funding, so a consumer adding funding again counted it twice.
    rg = (p["realised"] + p["fees"] + p["slippage"] - L["funding"]) if complete else None
    ledger_pnl = {"schema": "ledger_pnl/v1", "basis": "SIMULATED", **common, "period_start_ms": cfg.start_ms, "period_end_ms": cfg.end_ms, "realised_gross_usd": rg,
                  "fees_usd": fees, "funding_usd": L["funding"], "slippage_usd": p["slippage"] if complete else None, "taker_slippage_usd": p["taker_slippage"] if complete else None,
                  "adverse_selection_usd": p["adverse_selection"] if complete else None, "realised_net_usd": p["realised"] if complete else None, "unrealised_usd": p["unrealised"],
                  "exit_adjustment_usd": p["exit_adjustment"] if complete else None, "net_usd": net,
                  "net_definition": "net_usd = realised_net + unrealised = gross (price leg at mid + funding) - fees - slippage (taker walk + adverse selection); ms_accounting's own net omits adverse selection",
                  "ledger_status": L["status"], "ledger_flags": [_short(f, 120) for f in L["flags"][:20]]}

    # ---- portfolio_state (end of window)
    legs: Dict[Tuple[str, str, str], Dict[str, float]] = {}
    for f in res.fills:
        cat = res.fills_cat.get(f.fill_id, "linear")
        d = legs.setdefault((f.group_id, f.symbol, cat), {"qty": 0.0, "in_qty": 0.0, "in_cost": 0.0, "first_sign": 0.0})
        sgn = 1.0 if f.side == "Buy" else -1.0
        if d["first_sign"] == 0.0:
            d["first_sign"] = sgn
        d["qty"] += sgn * f.qty
        if sgn == d["first_sign"]:
            d["in_qty"] += f.qty
            d["in_cost"] += f.qty * f.price
    positions = []
    gross_exp = net_exp = 0.0
    unreal = 0.0
    unreal_known = True
    for (gid, sym, cat), d in sorted(legs.items()):
        if abs(d["qty"]) < 1e-9:
            continue
        mark = led.marks.get((sym, cat))
        entry = d["in_cost"] / d["in_qty"] if d["in_qty"] > 0 else None
        if mark is None or entry is None:
            unreal_known = False
        notional = abs(d["qty"]) * (mark if mark is not None else (entry or 0.0))
        gross_exp += notional
        net_exp += math.copysign(notional, d["qty"])
        u = (mark - entry) * d["qty"] if (mark is not None and entry is not None) else None
        unreal += u or 0.0
        positions.append({"symbol": sym if cat == "linear" else sym, "kind": kinds.get(gid, kind_default), "spot_hedge": hedge_of.get(sym, "HEDGE_UNKNOWN"),
                          "side": "Buy" if d["qty"] > 0 else "Sell", "notional_usd": notional, "entry_price": entry, "mark_price": mark, "unrealised_pnl_usd": u, "group_id": gid,
                          "category": cat})
    for row in positions:
        row.pop("category")
    equity = (cfg.equity0 + net) if (net is not None and complete) else None
    portfolio = {"schema": "portfolio_state/v1", "basis": "SIMULATED", **common, "equity_usd": equity, "gross_exposure_usd": gross_exp, "net_exposure_usd": net_exp,
                 "unrealised_pnl_usd": unreal if unreal_known else None, "positions": positions}

    # ---- risk_state
    eng = res.engine
    halts = sorted(eng.halts) if eng is not None else []
    st = res.stats
    agg = 0.0
    per_base: Dict[str, Dict[str, float]] = {}
    for (gid, sym, cat), d in legs.items():
        if abs(d["qty"]) < 1e-9:
            continue
        mark = led.marks.get((sym, cat))
        if mark is not None:
            per_base.setdefault(sym, {}).update({cat: abs(d["qty"]) * mark})
    agg_known = all(((sym, cat) in led.marks) for (gid, sym, cat), d in legs.items() if abs(d["qty"]) >= 1e-9)
    for b, v in per_base.items():
        agg += max(v.values())
    peak_gross = _num(st.get("max_gross_usd"))
    obs_dd = _num(report["risk"]["max_drawdown_frac"]) if isinstance(report.get("risk"), Mapping) else None

    def lim(name: str, unit: str, limit: Optional[float], value: Optional[float]) -> Dict[str, Any]:
        status = "UNKNOWN" if (limit is None or value is None) else ("BREACH" if value > limit + 1e-9 else "OK")
        return {"name": name, "unit": unit, "limit": limit, "value": value, "status": status}
    limits = [lim("peak_gross_exposure", "USD", _num(policy["max_gross_exposure_usd"]), peak_gross),
              lim("aggregate_notional_end", "USD", _num(policy["max_aggregate_notional_usd"]), agg if agg_known else None),
              lim("net_exposure_end", "USD", _num(policy["max_net_exposure_usd"]), abs(net_exp) if unreal_known else None),
              lim("peak_open_groups", "groups", float(policy["max_active_groups"]), _num(st.get("max_open_groups"))),
              lim("max_drawdown", "fraction", _num(cfg.portfolio.max_drawdown_frac), obs_dd)]
    alerts: List[Dict[str, str]] = []
    for f in sorted({(x["kind"], x.get("symbol", "")) for x in res.fault_log}):
        alerts.append({"severity": "WARN", "code": _short("FAULT_INJECTED:" + f[0], 80), "detail": _short(f"symbol={f[1] or '-'}", 120)})
    n_bad = sum(1 for s in res.states.values() if (s.value if hasattr(s, "value") else s) in ("UNHEDGED", "UNKNOWN_OUTCOME"))
    if n_bad:
        alerts.append({"severity": "WARN", "code": "GROUPS_UNHEDGED_OR_UNKNOWN", "detail": f"{n_bad} group(s) in UNHEDGED or UNKNOWN_OUTCOME at the end of the window"})
    wide = int(st.get("transport", {}).get("exits_on_wider_book_cap", 0)) if isinstance(st.get("transport"), Mapping) else 0
    if wide:
        alerts.append({"severity": "WARN", "code": "EXIT_ON_WIDER_BOOK_CAP", "detail": f"{wide} reduce-only exit(s) were taken against a book older than fill.stale_quote_ms (cap exit_book_max_age_ms={cfg.exit_book_max_age_ms})"})
    ast = gates.stats()
    if ast["orders_refused"] or ast["groups_refused_at_registration"]:
        alerts.append({"severity": "WARN", "code": "AUTHORITY_REFUSALS", "detail": _short(f"orders={ast['order_refusal_codes']} groups={ast['registration_refusal_codes']}", 300)})
    if not complete:
        alerts.append({"severity": "WARN", "code": "LEDGER_INCOMPLETE", "detail": "a ledger component is unknown: net is not stated"})
    risk_state = {"schema": "risk_state/v1", "basis": "SIMULATED", **common, "halted": bool(halts), "halt_reasons": [_short(h, 200) for h in halts], "limits": limits, "alerts": alerts}

    # ---- opportunities (last scan)
    scans = rec.scans
    last_ms, last = scans[-1] if scans else (cfg.start_ms, [])
    items = []
    for o in last[:MAX_ITEMS]:
        syms = [s[5:] if s.startswith("SPOT:") else s for s, _ in o.legs]
        syms = list(dict.fromkeys(syms))
        gross_bps = o.expected_funding_bps_horizon
        if gross_bps is not None and o.expected_price_leg_bps is not None:
            gross_bps = gross_bps + o.expected_price_leg_bps
        items.append({"candidate_id": o.candidate_id, "kind": o.kind, "symbols": syms, "spot_hedge": _hedge_label([hedge_of.get(s, "HEDGE_UNKNOWN") for s in syms]),
                      "hold_days": float(o.hold_days), "gross_bps_horizon": _num(gross_bps), "net_bps_horizon": _num(o.net_bps_horizon), "capacity_usd": _num(o.capacity_usd),
                      "prov": _prov_cap(o.prov, label), "exclusions": [_short(e, 80) for e in o.exclusions]})
    elig = [o for o in last if not o.exclusions and o.net_bps_horizon is not None and o.net_bps_horizon > 0 and o.rank_score is not None]
    est_day = None
    if elig:
        best = max(elig, key=lambda o: (o.rank_score, o.legs))
        notional = min(float(rec.params.get("notional_usd", SC.DEFAULT_PARAMS["notional_usd"])), best.capacity_usd or 0.0)
        est_day = best.net_bps_horizon / 1e4 * notional / best.hold_days if best.hold_days > 0 else None
    opportunities = {"schema": "opportunities/v1", "basis": _basis_from_provs([i["prov"] for i in items]), **common, "decision_ms": int(last_ms), "est_net_usd_per_day": est_day,
                     "items": items, "n_items_total": len(last), "n_items_shown": len(items)}

    # ---- competition (one candidate; never SUFFICIENT_EVIDENCE)
    n_pos_any = sum(1 for _ms, ops in scans for o in ops if not o.exclusions and o.net_bps_horizon is not None and o.net_bps_horizon > 0)
    closed = [g for g in res.groups.values() if g.closed and g.notional_usd > 0]
    # ONE net definition for the whole run (the ledger's): gross = price leg AT MID (actual fill prices + the taker walk added back) + funding; net = gross - fees - taker slippage - adverse selection
    gross_usd = [g.price_usd + g.taker_slip_usd + g.funding_usd for g in closed]
    net_usd = [gu - g.fees_usd - g.taker_slip_usd - g.adverse_usd for gu, g in zip(gross_usd, closed)]
    nets = [n / g.notional_usd * 1e4 for n, g in zip(net_usd, closed)]
    grosses = [n / g.notional_usd * 1e4 for n, g in zip(gross_usd, closed)]
    traded = sorted({s for g in res.groups.values() for s in g.symbols})
    indep = int(report["observations"]["independent"]) if isinstance(report.get("observations"), Mapping) else 0
    rprov = _prov_cap(str(report["provenance"]["weakest"]) if isinstance(report.get("provenance"), Mapping) else "ASSUMED", label)
    if gate is not None and not gate.allowed:                                # the competition's own verdict stands: this run did not (and could not) change it
        top = _short(gate.reason, 260)
        status, reason = {"REJECTED": ("REJECTED", f"candidate_competition verdict REJECTED: {top}"),
                          "INSUFFICIENT_DATA": ("INSUFFICIENT_EVIDENCE", f"candidate_competition verdict INSUFFICIENT_DATA, not promoted: {top}")}.get(gate.verdict, ("RESEARCHING", None))
    elif not scans:
        status, reason = "RESEARCHING", None
    elif n_pos_any == 0:
        status, reason = "REJECTED", f"NO_EDGE_IN_WINDOW: none of {len(scans)} scan(s) had an eligible opportunity with positive net after costs (this window only; not a general rejection)"
    elif indep < MIN_INDEPENDENT:
        status, reason = "INSUFFICIENT_EVIDENCE", f"{indep} independent closed round trip(s) (< {MIN_INDEPENDENT}); a replay with MODELLED fills is not execution evidence"
    else:
        status, reason = "INSUFFICIENT_EVIDENCE", "replay fills are MODELLED: not execution evidence (promotion_eligible is False)"
    comp_kind = kinds.get(next((g for g in sorted(res.groups) if g in kinds), ""), kind_default)
    cand = {"candidate_id": candidate_id, "kind": comp_kind, "spot_hedge": _hedge_label([hedge_of.get(s, "HEDGE_UNKNOWN") for s in traded]) if traded else "HEDGE_UNKNOWN",
            "status": status, "reason": reason, "gross_bps_horizon": (sum(grosses) / len(grosses)) if grosses else None, "net_bps_horizon": (sum(nets) / len(nets)) if nets else None,
            "n_observations": len(closed), "prov": rprov,
            "net_definition": "price leg at mid + funding - fees - taker slippage - adverse selection (the ledger's net), per closed group over its notional; mean of the groups' bps",
            "net_usd_closed_groups": sum(net_usd) if closed else None, "notional_usd_closed_groups": sum(g.notional_usd for g in closed) if closed else None}
    competition = {"schema": "competition/v1", "basis": "SIMULATED" if closed else "ESTIMATED", **common, "candidates": [cand]}
    return {"replay_report.json": replay_report, "portfolio_state.json": portfolio, "risk_state.json": risk_state, "ledger_pnl.json": ledger_pnl, "competition.json": competition,
            "opportunities.json": opportunities}


# ======================================================================== the run

def classify_status(*, gate: AllocationGate, res: RP.ReplayResult, rec: ScanRecorder, report: Mapping[str, Any], ledger_pnl: Mapping[str, Any], authority: Mapping[str, Any],
                    history: Mapping[str, Any], min_rows: int, decision_reasons: Mapping[str, int], reconcile: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The explicit, machine-readable reason a run produced what it produced. An empty result is a VALID result with one of these codes, never a failure. ``codes`` lists every code that
    applies (in STATUS_CODES order); ``primary`` is the first, the most fundamental one."""
    codes: Dict[str, str] = {}
    n_scans = len(rec.scans)
    n_pos = sum(1 for _ms, ops in rec.scans for o in ops if not o.exclusions and o.net_bps_horizon is not None and o.net_bps_horizon > 0)
    if gate.verdict == "REJECTED":
        codes["REJECTED_CANDIDATE"] = f"candidate_competition rejected {gate.candidate_id}: {gate.reason}"
    if not history["adequate"]:
        av = history["available_days"]
        codes["INADEQUATE_HISTORY"] = (f"{history['min_holding_rows']} independent holding rows need {history['required_days']:.0f} days of history ({history['formula']}); "
                                       f"{'unknown' if av is None else f'{av:.1f}'} available to this window")
    if n_pos == 0:
        codes["NO_OPPORTUNITY"] = f"none of {n_scans} scan(s) had an eligible opportunity with positive net after costs (this window only)"
    n_groups = len(res.groups)
    fills = [f for f in res.fills if not f.fill_id.startswith("FORCED")]
    if not gate.allowed:
        codes["NO_ALLOCATION"] = gate.reason
    elif n_pos > 0 and n_groups == 0:
        top = ", ".join(f"{k}x{v}" for k, v in sorted(decision_reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:3]) or "no allocator decision recorded"
        codes["NO_ALLOCATION"] = f"the allocator allocated nothing although {n_pos} positive opportunity(ies) were scanned: {top}"
    refused = int(authority.get("orders_refused", 0)) + int(authority.get("groups_refused_at_registration", 0)) + int(decision_reasons.get("RISK_REJECTED", 0))
    if gate.allowed and not fills and refused > 0:
        codes["RISK_REFUSAL"] = f"risk / authority refused: orders={authority.get('order_refusal_codes')} groups={authority.get('registration_refusal_codes')} allocator risk rejections={decision_reasons.get('RISK_REJECTED', 0)}"
    if gate.allowed and n_groups > 0 and not fills:
        codes["NO_FILL"] = f"{n_groups} group(s) were submitted and none filled (no maker fill without a strict trade-through; bars never fill by default)"
    bad = [g for g, st in res.states.items() if (st.value if hasattr(st, "value") else st) in ("UNHEDGED", "UNKNOWN_OUTCOME")]
    if fills and (not ledger_pnl["net_complete"] or bad):
        codes["UNKNOWN_OR_UNRECONCILED"] = ("a ledger component is unknown: net is not stated" if not ledger_pnl["net_complete"] else f"{len(bad)} group(s) UNHEDGED or UNKNOWN_OUTCOME at the end of the window")
    if reconcile is not None and reconcile["ever_disagreed"]:                  # ms_reconcile found components that disagree (and halted new exposure): the books are NOT reconciled
        prior = codes.get("UNKNOWN_OR_UNRECONCILED")
        codes["UNKNOWN_OR_UNRECONCILED"] = (prior + "; " if prior else "") + \
            f"reconciliation disagreed on {len(reconcile['history'])} finding(s): {', '.join(sorted({h['code'] for h in reconcile['history']}))}; new exposure was halted"
    indep = int(report["observations"]["independent"]) if isinstance(report.get("observations"), Mapping) else 0
    if fills and indep < min_rows:
        codes["INADEQUATE_SAMPLE"] = f"{indep} independent closed round trip(s) < {min_rows} required (min_holding_rows)"
    if fills:
        codes["SIMULATED_EXECUTION"] = "fills exist, every one MODELLED against a simulated venue: not execution evidence"
    ordered = [c for c in STATUS_CODES if c in codes]
    return {"primary": ordered[0] if ordered else "NO_OPPORTUNITY", "codes": ordered, "reasons": {c: codes[c] for c in ordered},
            "independent_observations": indep, "positive_opportunity_scans": n_pos, "n_scans": n_scans, "n_groups": n_groups, "n_fills": len(fills)}


def _reconcile_document(gates: AuthorityGates, led: LedgerAdapter, ident: T.RunIdentity, label: str, generated_ms: int, evidence: Mapping[str, Any]) -> Dict[str, Any]:
    """``reconcile_report.json`` (schema reconcile_report/v1): the LAST step's ms_reconcile report plus the net comparison (the ledger's canonical net against the artifacts' net), and the
    history of every finding raised at any step. ``halt_new_exposure`` is the end-of-window state; ``ever_disagreed`` says a disagreement happened at any step."""
    last = gates.rc_last
    canon = led.ledger.canonical_net()["net"]
    p = led.pnl()
    net_rep = RC.reconcile(RC.Views(net_pnl={"ledger": canon, "artifact": p["net"] if p["net_complete"] else None}), require_venue=False)
    findings = list(last.findings) if last is not None else []
    have = {x.reason() for x in findings}
    findings += [f for f in net_rep.findings if f.reason() not in have]
    hist = [dict(v, reason=k) for k, v in sorted(gates.rc_history.items())]
    seen = {h["reason"] for h in hist}
    hist += [{"reason": f.reason(), "code": f.code, "key": f.key, "views": list(f.views), "detail": f.detail, "first_ms": generated_ms, "last_ms": generated_ms, "n_steps": 1}
             for f in net_rep.findings if f.reason() not in seen]
    return {"schema": "reconcile_report/v1", "basis": "SIMULATED", "generated_ms": int(generated_ms), "data_label": label, "evidence_basis": dict(evidence), "identity": ident.as_dict(),
            "findings": [{"code": f.code, "key": f.key, "views": list(f.views), "detail": _short(f.detail, 200)} for f in findings], "halt_new_exposure": bool(findings),
            "reasons": [f.reason() for f in findings], "compared": dict(last.compared) if last is not None else {}, "n_steps_checked": gates.rc_steps, "ever_disagreed": bool(hist),
            "history": [{k: (_short(v, 200) if isinstance(v, str) else v) for k, v in h.items()} for h in hist],
            "reference": "venue position read (SimVenue book in REPLAY / PAPER); engine, portfolio, ledger and artifacts are derived views"}


def run(source: Any, *, universe_dir: Optional[str] = None, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None, spec: Mapping[str, Any], candidate_id: str,
        spec_digest: str, risk_policy: Optional[Mapping[str, Any]] = None, replay_cfg: RP.ReplayConfig, artifacts_dir: str, mode: str = "REPLAY",
        scan_params: Optional[Mapping[str, Any]] = None, generated_ms: Optional[int] = None, clock: Optional[Callable[[], int]] = None, state_dir: Optional[str] = None,
        aggregate_notional_usd: Optional[float] = None, venue: Optional[EX.SimVenue] = None, store: Optional[Any] = None,
        competition_row: Optional[Mapping[str, Any]] = None, code_revision: str = "UNKNOWN", min_holding_rows: int = 60, trial_ledger: Optional[str] = None,
        data_parts: Optional[Mapping[str, str]] = None, micro: Any = None,
        max_daily_loss_usd: Optional[float] = None, max_drawdown_frac: Optional[float] = None) -> PipelineResult:
    """Run the whole stack on SIMULATED transport and write the control-centre artifacts + ``pipeline_summary.json``. ``source`` is a history-root path or a ``LoadedHistory`` (both go
    through the VERIFIED loader) or a list of injected series. ``competition_row`` is the candidate's candidate_competition row: a candidate that is REJECTED / UNTESTED / INSUFFICIENT_DATA
    there (or has no row) allocates NOTHING (the scan is still run and shown). ``generated_ms`` (default: the end of the window) and the PAPER ``clock`` are injected: the same inputs give
    the same artifact bytes. Every file carries ONE ``identity`` (ms_types.RunIdentity)."""
    if mode not in MODES:
        raise PipelineRefused(f"MODE_MUST_BE_ONE_OF:{MODES}")
    if mode == "PAPER" and clock is None:
        raise PipelineRefused("PAPER_MODE_NEEDS_AN_INJECTED_CLOCK")
    if mode == "REPLAY" and clock is not None:
        raise PipelineRefused("REPLAY_MODE_USES_THE_GRID_NOT_A_CLOCK")
    if not isinstance(spec, Mapping) or spec.get("kind", "SPOT_PERP_CARRY") not in SC.KINDS or spec.get("style", "TAKER") not in SC.STYLES:
        raise PipelineRefused("SPEC_KIND_OR_STYLE_INVALID")
    if spec.get("style", "TAKER") != replay_cfg.portfolio.style:
        raise PipelineRefused(f"SPEC_STYLE_{spec.get('style', 'TAKER')}_DOES_NOT_MATCH_PORTFOLIO_STYLE_{replay_cfg.portfolio.style}: the scanner's costs would not be the plan's")
    if not (isinstance(candidate_id, str) and candidate_id.strip() and isinstance(spec_digest, str) and spec_digest.strip()):
        raise PipelineRefused("CANDIDATE_ID_AND_SPEC_DIGEST_REQUIRED")
    if replay_cfg.spec_digest != spec_digest:
        raise PipelineRefused("REPLAY_CFG_SPEC_DIGEST_MISMATCH")
    if not isinstance(artifacts_dir, str) or not artifacts_dir:
        raise PipelineRefused("ARTIFACTS_DIR_REQUIRED")
    if not (isinstance(code_revision, str) and code_revision.strip()):
        raise PipelineRefused("CODE_REVISION_REQUIRED_USE_UNKNOWN_WHEN_NOT_KNOWN")
    if not (isinstance(min_holding_rows, int) and not isinstance(min_holding_rows, bool) and min_holding_rows > 0):
        raise PipelineRefused("MIN_HOLDING_ROWS_MUST_BE_A_POSITIVE_INTEGER")

    raw_series, label, info = load_series(source, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
    if data_parts is not None:
        parts = dict(data_parts)
    elif isinstance(source, LoadedHistory):
        parts = dict(source.parts)
    elif label == DATA_HISTORY:
        parts = input_parts(info, spot_dir, None)
    else:
        parts = {"injected": "UNKNOWN"}
    series = _to_replay_series(raw_series, replay_cfg)
    micro_info: Optional[Dict[str, Any]] = None
    if micro is not None:
        series, micro_info = apply_micro(series, micro)
        parts = {**parts, "collector": micro.data_digest()}
    symbols = [s.symbol for s in series]
    scope = str(info.get("survivorship", "SURVIVORS_ONLY"))
    universe_scope = scope if scope in ("SURVIVORS_ONLY", "DELISTINGS_OBSERVED") else "SURVIVORS_ONLY"

    policy = dict(risk_policy) if risk_policy is not None else derive_risk_policy(symbols, candidate_id=candidate_id, spec_digest=spec_digest, portfolio=replay_cfg.portfolio,
                                                                                    equity_usd=replay_cfg.equity0, aggregate_notional_usd=aggregate_notional_usd,
                                                                                    exec_policy=replay_cfg.exec_policy, max_daily_loss_usd=max_daily_loss_usd,
                                                                                    max_drawdown_frac=max_drawdown_frac)
    bad = MR.policy_refusals(policy)
    if bad:
        raise PipelineRefused("RISK_POLICY_REFUSED:" + ",".join(bad))
    missing_syms = sorted(set(symbols) - set(policy["allowed_symbols"]))
    if missing_syms:
        raise PipelineRefused("RISK_POLICY_ALLOW_LIST_LACKS:" + ",".join(missing_syms))

    gate = evaluate_gate(competition_row, candidate_id=candidate_id)
    gen = int(generated_ms if generated_ms is not None else replay_cfg.end_ms)
    ddig = data_digest_of(parts)
    ident = T.RunIdentity(run_id=T.derive_run_id(mode=mode, candidate_id=candidate_id, spec_digest=spec_digest, data_digest=ddig, generated_ms=gen), mode=mode, candidate_id=candidate_id,
                          spec_digest=spec_digest, data_digest=ddig, code_revision=code_revision, generated_ms=gen)

    os.makedirs(artifacts_dir, exist_ok=True)
    auth_dir = state_dir or os.path.join(artifacts_dir, "_ms_authority_state")
    if mode == "REPLAY":                                                   # a replay is a fresh world: its authority ledgers start empty (PAPER keeps them)
        for name in ("ms_groups.jsonl", "ms_reservations.jsonl"):
            try:
                os.unlink(os.path.join(auth_dir, name))
            except FileNotFoundError:
                pass
    sim_venue = venue or EX.SimVenue()
    gates = AuthorityGates(policy, state_dir=auth_dir, venue=sim_venue, start_ms=replay_cfg.start_ms)
    gates.ledger_getter = lambda: holder[0].ledger if holder else None
    params = dict(scan_params or {})
    params.setdefault("hold_days", replay_cfg.hold_ms / DAY_MS)
    recorder = ScanRecorder(spec, candidate_id, spec_digest, params)
    recorder.suppress = not gate.allowed
    holder: List[LedgerAdapter] = []
    mark_prov = "ASSUMED" if label == DATA_INJECTED else "MEASURED"

    def ledger_factory() -> LedgerAdapter:
        holder.append(LedgerAdapter(mark_prov, replay_cfg.fill.taker_bps_perp, replay_cfg.fill.taker_bps_spot))
        return holder[-1]

    def risk_assess(plan: T.OrderGroupPlan, state: Mapping[str, Any], pol: Mapping[str, Any], snaps: Mapping[str, T.MarketSnapshot]) -> Any:
        return MR.risk_assess(plan, state, pol, snaps)                     # looked up at call time

    res = RP.run_replay(series, replay_cfg, scan_fn=recorder, risk_assess=risk_assess, risk_policy=policy, ledger_factory=ledger_factory, store=store, venue=sim_venue,
                        clock=clock, gates=gates)
    led = holder[0]
    trials_total = None
    if trial_ledger is not None:                                           # the persistent multiple-testing count: this configuration is ONE trial; an identical re-run is the same trial
        vid = hashlib.sha256(json.dumps([replay_cfg.start_ms, replay_cfg.end_ms, replay_cfg.hold_ms, replay_cfg.decision_every_ms, replay_cfg.portfolio.style, sorted(params.items())],
                                        sort_keys=True, default=str).encode()).hexdigest()[:16]
        trials_total = RP.record_trials(trial_ledger, [RP.variant_key(vid, spec_digest)])
    report = RP.build_report([res], trials_total=trials_total, trials_this_run=1, persistent_trials=trials_total is not None)
    if label == DATA_INJECTED:
        report["provenance"]["weakest"] = T.weakest(str(report["provenance"]["weakest"]), "ASSUMED")
    first_ms, last_ms = _series_span(series)
    history = history_requirement(formation_days=float(params.get("formation_days", SC.DEFAULT_PARAMS["formation_days"])), hold_days=replay_cfg.hold_ms / DAY_MS,
                                  min_rows=min_holding_rows, first_ms=first_ms, last_ms=last_ms, end_ms=replay_cfg.end_ms)
    evidence = {"data": label, "data_parts": dict(sorted(parts.items())), "holdout": (info.get("holdout") if label == DATA_HISTORY else None), "execution": "MODELLED",
                "books": "SYNTHETIC_ASSUMED_BOOK_FROM_MARKS (no collector: no measured depth or trades)" if micro_info is None else "MEASURED_BOOK_TAPE (PERP leg only; the spot leg uses the synthetic book; fills MODELLED)",
                "microstructure": micro_info,
                "fills": f"bar_policy={replay_cfg.fill.bar_policy}: a touch is never a fill; maker fills need a strict trade-through", "claim": "SIMULATED; never MEASURED execution"}
    arts = _build_artifacts(res, led, recorder, series, policy, gates, report, label=label, generated_ms=gen, spec=spec, candidate_id=candidate_id, universe_scope=universe_scope,
                            gate=gate, evidence_basis=evidence)
    arts = stamp_identity(arts, ident)
    files: Dict[str, str] = {}
    for name in ARTIFACT_FILES:
        files[name] = os.path.join(artifacts_dir, name)
        _write_json(files[name], arts[name])
    reasons: Dict[str, int] = {}
    for d in res.decisions:
        for r in d["reasons"][:2]:
            k = str(r).split(":", 1)[0]
            reasons[k] = reasons.get(k, 0) + 1
    assumed = sorted(set(res.assumed) | ({"INSTRUMENT_FILTERS_DEFAULTED"} if label == DATA_HISTORY else set()))
    pnl = led.pnl()
    rec_doc = _reconcile_document(gates, led, ident, label, gen, evidence)
    status = classify_status(gate=gate, res=res, rec=recorder, report=report, ledger_pnl=pnl, authority=gates.stats(), history=history, min_rows=min_holding_rows, decision_reasons=reasons, reconcile=rec_doc)
    summary = {"schema": SCHEMA, "mode": mode, "data_label": label, "generated_ms": gen, "identity": ident.as_dict(), "data_parts": dict(sorted(parts.items())), "evidence_basis": evidence, "symbols": symbols, "spec": dict(spec),
               "candidate_id": candidate_id, "spec_digest": spec_digest, "status": status, "gate": gate.as_dict(), "history": history,
               "sample": {"independent": status["independent_observations"], "min_holding_rows": min_holding_rows, "closed_groups": sum(1 for g in res.groups.values() if g.closed)},
               "execution": {"evidence": "MODELLED", "books": evidence["books"], "fills": evidence["fills"], "bar_policy": replay_cfg.fill.bar_policy,
                             "stale_quote_ms": replay_cfg.fill.stale_quote_ms, "exit_book_max_age_ms": replay_cfg.exit_book_max_age_ms,
                             "exits_on_wider_book_cap": int(res.stats.get("transport", {}).get("exits_on_wider_book_cap", 0)),
                             "exits_refused_stale_quote": int(res.stats.get("transport", {}).get("exits_refused_stale_quote", 0)),
                             "taker_entries_refused_stale_quote": int(res.stats.get("transport", {}).get("refused", {}).get("STALE_QUOTE", 0))},
               "trials_total": trials_total, "risk_policy": policy, "authority": gates.stats(), "decision_reason_counts": dict(sorted(reasons.items())), "assumed_inputs": assumed,
               "ledger": {"net": pnl["net"] if pnl["net_complete"] else None, "net_subtotal": pnl["net"], "net_complete": pnl["net_complete"], "flags": pnl["flags"][:20], "status": pnl["status"]},
               "reconcile": {"ever_disagreed": rec_doc["ever_disagreed"], "halt_new_exposure": rec_doc["halt_new_exposure"], "n_steps_checked": rec_doc["n_steps_checked"],
                             "codes": sorted({h["code"] for h in rec_doc["history"]})},
               "journal_head": res.journal_head, "promotion_eligible": False, "report": report,
               "note": "REPLAY/PAPER on a simulated transport. ONE net everywhere (artifacts, equity curve, observations, this summary): net = gross (price leg AT MID + funding) - fees - "
                       "taker slippage - adverse selection. The taker walk is inside the fill prices (added back to reach mid, then charged once); adverse selection is a modelled "
                       "post-fill markout that is in no fill price, so ms_accounting's own net (the 'ledger' dict) omits it and the pipeline deducts it once"}
    files[RECONCILE_FILE] = os.path.join(artifacts_dir, RECONCILE_FILE)
    _write_json(files[RECONCILE_FILE], rec_doc)
    files[SUMMARY_FILE] = os.path.join(artifacts_dir, SUMMARY_FILE)
    _write_json(files[SUMMARY_FILE], summary)
    return PipelineResult(mode, label, res, report, summary, files, led, recorder, gates, policy, arts)


def file_digests(artifacts_dir: str) -> Dict[str, str]:
    """sha256 of the six artifacts and the summary (for the determinism check)."""
    out = {}
    for name in ARTIFACT_FILES + (SUMMARY_FILE, RECONCILE_FILE):
        with open(os.path.join(artifacts_dir, name), "rb") as fh:
            out[name] = hashlib.sha256(fh.read()).hexdigest()
    return out


# ======================================================================== the command line: validate every input, then run

class CliRefused(Exception):
    """A refusal with a machine-readable code. The CLI prints ``{"ok": false, "error": {"code", "message"}}`` and exits 2; nothing ran and nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:                                    # argparse would print usage to stderr and exit 2: same exit code, but machine readable
        raise CliRefused("ARGUMENT_ERROR", message)


MS_MIN, MS_MAX = 10 ** 12, 10 ** 13                                          # epoch milliseconds (2001 .. 2286): seconds and microseconds are refused, not guessed
REGISTRY_DIR = os.path.join(BOT, "research", "candidates")


@dataclass
class Validated:
    history_root: str
    universe_dir: str
    spot_dir: str
    collector_root: Optional[str]
    artifacts_dir: str
    ledger_dir: str
    start_ms: int
    end_ms: int
    mark_interval: Optional[str]
    min_holding_rows: int
    hold_days: int
    decision_every_hours: int
    execution_policy: str
    risk_policy: Optional[Dict[str, Any]]
    max_daily_loss_usd: Optional[float]
    max_drawdown_frac: Optional[float]
    mode: str
    clock_ms: int
    code_revision: str
    candidate_path: str
    candidate_spec: Dict[str, Any]
    candidate_id: str
    spec_digest: str
    kind: str
    loaded: LoadedHistory
    micro: Any
    collector_digest: Optional[str]
    data_digest: str
    data_parts: Dict[str, str]
    unreserved_exploratory: bool = False


def _int_arg(v: Any, code: str, what: str, lo: int, hi: Optional[int] = None) -> int:
    try:
        n = int(str(v), 10)
    except (TypeError, ValueError):
        raise CliRefused(code, f"{what} must be an integer, got {_short(v, 40)!r}") from None
    if n < lo or (hi is not None and n > hi):
        raise CliRefused(code, f"{what} out of range [{lo}, {hi if hi is not None else 'inf'}]: {n}")
    return n


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _is_inside(child: str, parent: str) -> bool:
    c, p = _real(child), _real(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _inside_git(path: str) -> bool:
    """True when the path, or the nearest existing ancestor of it, is in a git work tree (a ``.git`` directory or file on the way up)."""
    p = _real(path)
    while not os.path.exists(p):
        q = os.path.dirname(p)
        if q == p:
            break
        p = q
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return True
        q = os.path.dirname(p)
        if q == p:
            return False
        p = q


def _require_dir(path: Any, code: str, what: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isdir(path) or os.path.islink(path):
        raise CliRefused(code, f"{what} must be an existing directory (not a symlink): {_short(path, 120)}")
    return os.path.abspath(path)


def _load_candidate(path: str, claimed_digest: Optional[str]) -> Tuple[Dict[str, Any], str, str, str]:
    import strategy_verdict as SV
    if not isinstance(path, str) or not os.path.isfile(path):
        raise CliRefused("CANDIDATE_MISSING", f"candidate spec file not found: {_short(path, 120)}")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("candidate spec larger than 2 MB")
        spec = json.loads(raw.decode("utf-8"), parse_constant=lambda c: (_ for _ in ()).throw(ValueError("NaN/Infinity")))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise CliRefused("CANDIDATE_UNREADABLE", f"candidate spec is not valid JSON: {_short(exc, 120)}") from None
    if not isinstance(spec, dict) or not isinstance(spec.get("candidate_id"), str) or not spec["candidate_id"].strip():
        raise CliRefused("CANDIDATE_UNREADABLE", "candidate spec has no candidate_id")
    digest = SV.spec_digest(spec)                                            # the SAME digest the competition / harness binds a candidate to
    in_registry = _is_inside(path, REGISTRY_DIR)
    if claimed_digest is None and not in_registry:
        raise CliRefused("CANDIDATE_DIGEST_REQUIRED", "a candidate spec outside research/candidates must come with --candidate-digest (its sha256 spec digest)")
    if claimed_digest is not None and claimed_digest != digest:
        raise CliRefused("CANDIDATE_DIGEST_MISMATCH", "the spec file does not match --candidate-digest: it was edited or is another file")
    kind = CANDIDATE_KIND.get(str(spec.get("family")))
    if kind is None:
        raise CliRefused("CANDIDATE_NOT_RUNNABLE", f"family {_short(spec.get('family'), 60)!r} is not runnable by this pipeline (runnable: {sorted(CANDIDATE_KIND)})")
    return spec, spec["candidate_id"], digest, kind


def _load_risk_policy(path: str) -> Dict[str, Any]:
    try:
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > 65_536:
            raise ValueError("not a regular file of at most 64 KB")
        with open(path, "rb") as fh:
            pol = json.loads(fh.read().decode("utf-8"), parse_constant=lambda c: (_ for _ in ()).throw(ValueError("NaN/Infinity")))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise CliRefused("RISK_POLICY_UNREADABLE", f"risk policy file unreadable: {_short(exc, 120)}") from None
    bad = MR.policy_refusals(pol)
    if bad:
        raise CliRefused("RISK_POLICY_REFUSED", "ms_risk's closed schema refuses the policy: " + ",".join(bad)[:300])
    return dict(pol)


def load_collector(root: str) -> Any:
    """The verified microstructure dataset (``ms_microstructure.load_cycles``: every cycle re-verified). Refused, never degraded, when the module is absent, the cycles do not verify, the
    dataset is empty, or its cohort was selected with a rule that reads funding (a funding edge may not be evaluated on a funding-selected cohort)."""
    import importlib
    try:
        mm = importlib.import_module("ms_microstructure")
    except ImportError:
        raise CliRefused("COLLECTOR_PATH_UNAVAILABLE", "--collector-root needs the ms_microstructure module, which is not in this tree") from None
    try:
        ds = mm.load_cycles(root, verify=True)
    except (ValueError, OSError) as exc:
        raise CliRefused("COLLECTOR_NOT_VERIFIED", _short(exc, 200)) from None
    if not ds.verified or ds.problems or any(c.problems for c in ds.cycles):
        raise CliRefused("COLLECTOR_NOT_VERIFIED", "the collector cycles do not verify: " + "; ".join(list(ds.problems)[:3])[:200])
    if not ds.symbols():
        raise CliRefused("COLLECTOR_EMPTY", "the collector root holds no symbol with a recorded book")
    if not mm.evaluates_funding_edge_ok(ds):
        raise CliRefused("COLLECTOR_COHORT_FUNDING_SELECTED", f"cohort {_short(ds.cohort_id, 40)} was selected by a rule that reads funding (cohort_v2) or an unknown rule: it must not be used to evaluate a funding edge; "
                         "use the funding-independent broad history cohort")
    return ds


MICRO_LIMITS = ("NO_SPOT_ORDERBOOK_RECORDED: the spot / hedge leg is NOT measured; the replay gives it the SYNTHETIC book (SYNTHETIC_BOOK_DEPTH_AND_SPREAD: ReplayConfig.synthetic_spread_bps "
                "and synthetic_depth_usd, 2 bps / 2M USD by default), flagged in assumed_inputs; it is never borrowed from the perp book",
                "MAKER_FILLS_USE_THE_REPLAYS_OWN_FILL_MODEL: the replay places makers with ms_fillmodel.place_maker (modelled queue and a MODELLED maker markout) against the measured perp book and "
                "tape; it does NOT use place_maker_from_micro, so the collector's tape-gap / queue-depletion evidence is not applied",
                "RECENT_TRADE_TAPE_IS_THE_LAST_TRADES_LIMIT_ROWS_PER_CYCLE: busy symbols at 60 s cadence often have TAPE_GAP, so only tape windows whose coverage is proven give ticks (no maker fills otherwise)",
                "QUEUE_POSITION_ESTIMATED_FROM_VISIBLE_SIZE_ONLY")


def apply_micro(series: Sequence[RP.ReplaySeries], micro: Any) -> Tuple[List[RP.ReplaySeries], Dict[str, Any]]:
    """Give each symbol the collector's MEASURED books (keyed by the collector's local receive time) and tape (only windows whose coverage the tape proves). Symbols without a recorded
    book are EXCLUDED from a collector run (never traded on a synthetic book beside measured ones) and listed."""
    import ms_microstructure as MM
    if not getattr(micro, "verified", False) or getattr(micro, "problems", ()):
        raise PipelineRefused("COLLECTOR_NOT_VERIFIED: an unverified collector dataset is never labelled MEASURED")
    if not MM.evaluates_funding_edge_ok(micro):
        raise PipelineRefused("COLLECTOR_COHORT_FUNDING_SELECTED: a funding-selected cohort must not evaluate a funding edge")
    out: List[RP.ReplaySeries] = []
    without: List[str] = []
    for x in series:
        snaps = sorted((v for v in micro.snapshots(x.symbol) if v.valid), key=lambda v: (v.local_receive_ms, v.cycle))
        if not snaps:
            without.append(x.symbol)
            continue
        books = tuple((v.local_receive_ms, v.bids, v.asks) for v in snaps)
        ticks = tuple(sorted({(t[0], t[1], t[2]) for v in snaps if v.tape_complete for t in v.trades}))
        out.append(dataclasses.replace(x, books=books, ticks=tuple(FM_Tick(a, b, c) for a, b, c in ticks)))
    if not out:
        raise PipelineRefused("COLLECTOR_HAS_NO_BOOK_FOR_ANY_HISTORY_SYMBOL")
    return out, {"symbols_with_micro": [x.symbol for x in out], "symbols_without_micro_excluded": sorted(without), "limits": list(MICRO_LIMITS), "cohort_id": micro.cohort_id}


def validate_inputs(ns: argparse.Namespace) -> Validated:
    """EVERY input is checked before anything runs (existence, the real verifiers, mode, window). Any failure is a ``CliRefused`` with a code. Writes nothing."""
    if ns.mode not in MODES:
        extra = (" TESTNET sessions are owned by the separate, owner-invoked ms_testnet_* session tool; this CLI does not accept TESTNET (and there is no mainnet mode)." if ns.mode == "TESTNET" else "")
        raise CliRefused("MODE_NOT_ACCEPTED", f"mode {_short(ns.mode, 40)!r} is not accepted: this CLI runs {' or '.join(MODES)} only (simulated transport)." + extra)
    if ns.execution_policy not in ("MAKER_FIRST", "TAKER"):
        raise CliRefused("EXECUTION_POLICY_INVALID", "--execution-policy must be MAKER_FIRST or TAKER")
    start = _int_arg(ns.start_ms, "WINDOW_INVALID", "--start-ms", MS_MIN, MS_MAX)
    end = _int_arg(ns.end_ms, "WINDOW_INVALID", "--end-ms", MS_MIN, MS_MAX)
    if start >= end:
        raise CliRefused("WINDOW_INVALID", f"--start-ms {start} must be before --end-ms {end}")
    rows = _int_arg(ns.min_holding_rows, "MIN_HOLDING_ROWS_INVALID", "--min-holding-rows", 1, 100_000)
    hold = _int_arg(ns.hold_days, "HOLD_DAYS_INVALID", "--hold-days", 1, 400)
    every = _int_arg(ns.decision_every_hours, "DECISION_EVERY_INVALID", "--decision-every-hours", 1, 24 * 30)
    if end - start < hold * DAY_MS:
        raise CliRefused("WINDOW_INVALID", f"the window ({(end - start) / DAY_MS:.2f} days) is shorter than one hold ({hold} days): nothing could complete")
    clock = _int_arg(ns.clock_ms if ns.clock_ms is not None else end, "CLOCK_MS_INVALID", "--clock-ms", MS_MIN, MS_MAX)
    rev = ns.code_revision if ns.code_revision is not None else git_revision()
    if not (rev == "UNKNOWN" or re.fullmatch(r"[0-9a-f]{40}", str(rev))):
        raise CliRefused("CODE_REVISION_INVALID", "--code-revision must be a 40-hex git object name or UNKNOWN")
    if ns.risk_policy is not None and ns.derive_risk_policy:
        raise CliRefused("RISK_POLICY_CONFLICT", "give EITHER --risk-policy FILE OR --derive-risk-policy, not both")
    daily = frac = None
    if ns.max_daily_loss_usd is not None:
        try:
            daily = float(ns.max_daily_loss_usd)
        except ValueError:
            daily = float("nan")
        if not (math.isfinite(daily) and daily > 0):
            raise CliRefused("LOSS_LIMIT_INVALID", "--max-daily-loss-usd must be a finite number > 0")
    if ns.max_drawdown_frac is not None:
        try:
            frac = float(ns.max_drawdown_frac)
        except ValueError:
            frac = float("nan")
        if not (math.isfinite(frac) and 0 < frac <= 1):
            raise CliRefused("LOSS_LIMIT_INVALID", "--max-drawdown-frac must be a number in (0, 1]")
    if ns.risk_policy is not None and (daily is not None or frac is not None):
        raise CliRefused("LOSS_LIMIT_NEEDS_DERIVED_POLICY", "--max-daily-loss-usd / --max-drawdown-frac set the DERIVED policy; a --risk-policy file carries its own limits")
    if ns.risk_policy is None and not ns.derive_risk_policy:
        raise CliRefused("RISK_POLICY_REQUIRED", "a risk policy is required: --risk-policy FILE (ms_risk closed schema) or an explicit --derive-risk-policy")
    hist = _require_dir(ns.history_root, "HISTORY_ROOT_MISSING", "--history-root")
    uni = _require_dir(ns.universe_dir, "UNIVERSE_DIR_MISSING", "--universe-dir")
    spot = _require_dir(ns.spot_dir, "SPOT_DIR_MISSING", "--spot-dir")
    col = _require_dir(ns.collector_root, "COLLECTOR_ROOT_MISSING", "--collector-root") if ns.collector_root is not None else None
    inputs = [d for d in (hist, uni, spot, col) if d]
    # ---- output directories
    art = os.path.abspath(ns.artifacts_dir)
    if os.path.islink(art):
        raise CliRefused("ARTIFACTS_DIR_IS_SYMLINK", "--artifacts-dir may not be a symlink")
    if not os.path.isdir(os.path.dirname(art)):
        raise CliRefused("ARTIFACTS_DIR_PARENT_MISSING", f"the parent of --artifacts-dir does not exist: {_short(os.path.dirname(art), 120)}")
    if any(_is_inside(art, d) or _is_inside(d, art) for d in inputs):
        raise CliRefused("ARTIFACTS_DIR_IS_AN_INPUT", "--artifacts-dir is, contains or sits inside an input dataset: an existing dataset is never an output directory")
    if _inside_git(art):
        raise CliRefused("ARTIFACTS_DIR_INSIDE_GIT", "--artifacts-dir is inside a git work tree: run output is never committed")
    if os.path.exists(art) and (not os.path.isdir(art) or os.listdir(art)):
        raise CliRefused("ARTIFACTS_DIR_NOT_EMPTY", "--artifacts-dir must be a NEW or EMPTY directory")
    led = os.path.abspath(ns.ledger_dir)
    if _is_inside(led, art) or _is_inside(art, led):
        raise CliRefused("LEDGER_DIR_INSIDE_ARTIFACTS_DIR", "--ledger-dir and --artifacts-dir may not contain one another (the ledger is persistent, the artifacts are per run)")
    if _is_inside(led, os.path.join(BOT, "state")) or _inside_git(led) or any(_is_inside(led, d) for d in inputs):
        raise CliRefused("LEDGER_DIR_REFUSED", "--ledger-dir may not be under bot/state, inside a git work tree or inside an input dataset")
    if os.path.islink(led) or (os.path.exists(led) and not os.path.isdir(led)) or not os.path.isdir(os.path.dirname(led)):
        raise CliRefused("LEDGER_DIR_REFUSED", "--ledger-dir must be a directory (or a new one in an existing parent), not a symlink")
    # ---- candidate, policy file
    spec, cid, digest, kind = _load_candidate(ns.candidate, ns.candidate_digest)
    policy = None
    if ns.risk_policy is not None:
        policy = _load_risk_policy(ns.risk_policy)
        if policy["candidate_id"] != cid or policy["spec_digest"] != digest:
            raise CliRefused("RISK_POLICY_CANDIDATE_MISMATCH", "the risk policy names another candidate or spec digest than --candidate")
    micro = load_collector(col) if col else None
    coll_digest = micro.data_digest() if micro is not None else None
    # ---- the real verifiers
    try:
        import history_holdout as HH
        unreserved = bool(getattr(ns, "unreserved_exploratory", False))
        loaded = verify_inputs(hist, universe_dir=uni, spot_dir=spot, mark_interval=ns.mark_interval, holdout_ledger=HH.default_registry_path(), unreserved_exploratory=unreserved)
    except PipelineRefused as exc:
        held = "HOLDOUT_NOT_RESERVED" in str(exc) or "HOLDOUT_SPENT" in str(exc) or "HOLDOUT_LEDGER" in str(exc)
        raise CliRefused("HOLDOUT_NOT_RESERVED" if held else "INPUT_NOT_VERIFIED", _short(exc, 800 if held else 400)) from None
    parts = dict(loaded.parts)
    if coll_digest:
        parts["collector"] = coll_digest
    first, last = _series_span(loaded.series)
    if first is None or end < first or start > last:
        raise CliRefused("WINDOW_OUTSIDE_DATA", f"the window [{start}, {end}] does not overlap the verified history [{first}, {last}]: nothing could be tested")
    if policy is not None:
        lacks = sorted({s.symbol for s in loaded.series} - set(policy["allowed_symbols"]))
        if lacks:
            raise CliRefused("RISK_POLICY_ALLOW_LIST_LACKS", "the risk policy allow-list lacks verified symbols (it is never widened): " + ",".join(lacks)[:200])
    return Validated(hist, uni, spot, col, art, led, start, end, ns.mark_interval, rows, hold, every, ns.execution_policy, policy, daily, frac, ns.mode, clock, rev, os.path.abspath(ns.candidate), spec, cid,
                     digest, kind, loaded, micro, coll_digest, data_digest_of(parts), parts, unreserved)


def build_replay_config(v: Validated) -> RP.ReplayConfig:
    style = v.execution_policy
    portfolio = PF.PortfolioPolicy(style=style, max_wait_ms=3 * RP.MS_H) if style == "MAKER_FIRST" else PF.PortfolioPolicy(style=style)
    # the conservative fill policy: bar_policy NO_FILL (a bar never fills an order; no measured trades => no maker fill)
    limits = {k: x for k, x in (("max_daily_loss_usd", v.max_daily_loss_usd), ("max_drawdown_frac", v.max_drawdown_frac)) if x is not None}
    portfolio = dataclasses.replace(portfolio, **limits)                      # the allocator enforces the same hard limits the derived policy gives ms_risk
    return RP.ReplayConfig(start_ms=v.start_ms, end_ms=v.end_ms, hold_ms=v.hold_days * DAY_MS, decision_every_ms=v.decision_every_hours * RP.MS_H, spec_digest=v.spec_digest, portfolio=portfolio)


def _emit(doc: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(doc, sort_keys=True, allow_nan=False, default=str) + "\n")
    sys.stdout.flush()


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--history-root", required=True, help="market_history root (funding/, mark-kline-<interval>/); re-verified before anything runs")
    p.add_argument("--universe-dir", required=True, help="verified universe_capture directory")
    p.add_argument("--spot-dir", required=True, help="verified spot-instruments snapshot directory (hedgeability)")
    p.add_argument("--collector-root", default=None, help="optional microstructure collector root: decision-time books and trades (needs ms_microstructure; refused when unavailable)")
    p.add_argument("--artifacts-dir", required=True, help="a NEW or EMPTY directory outside git (never an existing dataset)")
    p.add_argument("--ledger-dir", required=True, help="persistent directory for the multiple-testing trial ledgers (candidate_competition + this pipeline's trials); the reserved-holdout registry is per user, not here")
    p.add_argument("--unreserved-exploratory", action="store_true",
                   help="run on a history root with NO reserved holdout: the whole window is read, the result is stamped NOT_A_HOLDOUT_STUDY and the dataset can never be reserved afterwards "
                        "(default: refuse until `tools/history_holdout.py --reserve` has been run)")
    p.add_argument("--start-ms", required=True, help="window start, epoch ms")
    p.add_argument("--end-ms", required=True, help="window end, epoch ms")
    p.add_argument("--candidate", required=True, help="candidate spec JSON: a file under research/candidates, or any file together with --candidate-digest")
    p.add_argument("--candidate-digest", default=None, help="sha256 spec digest the --candidate file must have (required outside research/candidates)")
    p.add_argument("--mark-interval", default=None, help="mark-kline interval of the history root (default: the loader's own single-dataset rule)")
    p.add_argument("--min-holding-rows", default="60", help="independent holding rows a claim needs (sets the required history span and the sample check)")
    p.add_argument("--hold-days", default="7", help="holding period in whole days")
    p.add_argument("--decision-every-hours", default="8", help="hours between allocation decisions")
    p.add_argument("--execution-policy", required=True, help="MAKER_FIRST or TAKER")
    p.add_argument("--risk-policy", default=None, help="risk policy JSON, validated by ms_risk's closed schema")
    p.add_argument("--derive-risk-policy", action="store_true", help="derive a conservative policy from the verified symbol allow-list (explicit opt-in)")
    p.add_argument("--max-daily-loss-usd", default=None, help="hard daily-loss limit (USD) for the derived policy and the allocator; entries are refused when breached or when the inputs are unknown")
    p.add_argument("--max-drawdown-frac", default=None, help="hard drawdown limit in (0, 1] (default: the portfolio policy's 0.10)")
    p.add_argument("--mode", default="REPLAY", help="REPLAY or PAPER (simulated transport both; TESTNET is not accepted here)")
    p.add_argument("--clock-ms", default=None, help="the run's fixed timestamp for determinism (default: --end-ms; the wall clock is never read)")
    p.add_argument("--code-revision", default=None, help="40-hex git revision stamped on the artifacts (default: git rev-parse HEAD, else UNKNOWN)")


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="ms_pipeline", description="Multi-symbol carry pipeline: REPLAY / PAPER on a SIMULATED transport only (no network, no credential, no order). "
                 "Exit 0 = ran (an empty result is a valid result with an explicit status code), exit 2 = refused (machine-readable error on stdout).")
    sub = ap.add_subparsers(dest="command", required=True)
    _add_common(sub.add_parser("validate", help="verify every input and print the identity parts; write nothing"))
    _add_common(sub.add_parser("run", help="validate, run the pipeline and write the six control-centre artifacts + pipeline_summary.json"))
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        ns = build_parser().parse_args(argv)
        v = validate_inputs(ns)
        base = {"ok": True, "command": ns.command, "candidate": {"candidate_id": v.candidate_id, "spec_digest": v.spec_digest, "kind": v.kind}, "data_parts": dict(sorted(v.data_parts.items())),
                "data_digest": v.data_digest, "mode": v.mode, "window": {"start_ms": v.start_ms, "end_ms": v.end_ms}, "code_revision": v.code_revision}
        if ns.command == "validate":
            _emit(base)
            return 0
        os.makedirs(v.ledger_dir, exist_ok=True)
        warnings: List[str] = []
        try:
            row = competition_row_for(v.candidate_id, history_root=v.history_root, universe_dir=v.universe_dir, spot_dir=v.spot_dir, mark_interval=v.mark_interval, ledger_dir=v.ledger_dir,
                                      unreserved_exploratory=v.unreserved_exploratory)
        except (ValueError, OSError, KeyError) as exc:                       # the competition could not run: its verdict is unknown, and an unknown verdict allocates nothing
            row = None
            warnings.append("COMPETITION_FAILED:" + _short(exc, 160))
        cfg = build_replay_config(v)
        pr = run(v.loaded, spec={"kind": v.kind, "style": v.execution_policy}, candidate_id=v.candidate_id, spec_digest=v.spec_digest, risk_policy=v.risk_policy, replay_cfg=cfg,
                 artifacts_dir=v.artifacts_dir, mode=v.mode, generated_ms=v.clock_ms, clock=StepClock(v.start_ms, cfg.step_ms) if v.mode == "PAPER" else None, competition_row=row, micro=v.micro,
                 code_revision=v.code_revision, min_holding_rows=v.min_holding_rows, max_daily_loss_usd=v.max_daily_loss_usd, max_drawdown_frac=v.max_drawdown_frac, trial_ledger=os.path.join(v.ledger_dir, "ms_pipeline_trials.jsonl"), data_parts=v.data_parts)
        _emit({**base, "artifacts_dir": v.artifacts_dir, "identity": pr.summary["identity"], "status": {"primary": pr.summary["status"]["primary"], "codes": pr.summary["status"]["codes"]},
               "files": {n: os.path.basename(p) for n, p in sorted(pr.files.items())}, "digests": file_digests(v.artifacts_dir), "warnings": warnings})
        return 0
    except CliRefused as exc:
        _emit({"ok": False, "error": {"code": exc.code, "message": exc.message}})
        return 2
    except PipelineRefused as exc:
        _emit({"ok": False, "error": {"code": "PIPELINE_REFUSED", "message": _short(exc, 400)}})
        return 2


if __name__ == "__main__":
    # run as ``python3 tools/ms_pipeline.py`` this module is ``__main__``, and ms_authority only accepts the whitelisted ``ms_pipeline.SimulatedTransport``: hand over to the real module
    import ms_pipeline as _self
    sys.exit(_self.main())
