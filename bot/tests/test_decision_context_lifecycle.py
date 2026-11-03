"""The Buy-side lifecycle: a decision context reaches every fill, or the record
says exactly why not.

Background. The deployed drill image (built from 14f3d4a) persisted perp_close
records with status UNAVAILABLE / NO_DECISION_CONTEXT and every decision field
null. Proven here against the real code, not by correlation:
  * the drill's unwind runs OUTSIDE `on_candle`, whose wrapper clears the
    tick's decision marks, so `_declare_decision` found no mark, ended the
    context and declared nothing; LedgerBroker then found no context for the
    fill and `execution_record` answered NO_DECISION_CONTEXT;
  * the safety-net close did NOT write those records: at that commit it had no
    recording code at all.
Both are pinned below, together with the invariant that makes a recurrence
visible: every tracked order-send call site declares its decision.
"""
from __future__ import annotations

import ast
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, os.path.join(BOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("LEDGER_DISABLED", "1")

import carry_engine as CE                                     # noqa: E402
import drill                                                  # noqa: E402
import execution_cost_estimate as est                         # noqa: E402
import execution_record as xr                                 # noqa: E402
import execution_samples_report as esr                        # noqa: E402
import ledger as L                                            # noqa: E402
import test_drill as TD                                       # noqa: E402
from execution_fixtures import valid_record                   # noqa: E402


class BookVenue(TD.Venue):
    """Shaped like the real adapter: its book getter can take a timeout."""
    fail_book = False

    def get_book_top(self, symbol, product, *, timeout_s=None):
        if self.fail_book:
            raise RuntimeError("book down")
        return {"bid": TD.MARK - 0.05, "ask": TD.MARK + 0.05}


def drill_records(tmp_path, venue=None):
    path = str(tmp_path / "execution_records.jsonl")
    drill.run_drill(broker=venue or BookVenue(), arm=True, notional=100.0,
                    venue="testnet", execution_records=path)
    rows = [json.loads(x) for x in open(path).read().splitlines()]
    by_kind = {r["kind"]: r["fee_raw"]["execution"] for r in rows
               if r["kind"] in ("perp_open", "perp_close")}
    return path, by_kind


# -- the planned exit (the drill's unwind runs outside a tick) --------------------

def test_the_planned_exit_carries_its_full_decision_context(tmp_path):
    _p, rec = drill_records(tmp_path)
    close = rec["perp_close"]
    assert close["status"] == "MEASURED" and close["reasons"] == []
    assert close["decision_ms"] is not None and close["observed_ms"] is not None
    assert close["observed_ms"] <= close["decision_ms"]       # never post-decision
    assert close["book_source"] in ("tick_snapshot", "orderbook", "ticker_touch")
    assert close["reference_basis"] == "executable_side"
    assert close["decision_bid"] is not None and close["decision_ask"] is not None
    assert close["mid_price"] and close["notional_usd"] == pytest.approx(100.0, rel=0.05)
    assert close["order_link_id"] and close["side"] == "Buy"


def test_the_record_states_route_purpose_mode_and_fee_confirmation(tmp_path):
    _p, rec = drill_records(tmp_path)
    open_, close = rec["perp_open"], rec["perp_close"]
    assert (open_["route"], close["route"]) == ("normal_entry", "normal_exit")
    assert close["purpose"] == "DRILL_COMPLETE"
    assert open_["execution_mode"] == close["execution_mode"] == "overlay"
    assert close["fee_confirmed"] in (True, False)            # stated, not absent


def test_with_no_book_the_exit_says_no_price_not_no_context(tmp_path, monkeypatch):
    """The decision EXISTS (its time is known) even when its price could not be
    established; that is a different, specific statement."""
    monkeypatch.setattr(CE.CarryEngine, "TICK_BOOK_MAX_AGE_S", 0.0)
    v = BookVenue()
    v.fail_book = True
    _p, rec = drill_records(tmp_path, v)
    close = rec["perp_close"]
    assert close["status"] == "UNAVAILABLE"
    assert "NO_DECISION_CONTEXT" not in close["reasons"]
    assert close["reasons"] == ["NO_DECISION_PRICE"]
    assert close["decision_ms"] is not None                    # not fabricated null
    assert close["route"] == "normal_exit"                     # still classified


# -- the safety-net close: object identity, call order, key -----------------------

def test_the_close_declares_and_fills_on_the_same_broker_instance(tmp_path,
                                                                  monkeypatch):
    import close_testnet_short as cts
    seen = {}
    begin, place = L.LedgerBroker.begin_decision, L.LedgerBroker.place_market

    def spy_begin(self, **kw):
        seen["begin_id"] = id(self)
        seen["begin_key"] = (kw["symbol"], kw["product"], kw["side"])
        return begin(self, **kw)

    def spy_place(self, **kw):
        seen["place_id"] = id(self)
        seen["keys_at_fill"] = list(self._decisions)
        return place(self, **kw)
    monkeypatch.setattr(L.LedgerBroker, "begin_decision", spy_begin)
    monkeypatch.setattr(L.LedgerBroker, "place_market", spy_place)

    class V(BookVenue):
        short = 0.001

        def get_perp_position(self, s): return float(self.short)
        def get_open_carry_orders(self, a, b): return []
        def get_position_slots(self, s): return []

        def place_market(self, *, symbol, side, qty, product):
            self.short = 0.0
            return {"filled_qty": qty, "avg_price": TD.MARK,
                    "order_link_id": "x1", "fee": 0.2}
    out = cts.close_short(V(), "BTCUSDT", expected_qty=0.001,
                          sleep=lambda s: None,
                          records_path=str(tmp_path / "r.jsonl"))
    assert out["ok"]
    assert seen["begin_id"] == seen["place_id"]                # one instance
    assert seen["begin_key"] in seen["keys_at_fill"]           # still declared
    assert seen["begin_key"] == ("BTCUSDT", "linear", "Buy")


def test_the_close_flows_to_the_report_as_a_protective_close(tmp_path):
    import close_testnet_short as cts

    class V(BookVenue):
        short = 0.001

        def get_perp_position(self, s): return float(self.short)
        def get_open_carry_orders(self, a, b): return []
        def get_position_slots(self, s): return []

        def place_market(self, *, symbol, side, qty, product):
            self.short = 0.0
            return {"filled_qty": qty, "avg_price": TD.MARK,
                    "order_link_id": "x1", "fee": 0.2}
    path = str(tmp_path / "r.jsonl")
    assert cts.close_short(V(), "BTCUSDT", expected_qty=0.001,
                           sleep=lambda s: None, records_path=path)["ok"]
    ex = json.loads(open(path).read().splitlines()[0])["fee_raw"]["execution"]
    assert ex["status"] == "MEASURED" and ex["decision_ms"] is not None
    assert ex["route"] == "protective_close"
    assert ex["purpose"] == "safety_net_close"
    import time as _t
    out = esr.build("", mode="overlay", notional_usd=100.0, perp="BTCUSDT",
                    spot="BTCUSDT", now_ms=int(_t.time() * 1000) + 60_000,
                    records=[path], environment="testnet")
    buy = next(l for l in out["legs"] if l["leg"].startswith("linear/Buy"))
    # an emergency close is visible on its own and is NOT in the normal-exit bound
    assert buy["n"] == 0
    assert buy["by_route"]["protective_close"]["n"] == 1
    assert buy["by_route"]["protective_close"]["counted_in_bound"] is False
    # ...and it is a filled order on this leg, so the miss-rate denominator
    # counts it although the bound does not.
    assert esr.leg_filled_orders(buy, {"NO_EXECUTION_RECORD", "TOO_OLD",
                                        "OUTSIDE_SIZE_BAND"}) == 1


# -- the invariant that makes a recurrence visible --------------------------------

def _call_sites():
    """(relative file, enclosing function) of every place tracked non-test code
    sends an order to a broker."""
    sites = []
    for base, _dirs, files in os.walk(BOT):
        parts = os.path.relpath(base, BOT).split(os.sep)
        if "tests" in parts or any(x in (".venv", "venv", "__pycache__")
                                   for x in parts):
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            names = ("place_market", "place_post_only")
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    # a call, or ANY other reference that could alias the sender
                    # (`f = broker.place_market`, `getattr(b, "place_market")`)
                    if isinstance(node, ast.Attribute) and node.attr in names:
                        sites.append((os.path.relpath(path, BOT), fn.name,
                                      node.attr))
                    elif (isinstance(node, ast.Constant)
                          and node.value in names):
                        sites.append((os.path.relpath(path, BOT), fn.name,
                                      "ref:" + node.value))
    return sorted(set(sites))


def test_every_order_send_site_is_a_known_one():
    assert _call_sites() == [
        ("carry_engine.py", "_rest", "place_post_only"),
        ("carry_engine.py", "_take", "place_market"),
        ("ledger.py", "place_market", "place_market"),
        ("ledger.py", "place_post_only", "place_post_only"),
        ("tools/close_testnet_short.py", "close_short", "place_market"),
    ]


def test_the_engines_senders_are_reached_only_through_the_declaring_choke_point():
    tree = ast.parse(open(os.path.join(BOT, "carry_engine.py")).read())
    callers = {"_take": set(), "_rest": set(), "_rest_then_take": set()}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"
                        and node.func.attr in callers):
                    callers[node.func.attr].add(fn.name)
    # _take: _fire (declares), and the maker path that follows a declaration
    assert callers["_take"] <= {"_fire", "_rest_then_take"}
    assert callers["_rest"] <= {"_rest_then_take"}
    assert callers["_rest_then_take"] <= {"_fire"}
    # _fire itself must declare the decision BEFORE it sends anything
    fire = next(f for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == "_fire")
    calls = sorted((n.lineno, n.func.attr) for n in ast.walk(fire)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and isinstance(n.func.value, ast.Name)
                   and n.func.value.id == "self")
    order = [a for _l, a in calls]
    assert "_declare_decision" in order
    first_send = min(order.index(a) for a in ("_take", "_rest_then_take")
                     if a in order)
    assert order.index("_declare_decision") < first_send


