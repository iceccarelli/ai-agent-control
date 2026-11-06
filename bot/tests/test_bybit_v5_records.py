"""tools/bybit_v5_records.py: the documented Bybit V5 record shapes -> the ledger's canonical records.

The API-shaped rows below are SYNTHETIC but follow the documented field names, string typing and signs (execFee > 0 paid; transaction-log ``funding`` > 0 received,
``change = cashFlow + funding - fee``, ``feeRate`` flipped for side Sell on a SETTLEMENT row). Where the documentation is silent the tests assert that the module
refuses or flags rather than guesses.
"""
from __future__ import annotations

import ast
import json
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import account_review as AR  # noqa: E402
import bybit_v5_records as BV  # noqa: E402
import owner_ledger as OL  # noqa: E402
from test_account_review import write_jsonl  # noqa: E402
from test_owner_ledger import World, codes, hard, HOUR  # noqa: E402


# ------------------------------------------------------------------------------------------------------- documented-shape generators

def api_executions(w, fee_currency="USDT", **over):
    rows = []
    for i, f in enumerate(w.fills):
        r = {"symbol": f["symbol"], "orderId": f"o{i}", "orderLinkId": "", "side": f["side"], "orderPrice": str(f["price"]), "orderQty": str(f["qty"]),
             "leavesQty": "0", "orderType": "Market", "stopOrderType": "UNKNOWN", "execFee": repr(f["fee_usd"]), "execId": f["exec_id"],
             "execPrice": repr(f["price"]), "execQty": repr(f["qty"]), "execType": "Trade", "execValue": repr(f["qty"] * f["price"]), "execTime": str(f["ts_ms"]),
             "feeCurrency": fee_currency, "isMaker": f["liquidity"] == "maker", "feeRate": "0.00055", "markPrice": repr(f["price"]), "closedSize": "0", "seq": 1000 + i}
        r.update(over)
        rows.append(r)
    return rows


def api_txlog(w, funding_sign=1, newest_first=False):
    """The account's USDT transaction log for the world: two TRADE rows and one SETTLEMENT row per settlement, with a running balance."""
    ev = [(w.fills[0]["ts_ms"], "open", w.fills[0])] + [(r["ts_ms"], "fund", r) for r in w.funding] + [(w.fills[1]["ts_ms"], "close", w.fills[1])]
    ev.sort(key=lambda e: e[0])
    bal, rows = w.usdt0, []
    for n, (ts, kind, rec) in enumerate(ev):
        base = {"id": f"{n + 1}_BTCUSDT_{ts}", "tradeId": rec["exec_id"], "transactionTime": str(ts), "category": "linear", "symbol": "BTCUSDT", "currency": "USDT",
                "bonusChange": "0"}
        if kind == "fund":
            side = "Sell" if rec["position_qty"] < 0 else "Buy"
            pay = funding_sign * rec["payment_usd"]
            bal += pay
            rows.append(dict(base, type="SETTLEMENT", side=side, size=repr(rec["position_qty"]), qty=repr(abs(rec["position_qty"])), feeRate=repr(rec["rate"] if side == "Buy" else -rec["rate"]),
                             funding=repr(pay), fee="0", cashFlow="0", change=repr(pay), cashBalance=repr(bal)))
        else:
            fee = rec["fee_usd"]
            cash_flow = w.realised if kind == "close" else 0.0
            change = cash_flow - fee
            bal += change
            rows.append(dict(base, type="TRADE", side=rec["side"], size="0" if kind == "close" else repr(-w.qty), qty=repr(rec["qty"]), tradePrice=repr(rec["price"]),
                             feeRate="0.00055", funding="0", fee=repr(fee), cashFlow=repr(cash_flow), change=repr(change), cashBalance=repr(bal)))
    return list(reversed(rows)) if newest_first else rows


def manifest(w, tmp, *, execs=None, txlog=None, snaps=None, extra=None, column_maps=None):
    files = []

    def put(name, rows, family, **kw):
        write_jsonl(tmp / name, rows)
        files.append(dict({"family": family, "path": name}, **kw))
    put("execs.jsonl", execs if execs is not None else api_executions(w), "bybit_v5_execution", **(column_maps or {}))
    put("txlog.jsonl", txlog if txlog is not None else api_txlog(w), "bybit_v5_transaction_log")
    put("snaps.jsonl", snaps or w.snapshots(), "snapshots")
    for name, rows, family in (extra or []):
        put(name, rows, family)
    (tmp / "manifest.json").write_text(json.dumps({"symbol": "BTCUSDT", "taker_bps": 5.5, "files": files}))
    return str(tmp / "manifest.json")


