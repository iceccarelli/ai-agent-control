"""The probe observes the real payload completely, or says it could not, and
discloses no private account amounts unless the operator explicitly asks."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from carry_broker import (CarryBroker, PairIncident, read_log_pages,  # noqa: E402
                          txlog_windows)
from test_carry_broker import envelope  # noqa: E402
from tools import fee_payload_probe as fp  # noqa: E402

DAY = 86_400_000
NOW = 1_791_000_000_000

# Values that must NEVER appear in a default report. Distinctive on purpose.
SECRETS = {
    "funding": "13.370042", "size": "-0.0071234", "balance": "99999.123456",
    "cash_flow": "424242.4242", "fee": "0.0000777", "change": "31337.31337",
    "id": "ABCDEF0123456789XYZ", "qty": "0.00918273", "price": "81234.56789",
    "order_id": "ORD-9988776655-FULL",
}


def srow(i="1", **kw):
    base = {"id": f"{SECRETS['id']}{i}", "symbol": "BTCUSDT",
            "type": "SETTLEMENT", "category": "linear",
            "funding": SECRETS["funding"], "size": SECRETS["size"],
            "currency": "USDT", "transactionTime": str(NOW - 2 * DAY),
            "cashBalance": SECRETS["balance"], "cashFlow": SECRETS["cash_flow"],
            "fee": SECRETS["fee"], "change": SECRETS["change"],
            "feeRate": "0.0001"}
    base.update(kw)
    return base


def other_row(i="9", **kw):
    base = {"id": f"T{i}", "symbol": "BTCUSDT", "type": "TRADE",
            "category": "spot", "funding": "", "size": "0.1",
            "currency": "BTC", "transactionTime": str(NOW - 3 * DAY),
            "cashBalance": SECRETS["balance"], "fee": SECRETS["fee"]}
    base.update(kw)
    return base


class Log:
    """A scripted, cursor-paginated transaction log.

    `pages[kind]` is a list of pages; a page is the list of rows, an exception,
    or a raw payload dict. Page n is served for cursor `c<n-1>` (none for the
    first). Every request is recorded.
    """

    def __init__(self, settlement=None, any_type=None):
        self.pages = {"settlement": settlement or [[]],
                      "any_type": any_type or [[]]}
        self.requests = []
        self.calls = []

    def _request(self, method, endpoint, **kw):
        # Recorded, not asserted: an assertion raised here would be swallowed by
        # the paginator's error handling and read as an ordinary failed page.
        self.calls.append((method, endpoint))
        p = dict(kw["params"])
        self.requests.append(p)
        kind = "settlement" if p.get("type") == "SETTLEMENT" else "any_type"
        pages = self.pages[kind]
        n = 0 if not p.get("cursor") else int(p["cursor"][1:]) + 1
        page = pages[min(n, len(pages) - 1)] if callable(pages) is False else None
        if isinstance(page, Exception):
            raise page
        if isinstance(page, dict):
            return page
        more = n + 1 < len(pages)
        return envelope({"list": page, "nextPageCursor": f"c{n}" if more else ""})


def run(log, **kw):
    return fp.probe_transaction_log(log, "BTCUSDT", now_ms=NOW, **kw)


# --- P0: complete cursor pagination ---------------------------------------


class TestEveryCursorIsFollowed:
    def test_all_pages_of_both_queries_are_read_and_counted(self):
        log = Log(settlement=[[srow("1"), srow("2")], [srow("3")], [srow("4")]],
                  any_type=[[other_row("1")], [other_row("2")]])
        out = run(log)
        s, a = out["reads"]["settlement"], out["reads"]["any_type"]
        assert (s["pages"], s["rows"], s["complete"]) == (3, 4, True)
        assert (a["pages"], a["rows"], a["complete"]) == (2, 2, True)
        assert out["complete"] is True and out["verdict"] == "SETTLEMENT_ROWS_OBSERVED"

    def test_the_response_cursor_is_sent_back_as_the_next_requests_cursor(self):
        log = Log(settlement=[[srow("1")], [srow("2")], [srow("3")]])
        run(log)
        settle = [r for r in log.requests if r.get("type") == "SETTLEMENT"]
        assert [r.get("cursor") for r in settle] == [None, "c0", "c1"]

    def test_field_counts_cover_every_page_not_just_the_first(self):
        log = Log(settlement=[[srow("1")], [srow("2", size="")], [srow("3")]])
        out = run(log)
        size = out["reads"]["settlement"]["size"]
        assert size["negative"] == 2 and size["present"] == 2
        obs = out["size_field_observation"]
        assert obs["status"] == "SOME_ROWS_LACK_PARSEABLE_NONZERO_SIZE"
        assert obs["size_absent_blank_or_unparseable"] == 1


class TestIncompleteIsNeverSuccess:
    @pytest.mark.parametrize("pages,why", [
        ([[srow("1")], RuntimeError("log down")], "request failed"),
        ([{"retCode": 0, "result": None}], "no result object"),
        ([{"retCode": 0, "result": {"nextPageCursor": ""}}], "carries no list"),
        ([{"retCode": 0, "result": {"list": "nope"}}], "carries no list"),
        ([{"retCode": 0, "result": {"list": [srow("1"), "x"]}}], "not an object"),
        ([{"retCode": 0, "result": {"list": [], "nextPageCursor": 7}}],
         "non-string cursor"),
    ])
    def test_a_bad_page_makes_the_whole_observation_incomplete(self, pages, why):
        out = run(Log(settlement=pages))
        assert out["verdict"] == "LOG_OBSERVATION_INCOMPLETE"
        assert out["complete"] is False
        assert out["size_field_observation"] == {"status": "OBSERVATION_INCOMPLETE"}
        assert out["signed_size_semantics"] == "UNVERIFIED"
        assert why in json.dumps(out["errors"])
        assert out["reads"]["settlement"]["complete"] is False

    def test_a_repeated_cursor_is_refused_not_looped(self):
        class Loop(Log):
            def _request(self, method, endpoint, **kw):
                self.requests.append(dict(kw["params"]))
                return envelope({"list": [srow(str(len(self.requests)))],
                                 "nextPageCursor": "same"})
        log = Loop()
        out = run(log)
        assert out["verdict"] == "LOG_OBSERVATION_INCOMPLETE"
        assert "cursor repeated" in json.dumps(out["errors"])
        assert len(log.requests) < 12                       # it stopped

    def test_exhausting_the_page_bound_is_incomplete_not_a_short_success(self):
        class Endless(Log):
            def _request(self, method, endpoint, **kw):
                self.requests.append(dict(kw["params"]))
                n = len(self.requests)
                if n > 60:                    # fail fast, never hang CI
                    raise AssertionError("pagination ran past its own bound")
                return envelope({"list": [srow(str(n))],
                                 "nextPageCursor": f"cursor-{n}"})
        out = run(Endless(), max_pages=5)
        assert out["verdict"] == "LOG_OBSERVATION_INCOMPLETE"
        assert "did not finish within 5 pages" in json.dumps(out["errors"])
        assert out["reads"]["settlement"]["pages"] == 5

    def test_a_failure_on_a_later_page_does_not_pass_off_the_first_page(self):
        out = run(Log(settlement=[[srow("1"), srow("2")], RuntimeError("boom")]))
        assert out["verdict"] == "LOG_OBSERVATION_INCOMPLETE"
        assert out["size_field_observation"]["status"] == "OBSERVATION_INCOMPLETE"
        assert "nothing here is a claim about the whole log" in out["note"]

    def test_an_empty_log_that_ENDED_is_different_from_one_that_failed(self):
        ok = run(Log())
        assert ok["verdict"] == "NO_SETTLEMENT_ROWS_TO_OBSERVE" and ok["complete"]
        bad = run(Log(settlement=[RuntimeError("down")]))
        assert bad["verdict"] == "LOG_OBSERVATION_INCOMPLETE"


class TestOverlapAndIdentity:
    def test_a_row_returned_twice_by_overlapping_windows_is_one_row(self):
        row = srow("1")
        log = Log(settlement=[[row, srow("2"), dict(row)]])
        s = run(log)["reads"]["settlement"]
        assert s["rows"] == 2 and s["duplicates_collapsed"] == 1

    def test_distinct_rows_that_share_an_id_are_both_kept_and_reported(self):
        a = srow("1")
        b = dict(a, funding="-4.5", transactionTime=str(NOW - 9 * 3_600_000))
        s = run(Log(settlement=[[a, b]]))["reads"]["settlement"]
        assert s["rows"] == 2 and s["duplicates_collapsed"] == 0
        assert s["id"]["ids_shared_by_distinct_rows"] == 1

    def test_multi_window_reads_overlap_by_a_millisecond_and_stay_under_7_days(self):
        log = Log()
        out = run(log, days=20)
        assert out["windows"] == len(txlog_windows(NOW - 20 * DAY + 60_000, NOW)) == 3
        settle = [r for r in log.requests if r.get("type") == "SETTLEMENT"]
        assert all(r["endTime"] - r["startTime"] < 7 * DAY for r in settle)
        for prev, nxt in zip(settle, settle[1:]):
            assert nxt["startTime"] == prev["endTime"]          # no gap


class TestThePaginatorItself:
    def test_it_returns_rows_in_order_and_marks_completion(self):
        pages = iter([envelope({"list": [{"a": 1}], "nextPageCursor": "x"}),
                      envelope({"list": [{"a": 2}], "nextPageCursor": ""})])
        res = read_log_pages(lambda p: next(pages), {})
        assert res.complete and [r["a"] for r in res.rows] == [1, 2]

    def test_production_funding_reader_refuses_partial_logs(self):
        class V:
            calls = 0

            def _request(self, method, endpoint, **kw):
                V.calls += 1
                if V.calls == 1:
                    return envelope({"list": [], "nextPageCursor": "c0"})
                raise RuntimeError("down")
        broker = CarryBroker(client=V(), sequence_source=lambda *a: 0,
                             order_gate=lambda: (False, "ro"))
        with pytest.raises(PairIncident):
            broker.get_funding_payments("BTCUSDT", 0, 5_000)


# --- P1: privacy ----------------------------------------------------------


def all_text(obj):
    return json.dumps(obj, sort_keys=True, default=str)


class TestNoPrivateAmountsByDefault:
    def log(self):
        return Log(settlement=[[srow("1"), srow("2")]],
                   any_type=[[other_row("1"), srow("3")]])

    def test_the_default_log_report_discloses_no_forbidden_value(self):
        text = all_text(run(self.log()))
        for name, value in SECRETS.items():
            assert value not in text, f"default report leaked {name}"
        # fragments of the secret values must not leak either
        for fragment in ("13.370", "0.0071", "99999", "424242", "31337"):
            assert fragment not in text, fragment
        assert "raw_samples" not in text

    def test_presence_and_parseability_are_still_provable(self):
        out = run(self.log())
        s = out["reads"]["settlement"]
        assert s["rows"] == 2
        assert s["funding"] == {"present": 2, "parseable": 2, "positive": 2,
                                "negative": 0, "zero": 0}
        assert s["size"]["negative"] == 2 and s["size"]["positive"] == 0
        assert s["key_presence"]["cashBalance"] == 2     # presence only
        assert s["id"]["distinct"] == 2 and s["id"]["length_min"] > 10
        assert s["currencies"] == ["USDT"] and s["types_seen"] == ["SETTLEMENT"]
        assert s["transactionTime"]["parseable"] == 2
        assert out["signed_size_semantics"] == "UNVERIFIED"

    def test_unparseable_values_are_visible_as_present_but_not_parseable(self):
        log = Log(settlement=[[srow("1", funding="n/a", size="x")]])
        s = run(log)["reads"]["settlement"]
        assert s["funding"]["present"] == 1 and s["funding"]["parseable"] == 0
        assert s["size"]["present"] == 1 and s["size"]["parseable"] == 0
        obs = run(log)["size_field_observation"]
        assert obs["status"] == "SOME_ROWS_LACK_PARSEABLE_NONZERO_SIZE"

    def test_raw_samples_are_opt_in_truncate_ids_and_never_show_balances(self):
        text = all_text(run(self.log(), raw_samples=True))
        assert SECRETS["funding"] in text and SECRETS["size"] in text   # asked for
        assert SECRETS["id"] not in text                        # never a full id
        assert "ABCDEF..." in text
        for never in ("balance", "cash_flow", "fee", "change"):
            assert SECRETS[never] not in text

    def test_the_order_probe_default_has_no_amounts_prices_or_ids(self):
        class Orders:
            def _request(self, method, endpoint, **kw):
                assert (method, endpoint) == ("GET", "/v5/order/history")
                return envelope({"list": [{
                    "orderId": SECRETS["order_id"], "side": "Buy",
                    "orderType": "Market", "orderStatus": "Filled",
                    "cumExecQty": SECRETS["qty"], "avgPrice": SECRETS["price"],
                    "cumFeeDetail": {"BTC": SECRETS["fee"]}}]})
        out = fp.probe(Orders(), "BTCUSDT")
        text = all_text(out)
        for name in ("qty", "price", "fee", "order_id"):
            assert SECRETS[name] not in text, name
        o = out["orders"][0]
        assert o["has_cumFeeDetail"] and o["parsed_known"]
        assert o["fee_currencies"] == ["BTC"] and o["parsed_source"] == "cumFeeDetail"
        raw = all_text(fp.probe(Orders(), "BTCUSDT", raw_samples=True))
        assert SECRETS["fee"] in raw and SECRETS["order_id"] not in raw

    def test_an_unparsed_real_fee_shape_is_surfaced_not_hidden(self):
        class Orders:
            def _request(self, method, endpoint, **kw):
                rows = [{"orderId": "o", "side": "Buy", "cumExecQty": "1",
                         "cumFeeDetail": "surprise"}]
                return envelope({"list": rows if kw["params"]["category"]
                                 == "spot" else []})
        out = fp.probe(Orders(), "BTCUSDT")
        assert out["parsed_unknown"] == 1 and out["orders"][0]["parsed_known"] is False
        assert out["orders"][0]["fee_fields_present"] == ["cumFeeDetail"]


# --- guards ---------------------------------------------------------------


class TestReadOnlyAndGuarded:
    def test_it_only_ever_issues_gets_to_the_transaction_log(self):
        log = Log(settlement=[[srow("1")], [srow("2")]], any_type=[[other_row()]])
        run(log, days=20)
        assert log.calls, "the probe made no request"
        assert set(log.calls) == {("GET", "/v5/account/transaction-log")}

    def test_the_order_probe_only_ever_issues_gets_to_order_history(self):
        calls = []

        class Orders:
            def _request(self, method, endpoint, **kw):
                calls.append((method, endpoint))
                return envelope({"list": []})
        fp.probe(Orders(), "BTCUSDT")
        assert set(calls) == {("GET", "/v5/order/history")}

    def test_the_real_command_line_refuses_a_contradictory_environment(self):
        env = {"PATH": os.environ["PATH"], "HOME": "/tmp",
               "BYBIT_VENUE": "testnet", "USE_TESTNET": "0",
               "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(HERE), "tools",
                                          "fee_payload_probe.py"), "--funding"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 3 and "PREFLIGHT_REFUSED" in proc.stdout
        assert "Traceback" not in proc.stderr

    def test_days_out_of_range_is_refused(self):
        assert fp.main(["--days", "0"]) == 3
        assert fp.main(["--days", "91"]) == 3


# --- the size claim: field presence is not sign semantics -------------------


def _obs(rows):
    return run(Log(settlement=[rows]))


class TestSignedSizeSemanticsAreNeverClaimedByTheProbe:
    @pytest.mark.parametrize("sizes", [
        ["0.5", "0.25"],                     # all positive
        ["-0.5", "-0.25"],                   # all negative
        ["-0.5", "0.25", "-0.1"],            # mixed
    ])
    def test_numeric_signs_alone_never_confirm_what_the_sign_means(self, sizes):
        out = _obs([srow(str(i), size=v) for i, v in enumerate(sizes)])
        assert out["verdict"] == "SETTLEMENT_ROWS_OBSERVED" and out["complete"]
        assert out["signed_size_semantics"] == "UNVERIFIED"
        assert "independent signed position" in out["signed_size_semantics_reason"]
        obs = out["size_field_observation"]
        assert obs["status"] == "ALL_ROWS_HAVE_PARSEABLE_NONZERO_SIZE"
        assert obs["numeric_negative"] == sum(1 for v in sizes if float(v) < 0)
        assert obs["numeric_positive"] == sum(1 for v in sizes if float(v) > 0)
        assert "NOT verified" in out["note"]
        text = json.dumps(out).lower()
        for old in ("settlement_rows_carry_size", "with_direction", '"short"',
                    '"long"', "ownership can be verified", "signed `size`: ownership"):
            assert old not in text, old

    @pytest.mark.parametrize("size,label", [
        ("", "blank"), ("   ", "blank"), (None, "absent"), ("0", "zero"),
        ("0.0", "zero"), ("abc", "unparseable"), ("nan", "unparseable"),
    ])
    def test_missing_blank_zero_or_unparseable_size_is_never_a_positive_verdict(
            self, size, label):
        row = srow("1")
        if size is None:
            del row["size"]
        else:
            row["size"] = size
        out = _obs([row])
        obs = out["size_field_observation"]
        assert obs["status"] == "SOME_ROWS_LACK_PARSEABLE_NONZERO_SIZE", label
        assert out["signed_size_semantics"] == "UNVERIFIED"
        assert "provisional" in out["note"]
        assert obs["numeric_negative"] == obs["numeric_positive"] == 0

    def test_one_bad_row_among_good_ones_is_reported_as_a_gap(self):
        out = _obs([srow("1", size="-0.5"), srow("2", size=""), srow("3", size="0.5")])
        obs = out["size_field_observation"]
        assert obs["status"] == "SOME_ROWS_LACK_PARSEABLE_NONZERO_SIZE"
        assert obs["size_absent_blank_or_unparseable"] == 1 and obs["rows"] == 3

    def test_a_complete_empty_result_says_so_and_infers_nothing(self):
        out = run(Log(any_type=[[other_row("1"), other_row("2", size="-3")]]))
        assert out["complete"] is True
        assert out["verdict"] == "NO_SETTLEMENT_ROWS_TO_OBSERVE"
        assert out["size_field_observation"] == {"status": "NO_SETTLEMENT_ROWS_OBSERVED"}
        assert out["signed_size_semantics"] == "UNVERIFIED"
        assert "no SETTLEMENT rows" in out["note"]
        assert out["settlement_rows_seen"] == 0

    def test_empty_and_incomplete_are_three_distinct_states(self):
        empty = run(Log())["size_field_observation"]["status"]
        seen = _obs([srow("1")])["size_field_observation"]["status"]
        failed = run(Log(settlement=[[srow("1")], RuntimeError("x")]))
        assert failed["size_field_observation"]["status"] == "OBSERVATION_INCOMPLETE"
        assert failed["complete"] is False
        assert len({empty, seen, failed["size_field_observation"]["status"]}) == 3

    def test_a_failure_on_a_later_page_is_never_a_complete_observation(self):
        out = run(Log(settlement=[[srow("1", size="-1")], [srow("2", size="-1")],
                                  RuntimeError("page 3 down")]))
        assert out["complete"] is False
        assert out["verdict"] == "LOG_OBSERVATION_INCOMPLETE"
        assert out["size_field_observation"]["status"] == "OBSERVATION_INCOMPLETE"
        assert out["reads"]["settlement"]["complete"] is False

    def test_the_size_observation_discloses_counts_only(self):
        out = _obs([srow("1"), srow("2", size="-0.0071234")])
        text = all_text(out)
        for name, value in SECRETS.items():
            assert value not in text, name
        assert set(out["size_field_observation"]) == {
            "status", "rows", "size_present", "size_parseable",
            "numeric_negative", "numeric_positive", "numeric_zero",
            "size_absent_blank_or_unparseable", "meaning"}

    def test_raw_samples_stay_opt_in_with_their_privacy_notice(self):
        default = _obs([srow("1")])
        assert "raw_samples" not in all_text(default)
        assert "no account amounts" in default["privacy"]
        asked = run(Log(settlement=[[srow("1")]]), raw_samples=True)
        assert "RAW SAMPLES INCLUDED" in asked["privacy"]
        assert SECRETS["size"] in all_text(asked)

    def test_the_probe_still_issues_only_get_requests(self):
        log = Log(settlement=[[srow("1")], [srow("2")]], any_type=[[other_row()]])
        _obs([srow("1")])
        run(log, days=15)
        assert log.calls and set(log.calls) == {("GET", "/v5/account/transaction-log")}


class TestTheProductionLedgerStillFailsClosedOnSize:
    """What the probe cannot decide, the reconciliation path decides - and it
    refuses to attribute a row whose signed size does not equal the book's."""

    def _book(self):
        import ledger as L
        j = L.Journal()
        L.perp_open(j, ms=NOW, ref="o1", qty=0.5, price=80_000.0, fee_usd=1.0,
                    fee_status="confirmed")
        return L, j

    @pytest.mark.parametrize("size", [0.5, 0.0, None, -9.0])
    def test_a_size_that_is_not_the_books_signed_position_books_nothing(self, size):
        L, j = self._book()
        pay = {"id": "S1", "ms": NOW + 3 * 3_600_000, "usd": 7.77,
               "currency": "USDT", "size": size, "symbol": "BTCUSDT"}
        out = L.book_funding_payments(j, [pay], ms=NOW + 4 * 3_600_000)
        st = j.statement()
        assert out["unattributed"] == 1 and out["booked"] == 0
        assert st["funding_usd"] == 0.0 and st["funding_venue_usd"] == 0.0
        assert st["funding_confirmed"] is False

    def test_only_the_books_own_signed_size_is_attributed(self):
        L, j = self._book()
        pay = {"id": "S1", "ms": NOW + 3 * 3_600_000, "usd": 7.77,
               "currency": "USDT", "size": -0.5, "symbol": "BTCUSDT"}
        assert L.book_funding_payments(j, [pay], ms=NOW + 4 * 3_600_000)["booked"] == 1

    def test_the_recorded_semantics_remain_unconfirmed(self):
        import funding_reconcile as R
        assert "unconfirmed" in R.SIZE_SEMANTICS