# -- rejection reasons are preserved, not collapsed -------------------------------

OWNER_PATTERN = {"schema": 1, "status": "UNAVAILABLE",
                 "reasons": ["NO_DECISION_CONTEXT"], "side": "Buy",
                 "symbol": "BTCUSDT", "product": "linear",
                 "order_type": "market", "decision_ms": None,
                 "observed_ms": None, "book_source": None, "book_age_ms": None,
                 "reference_basis": "", "notional_usd": None,
                 "filled_qty": 0.001, "avg_price": 77000.0}


class E:
    def __init__(self, ms, rec, i):
        self.kind, self.ms, self.ref = "perp_close", ms, f"c{i}"
        self.fee_raw = {"execution": rec}


def test_the_owner_pattern_is_rejected_with_its_reason_kept():
    now = 1_700_000_100_000
    es = [E(now - 1000 * i, dict(OWNER_PATTERN), i) for i in range(2)]
    detail = {}
    obs, rej = est.extract(es, as_of_ms=now, symbol="BTCUSDT",
                           product="linear", side="Buy", detail=detail)
    assert obs == [] and rej == {"NOT_BOOK_MEASURED": 2}      # unchanged shape
    assert detail == {"NOT_BOOK_MEASURED": {"NO_DECISION_CONTEXT": 2}}


