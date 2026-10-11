"""The probe pairs raw venue fee fields with the parse, and sends nothing."""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from test_carry_broker import envelope  # noqa: E402
from tools import fee_payload_probe as fp  # noqa: E402


class History:
    def __init__(self, spot=(), linear=(), fail=()):
        self.rows = {"spot": list(spot), "linear": list(linear)}
        self.fail, self.calls = set(fail), []

    def _request(self, method, endpoint, **kw):
        self.calls.append((method, endpoint))
        cat = kw["params"]["category"]
        if cat in self.fail:
            raise RuntimeError("history down")
        return envelope({"list": self.rows[cat]})


def row(**kw):
    base = {"orderId": "1234567890abcdef", "side": "Buy", "orderType": "Market",
            "orderStatus": "Filled", "cumExecQty": "0.001", "avgPrice": "82000"}
    base.update(kw)
    return base


def test_modern_and_legacy_rows_are_paired_with_their_parse():
    h = History(spot=[row(cumFeeDetail={"BTC": "0.000001"})],
                linear=[row(cumExecFee="0.045", feeCurrency="USDT")])
    out = fp.probe(h, "BTCUSDT")
    assert out["verdict"] == "FEE_PAYLOAD_OBSERVED"
    assert (out["with_cumFeeDetail"], out["parsed_known"],
            out["parsed_unknown"]) == (1, 2, 0)
    spot = next(o for o in out["orders"] if o["category"] == "spot")
    assert spot["raw_fee_fields"] == {"cumFeeDetail": {"BTC": "0.000001"}}
    assert spot["parsed"]["source"] == "cumFeeDetail"
    assert spot["order"] == "12345678..."            # truncated id only


def test_an_unparsed_real_shape_is_surfaced_not_hidden():
    out = fp.probe(History(linear=[row(cumFeeDetail="surprise")]), "BTCUSDT")
    assert out["parsed_unknown"] == 1 and out["parsed_known"] == 0
    assert out["orders"][0]["raw_fee_fields"] == {"cumFeeDetail": "surprise"}


def test_unfilled_orders_are_ignored_and_empty_history_is_said_plainly():
    out = fp.probe(History(spot=[row(cumExecQty="0", orderStatus="Cancelled")]),
                   "BTCUSDT")
    assert out["verdict"] == "NO_FILLED_ORDERS_TO_OBSERVE"


def test_an_unreadable_category_is_reported_not_swallowed():
    out = fp.probe(History(spot=[row(cumExecQty="1")], fail=["linear"]), "BTCUSDT")
    assert "linear" in out["read_errors"] and out["filled_orders_seen"] == 1


def test_it_only_ever_reads_history():
    h = History(spot=[row()])
    fp.probe(h, "BTCUSDT")
    assert {(m, e) for m, e in h.calls} == {("GET", "/v5/order/history")}


def test_the_real_command_line_refuses_a_contradictory_environment():
    env = {"PATH": os.environ["PATH"], "HOME": "/tmp", "BYBIT_VENUE": "testnet",
           "USE_TESTNET": "0", "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}
    proc = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(HERE), "tools",
                                      "fee_payload_probe.py")],
        env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3 and "PREFLIGHT_REFUSED" in proc.stdout
    assert "Traceback" not in proc.stderr
