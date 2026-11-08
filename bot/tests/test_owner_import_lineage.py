"""Immutable evidence lineage in tools/owner_import.py: every figure walks raw record -> normalized record -> validation -> dedup -> reconciliation ->
uncertainty -> ledger -> owner P&L, and a number without provenance cannot leave ``_seal``.

SYNTHETIC fixtures (tests/fixtures_owner_import.py). Every expected figure is literal arithmetic written here; the source-row sums are recomputed by opening the
ORIGINAL files at the (sha256, row) a figure points to. Everything runs in tmp_path; nothing opens bot/state, runs/, a *.db or a credential.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, os.path.join(BOT, "tests"))
sys.path.insert(0, BOT)

import account_review as AR  # noqa: E402
import owner_import as OI  # noqa: E402
import fixtures_owner_import as F  # noqa: E402
from fixtures_owner_import import Trip, lay_out, marks, wallet, with_interest, write_jsonl  # noqa: E402

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


def close(a, b, tol=1e-6):
    assert abs(a - b) <= tol, f"{a} != {b}"


def go(tmp_path, trip=None, *, mk="default", **kw):
    trip = trip or Trip()
    pairs = lay_out(tmp_path, trip, **{k: v for k, v in kw.items() if k in ("execs", "txlog", "w0", "w1", "with_wallets", "spot_exec")})
    rest = {k: v for k, v in kw.items() if k not in ("execs", "txlog", "w0", "w1", "with_wallets", "spot_exec")}
    return OI.run_import(pairs, marks=marks() if mk == "default" else mk, **rest), pairs


def files_by_sha(pairs):
    return {hashlib.sha256(open(p, "rb").read()).hexdigest(): p for _, p in pairs}


def raw_row(files, src):
    """Open the ORIGINAL file at the pointer a figure carries and return the row."""
    lines = [ln for ln in open(files[src["sha256"]], encoding="utf-8").read().split("\n") if ln.strip()]
    return json.loads(lines[src["row"] - 1])


def fig(rep, path):
    return rep["lineage"]["figures"][path]


def no_headline(rep):
    assert rep["headline_realised_cash_usd"] is None
    assert rep["status"] not in ("RECONCILED", "VERIFIED")
    for layer in rep["layers"].values():
        assert layer["status"] not in ("RECONCILED", "VERIFIED")


# ------------------------------------------------------------------------------------------------ structure and hand-computed sums

def test_every_figure_lists_its_source_rows_and_the_sum_over_them_is_the_figure(tmp_path):
    t = Trip()
    tx = with_interest(t.txlog(), F.T0 + 10 * F.HOUR, -1.25)
    cu, cb = t.closing()
    rep, pairs = go(tmp_path, t, txlog=tx, w1=wallet(F.W1_TS, cu - 1.25, cb))
    files = files_by_sha(pairs)
    assert rep["status"] == "RECONCILED"
    assert rep["lineage"]["chain"] == ["raw record", "normalized record", "validation", "dedup", "reconciliation", "uncertainty", "ledger", "owner P&L"]
    # by hand: 500.00 realised; fees 16.50 + 16.225; 3 x 3.00 funding; interest -1.25
    pre = "layers.realised_cash.figures."
    p = fig(rep, pre + "perp_fees_usd")
    assert p["value"] == pytest.approx(-(16.5 + 16.225))
    assert sorted(s["record_id"] for s in p["sources"]) == ["exec:x1", "exec:x2"]
    close(-sum(float(raw_row(files, s)["execFee"]) for s in p["sources"]), -32.725)         # the figure IS the sum of the listed original rows
    assert all(raw_row(files, s)["execId"] == s["record_id"][len("exec:"):] for s in p["sources"])
    f = fig(rep, pre + "funding_net_usd")
    assert len(f["sources"]) == 3 and f["value"] == pytest.approx(3 * 3.0)
    close(sum(float(raw_row(files, s)["funding"]) for s in f["sources"]), 9.0)
    assert all(raw_row(files, s)["type"] == "SETTLEMENT" for s in f["sources"])
    i = fig(rep, pre + "interest_usd")
    assert [s["record_id"].split("|")[0] for s in i["sources"]] == ["txlog:900_INTEREST_%d" % (F.T0 + 10 * F.HOUR)]
    close(i["value"], -1.25)
    close(sum(float(raw_row(files, s)["change"]) for s in i["sources"]), -1.25)
    r = fig(rep, pre + "perp_realised_usd")
    close(r["value"], 500.0)
    close(sum(float(raw_row(files, s)["execQty"]) * float(raw_row(files, s)["execPrice"]) * (1 if raw_row(files, s)["side"] == "Sell" else -1) for s in r["sources"]),
          0.5 * 60_000.0 - 0.5 * 59_000.0)
    o = fig(rep, pre + "other_cash_lines_usd")
    assert o["value"] == 0.0 and o["empty_set"] is True and o["sources"] == []        # an empty set, said so; not a number from nowhere
    net = fig(rep, "layers.realised_cash.net_realised_cash_usd")
    close(net["value"], 500.0 - 32.725 + 9.0 - 1.25 + 0.0)                              # 475.025
    close(net["source_sum"], 475.025)
    assert net["source_check"] == "MATCH" and {s["record_id"] for s in net["sources"]} >= {s["record_id"] for s in p["sources"] + f["sources"] + i["sources"]}
    h = fig(rep, "headline_realised_cash_usd")
    close(h["value"], 475.025)
    assert h["derived_from"] == ["layers.realised_cash.net_realised_cash_usd"] and h["source_ids"] == net["source_ids"]
    for path, e in rep["lineage"]["figures"].items():
        assert e["basis"] in OI.BASES and e["nature"] in ("realized", "unrealized", "estimated"), path
        assert e["source_check"] in ("MATCH", "NOT_A_ROW_SUM", "WITHHELD"), (path, e["source_check"])
        assert len(e["provenance_digest"]) == 64
        if e["value"] is not None:
            assert e["sources"] or e.get("empty_set"), path
    # the owner's own cash identity: 10,000 + 475.025 = the closing USDT the wallet shows
    close(cu - 1.25, 10_000.0 + 475.025 - 0.0 - 0.0 + (0.0))


def test_stage_counts_and_the_digest_are_reproducible(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    r1, _ = go(a)
    r2, _ = go(b)
    assert r1["lineage"]["digest"] == r2["lineage"]["digest"] and len(r1["lineage"]["digest"]) == 64
    st = r1["lineage"]["stages"]
    # 2 executions + (2 trades + 3 settlements) log rows + 2 wallet envelopes = 9 read; the 7 row records are what the ledger consumes
    assert st["read"]["linear_execution"] == 2 and st["read"]["transaction_log"] == 5 and st["read"]["total"] == 9
    assert st["normalized"]["total"] == 9 and st["rejected"]["count"] == 0
    assert st["duplicate_dropped"]["count"] == 0 and st["conflicting"]["count"] == 0
    assert st["kept_records"]["total"] == 7
    assert st["reconciled"]["count"] == 7 and st["unreconciled"]["count"] == 0
    assert st["uncertainty"]["hard_flags"] == [] and "no exchange authentication" in st["uncertainty"]["unchecked"][-1]
    assert st["figures"]["total"] == st["figures"]["numeric"] + st["figures"]["null_with_reason"]


def test_overlapping_file_is_counted_in_the_stage_ledger(tmp_path):
    t = Trip()
    pairs = lay_out(tmp_path, t)
    second = write_jsonl(tmp_path / "exec_overlap.jsonl", t.executions())
    rep = OI.run_import(pairs + [("linear_execution", second)], marks=marks())
    st = rep["lineage"]["stages"]
    assert st["read"]["linear_execution"] == 4 and st["duplicate_dropped"]["count"] == 2 and st["duplicate_dropped"]["identical_within_one_file"] == 0
    assert st["kept_records"]["linear_execution"] == 2 and rep["status"] == "RECONCILED"
    assert [s["record_id"] for s in fig(rep, "layers.realised_cash.figures.perp_fees_usd")["sources"]] == ["exec:x1", "exec:x2"]       # counted once


# ------------------------------------------------------------------------------------------------ one row changes the lineage

def _digest_pack(rep):
    f = rep["lineage"]["figures"]
    return rep["lineage"]["digest"], {k: v["provenance_digest"] for k, v in f.items()}


def test_altering_deleting_or_adding_one_input_row_changes_the_lineage_digest_and_the_affected_figure(tmp_path):
    t = Trip()
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base, _ = go(base_dir, t)
    d0, f0 = _digest_pack(base)
    pf, pfund = "layers.realised_cash.figures.perp_fees_usd", "layers.realised_cash.figures.funding_net_usd"

    # ALTER one execution's fee 16.50 -> 16.60 (0.6 %: inside the fee-rate tolerance, so the import completes and the lineage must show it)
    ex = t.executions()
    ex[0]["execFee"] = repr(16.6)
    alt_dir = tmp_path / "alt"
    alt_dir.mkdir()
    alt, _ = go(alt_dir, t, execs=ex)
    d1, f1 = _digest_pack(alt)
    assert d1 != d0
    assert f1[pf] != f0[pf]                                              # the fee figure's provenance moved ...
    assert f1[pfund] == f0[pfund]                                        # ... the funding figure, which never read that row, did not
    close(alt["lineage"]["figures"]["layers.realised_cash.figures.perp_fees_usd"]["value"], -(16.6 + 16.225))
    assert alt["status"] != "RECONCILED"                                 # the altered fee no longer agrees with the statement

    # DELETE the last statement row (a funding settlement)
    tx = t.txlog()
    del_dir = tmp_path / "del"
    del_dir.mkdir()
    gone, _ = go(del_dir, t, txlog=[r for r in tx if r["id"] != next(x["id"] for x in tx if x["type"] == "SETTLEMENT")])
    d2, f2 = _digest_pack(gone)
    assert d2 != d0 and f2[pfund] != f0[pfund]
    assert len(fig(gone, "layers.realised_cash.figures." + "funding_net_usd")["sources"]) == 2
    close(fig(gone, "layers.realised_cash.figures.funding_net_usd")["value"], 2 * 3.0)
    assert gone["status"] != "RECONCILED" and gone["headline_realised_cash_usd"] is None

    # ADD one execution at the end of the file
    extra = t.executions() + [F.exec_row(9, "Buy", 0.1, 59_000.0, 0.1 * 59_000.0 * 0.00055, F.CLOSE_TS + 9000)]
    add_dir = tmp_path / "add"
    add_dir.mkdir()
    more, _ = go(add_dir, t, execs=extra)
    d3, f3 = _digest_pack(more)
    assert d3 != d0 and f3[pf] != f0[pf] and f3[pfund] == f0[pfund]
    assert "exec:x9" in fig(more, "layers.realised_cash.figures.perp_fees_usd")["source_ids"]
    assert more["status"] != "RECONCILED"
    # control: the same input again is bit-identical
    same_dir = tmp_path / "same"
    same_dir.mkdir()
    again, _ = go(same_dir, t)
    assert _digest_pack(again) == (d0, f0)


# ------------------------------------------------------------------------------------------------ the invariant

def _sealed(tmp_path):
    rep, _ = go(tmp_path)
    return rep


def test_a_figure_without_a_lineage_entry_cannot_be_sealed(tmp_path):
    rep = _sealed(tmp_path)
    broken = copy.deepcopy(rep)
    del broken["lineage"]["figures"]["layers.realised_cash.figures.perp_fees_usd"]
    with pytest.raises(AssertionError, match="figure without lineage"):
        OI._seal(broken)
    # a number slipped into the report body with no entry at all
    smuggled = copy.deepcopy(rep)
    smuggled["layers"]["realised_cash"]["figures"]["sneaky_usd"] = 123.0
    with pytest.raises(AssertionError, match="figure without lineage"):
        OI._seal(smuggled)
    # an entry that claims a value but has neither rows, nor source figures, nor an explicit empty set
    hollow = copy.deepcopy(rep)
    e = hollow["lineage"]["figures"]["layers.realised_cash.figures.perp_fees_usd"]
    e["sources"] = []
    with pytest.raises(AssertionError, match="no source row"):
        OI._seal(hollow)
    # a basis label outside the vocabulary
    labelled = copy.deepcopy(rep)
    labelled["lineage"]["figures"]["headline_realised_cash_usd"]["basis"] = "trust-me"
    with pytest.raises(AssertionError):
        OI._seal(labelled)
    assert OI._seal(copy.deepcopy(rep))["lineage"]["digest"] == rep["lineage"]["digest"]      # the honest report seals to itself


def test_a_withheld_figure_is_an_explicit_null_with_a_reason_never_zero(tmp_path):
    t = Trip()
    rep, _ = go(tmp_path, t, with_wallets=False)
    assert rep["status"] == "PROVISIONAL" and rep["headline_realised_cash_usd"] is None
    i = fig(rep, "layers.realised_cash.figures.interest_usd")
    assert i["value"] is None and i["why"].startswith("UNKNOWN, not zero") and i["sources"] == []
    h = fig(rep, "headline_realised_cash_usd")
    assert h["value"] is None and h["why"]
    # what IS known is still sourced, and labelled provisional: 500 - 32.725 + 9 = 476.275 is a partial, not the net
    pn = fig(rep, "layers.realised_cash.partial_net_of_known_components_usd")
    close(pn["value"], 500.0 - 32.725 + 9.0)
    assert pn["basis"] == "incomplete" and pn["source_check"] == "MATCH"
    assert fig(rep, "layers.realised_cash.figures.funding_net_usd")["basis"] == "provisional"
    # the unvalued inventory is null with a reason as well
    inv, _ = go(tmp_path / "m", t, mk=[]) if (tmp_path / "m").mkdir() is None else (None, None)
    assert inv["layers"]["inventory_valuation"]["btc_value_usd"] is None
    e = fig(inv, "layers.inventory_valuation.btc_value_usd")
    assert e["value"] is None and "INVENTORY_UNVALUED" in e["why"]
    no_headline(inv)


def test_basis_labels_separate_realized_unrealized_estimated_provisional_reconciled_incomplete(tmp_path):
    t = Trip()
    rep, _ = go(tmp_path, t)
    assert fig(rep, "layers.realised_cash.figures.perp_fees_usd")["basis"] == "reconciled"
    assert fig(rep, "layers.realised_cash.figures.perp_fees_usd")["nature"] == "realized"
    assert fig(rep, "layers.inventory_valuation.btc_value_usd")["basis"] == "estimated"
    assert fig(rep, "layers.economic_total.net_economic_pnl_usd")["basis"] == "estimated"
    close(fig(rep, "layers.inventory_valuation.btc_value_usd")["value"], 59_000.0)
    assert fig(rep, "layers.inventory_valuation.btc_value_usd")["source_check"] == "MATCH"
    assert fig(rep, "layers.inventory_valuation.perp_unrealised_usd")["nature"] == "unrealized"
    assert fig(rep, "layers.inventory_valuation.perp_unrealised_usd")["basis"] == "unrealized"
    close(fig(rep, "layers.economic_total.net_economic_pnl_usd")["value"], 476.275 - 1_000.0)
    d = tmp_path / "np"
    d.mkdir()
    prov, _ = go(d, t, with_wallets=False)
    assert fig(prov, "layers.realised_cash.figures.perp_fees_usd")["basis"] == "provisional"
    d = tmp_path / "inc"
    d.mkdir()
    cu, cb = t.closing()
    inc, _ = go(d, t, w1=wallet(F.W1_TS, cu + 250.0, cb))
    assert inc["status"] in ("INCOMPLETE", "DISPUTED")
    assert fig(inc, "layers.realised_cash.figures.perp_fees_usd")["basis"] == "incomplete"
    assert fig(inc, "headline_realised_cash_usd")["value"] is None
    assert set(OI.BASES) == {"realized", "unrealized", "estimated", "provisional", "reconciled", "incomplete"}


def test_a_disputed_report_still_carries_lineage_and_a_null_headline_with_its_reason(tmp_path):
    t = Trip()
    pairs = lay_out(tmp_path, t)
    ex = t.executions()
    ex2 = [dict(r) for r in ex]
    ex2[0]["execQty"] = "0.4"
    ex2[0]["execValue"] = repr(0.4 * 60_000.0)
    second = write_jsonl(tmp_path / "exec_other.jsonl", ex2)
    rep = OI.run_import(pairs + [("linear_execution", second)], marks=marks())
    assert rep["status"] == "DISPUTED" and rep["headline_realised_cash_usd"] is None
    assert rep["lineage"]["stages"]["conflicting"]["count"] == 1 and rep["lineage"]["stages"]["conflicting"]["ids"] == ["x1"]
    assert rep["lineage"]["stages"]["reconciled"]["count"] == 0
    h = fig(rep, "headline_realised_cash_usd")
    assert h["value"] is None and h["why"] and len(rep["lineage"]["digest"]) == 64


# ------------------------------------------------------------------------------------------------ hostile owner exports

def test_hostile_truncated_export_missing_its_last_page(tmp_path):
    t = Trip()
    tx, ex = t.txlog(), t.executions()
    # (a) the statement lost its last page (the buy-back and its cash) but the executions and both wallets are whole
    a = tmp_path / "a"
    a.mkdir()
    rep, _ = go(a, t, txlog=tx[:-1])
    assert rep["status"] == "DISPUTED" and rep["headline_realised_cash_usd"] is None
    codes = {f["code"] for f in rep["flags"]}
    assert {"POSITION_MISMATCH", "FEE_TOTALS_DIFFER"} <= codes
    no_headline(rep)
    # (b) executions AND statement both cut before the buy-back, with the perpetual marked: the closing wallet (which saw the buy-back) cannot be explained
    b = tmp_path / "b"
    b.mkdir()
    mk = marks() + [{"instrument": "BTCUSDT_PERP", "ts_ms": F.W0_TS, "price": 60_000.0, "source": "owner test mark"},
                    {"instrument": "BTCUSDT_PERP", "ts_ms": F.W1_TS, "price": 59_000.0, "source": "owner test mark"}]
    rep, _ = go(b, t, txlog=tx[:-1], execs=ex[:-1], mk=mk)
    assert rep["status"] in ("INCOMPLETE", "DISPUTED") and rep["status"] != "RECONCILED"
    no_headline(rep)
    assert abs(rep["ledger"]["residuals"]["usdt"]) > 1.0          # the missing page shows as an unexplained closing balance, not as a smaller profit
    # (c) both files cut and no perpetual mark: unvalued, never a headline
    c = tmp_path / "c"
    c.mkdir()
    rep, _ = go(c, t, txlog=tx[:-1], execs=ex[:-1])
    assert rep["status"] != "RECONCILED" and rep["headline_realised_cash_usd"] is None
    assert {"INVENTORY_UNVALUED"} <= {f["code"] for f in rep["flags"]}
    # (d) a statement cut mid-row is refused whole
    d = tmp_path / "d"
    d.mkdir()
    pairs = lay_out(d, t)
    raw = open(pairs[1][1], "rb").read()
    open(pairs[1][1], "wb").write(raw[: len(raw) // 2])
    with pytest.raises(OI.ImportRefused) as ei:
        OI.run_import(pairs, marks=marks())
    assert ei.value.code == "TRUNCATED_OR_MALFORMED"


def test_hostile_missing_interest_rows(tmp_path):
    t = Trip()
    cu, cb = t.closing()
    # the wallet is after 1.25 USDT of interest, the statement export dropped the interest line
    rep, _ = go(tmp_path, t, w1=wallet(F.W1_TS, cu - 1.25, cb))
    assert rep["status"] == "INCOMPLETE" and rep["headline_realised_cash_usd"] is None
    assert "STATEMENT_NOT_ANCHORED" in {f["code"] for f in rep["flags"]}
    assert rep["ledger"]["residuals"]["usdt"] == pytest.approx(-1.25)       # the 1.25 shows up as a residual, in the open
    no_headline(rep)
    # control: the interest line present reconciles to 476.275 - 1.25
    d = tmp_path / "ok"
    d.mkdir()
    ok, _ = go(d, t, txlog=with_interest(t.txlog(), F.T0 + 10 * F.HOUR, -1.25), w1=wallet(F.W1_TS, cu - 1.25, cb))
    assert ok["status"] == "RECONCILED"
    close(ok["headline_realised_cash_usd"], 476.275 - 1.25)


def test_hostile_duplicated_page_inside_one_file_lowers_the_status(tmp_path):
    t = Trip()
    tx = t.txlog()
    rep, _ = go(tmp_path, t, txlog=tx + tx[:2])                # the first page appears twice in the same file
    assert rep["status"] == "INCOMPLETE" and rep["headline_realised_cash_usd"] is None
    assert "DUPLICATE_PAGE_IN_FILE" in {f["code"] for f in rep["flags"]}
    assert rep["lineage"]["stages"]["duplicate_dropped"]["identical_within_one_file"] == 2
    # counted once, so the figures are NOT doubled
    close(rep["layers"]["realised_cash"]["figures"]["perp_fees_usd"], -32.725)
    no_headline(rep)
    d = tmp_path / "ex"
    d.mkdir()
    ex = t.executions()
    rep, _ = go(d, t, execs=ex + ex)
    assert rep["status"] == "INCOMPLETE" and rep["headline_realised_cash_usd"] is None
    close(rep["layers"]["realised_cash"]["figures"]["perp_realised_usd"], 500.0)


def test_hostile_row_from_another_symbol_is_refused_by_name(tmp_path):
    t = Trip()
    ex = t.executions()
    ex.append(F.exec_row(7, "Sell", 1.0, 3_000.0, 1.0 * 3_000.0 * 0.00055, F.OPEN_TS + 5000, symbol="ETHUSDT"))
    pairs = lay_out(tmp_path, t, execs=ex)
    with pytest.raises(OI.ImportRefused) as ei:
        OI.run_import(pairs, marks=marks())
    assert ei.value.code == "SYMBOL_MISMATCH" and "ETHUSDT" in str(ei.value)
    d = tmp_path / "tx"
    d.mkdir()
    tx = t.txlog()
    tx[2] = dict(tx[2], symbol="ETHUSDT")
    with pytest.raises(OI.ImportRefused) as ei:
        OI.run_import(lay_out(d, t, txlog=tx), marks=marks())
    assert ei.value.code == "SYMBOL_MISMATCH"


def test_hostile_funding_row_times_ten_is_disputed_with_no_headline(tmp_path):
    t = Trip()
    tx = t.txlog()
    for r in tx:
        if r["type"] == "SETTLEMENT":
            r["funding"] = repr(float(r["funding"]) * 10)
            r["change"] = r["funding"]
    rep, _ = go(tmp_path, t, txlog=tx)
    assert rep["status"] == "DISPUTED" and "FUNDING_MAGNITUDE_IMPLAUSIBLE" in {f["code"] for f in rep["flags"] if f["severity"] == "HARD"}
    no_headline(rep)
    # the x10 figure is still traceable (3 x 30.00 = 90) but is labelled incomplete, never reconciled
    e = fig(rep, "layers.realised_cash.figures.funding_net_usd")
    close(e["value"], 90.0)
    assert e["basis"] == "incomplete" and len(e["sources"]) == 3
    assert fig(rep, "headline_realised_cash_usd")["value"] is None


def test_hostile_fee_in_the_wrong_currency_is_refused_by_name(tmp_path):
    t = Trip()
    ex = t.executions()
    ex[0] = dict(ex[0], feeCurrency="BTC", execFee=repr(0.5 * 60_000.0 * 0.00055 / 60_000.0))        # 0.000275 BTC, not 16.50 USDT
    with pytest.raises(OI.ImportRefused) as ei:
        OI.run_import(lay_out(tmp_path, t, execs=ex), marks=marks())
    assert ei.value.code == "UNIT" and "feeCurrency" in str(ei.value)
    # the statement side: a linear TRADE row booked in BTC is not summed as dollars either
    d = tmp_path / "tx"
    d.mkdir()
    tx = t.txlog()
    k = next(i for i, r in enumerate(tx) if r["type"] == "TRADE")
    tx[k] = dict(tx[k], currency="BTC")
    with pytest.raises(OI.ImportRefused):
        OI.run_import(lay_out(d, t, txlog=tx), marks=marks())


def test_every_hostile_variant_leaves_no_headline_and_a_sealed_lineage(tmp_path):
    t = Trip()
    tx, ex = t.txlog(), t.executions()
    cases = {
        "tail": dict(txlog=tx[:-1]),
        "dup": dict(txlog=tx + tx[:1]),
        "orphan_fee": dict(execs=[dict(ex[0], execFee=repr(float(ex[0]["execFee"]) * 1.5))] + ex[1:]),
    }
    for name, kw in cases.items():
        d = tmp_path / name
        d.mkdir()
        try:
            rep, _ = go(d, t, **kw)
        except OI.ImportRefused:
            continue
        assert rep["status"] != "RECONCILED" and rep["headline_realised_cash_usd"] is None, name
        assert len(rep["lineage"]["digest"]) == 64, name
        for path, e in rep["lineage"]["figures"].items():
            assert e["value"] is None and e.get("why") or e["sources"] or e.get("empty_set"), (name, path)


def test_a_change_that_leaves_every_figure_value_alone_still_moves_the_row_and_figure_provenance(tmp_path):
    """Provenance is content-addressed, not value-addressed: editing a field no figure reads (orderId) keeps every number but changes that row's digest, the
    provenance of the figures that cite the row, and the lineage digest; the figure the row does NOT feed is unchanged."""
    t = Trip()
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    base, _ = go(a, t)
    ex = t.executions()
    ex[1]["orderId"] = "edited-after-export"
    alt, _ = go(b, t, execs=ex)
    assert alt["status"] == base["status"] == "RECONCILED" and alt["headline_realised_cash_usd"] == base["headline_realised_cash_usd"]
    pf = "layers.realised_cash.figures.perp_fees_usd"
    assert fig(alt, pf)["value"] == fig(base, pf)["value"]
    assert fig(alt, pf)["provenance_digest"] != fig(base, pf)["provenance_digest"]
    assert [s["row_sha256"] for s in fig(alt, pf)["sources"]][0] == [s["row_sha256"] for s in fig(base, pf)["sources"]][0]      # row 1 untouched
    assert [s["row_sha256"] for s in fig(alt, pf)["sources"]][1] != [s["row_sha256"] for s in fig(base, pf)["sources"]][1]      # row 2 edited
    fu = "layers.realised_cash.figures.funding_net_usd"
    assert fig(alt, fu)["provenance_digest"] == fig(base, fu)["provenance_digest"]
    assert alt["lineage"]["digest"] != base["lineage"]["digest"]
