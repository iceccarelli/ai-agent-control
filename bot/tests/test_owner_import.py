"""tools/owner_import.py: the owner-account importer in front of the existing owner_ledger / account_review path.

SYNTHETIC, disposable fixtures only (tests/fixtures_owner_import.py lays rows out; it never supplies an expected figure). For each scenario the expected cash / P&L is
written here as literal arithmetic AND recomputed by ``indep()``, a second implementation that reads the raw API rows by a different route (cash identity over the
fills, instead of the ledger's average-entry accounting). Everything runs in tmp_path; nothing opens bot/state, runs/, a *.db or a credential.
"""
from __future__ import annotations

import copy
import json
import os
import stat
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, os.path.join(BOT, "tests"))
sys.path.insert(0, BOT)

import account_review as AR  # noqa: E402
import owner_import as OI  # noqa: E402
import fixtures_owner_import as F  # noqa: E402
from fixtures_owner_import import Trip, exec_row, lay_out, marks, wallet, write_json, write_jsonl  # noqa: E402

_REAL_READ_HISTORY = AR._read_history
_HISTORY = {}


@pytest.fixture(autouse=True)
def _read_the_committed_history_once(monkeypatch):
    def cached(path, default_name, use_default):
        if path is None and use_default:
            if default_name not in _HISTORY:
                _HISTORY[default_name] = _REAL_READ_HISTORY(path, default_name, use_default)
            return _HISTORY[default_name]
        return _REAL_READ_HISTORY(path, default_name, use_default)
    monkeypatch.setattr(AR, "_read_history", cached)


# ------------------------------------------------------------------------------------------------ the second implementation

def indep(exec_rows, tx_rows):
    """Cash identity over the RAW rows, no shared code with the ledger: realised = what flat round trips took in minus what they paid out (flat-to-flat only);
    fees = sum of execFee; funding = sum of SETTLEMENT ``funding``; net = realised - fees + funding. Also the closing USDT as the log's own last balance."""
    sells = sum(float(r["execQty"]) * float(r["execPrice"]) for r in exec_rows if r["side"] == "Sell")
    buys = sum(float(r["execQty"]) * float(r["execPrice"]) for r in exec_rows if r["side"] == "Buy")
    qty_net = sum(float(r["execQty"]) * (1 if r["side"] == "Buy" else -1) for r in exec_rows)
    assert abs(qty_net) < 1e-12, "indep() is for flat-to-flat books"
    realised = sells - buys
    fees = sum(float(r["execFee"]) for r in exec_rows)
    funding = sum(float(r["funding"]) for r in tx_rows if r["type"] == "SETTLEMENT")
    last_usdt = [float(r["cashBalance"]) for r in sorted(tx_rows, key=lambda r: int(r["transactionTime"])) if r["currency"] == "USDT"][-1]
    return {"realised": realised, "fees": fees, "funding": funding, "net": realised - fees + funding, "closing_usdt": last_usdt}


def close(a, b, tol=1e-6):
    assert abs(a - b) <= tol, f"{a} != {b}"


def run(tmp_path, trip=None, *, mk="default", **kw):
    trip = trip or Trip()
    pairs = lay_out(tmp_path, trip, **{k: v for k, v in kw.items() if k in ("execs", "txlog", "w0", "w1", "with_wallets", "spot_exec")})
    rest = {k: v for k, v in kw.items() if k not in ("execs", "txlog", "w0", "w1", "with_wallets", "spot_exec")}
    return OI.run_import(pairs, marks=marks() if mk == "default" else mk, **rest)


def flag_codes(rep, sev=None):
    return sorted({f["code"] for f in rep["flags"] if sev is None or f["severity"] == sev})


def no_profit_claim(rep):
    """An account that is not complete must never show a verified / reconciled profit, anywhere in the report."""
    assert rep["status"] in OI.STATUSES
    assert rep["headline_realised_cash_usd"] is None
    for layer in rep["layers"].values():
        assert layer["status"] not in ("VERIFIED", "RECONCILED"), layer
    assert "RECONCILED" != rep["status"] and "VERIFIED" != rep["status"]


def refused(code, pairs, **kw):
    with pytest.raises(OI.ImportRefused) as ei:
        OI.run_import(pairs, **kw)
    assert ei.value.code == code, str(ei.value)
    return ei.value


# ------------------------------------------------------------------------------------------------ the clean books

def test_status_vocabulary_is_exactly_the_eight_words():
    assert OI.STATUSES == ("VERIFIED", "RECONCILED", "ESTIMATED", "PROVISIONAL", "STALE", "DISPUTED", "INCOMPLETE", "UNAVAILABLE")