def run(w, tmp, **kw):
    return OL.build(OL.load_manifest(manifest(w, tmp, **kw)))


# --------------------------------------------------------------------------------------------------------------------------- the clean path

def test_documented_shapes_reconcile_to_the_cent_and_match_the_canonical_path(tmp_path):
    w = World()
    rep = run(w, tmp_path)
    assert hard(rep) == [] and rep["residuals"]["usdt"] == pytest.approx(0.0, abs=1e-6) and rep["statement"]["continuity_breaks"] == []
    assert rep["verdict"] in ("RECONCILED_BUT_UNCHECKED", "RECONCILED_NOT_AUTHENTICATED"), rep["flags"]
    b = rep["equity_bridge"]
    assert b["perp_realised_usd"] == pytest.approx(w.realised) and b["perp_fees_usd"] == pytest.approx(-w.fees) and b["funding_net_usd"] == pytest.approx(w.funding_sum)
    assert rep["statement"]["fee_total"] == pytest.approx(w.fees) and rep["statement"]["funding_total"] == pytest.approx(w.funding_sum)
    assert set(rep["custody"]["adapters"]) == {"bybit_v5_execution", "bybit_v5_transaction_log"}
    assert any("web-UI" in u for u in rep["custody"]["undocumented_by_the_venue"])
    # the columns the adapter does not read are listed, not used
    assert "orderId" in rep["custody"]["ignored_columns"]["bybit_v5_execution"] and "bonusChange" in rep["custody"]["ignored_columns"]["bybit_v5_transaction_log"]
    # authenticity is never claimed from internal consistency
    assert "ACCOUNT_VERIFIED" not in json.dumps(rep)


def test_a_newest_first_log_with_rows_in_the_same_millisecond_still_chains(tmp_path):
    w = World()
    log = api_txlog(w, newest_first=True)
    fill1_ts = w.fills[1]["ts_ms"]
    # put the settlement-free closing TRADE and an extra same-ms interest row in one millisecond, in the order a newest-first export would list them
    bal_before = float(next(r for r in log if r["type"] == "TRADE" and r["transactionTime"] == str(fill1_ts))["cashBalance"])
    log.insert(0, {"id": "x", "tradeId": "", "transactionTime": str(fill1_ts), "category": "linear", "symbol": "BTCUSDT", "currency": "USDT", "type": "INTEREST", "side": "None",
                   "size": "0", "feeRate": "0", "funding": "0", "fee": "0", "cashFlow": "-1.5", "change": "-1.5", "cashBalance": repr(bal_before - 1.5)})
    w.interest = -1.5                                   # which of cashFlow/fee carries interest is undocumented; only the documented identity and `change` are used
    rep = run(w, tmp_path, txlog=log, snaps=w.snapshots())
    assert rep["statement"]["continuity_breaks"] == [], rep["statement"]
    assert hard(rep) == [] and rep["equity_bridge"]["interest_usd"] == pytest.approx(-1.5)


# ------------------------------------------------------------------------------------------------------------------------ executions

def test_exec_fee_positive_is_paid_negative_is_a_rebate_and_ismaker_decides_liquidity():
    rows = [{"execId": "a", "execTime": "1700000000000", "symbol": "BTCUSDT", "side": "Sell", "execQty": "0.5", "execPrice": "60000", "execFee": "16.5", "isMaker": False,
             "execType": "Trade", "feeCurrency": "USDT"},
            {"execId": "b", "execTime": "1700000001000", "symbol": "BTCUSDT", "side": "Buy", "execQty": "0.5", "execPrice": "59000", "execFee": "-1.475", "isMaker": True,
             "execType": "Trade", "feeCurrency": "USDT"}]
    out, notes = BV.execution_to_fills(rows, "f")
    assert [(r["exec_id"], r["fee_usd"], r["liquidity"], r["side"], r["qty"], r["price"]) for r in out] == \
        [("a", 16.5, "taker", "Sell", 0.5, 60000.0), ("b", -1.475, "maker", "Buy", 0.5, 59000.0)] and notes == []


