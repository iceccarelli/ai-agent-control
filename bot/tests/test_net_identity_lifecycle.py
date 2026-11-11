"""Full-lifecycle identity matrix for the THREE ledgers that must state one net (ms_accounting.Ledger = canonical, ms_replay.LocalLedger, ms_pipeline.LedgerAdapter).

REQUIRED values are hand-computed (see each case). The contract for every consumer: the net is meaningful ONLY when the ledger itself says the result is complete (``net_complete``); an unknown or
unverified component, an integrity conflict, a rebate nobody measured or a position with no mark makes the result INCOMPLETE and its partial figure is a SUBTOTAL, never a net. A ledger that
still DISAGREES with the REQUIRED value is marked ``xfail(strict=True)``: the mark goes red (XPASS strict) the moment the divergence is fixed, so the table can only get shorter.

Common round trip (BTCUSDT; spot leg 's', perp leg 'p'; qty 1): spot buy 50,000 / sell 50,100 (+100); perp sell 50,000 / buy 50,090 (-90) => price +10.
Fees 50.0 + 27.5 + 50.1 + 27.5 = 155.1.  Funding 3 settlements x +10 = +30.   NET = 10 + 30 - 155.1 = -115.1.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import ms_accounting as MA  # noqa: E402
import ms_pipeline as PL  # noqa: E402
import ms_replay as RP  # noqa: E402
import ms_types as T  # noqa: E402

H = 3_600_000


def F(i, leg, side, qty, px, fee, ts, prov="MEASURED"):
    return dict(i=i, leg=leg, side=side, qty=qty, px=px, fee=fee, ts=ts, prov=prov, cat="spot" if leg == "s" else "linear")


BASE = [F("sb", "s", "Buy", 1, 50000, 50.0, 1), F("ps", "p", "Sell", 1, 50000, 27.5, 2), F("ss", "s", "Sell", 1, 50100, 50.1, 25 * H), F("pb", "p", "Buy", 1, 50090, 27.5, 25 * H + 1)]
F3 = [(8 * H, 10.0), (16 * H, 10.0), (24 * H, 10.0)]


def _book(L, kind, fills, funding, marks):
    for f in fills:
        tf = T.Fill(f["i"], "g", f["leg"], "BTCUSDT", f["side"], f["qty"], f["px"], f["fee"], "USDT", "TAKER", f["ts"], f["prov"])
        L.record_fill(tf, category=f["cat"], **({"position_leg": f["leg"]} if kind == "Ledger" else {}))
    for ts, amt in funding:
        L.record_funding("BTCUSDT", ts, amt, "MEASURED")
    for cat, ts, px, bid, ask in marks:
        L.mark("BTCUSDT", ts, px, category=cat, bid=bid, ask=ask) if kind == "Ledger" else L.mark("BTCUSDT" if cat == "linear" else "SPOT:BTCUSDT", ts, px, bid=bid, ask=ask)


def clean_net(kind, fills, funding=F3, marks=(), after=None):
    """The number a consumer may treat as THE net: None unless the ledger ITSELF says the result is complete. (REQUIRED, by the completeness rule: an integrity conflict is not complete.)"""
    L = {"Ledger": MA.Ledger, "LocalLedger": RP.LocalLedger, "LedgerAdapter": PL.LedgerAdapter}[kind]()
    _book(L, kind, fills, funding, marks)
    if after:
        after(L, kind)
    if kind == "Ledger":
        c = L.canonical_net()
        return c["net"] if c["net_complete"] else None                  # the ledger's OWN completeness statement: no consumer-side patching
    p = L.pnl()
    return p["net"] if p["net_complete"] else None                       # the ledger's OWN completeness statement, the same rule for every ledger


def _dup_funding_and_fills(L, kind):
    for f in BASE[:2]:
        tf = T.Fill(f["i"], "g", f["leg"], "BTCUSDT", f["side"], f["qty"], f["px"], f["fee"], "USDT", "TAKER", f["ts"], f["prov"])
        L.record_fill(tf, category=f["cat"], **({"position_leg": f["leg"]} if kind == "Ledger" else {}))
    L.record_funding("BTCUSDT", 8 * H, 10.0, "MEASURED")


def _conflicting_dup(L, kind):
    f = dict(BASE[1], px=50001)
    tf = T.Fill(f["i"], "g", f["leg"], "BTCUSDT", f["side"], f["qty"], f["px"], f["fee"], "USDT", "TAKER", f["ts"], f["prov"])
    L.record_fill(tf, category=f["cat"], **({"position_leg": f["leg"]} if kind == "Ledger" else {}))


def sub(base, **over):
    return [dict(f, **over[f["i"]]) if f["i"] in over else f for f in base]


# id, title, fills, funding, marks, after, REQUIRED (Decimal-able float or None = "no clean number")
CASES = {
    "C01_control": (BASE, F3, (), None, -115.1),
    "C04_measured_zero_fee_is_a_real_zero": (sub(BASE, ps=dict(fee=0.0)), F3, (), None, -87.6),                        # 10 + 30 - (50 + 0 + 50.1 + 27.5)
    "C05_partial_entry_fills_average_cost": ([F("sb", "s", "Buy", 1, 50000, 50.0, 1), F("ps1", "p", "Sell", 0.4, 50000, 11.0, 2), F("ps2", "p", "Sell", 0.6, 50010, 16.5, 3),
                                              F("ss", "s", "Sell", 1, 50100, 50.1, 25 * H), F("pb", "p", "Buy", 1, 50090, 27.5, 25 * H + 1)], F3, (), None, -109.1),   # perp avg 50006: price 100 - 84; fees 155.1
    "C06_partial_exit_fills": ([F("sb", "s", "Buy", 1, 50000, 50.0, 1), F("ps", "p", "Sell", 1, 50000, 27.5, 2), F("ss", "s", "Sell", 1, 50100, 50.1, 25 * H),
                                F("pb1", "p", "Buy", 0.5, 50080, 13.75, 25 * H + 1), F("pb2", "p", "Buy", 0.5, 50100, 13.75, 25 * H + 2)], F3, (), None, -115.1),            # avg exit 50090
    "C07_open_pair_with_books_exit_side": (BASE[:2], F3, (("spot", 26 * H, 50100, 50095, 50105), ("linear", 26 * H, 50090, 50085, 50095)), None, -47.5),                   # long at bid 50095 (+95), short at ask 50095 (-95); 30 - 77.5
    "C12_funding_is_a_cost": (BASE, [(t, -10.0) for t, _ in F3], (), None, -175.1),                                                                                       # 10 - 30 - 155.1
    "C13_duplicate_fills_and_funding_booked_twice": (BASE, F3, (), _dup_funding_and_fills, -115.1),                                                                      # idempotent by id / (symbol, ts)
    "C15_out_of_order_delivery": (list(reversed(BASE)), F3, (), None, -115.1),                                                                                            # a pure fold over (ts, id)
    # ---- the cases where an UNKNOWN component must make the result UNKNOWN (REQUIRED = None)
    "C02_rebate_never_assumed": (sub(BASE, ps=dict(fee=-5.0)), F3, (), None, None),
    "C03_zero_fee_on_an_unmeasured_fill": (sub(BASE, ps=dict(fee=0.0, prov="SIMULATED")), F3, (), None, None),
    "C08_open_pair_without_any_mark": (BASE[:2], F3, (), None, None),
    "C10_held_across_settlements_funding_absent": (BASE, [], (), None, None),
    "C11_one_funding_amount_unknown": (BASE, [(8 * H, 10.0), (16 * H, None), (24 * H, 10.0)], (), None, None),
    "C14_duplicate_fill_id_with_different_content": (BASE, F3, (), _conflicting_dup, None),                                                                               # integrity conflict: not clean
}
# (case, ledger) -> why it diverges TODAY. strict xfail: fixing the divergence turns the mark into a failure, so it must be deleted.
DIVERGES = {
    # funding COVERAGE is the caller's statement in both replay ledgers: the replay books EVERY settlement print it crosses, so "no print booked" is a stated fact there, not an unknown. The canonical ledger
    # knows the held window and says None. A replay over a feed with gaps would be flattered here; closing it needs the replay to declare which windows its feed covers (not done).
    ("C10_held_across_settlements_funding_absent", "LocalLedger"): "no funding record = funding 0, complete (-145.10): coverage is the caller's statement",
    ("C10_held_across_settlements_funding_absent", "LedgerAdapter"): "declare_no_funding at the FIRST FILL (by design, DERIVED) turns 'never booked' into a stated zero: -145.10 and net_complete=True",
}


def _params():
    out = []
    for cid, spec in CASES.items():
        for kind in ("Ledger", "LocalLedger", "LedgerAdapter"):
            why = DIVERGES.get((cid, kind))
            marks = [pytest.mark.xfail(strict=True, reason=why)] if why else []
            out.append(pytest.param(cid, kind, id=f"{cid}-{kind}", marks=marks))
    return out


@pytest.mark.parametrize("cid,kind", _params())
def test_every_ledger_states_the_same_net_or_says_it_does_not_know(cid, kind):
    fills, funding, marks, after, required = CASES[cid]
    got = clean_net(kind, fills, funding, marks, after)
    if required is None:
        assert got is None, f"required: no clean number; {kind} says {got!r}"
    else:
        assert isinstance(got, float) and got == pytest.approx(required, abs=1e-9), f"required {required}; {kind} says {got!r}"


def test_every_divergence_mark_names_a_real_case():
    assert all(c in CASES for c, _k in DIVERGES)