def test_a_clean_round_trip_reconciles_to_hand_arithmetic_and_the_second_implementation(tmp_path):
    t = Trip()
    rep = run(tmp_path, t)
    # by hand: short 0.5 BTC sold at 60,000 and bought back at 59,000
    realised = (60_000.0 - 59_000.0) * 0.5                       # 500.00
    fee_open = 0.5 * 60_000.0 * 0.00055                           # 16.50
    fee_close = 0.5 * 59_000.0 * 0.00055                          # 16.225
    funding = 3 * (0.5 * 60_000.0 * 0.0001)                       # 3 settlements x 3.00 = 9.00
    net = 500.0 - 16.5 - 16.225 + 9.0                             # 476.275
    close(net, realised - fee_open - fee_close + funding)
    cash = rep["layers"]["realised_cash"]
    assert rep["status"] == "RECONCILED" and cash["status"] == "RECONCILED"
    close(cash["figures"]["perp_realised_usd"], 500.0)
    close(cash["figures"]["perp_fees_usd"], -32.725)
    close(cash["figures"]["funding_net_usd"], 9.0)
    close(cash["net_realised_cash_usd"], 476.275)
    close(rep["headline_realised_cash_usd"], 476.275)
    ind = indep(t.executions(), t.txlog())
    close(ind["net"], 476.275)
    close(ind["realised"], 500.0)
    close(ind["fees"], 32.725)
    close(ind["funding"], 9.0)
    close(ind["closing_usdt"], 10_000.0 + 476.275)
    assert all(abs(v) < 1e-6 for k, v in rep["ledger"]["residuals"].items() if k in ("usdt", "btc_qty", "equity_usd"))
    # the owned BTC (1.0) fell 1,000; the 0.5 hedge is in realised: economic total is ESTIMATED because it rests on supplied marks
    inv, eco = rep["layers"]["inventory_valuation"], rep["layers"]["economic_total"]
    assert inv["status"] == "ESTIMATED" and eco["status"] == "ESTIMATED"
    close(inv["btc_value_usd"], 1.0 * 59_000.0)
    close(inv["btc_economic_pnl_usd"], 1.0 * (59_000.0 - 60_000.0))
    close(eco["net_economic_pnl_usd"], 476.275 - 1_000.0)
    assert all(f["file_status"] == "VERIFIED" for f in rep["lineage"]["files"])
    assert "no exchange authentication: files are the owner's, a digest proves bytes, not origin" in rep["unchecked"]