def test_a_routed_exit_without_context_keeps_its_reason_in_the_bound_population():
    now = 1_700_000_100_000
    es = [E(now - 1000 * i, dict(OWNER_PATTERN, route="normal_exit"), i)
          for i in range(2)]
    leg = est.estimate_leg(es, as_of_ms=now, symbol="BTCUSDT", product="linear",
                           side="Buy", planned_notional_usd=100.0)
    assert leg["rejected"] == {"NOT_BOOK_MEASURED": 2}
    assert leg["rejected_detail"] == {
        "NOT_BOOK_MEASURED": {"NO_DECISION_CONTEXT": 2}}
    assert leg["status"] == est.UNAVAILABLE


def test_legacy_owner_records_with_no_route_are_unclassified_but_not_hidden():
    """The records already on the volume state no route. A buy of the perp could
    be a planned or an emergency exit, so it is UNCLASSIFIED: kept out of the
    planned-exit bound, and its rejection stays visible beside it."""
    now = 1_700_000_100_000
    es = [E(now - 1000 * i, dict(OWNER_PATTERN), i) for i in range(2)]
    leg = est.estimate_leg(es, as_of_ms=now, symbol="BTCUSDT", product="linear",
                           side="Buy", planned_notional_usd=100.0)
    assert leg["n"] == 0 and leg["status"] == est.UNAVAILABLE
    un = leg["by_route"]["unclassified_exit"]
    assert un["n"] == 0 and un["counted_in_bound"] is False
    assert un["rejected"] == {"NOT_BOOK_MEASURED": 2}


def test_a_context_less_exit_keeps_its_reason_in_its_own_route():
    """A record that states no route and no context lands in unclassified_exit,
    so its reason must be readable there, not only as a bare count."""
    now = 1_700_000_100_000
    es = [E(now - 1000 * i, dict(OWNER_PATTERN), i) for i in range(2)]
    leg = est.estimate_leg(es, as_of_ms=now, symbol="BTCUSDT", product="linear",
                           side="Buy", planned_notional_usd=100.0)
    un = leg["by_route"]["unclassified_exit"]
    assert un["rejected_detail"] == {
        "NOT_BOOK_MEASURED": {"NO_DECISION_CONTEXT": 2}}