@pytest.mark.parametrize("etype", ["Funding", "AdlTrade", "BustTrade", "BlockTrade", "MovePosition", "Settle", "FutureSpread", "UNKNOWN", "SomethingNew"])
def test_executions_that_are_not_plain_trades_are_refused_by_name(etype):
    row = {"execId": "a", "execTime": "1700000000000", "symbol": "BTCUSDT", "side": "Sell", "execQty": "0.5", "execPrice": "60000", "execFee": "16.5", "isMaker": False,
           "execType": etype, "feeCurrency": "USDT"}
    with pytest.raises(AR.RecordError, match="execType"):
        BV.execution_to_fills([row], "f")


def _exec(**kw):
    r = {"execId": "a", "execTime": "1700000000000", "symbol": "BTCUSDT", "side": "Sell", "execQty": "0.5", "execPrice": "60000", "execFee": "16.5", "isMaker": False,
         "execType": "Trade", "feeCurrency": "USDT", "execValue": "30000"}
    r.update(kw)
    return r


@pytest.mark.parametrize("kw,match", [({"feeCurrency": "BTC"}, "feeCurrency"), ({"feeCurrency": "EUR"}, "feeCurrency"), ({"isMaker": "maybe"}, "boolean"),
                                      ({"execValue": "99999"}, "execValue"), ({"symbol": "ETHUSDT"}, "symbol"), ({"side": "buy"}, "side"),
                                      ({"execFee": "abc"}, "execFee"), ({"execQty": "NaN"}, "execQty")])
def test_malformed_or_contradictory_executions_are_errors(kw, match):
    with pytest.raises(AR.RecordError, match=match):
        BV.execution_to_fills([_exec(**kw)], "f")


def test_a_missing_documented_field_is_named():
    r = _exec()
    del r["execFee"]
    with pytest.raises(AR.RecordError, match="execFee"):
        BV.execution_to_fills([r], "f")


def test_an_empty_fee_currency_is_taken_as_usdt_only_with_a_flag_that_keeps_the_top_rung_out_of_reach(tmp_path):
    w = World()
    rep = run(w, tmp_path, execs=api_executions(w, fee_currency=""))
    assert "FEE_CURRENCY_NOT_STATED" in codes(rep, OL.SEVERITY_GAP) and hard(rep) == []
    assert rep["verdict"] == "RECONCILED_BUT_UNCHECKED"                         # never RECONCILED_NOT_AUTHENTICATED on an assumed fee currency


def test_the_spot_adapter_needs_a_stated_fee_currency():
    r = {"execId": "s1", "execTime": "1700000000000", "symbol": "BTCUSDT", "side": "Buy", "execQty": "0.2", "execPrice": "60000", "execFee": "0.0002", "execType": "Trade"}
    with pytest.raises(AR.RecordError, match="feeCurrency"):
        BV.spot_execution_to_trades([r], "f")
    out, _ = BV.spot_execution_to_trades([dict(r, feeCurrency="BTC")], "f")
    assert out == [{"trade_id": "s1", "ts_ms": "1700000000000", "symbol": "BTCUSDT", "side": "Buy", "qty": 0.2, "price": 60000.0, "fee": 0.0002, "fee_coin": "BTC"}]


# ------------------------------------------------------------------------------------------------------------------ transaction log

def _row(**kw):
    r = {"id": "1", "tradeId": "t1", "transactionTime": "1700000000000", "category": "linear", "symbol": "BTCUSDT", "currency": "USDT", "type": "SETTLEMENT", "side": "Buy",
         "size": "0.5", "feeRate": "0.0001", "funding": "-3", "fee": "0", "cashFlow": "0", "change": "-3", "cashBalance": "997"}
    r.update(kw)
    return r


def test_a_long_pays_a_positive_rate_and_a_short_receives_it_with_the_documented_feerate_flip():
    long_ = BV.transaction_log_to_records([_row()], "f")
    assert long_["funding"] == [{"exec_id": "1|t1|1700000000000", "ts_ms": "1700000000000", "symbol": "BTCUSDT", "position_qty": 0.5, "rate": 0.0001, "payment_usd": -3.0}]
    short = BV.transaction_log_to_records([_row(side="Sell", size="-0.5", feeRate="-0.0001", funding="3", change="3", cashBalance="1003")], "f")
    assert short["funding"][0]["position_qty"] == -0.5 and short["funding"][0]["rate"] == pytest.approx(0.0001) and short["funding"][0]["payment_usd"] == 3.0
    assert [(l["category"], l["change"], l["cash_balance"]) for l in short["cash_lines"]] == [("FUNDING", 3.0, 1003.0)]