def test_lineage_hashes_the_original_bytes_and_the_import_is_reproducible(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    r1, r2 = run(a), run(b)
    assert r1 == r2 and r1["import_id"] == r2["import_id"] and r1["report_sha256"] == r2["report_sha256"]
    import hashlib
    for ent in r1["lineage"]["files"]:
        raw = open(a / ent["file"], "rb").read()
        assert ent["sha256"] == hashlib.sha256(raw).hexdigest() and ent["bytes"] == len(raw)
        assert ent["first_ts_utc"].endswith("Z") or "+00:00" in ent["first_ts_utc"] or "T" in ent["first_ts_utc"]
    c = tmp_path / "c"
    c.mkdir()
    t = Trip(usdt0=10_000.5)                                      # different content -> different identity
    r3 = run(c, t)
    assert r3["import_id"] != r1["import_id"]
    d = tmp_path / "d"
    d.mkdir()
    assert run(d, mk=marks(spot_close=59_001.0))["import_id"] != r1["import_id"]     # marks are part of the identity


# ------------------------------------------------------------------------------------------------ duplicates, order, partials

def test_identical_duplicate_fill_is_dropped_counted_and_changes_nothing(tmp_path):
    t = Trip()
    pairs = lay_out(tmp_path, t)
    second = write_jsonl(tmp_path / "exec_overlap.jsonl", t.executions())          # an overlapping export: every fill again
    rep = OI.run_import(pairs + [("linear_execution", second)], marks=marks())
    assert rep["duplicates"]["identical_dropped"] == 2 and rep["duplicates"]["conflicting"] == []
    assert rep["status"] == "RECONCILED"
    close(rep["headline_realised_cash_usd"], 500.0 - 16.5 - 16.225 + 9.0)                  # not 476.275 + 500 - ...: counted once
    # positive/negative control: without the guard the realised leg would double to 1000
    assert abs(rep["layers"]["realised_cash"]["figures"]["perp_realised_usd"] - 500.0) < 1e-9


def test_conflicting_duplicate_is_disputed_and_computes_nothing(tmp_path):
    t = Trip()
    pairs = lay_out(tmp_path, t)
    bad = t.executions()
    bad[0]["execPrice"] = "60100.0"                                                  # same execId x1, another price
    bad[0]["execValue"] = repr(0.5 * 60_100.0)
    pairs.append(("linear_execution", write_jsonl(tmp_path / "exec_conflict.jsonl", bad)))
    rep = OI.run_import(pairs, marks=marks())
    assert rep["status"] == "DISPUTED" and rep["layers"] == {}
    assert rep["duplicates"]["conflicting"][0]["id"] == "x1"
    assert rep["headline_realised_cash_usd"] is None and rep["status"] not in OI._PROFIT_OK
    # the same disagreement inside the transaction log
    tx = t.txlog()
    tx.append(dict(tx[0], change="-1.0"))
    sub = tmp_path / "txd"
    sub.mkdir()
    pairs2 = lay_out(sub, t, txlog=tx)
    assert OI.run_import(pairs2, marks=marks())["status"] == "DISPUTED"


def test_out_of_order_fills_and_a_newest_first_log_give_the_same_answer(tmp_path):
    t = Trip()
    base = run(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    sub = tmp_path / "y"
    sub.mkdir()
    rep = run(sub, t, execs=list(reversed(t.executions())), txlog=t.txlog(newest_first=True))
    assert rep["status"] == "RECONCILED"
    close(rep["headline_realised_cash_usd"], base["headline_realised_cash_usd"])
    close(rep["headline_realised_cash_usd"], 500.0 - 16.5 - 16.225 + 9.0)


def test_partial_fills_reconcile_to_the_cent(tmp_path):
    t = Trip(opens=((0.2, 60_000.0), (0.3, 60_000.0)), closes=((0.3, 59_000.0), (0.2, 59_500.0)))
    rep = run(tmp_path, t)
    # by hand: realised 0.3 x (60000-59000) + 0.2 x (60000-59500) = 300 + 100; fees 0.00055 x (12000 + 18000 + 17700 + 11900); funding 3 x 3.00
    realised = 0.3 * 1_000.0 + 0.2 * 500.0
    fees = 0.00055 * (0.2 * 60_000.0 + 0.3 * 60_000.0 + 0.3 * 59_000.0 + 0.2 * 59_500.0)
    close(realised, 400.0)
    close(fees, 6.6 + 9.9 + 9.735 + 6.545)
    net = 400.0 - 32.78 + 9.0
    assert rep["status"] == "RECONCILED"
    close(rep["headline_realised_cash_usd"], net)
    close(indep(t.executions(), t.txlog())["net"], net)
    assert rep["layers"]["realised_cash"]["figures"]["perp_realised_usd"] == pytest.approx(400.0)


# ------------------------------------------------------------------------------------------------ fee currency (spot)

@pytest.mark.parametrize("fee_coin, fee, btc_after, usdt_after, btc_econ", [
    ("USDT", 6.0, 1.1, 10_000.0 - 6_000.0 - 6.0, 1.1 * 59_000.0 - 60_000.0 - (6_000.0 + 6.0)),
    ("BTC", 0.0001, 1.1 - 0.0001, 10_000.0 - 6_000.0, (1.1 - 0.0001) * 59_000.0 - 60_000.0 - 6_000.0),
])
def test_spot_fee_in_quote_coin_costs_cash_and_in_base_coin_reduces_held_quantity(tmp_path, fee_coin, fee, btc_after, usdt_after, btc_econ):
    t = Trip(spot=[(10, "Buy", 0.1, 60_000.0, fee, fee_coin)])
    rep = run(tmp_path, t)
    assert rep["status"] == "RECONCILED", rep["flags"]
    inv = rep["layers"]["inventory_valuation"]
    # by hand (literal): quote fee -> BTC 1.1, USDT down 6,006; base fee -> BTC 1.0999, USDT down 6,000
    if fee_coin == "USDT":
        close(inv["btc_qty"], 1.1)
        close(btc_econ, -1_106.0)                                  # 1.1 x 59,000 - 60,000 - 6,006
    else:
        close(inv["btc_qty"], 1.0999)
        close(btc_econ, -1_105.9)                                  # 1.0999 x 59,000 - 60,000 - 6,000
    close(inv["btc_economic_pnl_usd"], btc_econ)
    # the second implementation: the wallet itself, from the raw log
    closing = {c["coin"]: float(c["walletBalance"]) for c in json.load(open(tmp_path / "wallet_close.json"))["result"]["list"][0]["coin"]}
    close(closing["BTC"], btc_after)
    close(closing["USDT"], usdt_after + 476.275)
    # the perpetual net is unaffected by how the spot fee was paid
    close(rep["headline_realised_cash_usd"], 476.275)


def test_a_base_coin_fee_written_as_usd_is_refused_as_a_unit_slip(tmp_path):
    t = Trip(spot=[(10, "Buy", 0.1, 60_000.0, 6.0, "BTC")])             # 6.0 "BTC" of fee on a 0.1 BTC buy
    pairs = lay_out(tmp_path, t)
    refused("UNIT", pairs, marks=marks())


# ------------------------------------------------------------------------------------------------ symbol, units, funding

def test_a_wrong_symbol_row_is_refused_never_taken_as_btc(tmp_path):
    t = Trip()
    ex = t.executions()
    ex[1] = dict(ex[1], symbol="ETHUSDT")
    refused("SYMBOL_MISMATCH", lay_out(tmp_path, t, execs=ex), marks=marks())
    sub = tmp_path / "tx"
    sub.mkdir()
    tx = t.txlog()
    tx[1] = dict(tx[1], symbol="ETHUSDT")
    refused("SYMBOL_MISMATCH", lay_out(sub, t, txlog=tx), marks=marks())
    ok = tmp_path / "ok"
    ok.mkdir()
    assert run(ok, t)["status"] == "RECONCILED"                           # positive control: BTCUSDT rows pass


def test_fee_rate_unit_slip_percent_versus_fraction(tmp_path):
    t = Trip()
    ex = t.executions()
    ex[0]["feeRate"] = "0.055"                                             # 0.055 % written as a fraction = 5.5 %
    refused("UNIT", lay_out(tmp_path, t, execs=ex), marks=marks())
    sub = tmp_path / "b"
    sub.mkdir()
    ex2 = t.executions()
    ex2[0]["execFee"] = "165.0"                                            # the fee itself x10 against an honest feeRate
    rep = run(sub, t, execs=ex2)
    assert rep["status"] == "DISPUTED" and "FEE_RATE_INCONSISTENT" in flag_codes(rep, "HARD")
    no_profit_claim(rep)
    ok = tmp_path / "c"
    ok.mkdir()
    assert run(ok, t)["status"] == "RECONCILED"


def test_funding_sign_inverted_is_a_contradiction_not_a_loss(tmp_path):
    t = Trip()
    rep = run(tmp_path, t, txlog=t.txlog(funding_sign=-1))
    # a short at a positive rate RECEIVES 3.00 per settlement; the log says it paid. by hand: -9.00 instead of +9.00
    assert rep["status"] == "DISPUTED" and "FUNDING_SIGN_CONTRADICTION" in flag_codes(rep, "HARD")
    no_profit_claim(rep)
    assert indep(t.executions(), t.txlog(funding_sign=-1))["funding"] == pytest.approx(-9.0)


def test_funding_times_ten_and_a_percent_for_fraction_slip_are_refused_by_magnitude(tmp_path):
    t = Trip()
    rep = run(tmp_path, t, txlog=t.txlog(funding_scale=10.0))
    assert rep["status"] == "DISPUTED" and "FUNDING_MAGNITUDE_IMPLAUSIBLE" in flag_codes(rep, "HARD")
    no_profit_claim(rep)
    assert indep(t.executions(), t.txlog(funding_scale=10.0))["funding"] == pytest.approx(90.0)    # what the x10 file claims: 3 x 30.00
    sub = tmp_path / "r"
    sub.mkdir()
    tx = t.txlog()
    for r in tx:                                                           # rate printed as percent (x100), payment honest
        if r["type"] == "SETTLEMENT":
            r["feeRate"] = repr(float(r["feeRate"]) * 100)
    rep2 = run(sub, t, txlog=tx)
    assert rep2["status"] == "DISPUTED"
    ok = tmp_path / "ok"
    ok.mkdir()
    assert run(ok, t)["status"] == "RECONCILED"


def test_missing_funding_settlement_is_incomplete_and_never_zero(tmp_path):
    t = Trip()
    rep = run(tmp_path, t, txlog=t.txlog(drop_settlement=1))
    assert rep["status"] == "INCOMPLETE"
    no_profit_claim(rep)
    cash = rep["layers"]["realised_cash"]
    assert cash["settlements"]["missing"] == 1 and cash["settlements"]["held"] == 3
    assert rep["partial_figures"]["funding_net_usd"] == pytest.approx(6.0)        # the 2 settlements present; the missing one is NOT booked as 0 in a verified total
    assert "PERP_MISSING_SETTLEMENT" in flag_codes(rep)
    assert indep(t.executions(), t.txlog(drop_settlement=1))["funding"] == pytest.approx(6.0)


# ------------------------------------------------------------------------------------------------ marks and valuation

def test_missing_mark_leaves_the_inventory_unvalued_never_zero(tmp_path):
    rep = run(tmp_path, mk=[])
    inv = rep["layers"]["inventory_valuation"]
    assert inv["status"] == "UNAVAILABLE" and inv["reason"] == "INVENTORY_UNVALUED"
    assert inv["btc_value_usd"] is None and inv["perp_unrealised_usd"] is None
    assert rep["layers"]["economic_total"]["net_economic_pnl_usd"] is None and rep["layers"]["economic_total"]["status"] == "UNAVAILABLE"
    assert "INVENTORY_UNVALUED" in flag_codes(rep)
    # the cash leg that needs no mark is still shown, but unanchored to balances: PROVISIONAL, not RECONCILED
    assert rep["status"] == "PROVISIONAL" and rep["layers"]["realised_cash"]["status"] == "PROVISIONAL"
    # CHANGED in integration review: this used to assert headline_realised_cash_usd == 476.275 here. That figure omitted interest and every other statement line (unknown, not zero), so a
    # PROVISIONAL account headlined a profit it could not support. The known components are now reported only as a labelled partial.
    assert rep["headline_realised_cash_usd"] is None
    close(rep["layers"]["realised_cash"]["partial_net_of_known_components_usd"], 476.275)


def test_only_the_closing_mark_missing_leaves_the_26h_old_opening_mark_stale_and_the_total_withheld(tmp_path):
    rep = run(tmp_path, mk=marks()[:1])
    inv = rep["layers"]["inventory_valuation"]
    assert inv["status"] == "STALE" and "MARK_STALE" in flag_codes(rep)           # the opening mark is NOT a closing mark: labelled, not trusted
    assert rep["layers"]["economic_total"]["net_economic_pnl_usd"] is None and rep["layers"]["economic_total"]["status"] == "STALE"
    sub = tmp_path / "none"
    sub.mkdir()
    only_future = [dict(marks()[0], ts_ms=F.W0_TS + 1), dict(marks()[1], ts_ms=F.W1_TS + 1)]      # nothing at or before the opening instant
    rep2 = run(sub, mk=only_future)
    assert rep2["layers"]["inventory_valuation"]["btc_value_usd"] is None and rep2["layers"]["inventory_valuation"]["status"] == "UNAVAILABLE"


def test_a_stale_mark_is_marked_stale_and_a_future_mark_is_not_used(tmp_path):
    old = marks(ts_close=F.W1_TS - 12 * F.HOUR)                               # 12 h old at the closing instant, limit 6 h
    rep = run(tmp_path, mk=old)
    assert rep["layers"]["inventory_valuation"]["status"] == "STALE" and "MARK_STALE" in flag_codes(rep)
    assert rep["layers"]["economic_total"]["net_economic_pnl_usd"] is None
    sub = tmp_path / "f"
    sub.mkdir()
    future = [marks()[0], dict(marks()[1], ts_ms=F.W1_TS + 1)]               # a mark AFTER the snapshot is hindsight
    rep2 = run(sub, mk=future)
    assert rep2["layers"]["inventory_valuation"]["status"] == "STALE"            # the hindsight mark is ignored; the 26 h older opening mark is all that is left
    assert rep2["layers"]["inventory_valuation"]["marks_used"][0]["ts_ms"] == F.W0_TS


def test_a_mark_in_the_wrong_unit_is_refused(tmp_path):
    pairs = lay_out(tmp_path, Trip())
    refused("MARK", pairs, marks=marks(spot_close=5_900_000.0))               # cents
    refused("MARK", pairs, marks=[dict(marks()[0], source="")])


def test_an_open_perpetual_leg_needs_its_own_mark(tmp_path):
    t = Trip(closes=((0.2, 59_000.0),))                                       # only 0.2 of the 0.5 short is bought back: 0.3 stays open
    rep = run(tmp_path, t, mk=marks())
    assert rep["layers"]["inventory_valuation"]["status"] == "UNAVAILABLE"
    assert rep["layers"]["inventory_valuation"]["perp_unrealised_usd"] is None
    assert any("BTCUSDT_PERP" in d for d in rep["layers"]["inventory_valuation"]["detail"])
    s2 = tmp_path / "p"
    s2.mkdir()
    perp = marks() + [{"instrument": "BTCUSDT_PERP", "ts_ms": F.W1_TS, "price": 59_100.0, "source": "owner-supplied test mark"}]
    rep2 = run(s2, t, mk=perp)
    inv = rep2["layers"]["inventory_valuation"]
    # by hand: short 0.3 entered at 60,000, mark 59,100 -> unrealised +0.3 x 900 = 270
    close(inv["perp_unrealised_usd"], 0.3 * (60_000.0 - 59_100.0))
    close(inv["perp_unrealised_usd"], 270.0)
    assert inv["status"] == "ESTIMATED"


# ------------------------------------------------------------------------------------------------ missing data

def test_a_missing_fee_is_incomplete_not_free(tmp_path):
    t = Trip()
    ex = t.executions()
    ex[0]["execFee"] = "0"                                                     # fee dropped to zero though feeRate says 0.00055
    rep = run(tmp_path, t, execs=ex)
    assert rep["status"] in ("INCOMPLETE", "DISPUTED")
    assert "MISSING_FEE" in flag_codes(rep)
    no_profit_claim(rep)
    sub = tmp_path / "k"
    sub.mkdir()
    ex2 = t.executions()
    del ex2[0]["execFee"]                                                      # the column itself missing: refused at the schema
    refused("SCHEMA", lay_out(sub, t, execs=ex2), marks=marks())


def test_a_fill_missing_from_the_execution_list_is_not_a_clean_pass(tmp_path):
    t = Trip()
    rep = run(tmp_path, t, execs=t.executions()[:1])                           # the buy-back is in the log but not in the executions
    assert rep["status"] in ("INCOMPLETE", "DISPUTED")
    no_profit_claim(rep)


def test_a_position_the_log_disagrees_with_is_disputed(tmp_path):
    t = Trip()
    tx = t.txlog()
    tx[-1]["size"] = "-0.1"
    rep = run(tmp_path, t, txlog=tx)
    assert rep["status"] == "DISPUTED" and "POSITION_MISMATCH" in flag_codes(rep, "HARD")
    no_profit_claim(rep)


def test_a_closing_balance_that_does_not_follow_from_the_records_never_reconciles(tmp_path):
    t = Trip()
    cu, cb = t.closing()
    rep = run(tmp_path, t, w1=wallet(F.W1_TS, cu + 250.0, cb))                 # 250 USDT nobody can explain
    assert rep["status"] in ("INCOMPLETE", "DISPUTED")
    no_profit_claim(rep)
    assert abs(rep["ledger"]["residuals"]["usdt"] - 250.0) < 1e-6


def test_a_borrowed_coin_is_not_owned_inventory(tmp_path):
    t = Trip()
    cu, cb = t.closing()
    w1 = wallet(F.W1_TS, cu, cb)
    w1["result"]["list"][0]["coin"][1]["borrowAmount"] = "0.2"
    rep = run(tmp_path, t, w1=w1)
    assert rep["status"] == "INCOMPLETE" and "BORROWED_COIN" in flag_codes(rep)


def test_without_wallet_files_the_account_is_provisional_at_best(tmp_path):
    rep = run(tmp_path, with_wallets=False)
    assert rep["status"] == "PROVISIONAL" and "NO_BALANCE_ANCHOR" in flag_codes(rep)
    assert rep["layers"]["inventory_valuation"]["btc_value_usd"] is None


# ------------------------------------------------------------------------------------------------ file type, schema, truncation, timestamps

def test_truncated_files_are_refused_whole(tmp_path):
    t = Trip()
    pairs = lay_out(tmp_path, t)
    raw = open(tmp_path / "txlog.jsonl", "rb").read()
    open(tmp_path / "txlog.jsonl", "wb").write(raw[: len(raw) - 40])           # cut mid-line
    refused("TRUNCATED_OR_MALFORMED", pairs, marks=marks())
    sub = tmp_path / "w"
    sub.mkdir()
    pairs2 = lay_out(sub, t)
    wraw = open(sub / "wallet_close.json", "rb").read()
    open(sub / "wallet_close.json", "wb").write(wraw[: len(wraw) // 2])
    refused("TRUNCATED_OR_MALFORMED", pairs2, marks=marks())


def test_wrong_file_type_unverified_formats_and_unknown_shapes_are_refused(tmp_path):
    t = Trip()
    pairs = lay_out(tmp_path, t)
    swapped = [("linear_execution", dict(pairs)["transaction_log"])] + [p for p in pairs if p[0] != "linear_execution"]
    refused("WRONG_FILE_TYPE", swapped, marks=marks())
    csvp = tmp_path / "txlog_ui.csv"
    csvp.write_text("Uid,Currency,Contract,Type,Direction,Quantity\n1,USDT,BTCUSDT,TRADE,Sell,0.5\n")
    refused("UNVERIFIED_FORMAT", [("transaction_log", str(csvp))])
    classic = write_jsonl(tmp_path / "classic.jsonl", [{"tx_id": "1", "tx_time": "1790000000", "exec_type": "Funding", "wallet_balance": "1", "cash_flow": "1", "change": "1", "cashBalance": "1", "transactionTime": "1"}])
    refused("UNVERIFIED_FORMAT", [("transaction_log", classic)])
    junk = write_jsonl(tmp_path / "junk.jsonl", [{"a": 1, "b": 2}])
    refused("UNKNOWN_FILE_TYPE", [("transaction_log", junk)])
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    refused("UNKNOWN_FILE_TYPE", [("transaction_log", str(txt))])
    ethr = write_json(tmp_path / "wallet_wrong.json", {"retCode": 0, "result": {"list": [{"accountType": "CONTRACT", "coin": []}]}, "time": F.W0_TS})
    refused("UNVERIFIED_FORMAT", [("wallet_opening", ethr)])


def test_timestamps_in_seconds_iso_or_float_are_refused_not_converted(tmp_path):
    t = Trip()
    for bad in ("1790000000", "2026-10-01T00:30:00Z", "1790000000000.5", ""):
        sub = tmp_path / ("t" + str(abs(hash(bad))))
        sub.mkdir()
        ex = t.executions()
        ex[0]["execTime"] = bad
        with pytest.raises(OI.ImportRefused) as ei:
            OI.run_import(lay_out(sub, t, execs=ex), marks=marks())
        assert ei.value.code in ("TIMESTAMP", "SCHEMA")
    ok = tmp_path / "ok"
    ok.mkdir()
    assert run(ok, t)["status"] == "RECONCILED"


def test_unknown_columns_are_ignored_but_a_missing_documented_column_is_refused(tmp_path):
    t = Trip()
    ex = [dict(r, extraColumn="x") for r in t.executions()]
    assert run(tmp_path, t, execs=ex)["status"] == "RECONCILED"
    sub = tmp_path / "m"
    sub.mkdir()
    ex2 = t.executions()
    refused("UNKNOWN_FILE_TYPE", lay_out(sub, t, execs=[{k: v for k, v in r.items() if k != "execQty"} for r in t.executions()]), marks=marks())
    s3 = tmp_path / "n"
    s3.mkdir()
    ex3 = t.executions()
    del ex3[1]["isMaker"]
    refused("SCHEMA", lay_out(s3, t, execs=ex3), marks=marks())


# ------------------------------------------------------------------------------------------------ protected paths, outputs

def test_protected_input_and_output_names_are_refused(tmp_path):
    t = Trip()
    good = lay_out(tmp_path, t)
    for name in ("trading_state.jsonl", ".env.jsonl", "api_key.jsonl", "evidence.jsonl", "state.db"):
        p = write_jsonl(tmp_path / name, t.executions())
        refused("PROTECTED_PATH", [("linear_execution", p)])
    runs = tmp_path / "runs"
    runs.mkdir()
    refused("PROTECTED_PATH", [("linear_execution", write_jsonl(runs / "exec.jsonl", t.executions()))])
    st = tmp_path / "bot" / "state"
    st.mkdir(parents=True)
    refused("PROTECTED_PATH", [("linear_execution", write_jsonl(st / "exec.jsonl", t.executions()))])
    rep = OI.run_import(good, marks=marks())
    for bad_out in ("trading_state_report.json", ".env", "credentials.json"):
        with pytest.raises(OI.ImportRefused) as ei:
            OI.write_report(rep, str(tmp_path / bad_out))
        assert ei.value.code == "PROTECTED_PATH"
    # positive control: the ordinary names pass
    assert OI.write_report(rep, str(tmp_path / "report.json"))


def test_report_is_created_exclusively_0600_and_inputs_are_untouched(tmp_path):
    t = Trip()
    pairs = lay_out(tmp_path, t)
    before = {p: (open(p, "rb").read(), os.stat(p).st_mtime_ns, os.stat(p).st_mode) for _, p in pairs}
    rep = OI.run_import(pairs, marks=marks())
    out = tmp_path / "report.json"
    OI.write_report(rep, str(out), [p for _, p in pairs])
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert json.load(open(out))["import_id"] == rep["import_id"]
    first = open(out, "rb").read()
    with pytest.raises(OI.ImportRefused) as ei:
        OI.write_report(rep, str(out))
    assert ei.value.code == "OUTPUT_EXISTS" and open(out, "rb").read() == first
    with pytest.raises(OI.ImportRefused):
        OI.write_report(rep, pairs[0][1], [pairs[0][1]])                        # an input is never an output
    assert {p: (open(p, "rb").read(), os.stat(p).st_mtime_ns, os.stat(p).st_mode) for _, p in pairs} == before
    link = tmp_path / "link.json"
    os.symlink(str(tmp_path / "elsewhere.json"), link)
    with pytest.raises(OI.ImportRefused):
        OI.write_report(rep, str(link))                                         # a dangling symlink is not followed


def test_command_line_end_to_end(tmp_path, capsys):
    t = Trip()
    pairs = lay_out(tmp_path, t)
    mp = write_json(tmp_path / "marks_in.json", marks())
    args = []
    for r, p in pairs:
        args += ["--input", f"{r}={p}"]
    out = tmp_path / "rep.json"
    assert OI.main(args + ["--marks", mp, "--out", str(out)]) == 0
    assert json.load(open(out))["status"] == "RECONCILED"
    assert OI.main(args + ["--marks", mp, "--out", str(out)]) == 2             # never overwrites
    assert OI.main(["--input", f"transaction_log={tmp_path / 'nope.jsonl'}", "--out", str(tmp_path / "x.json")]) == 2


def test_the_status_of_every_report_in_this_module_is_in_the_vocabulary_and_never_verified_for_a_profit(tmp_path):
    t = Trip()
    reports = [run(tmp_path / "a") if (tmp_path / "a").mkdir() is None else None]
    for i, kw in enumerate(({"mk": []}, {"txlog": t.txlog(drop_settlement=0)}, {"txlog": t.txlog(funding_sign=-1)}, {"with_wallets": False})):
        sub = tmp_path / f"s{i}"
        sub.mkdir()
        reports.append(run(sub, t, **kw))
    for r in reports:
        assert r["status"] in OI.STATUSES
        assert r["status"] != "VERIFIED"
        for layer in r["layers"].values():
            assert layer["status"] in OI.STATUSES and layer["status"] != "VERIFIED"


def test_unknown_components_are_not_zero_so_a_partial_sum_is_never_the_headline(tmp_path):
    """Found in review: with no wallet files the report was PROVISIONAL yet headlined +476.275, silently omitting interest and every other cash line."""
    import owner_import as OI
    import fixtures_owner_import as F
    pairs = F.lay_out(tmp_path, F.Trip(), with_wallets=False)
    rep = OI.run_import(pairs)
    layer = rep["layers"]["realised_cash"]
    assert rep["headline_realised_cash_usd"] is None and layer["net_realised_cash_usd"] is None
    assert set(layer["unknown_components"]) == {"interest_usd", "other_cash_lines_usd"}
    assert isinstance(layer["partial_net_of_known_components_usd"], float)            # reported, labelled, never the headline


# ---------------------------------------------------------------- the headline is realised cash, said so, and is not the verdict's figure

def test_the_headline_is_named_and_labelled_realised_cash_and_the_economic_total_can_be_negative(tmp_path):
    rep = run(tmp_path, Trip())
    assert "headline_profit_usd" not in rep                                         # the misleading name is gone
    assert rep["headline_basis"] == OI.HEADLINE_BASIS == "realised_cash_only"
    assert "NOT total profit" in rep["headline_caveat"] and "economic_total" in rep["headline_caveat"]
    assert rep["headline_realised_cash_usd"] > 0 > rep["layers"]["economic_total"]["net_economic_pnl_usd"]     # the very case the old name hid
    assert "headline_realised_cash_usd" in rep["lineage"]["figures"] and "REALISED CASH ONLY" in rep["lineage"]["figures"]["headline_realised_cash_usd"]["formula"]


# ------------------------------------------------------------------------------------------------ Round 20: a negative balance with no stated borrow is an unknown loan, not a clean book

def _wallet_without_borrow_amount(ts, usdt, btc):
    w = wallet(ts, usdt, btc)
    for coin in w["result"]["list"][0]["coin"]:
        coin.pop("borrowAmount", None)
    return w


@pytest.mark.parametrize("borrow", ["absent", "empty", "zero"])
def test_a_negative_usdt_balance_without_a_stated_borrow_is_not_reconciled_and_states_no_profit(tmp_path, borrow):
    """REPRODUCED (Round 20, specialist C): opening USDT -5,000 with borrowAmount absent (or '') imported as RECONCILED with a +$476.28 headline and no flag, while the same file with borrowAmount
    '5000' was INCOMPLETE. On a unified account a negative balance IS a loan: its principal, accrued interest and collateral treatment are unknown, so no profit is stated."""
    t = Trip(usdt0=-5000.0)
    cu, cb = t.closing()
    w0, w1 = _wallet_without_borrow_amount(F.W0_TS, -5000.0, 1.0), _wallet_without_borrow_amount(F.W1_TS, cu, cb)
    if borrow == "empty":
        w0["result"]["list"][0]["coin"][0]["borrowAmount"] = ""
    elif borrow == "zero":
        w0["result"]["list"][0]["coin"][0]["borrowAmount"] = "0"
    rep = run(tmp_path, t, w0=w0, w1=w1)
    assert "BORROW_PRINCIPAL_UNSTATED" in flag_codes(rep, "GAP"), flag_codes(rep)
    no_profit_claim(rep)
    assert rep["status"] == "INCOMPLETE"


def test_a_clean_book_with_a_positive_balance_is_still_reconciled(tmp_path):
    """Control: the new flag never fires on a positive balance, so the clean round trip keeps its RECONCILED status and headline."""
    rep = run(tmp_path, Trip())
    assert "BORROW_PRINCIPAL_UNSTATED" not in flag_codes(rep) and rep["headline_realised_cash_usd"] is not None
