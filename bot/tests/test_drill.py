"""0045 — the drill, as code.

WHAT WAS MISSING
================
Phase D is "venue truth at $100, recorded drill", and there was no drill. There
was a list of things a human would remember to do in the right order, in a
markdown file, with nothing checking that they happened or that they happened
in that order.

That is the gap between a plan and an asset. A drill that lives in prose is a
drill that gets half-run at 2am by somebody who is tired, and whose evidence is
a terminal scrollback that nobody kept.

WHAT THIS IS
============
A staged program. Each stage produces EVIDENCE — a value read from the venue,
not a checkmark — and the drill REFUSES to advance when a stage fails. The
transcript it writes is the Phase D deliverable: what was asked, what the venue
answered, what was sent, what came back, and what the ledger says afterwards.

WHY IT IS TESTED AGAINST A FAKE VENUE
=====================================
Because the sequence has to be right BEFORE it meets a real one. Every failure
mode below — an unreachable venue, an empty wallet, a leg that does not land,
a pair that does not reconcile, an unwind that fails — is exercised here at
zero cost. The first time this runs against Bybit it will already have run
hundreds of times against a venue that can be made to misbehave on command.

SAFE BY DEFAULT
===============
Without `--arm` it reads and reports and sends nothing. With `--arm` it still
goes through the same order gate every other path does: PAPER refuses, mainnet
needs live authorisation AND a signed promotion gate. The drill is not a way
around any of that and a test asserts it.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drill as D  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000


class Venue:
    """A venue that can be made to misbehave on command."""

    def __init__(self, *, inventory=0.01, reachable=True, perp_fills=True,
                 unwind_fills=True, margin=5.0, venue_perp=None):
        self.inventory = inventory
        self.reachable = reachable
        self.perp_fills = perp_fills
        self.unwind_fills = unwind_fills
        self.margin = margin
        self._venue_perp = venue_perp
        self.orders = []
        self.perp_qty = 0.0

    def _live(self):
        if not self.reachable:
            raise RuntimeError("venue unreachable")

    def get_mark(self, symbol):
        self._live()
        return MARK

    def get_spot_mark(self, symbol):
        self._live()
        return MARK * 0.9996

    def get_funding_bps(self, symbol):
        self._live()
        return 3.0

    def get_funding_print(self, symbol):
        self._live()
        import time
        return 3.0, int(time.time() * 1000)

    def get_funding_history(self, symbol, limit=8):
        self._live()
        import time
        now = int(time.time() * 1000)
        return [(3.0, now - k * H8) for k in range(limit - 1, -1, -1)]

    def get_margin_multiple(self, symbol):
        self._live()
        return self.margin

    def get_lot_rules(self, symbol, product):
        self._live()
        return {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        self._live()
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}

    def get_spot_inventory(self, symbol):
        self._live()
        return self.inventory

    def get_perp_position(self, symbol):
        self._live()
        return abs(self.perp_qty if self._venue_perp is None
                   else self._venue_perp)

    def get_open_carry_orders(self, spot_symbol, perp_symbol):
        self._live()
        return []

    def get_book_top(self, symbol, product):
        self._live()
        return {"bid": MARK - 0.05, "ask": MARK + 0.05}

    def place_market(self, *, symbol, side, qty, product):
        self._live()
        closing = side == "Buy" and product == "linear"
        if not self.perp_fills and not closing:
            return None
        if closing and not self.unwind_fills:
            return None
        self.orders.append((side, product, qty))
        self.perp_qty += qty if side == "Sell" else -qty
        return {"filled_qty": qty, "avg_price": MARK,
                "order_link_id": f"d{len(self.orders)}",
                "fee": qty * MARK * 5.5 / 1e4}


def run(venue, **kw):
    kw.setdefault("arm", True)
    kw.setdefault("notional", 100.0)
    return D.run_drill(broker=venue, **kw)


class TestItRefusesBeforeItRisksAnything:
    def test_an_unreachable_venue_stops_at_preflight(self):
        report = run(Venue(reachable=False))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "reachability"
        assert report["orders_sent"] == 0

    def test_an_empty_wallet_stops_before_any_order(self):
        """The overlay hedges BTC the client already owns. With none there is
        nothing to hedge and the drill measures nothing — so it says so
        instead of opening and calling it a success."""
        report = run(Venue(inventory=0.0))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "inventory"
        assert report["orders_sent"] == 0

    def test_inventory_below_one_venue_lot_stops_too(self):
        report = run(Venue(inventory=0.0005))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "inventory"

    def test_thin_margin_stops_before_any_order(self):
        report = run(Venue(margin=1.1))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "margin"
        assert report["orders_sent"] == 0

    def test_a_position_already_at_the_venue_stops_it(self):
        """A drill that opens on top of something already there is not a
        drill, it is an incident."""
        report = run(Venue(venue_perp=0.5))
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "flat"
        assert report["orders_sent"] == 0


class TestWithoutArmItSendsNothing:
    def test_a_dry_run_reaches_the_end_and_places_no_order(self):
        venue = Venue()
        report = run(venue, arm=False)
        assert report["armed"] is False
        assert report["orders_sent"] == 0
        assert venue.orders == []
        assert report["verdict"] == "PREFLIGHT_ONLY"

    def test_a_dry_run_still_reports_what_the_venue_said(self):
        report = run(Venue(), arm=False)
        ev = report["evidence"]
        assert ev["reachability"]["perp_mark"] == MARK
        assert ev["inventory"]["btc"] == pytest.approx(0.01)
        assert ev["venue_rules"]["qty_step"] == 0.001
        assert ev["fee_tier"]["taker_bps"] == 5.5


class TestTheHappyPathIsAWholeRoundTrip:
    def test_it_opens_reconciles_unwinds_and_reconciles(self):
        venue = Venue()
        report = run(venue)
        assert report["verdict"] == "PASSED", report.get("failed_stage")
        assert [s["stage"] for s in report["stages"]][-1] == "statement"
        assert report["orders_sent"] == 2          # overlay: one out, one back
        assert venue.perp_qty == pytest.approx(0.0)

    def test_the_book_is_flat_at_the_end(self):
        report = run(Venue())
        assert report["evidence"]["final_reconcile"]["venue_perp_qty"] == 0.0
        assert report["evidence"]["final_reconcile"]["verdict"] == "FLAT"

    def test_the_ledger_accounts_for_every_fill(self):
        report = run(Venue())
        st = report["evidence"]["statement"]
        assert st["fees_usd"] > 0
        assert st["fees_unknown"] == 0
        assert report["ledger_entries"] >= 2

    def test_the_transcript_names_the_venue_answer_not_a_checkmark(self):
        """Evidence, not ticks. A stage that recorded only `ok: true` proves
        nothing to anybody reading it afterwards."""
        report = run(Venue())
        for stage in report["stages"]:
            assert stage["evidence"], stage["stage"]
            assert stage["evidence"] != {"ok": True}

    def test_every_stage_is_stamped_in_order(self):
        report = run(Venue())
        stamps = [s["at_ms"] for s in report["stages"]]
        assert stamps == sorted(stamps)


class TestWhenTheVenueMisbehavesMidDrill:
    def test_a_perp_leg_that_never_lands_is_a_failure_not_a_pass(self):
        venue = Venue(perp_fills=False)
        report = run(venue)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "open"
        assert venue.perp_qty == pytest.approx(0.0)

    def test_an_unwind_that_does_not_fill_is_loud_and_leaves_the_position(self):
        """The worst outcome, and the one the transcript must be unambiguous
        about: the drill is over, the book is NOT flat, and a human owns it."""
        venue = Venue(unwind_fills=False)
        report = run(venue)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "unwind"
        assert report["still_open"] is True
        assert "HUMAN" in report["next_action"].upper()

    def test_a_failed_drill_still_writes_a_transcript(self):
        report = run(Venue(unwind_fills=False))
        assert report["stages"], "a failure with no transcript is not evidence"
        assert report["failed_stage"] in [s["stage"] for s in report["stages"]]


class TestTheDrillIsNotAWayAroundTheGates:
    def test_it_builds_no_venue_client_of_its_own(self):
        """The drill uses the broker it is HANDED. In production that comes
        from build_bot, so it is a LedgerBroker around a CarryBroker with the
        order gate build_bot chose — PAPER refuses, mainnet needs live
        authorisation and a signed promotion gate. A drill that built its own
        client would be a second order path with its own rules."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(D))
        built = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        for forbidden in ("CarryBroker", "BybitClient", "RequestsTransport"):
            assert forbidden not in built, forbidden

    def test_the_cli_takes_the_brokers_build_bot_made(self):
        """0046 moved the import into `_load_bot` so a missing dependency is
        a sentence rather than a traceback. The invariant is unchanged: the
        CLI's broker is whatever build_bot decided, gate and all."""
        import inspect
        assert "build_bot" in inspect.getsource(D._load_bot)
        assert "bot.carry.broker" in inspect.getsource(D.main)

    def test_it_never_raises_the_cap(self):
        """$100 is the cap and the drill does not get an exemption."""
        import shadow
        report = run(Venue(inventory=10.0), notional=None)
        assert report["notional_usd"] == pytest.approx(
            shadow.SHADOW_MAX_NOTIONAL_USD)

    def test_a_notional_above_the_cap_is_refused(self):
        report = run(Venue(inventory=10.0), notional=5_000.0)
        assert report["verdict"] == "FAILED"
        assert report["failed_stage"] == "cap"
        assert report["orders_sent"] == 0

    def test_it_declares_no_order_function_of_its_own(self):
        """Every order in the drill goes through the engine and the broker.
        A drill with its own POST is a second order path, and a second order
        path is the thing this repository exists to not have."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(D))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("_request", "post", "submit_order"), \
                    node.attr


class TestTheTranscriptIsWrittenDown:
    def test_it_writes_a_file_and_names_it(self, tmp_path):
        out = str(tmp_path / "drill.json")
        report = run(Venue(), out=out)
        assert os.path.exists(out)
        import json
        with open(out, encoding="utf-8") as handle:
            saved = json.load(handle)
        assert saved["verdict"] == report["verdict"]
        assert len(saved["stages"]) == len(report["stages"])

    def test_the_transcript_records_what_was_not_measured(self):
        report = run(Venue())
        joined = " ".join(report["not_proven"]).lower()
        for term in ("liquidation", "rate limit"):
            assert term in joined, term