@pytest.mark.parametrize("kw,match", [({"change": "-2"}, "identity"), ({"side": "None"}, "side"), ({"cashFlow": "1", "change": "-2"}, "session settlement"),
                                      ({"fee": "0.1", "change": "-3.1"}, "session settlement"), ({"symbol": "ETHUSDT"}, "symbol"), ({"currency": "USDC"}, "USDT")])
def test_a_settlement_row_that_breaks_the_documented_shape_is_refused(kw, match):
    if kw.get("currency") == "USDC":
        out = BV.transaction_log_to_records([_row(**kw)], "f")                  # a non-operation currency is skipped and REPORTED, not mapped
        assert out["funding"] == [] and out["notes"][0]["code"] == "ADAPTER_SKIPPED_ROWS"
        return
    with pytest.raises(AR.RecordError, match=match):
        BV.transaction_log_to_records([_row(**kw)], "f")


def test_the_identity_catches_a_classic_log_signed_the_other_way():
    # the classic contract log prints funding with the OPPOSITE sign and change = cashFlow - funding - fee: its rows fail the UTA identity
    with pytest.raises(AR.RecordError, match="identity"):
        BV.transaction_log_to_records([_row(funding="3", change="-3")], "f")


def test_a_whole_file_signed_the_wrong_way_is_a_contradiction_not_a_loss(tmp_path):
    w = World()
    rep = run(w, tmp_path, txlog=api_txlog(w, funding_sign=-1))
    assert "PERP_SIGN_CONVENTION_INVERTED" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_an_unmapped_row_type_is_never_income_and_blocks_a_conclusive_result(tmp_path):
    w = World()
    log = api_txlog(w)
    log.append({"id": "b", "tradeId": "", "transactionTime": str(w.t_close), "category": "", "symbol": "", "currency": "USDT", "type": "BONUS", "side": "None", "size": "0",
                "feeRate": "0", "funding": "0", "fee": "0", "cashFlow": "0", "change": "0", "cashBalance": repr(float(log[-1]["cashBalance"]))})
    rep = run(w, tmp_path, txlog=log)
    assert "UNCLASSIFIED_CASH_LINE" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"
    assert rep["equity_bridge"]["other_cash_lines_usd"] == 0.0


def test_rows_in_other_currencies_are_reported_as_skipped_not_dropped_silently(tmp_path):
    w = World()
    log = api_txlog(w) + [{"id": "e", "tradeId": "", "transactionTime": str(w.t_open + HOUR), "category": "spot", "symbol": "ETHUSDT", "currency": "ETH", "type": "TRADE",
                           "side": "Buy", "size": "0", "feeRate": "0", "funding": "0", "fee": "0", "cashFlow": "0", "change": "0.5", "cashBalance": "0.5"}]
    rep = run(w, tmp_path, txlog=log)
    assert "ADAPTER_SKIPPED_ROWS" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"