def test_legacy_sell_records_still_count_as_entries():
    """The two measured Sell samples already on the volume are entries: an
    entry side is inferable, so they keep counting."""
    now = 1_700_000_100_000
    rec = valid_record("Sell", "linear", ms=now - 5000, link="s0")
    rec.pop("route", None)
    class S:
        kind, ms, ref = "perp_open", now - 5000, "s0"
        fee_raw = {"execution": rec}
    obs, _ = est.extract([S()], as_of_ms=now, symbol="BTCUSDT",
                         product="linear", side="Sell",
                         routes=est.required_routes("linear", "Sell"))
    assert len(obs) == 1


@pytest.mark.parametrize("reason,route", [
    ("FUNDING_INVERTED", "normal_exit"), ("DRILL_COMPLETE", "normal_exit"),
    ("MARGIN_HEADROOM_1.50x_BELOW_FLOOR", "protective_close"),
    ("ACCOUNT_MM_RATE_0.90_ABOVE_FLOOR", "protective_close"),
    ("LIQUIDATION_9.0PCT_AWAY_BELOW_FLOOR", "protective_close"),
    ("LEGS_LANDED_UNPAIRED", "protective_close"),
    ("DRILL_ABORT", "protective_close"),
    ("SOMETHING_NEW", "unclassified_exit"), ("", "unclassified_exit"),
    (None, "unclassified_exit")])
def test_exit_reasons_map_to_routes_and_an_unknown_one_is_never_normal(reason,
                                                                       route):
    assert xr.route_for_exit(reason) == route


def test_a_protective_close_is_visible_beside_the_bound_not_inside_it():
    import time as _t
    now = int(_t.time() * 1000)
    es = []
    for i in range(25):                      # 25 easy planned exits
        ms = now - 60_000 - 2000 * i
        es.append(E(ms, valid_record("Buy", "linear", ms=ms, link=f"n{i}"), i))
    # one very expensive emergency close
    ms = now - 30_000
    bad = valid_record("Buy", "linear", ms=ms, link="x", route="protective_close",
                       slip_usd=5.0)
    es.append(E(ms, bad, 99))
    leg = est.estimate_leg(es, as_of_ms=now, symbol="BTCUSDT", product="linear",
                           side="Buy", planned_notional_usd=600.0)
    assert leg["n"] == 25                                    # bound: planned only
    pc = leg["by_route"]["protective_close"]
    assert pc["n"] == 1 and pc["counted_in_bound"] is False
    assert pc["slippage_bps_max"] > leg["slippage_bps"]["bound"]   # not hidden


# -- routes through the REAL engine ------------------------------------------------

def _engine_with_ledger():
    from test_carry_cold_start import MARK, Venue, engine
    v = Venue(inventory=5.0)
    lb = L.LedgerBroker(v, L.Journal(), ms=lambda: 1_700_000_000_000)
    return v, lb, engine(lb, mode="overlay"), MARK


def _routes(lb):
    return [(r.get("side"), r.get("route"), r.get("purpose"))
            for r in lb.executions if r.get("product") == "linear"]


def test_a_margin_breach_exit_is_a_protective_close():
    from test_carry_cold_start import H8
    v, lb, e, MARK = _engine_with_ledger()
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    v.get_margin_multiple = lambda s: 1.1
    e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                funding_print_ms=4 * H8, timestamp_ms=4 * H8)
    sells = [r for r in _routes(lb) if r[0] == "Sell"]
    buys = [r for r in _routes(lb) if r[0] == "Buy"]
    assert sells == [("Sell", "normal_entry", "OPEN")]
    assert len(buys) == 1 and buys[0][1] == "protective_close"
    assert buys[0][2].startswith("MARGIN_HEADROOM")


def test_a_funding_inversion_exit_is_a_normal_exit():
    from test_carry_cold_start import H8
    v, lb, e, MARK = _engine_with_ledger()
    e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    for k in (4, 5, 6):
        e.on_candle(mark=MARK, funding_bps=-0.5, spot=MARK,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    buys = [r for r in _routes(lb) if r[0] == "Buy"]
    assert len(buys) == 1 and buys[0][:2] == ("Buy", "normal_exit")
    assert buys[0][2] == "FUNDING_INVERTED"


def test_the_exit_reason_does_not_leak_into_a_later_order():
    from test_carry_cold_start import H8
    v, lb, e, MARK = _engine_with_ledger()
    e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    for k in (4, 5, 6):
        e.on_candle(mark=MARK, funding_bps=-0.5, spot=MARK,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    assert e._exit_reason is None
