#!/usr/bin/env python3
"""ONE economics sheet per decision: every line the owner needs to judge whether a trade can pay, each with the BASIS it stands on, and UNKNOWN wherever nothing established it.

Round 22 (the owner's directive: one capital and economics definition; for each candidate the gross edge, fees, spread, slippage, funding, financing, capital, holding period, operational costs, net,
break-even, worst-case loss and uncertainty; "unknown must remain UNKNOWN"). This module is a PURE COMPOSITION of what the repository already computes: the economic gate's verdict (``ms_edge_gate.EdgeVerdict``
as a dict: cost lines, capital block, provenance, net, break-even), the ONE capital definition (``economic_model.structure_capital_usd``), and optionally the eligibility gate's result. It estimates nothing, reads
no data, no clock, no network and no environment, and it never turns an unknown into zero.

Provenance labels (the economic model's own, strongest first): ``measured`` (observed from a venue fill on THIS account), ``venue_stated`` (the venue published it, e.g. a fee tier read from the account),
``derived`` (computed from other labelled figures), ``modelled`` (a typed or fitted assumption with no measurement behind it), ``assumed`` (a placeholder), ``unknown``; plus ``policy`` for a configured cap
(a trigger, not a model). ``uncalibrated_modelled`` lists the modelled scenario inputs nothing has measured: a net edge that rests on them is a model output, not a result.

The sheet's own verdict is only a reading aid: ``net_positive`` is True only when the verdict's net is a number above zero AND no cost line is unknown; otherwise it is False or None (UNKNOWN), never True.
Whether a trade is allowed is the eligibility gate's job, not this sheet's."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from decimal import Decimal as D
from typing import Any, Dict, List, Mapping, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [p for p in (HERE, os.path.dirname(HERE)) if p not in sys.path]
import economic_model as EM  # noqa: E402

SCHEMA = "economics_sheet/1"
UNKNOWN = "UNKNOWN"
BPS_LINES = ("fee", "spread", "slippage", "impact", "basis", "hedge", "adverse", "borrow", "financing", "funding_cost")


def _is_num(x: Any) -> bool:
    """A finite number (as a number or a decimal string). NaN, inf, a word and None are not."""
    if isinstance(x, bool) or x is None:
        return False
    try:
        v = float(str(x))
    except (TypeError, ValueError):
        return False
    return v == v and abs(v) != float("inf")


def _line(value: Any, unit: str, basis: Optional[str], note: Optional[str] = None) -> Dict[str, Any]:
    known = _is_num(value)
    return {"value": str(value) if known else UNKNOWN, "unit": unit, "provenance": (basis or "unknown") if known else "unknown", **({"note": note} if note else {})}


def _group(costs: Mapping[str, Any], prov: Mapping[str, Any], names: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in names:
        out[k] = _line(costs.get(k), "bps", (prov.get("components") or {}).get(k)) if k in costs else {"value": UNKNOWN, "unit": "bps", "provenance": "unknown", "note": "not part of this verdict"}
    return out


def build(edge: Mapping[str, Any], *, structure: str = EM.ACQUIRED_SPOT_PERP, notional_usd: Any = None, leverage: Any = 1, hold_s: Any = None, eligibility: Optional[Mapping[str, Any]] = None,
          loss_cap_usd: Any = None, worst_case_loss_usd: Any = None, operating_cost_usd_per_day: Any = None) -> Dict[str, Any]:
    """The sheet for one decision. ``edge`` is ``EdgeVerdict.as_dict()`` (or the same shape from a decision record); everything else is optional and stays UNKNOWN when absent."""
    e = edge if isinstance(edge, Mapping) else {}
    costs = e.get("costs_bps") if isinstance(e.get("costs_bps"), Mapping) else {}
    prov = e.get("provenance") if isinstance(e.get("provenance"), Mapping) else {}
    cap = e.get("capital") if isinstance(e.get("capital"), Mapping) else {}
    fee_names = sorted(k for k in costs if "fee" in k)
    spread_names = sorted(k for k in costs if "spread" in k)
    slip_names = sorted(k for k in costs if "slip" in k or "impact" in k or "walk" in k)
    fin_names = sorted(k for k in costs if "financ" in k or "capital" in k or "borrow" in k)
    other_names = sorted(k for k in costs if k not in set(fee_names + spread_names + slip_names + fin_names))
    # capital: the ONE definition, from the structure and the notional; the verdict's own committed figure is shown beside it
    cap_def: Dict[str, Any] = {"structure": structure}
    try:
        if _is_num(notional_usd) and float(str(notional_usd)) > 0:
            q = EM.structure_capital_usd(structure, notional_usd, leverage)
            cap_def.update(committed_usd_by_the_one_definition=str(q.value), provenance=q.provenance, multiple_of_notional=str(q.value / D(str(notional_usd))) if q.known else UNKNOWN)
        else:
            cap_def.update(committed_usd_by_the_one_definition=UNKNOWN, provenance=UNKNOWN, note="no notional given")
    except Exception as exc:  # noqa: BLE001 - an unpriceable structure is UNKNOWN, not zero
        cap_def.update(committed_usd_by_the_one_definition=UNKNOWN, provenance=UNKNOWN, note=f"{type(exc).__name__}")
    cap_def["committed_usd_in_the_verdict"] = cap.get("committed_usd", UNKNOWN) if _is_num(cap.get("committed_usd")) else UNKNOWN
    cap_def["cost_of_capital_state"] = cap.get("state") or UNKNOWN
    cap_def["cost_of_capital_apr"] = cap.get("cash_alternative_apr") if _is_num(cap.get("cash_alternative_apr")) else UNKNOWN
    cap_def["cost_of_capital_source"] = cap.get("cash_alternative_source") or UNKNOWN
    net = e.get("net_edge_bps")
    net_known = _is_num(net)
    unknown_lines = sorted(k for k, v in costs.items() if not _is_num(v))
    sheet: Dict[str, Any] = {
        "schema": SCHEMA,
        "session_class": e.get("session_class") or UNKNOWN,
        "gross_edge": {"expected_funding_bps": _line(e.get("expected_funding_bps"), "bps", "modelled", "the funding print the model expects to capture, after the gate's haircut; a forecast, not a receipt"),
                       "price_move": {"value": UNKNOWN, "unit": "bps", "provenance": "unknown", "note": "the hedged pair's price leg is not forecast; its residual is a risk, not an edge"}},
        "fees": _group(costs, prov, fee_names),
        "spread": _group(costs, prov, spread_names),
        "slippage_and_impact": _group(costs, prov, slip_names),
        "funding": {"settlements_in_hold": e.get("settlements_in_hold") if isinstance(e.get("settlements_in_hold"), int) else UNKNOWN,
                    "expected_funding_bps": _line(e.get("expected_funding_bps"), "bps", "modelled")},
        "financing_and_borrow": _group(costs, prov, fin_names),
        "other_cost_lines": _group(costs, prov, other_names),
        "capital": cap_def,
        "holding_period": {"hold_s": hold_s if _is_num(hold_s) else UNKNOWN, "settlements_in_hold": e.get("settlements_in_hold") if isinstance(e.get("settlements_in_hold"), int) else UNKNOWN},
        "operational_costs": _line(operating_cost_usd_per_day, "USD per day", "stated" if _is_num(operating_cost_usd_per_day) else None,
                                   "infrastructure and operator costs are not modelled in the edge verdict; the owner's real figure belongs here" if not _is_num(operating_cost_usd_per_day)
                                   else "a figure the CALLER supplied: this sheet cannot verify it"),
        "net": {"net_edge_bps": _line(net, "bps", "derived"), "expected_net_usd": _line(e.get("expected_net_usd"), "USD", "derived"),
                "net_positive": None if (not net_known or unknown_lines or not costs) else (float(str(net)) > 0),
                "unknown_cost_lines": unknown_lines},
        "break_even": {"required_funding_bps_per_settlement": _line(e.get("required_funding_bps"), "bps", "derived", "the funding at which the net equals the hurdle")},
        "worst_case_loss": {"modelled_usd": _line(worst_case_loss_usd, "USD", "modelled" if _is_num(worst_case_loss_usd) else None, "no modelled worst case exists unless one is supplied" if not _is_num(worst_case_loss_usd) else None),
                            "policy_cap_usd": _line(loss_cap_usd, "USD", "policy", "a POLICY cap the session is stopped at, a trigger, not a model: the unwind that follows it can lose more")},
        "uncertainty": {"assumed_inputs": list(e.get("assumed_inputs") or [])[:12], "uncalibrated_modelled": list(prov.get("uncalibrated_modelled") or [])[:12], "fee_tier_sources": prov.get("fee_tier") or UNKNOWN,
                        "scenario_inputs": prov.get("scenario_inputs") or UNKNOWN, "calibration": prov.get("calibration"),
                        "reading": "a net edge that rests on ASSUMED or uncalibrated MODELLED inputs is a model output, not a measured result; Testnet figures are fake money and say nothing about Mainnet expectancy"},
    }
    if isinstance(eligibility, Mapping):
        sheet["eligibility"] = {"verdict": eligibility.get("verdict"), "profile": eligibility.get("profile"), "failed": list(eligibility.get("failed") or []), "unknown": list(eligibility.get("unknown") or []),
                                "waived": list(eligibility.get("waived") or []), "digest": eligibility.get("digest")}
    sheet["digest"] = hashlib.sha256(json.dumps(sheet, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    return sheet


def flatten_unknowns(sheet: Mapping[str, Any], _path: str = "") -> List[str]:
    """Dotted paths of every line whose value is UNKNOWN: what the sheet could not establish, for the report that sits beside it."""
    out: List[str] = []
    for k, v in sheet.items():
        p = f"{_path}.{k}" if _path else str(k)
        if isinstance(v, Mapping):
            if v.get("value") == UNKNOWN and "unit" in v:
                out.append(p)
            else:
                out += flatten_unknowns(v, p)
    return out