def test_transfers_are_external_flows_with_the_sign_checked_and_a_btc_transfer_stays_unpriced(tmp_path):
    w = World()
    log = api_txlog(w)
    t_dep = w.t_open + HOUR
    first_bal = float(log[0]["cashBalance"])
    # a 2,000 USDT transfer in, stamped before the first trade: every later balance moves by 2,000
    for r in log:
        r["cashBalance"] = repr(float(r["cashBalance"]) + 2000.0)
    log.insert(0, {"id": "d", "tradeId": "", "transactionTime": str(t_dep - 1), "category": "", "symbol": "", "currency": "USDT", "type": "TRANSFER_IN", "side": "None", "size": "0",
                   "feeRate": "0", "funding": "0", "fee": "0", "cashFlow": "2000", "change": "2000", "cashBalance": repr(w.usdt0 + 2000.0)})
    log.sort(key=lambda r: int(r["transactionTime"]))
    ww = World()
    ww.flows = [{"flow_id": "x", "ts_ms": t_dep - 1, "coin": "USDT", "amount": 2000.0, "kind": "DEPOSIT"}]
    snaps = ww.snapshots()
    rep = run(ww, tmp_path, txlog=log, snaps=snaps)
    assert rep["equity_bridge"]["external_flows_usd"] == 2000.0 and hard(rep) == [] and "FLOW_NOT_IN_STATEMENT" not in codes(rep), rep["flags"]
    assert any(f["code"] == "TRANSFERS_TREATED_AS_EXTERNAL" for f in rep["flags"])
    with pytest.raises(AR.RecordError, match="sign"):
        BV.transaction_log_to_records([_row(type="TRANSFER_OUT", funding="0", change="2000", cashFlow="2000")], "f")
    btc = BV.transaction_log_to_records([_row(type="TRANSFER_IN", currency="BTC", funding="0", change="0.25", cashFlow="0.25", cashBalance="1.25")], "f")
    assert btc["external_flows"][0]["coin"] == "BTC" and "usd_value" not in btc["external_flows"][0]      # no price is invented


def test_a_trade_row_is_split_into_lines_that_share_one_running_balance():
    out = BV.transaction_log_to_records([_row(type="TRADE", side="Buy", funding="0", cashFlow="25", fee="1.5", change="23.5", cashBalance="1023.5")], "f")
    cats = [(l["category"], l["change"], l["cash_balance"], l["row_id"]) for l in out["cash_lines"]]
    assert cats == [("TRADE", 25.0, None, "1|t1|1700000000000"), ("FEE", -1.5, 1023.5, "1|t1|1700000000000")]
    rebate = BV.transaction_log_to_records([_row(type="TRADE", side="Buy", funding="0", cashFlow="0", fee="-0.4", change="0.4", cashBalance="1000.4")], "f")
    assert [(l["category"], l["change"]) for l in rebate["cash_lines"]] == [("REBATE", 0.4)]
    with pytest.raises(AR.RecordError, match="funding"):
        BV.transaction_log_to_records([_row(type="TRADE", funding="1", change="1")], "f")


# --------------------------------------------------------------------------------------------------- a foreign export, and purity

def test_a_ui_style_export_is_read_only_through_an_explicit_column_map_never_guessed(tmp_path):
    w = World()
    api = api_executions(w)
    # a SYNTHETIC foreign layout (not Bybit's real UI file, whose columns are undocumented): the owner maps each column to the documented API name
    foreign = [{"Exec": r["execId"], "T(UTC ms)": r["execTime"], "Pair": r["symbol"], "Dir": r["side"].upper(), "Size": r["execQty"], "Px": r["execPrice"], "Fee": r["execFee"],
                "Maker": "true" if r["isMaker"] else "false", "Type": "Trade", "Ccy": "USDT"} for r in api]
    cmap = {"column_map": {"execId": "Exec", "execTime": "T(UTC ms)", "symbol": "Pair", "side": {"column": "Dir", "transform": 'map:{"BUY":"Buy","SELL":"Sell"}'},
                           "execQty": "Size", "execPrice": "Px", "execFee": "Fee", "isMaker": "Maker", "execType": "Type", "feeCurrency": "Ccy"}}
    rep = run(w, tmp_path, execs=foreign, column_maps=cmap)
    assert hard(rep) == [] and rep["residuals"]["usdt"] == pytest.approx(0.0, abs=1e-6)
    # without the map the file is refused naming the missing documented field
    with pytest.raises(AR.RecordError, match="execId"):
        OL.load_manifest(manifest(w, tmp_path / "n" if (tmp_path / "n").mkdir() is None else tmp_path, execs=foreign))


def test_the_adapter_module_does_no_io_and_imports_nothing_that_can_reach_the_outside():
    tree = ast.parse(open(BV.__file__, encoding="utf-8").read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert imported <= {"__future__", "math", "typing", "account_review"}, imported
    names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not names & {"open", "exec", "eval", "compile", "__import__", "input"}


def test_every_documented_gap_is_listed_and_every_adapter_names_its_source():
    assert set(BV.DOC_BASIS) == set(BV.ADAPTERS) and all(u.startswith("https://bybit-exchange.github.io/docs/v5/") for u in BV.DOC_BASIS.values())
    assert len(BV.UNDOCUMENTED) >= 8
