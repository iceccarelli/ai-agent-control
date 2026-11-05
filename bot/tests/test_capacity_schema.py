import copy
import math

import pytest

import capacity_schema as CS
from capacity_helpers import base_row, clone, series, START, H8


@pytest.fixture(scope="module")
def good():
    return base_row()


def codes(issues):
    return [c for c, _ in issues]


def test_a_row_from_the_real_recorder_path_is_valid(good):
    assert CS.validate_row(good) == []
    assert good["schema"] == "capacity/2" and good["venue"] == "bybit" and good["category"] == "linear"
    assert good["row_id"] == "mainnet:BTCUSDT:1" and good["model_version"].startswith("md_capacity/")
    assert good["fees"]["source"] and good["funding"]["interval_h"] == 8


def test_a_legacy_row_is_quarantined_not_accepted_and_not_called_corrupt():
    legacy = {"at_ms": 1, "curve": [], "environment": "mainnet"}
    assert codes(CS.validate_row(legacy)) == [CS.LEGACY]
    v = CS.validate_stream([legacy, legacy])
    assert v["accepted"] == [] and v["n_legacy_quarantined"] == 2 and not v["stream_ok"]


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r.pop("symbol"), CS.MISSING_FIELD),
    (lambda r: r.__setitem__("schema", "capacity/3"), CS.BAD_SCHEMA),
    (lambda r: r["funding"].__setitem__("forecast_bps", float("nan")), CS.NON_FINITE),
    (lambda r: r["funding"].__setitem__("forecast_bps", float("inf")), CS.NON_FINITE),
    (lambda r: r.__setitem__("at_ms", -5), CS.BAD_TIMESTAMP),
    (lambda r: r["times"].__setitem__("perp_book_exch_ts_ms", r["at_ms"] + 10 * 60_000), CS.BAD_TIMESTAMP),
    (lambda r: r["funding"].__setitem__("next_settlement_ms", r["at_ms"] - 1), CS.BAD_SETTLEMENT),
    (lambda r: r["funding"].__setitem__("next_settlement_ms", r["at_ms"] + 30 * 3_600_000), CS.BAD_SETTLEMENT),
    (lambda r: r["funding"].__setitem__("interval_h", 0), CS.BAD_SETTLEMENT),
    (lambda r: r["funding"].__setitem__("last_print_settled_ms", r["at_ms"] + 5), CS.BAD_SETTLEMENT),
    (lambda r: r["book"].__setitem__("perp_bid_levels", -1), CS.BAD_BOOK),
    (lambda r: r.__setitem__("curve", []), CS.BAD_CURVE),
    (lambda r: r["curve"][0].__setitem__("total_round_trip_bps", float("nan")), CS.NON_FINITE),
    (lambda r: r["source"].__setitem__("store_record_id", None), CS.MISSING_FIELD),
    (lambda r: r.__setitem__("row_id", "mainnet:BTCUSDT:999"), CS.BAD_TYPE),
    (lambda r: r["fees"].__setitem__("source", None), CS.MISSING_FIELD),
    (lambda r: r.__setitem__("venue", "kraken"), CS.MIXED),
])
def test_each_corruption_is_rejected_with_its_named_reason(good, mutate, code):
    r = copy.deepcopy(good)
    mutate(r)
    assert code in codes(CS.validate_row(r)), CS.validate_row(r)


def test_non_dict_rows_are_corrupt():
    assert codes(CS.validate_row("nope")) == [CS.CORRUPT]
    assert codes(CS.validate_row(None)) == [CS.CORRUPT]


def test_a_stream_mixing_environment_or_symbol_is_rejected_whole(good):
    a = clone(good, 1, good["at_ms"], 0.5, good["funding"]["next_settlement_ms"])
    b = clone(good, 2, good["at_ms"] + 1000, 0.5, good["funding"]["next_settlement_ms"], env="testnet")
    v = CS.validate_stream([a, b])
    assert v["accepted"] == [] and any(p["code"] == CS.MIXED for p in v["problems"])
    c = clone(good, 3, good["at_ms"] + 1000, 0.5, good["funding"]["next_settlement_ms"], symbol="ETHUSDT")
    assert CS.validate_stream([a, c])["accepted"] == []


def test_duplicate_ids_and_time_regressions_reject_the_stream(good):
    ns = good["funding"]["next_settlement_ms"]
    a = clone(good, 1, good["at_ms"], 0.5, ns)
    dup = clone(good, 1, good["at_ms"] + 1000, 0.5, ns)
    assert any(p["code"] == CS.DUPLICATE_ID for p in CS.validate_stream([a, dup])["problems"])
    back = clone(good, 2, good["at_ms"] - 5000, 0.5, ns)
    v = CS.validate_stream([a, back])
    assert any(p["code"] == CS.TIME_REGRESSION for p in v["problems"]) and v["accepted"] == []


def test_a_clean_series_is_accepted_whole_and_legacy_rows_never_enter_it(good):
    rows = series(good, 3, lambda T, t: 0.6, lambda T: 0.6)
    v = CS.validate_stream(rows)
    assert v["stream_ok"] and len(v["accepted"]) == len(rows) and v["problems"] == []
    legacy = {"at_ms": 1, "curve": []}
    v2 = CS.validate_stream(rows + [legacy])
    assert v2["n_legacy_quarantined"] == 1 and len(v2["accepted"]) == len(rows)
