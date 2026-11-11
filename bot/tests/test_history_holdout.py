"""A holdout is a promise about ignorance: nothing may have read a region of time before the one test that is allowed to.

``tools/reserved_holdout.py`` already records reads of the old Binance daily corpus and proved the point (slice 57 declared an "out-of-sample" window that a full-sample run had read). The
cross-sectional study reads a DIFFERENT store (a market_history root through ``candidate_readiness.load_history_root``), which that ledger never saw.

``tools/history_holdout.py`` closes that for the history path, and an independent review of its first version (state keyed by ledger directory, by mark interval, reservable after an
unrecorded read, a fail-open library default, a late listing that aborted every load, six surviving mutants) shaped this file: the registry is keyed by VENUE and TIME, one per user; a
recorded read burns the time it covered; an evaluator's registry argument is required and the only opt-out is the explicit ``UNGUARDED``; the loader's cut is tested through every entry point.

Every dataset here is the repo's own real-writer fixture; no number is an exchange observation.
"""
from __future__ import annotations

import ast
import json
import multiprocessing
import os
import shutil
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_competition as CC_  # noqa: E402
import candidate_readiness as CR  # noqa: E402
import evidence_chain as EC  # noqa: E402
import history_holdout as HH  # noqa: E402
import market_history as MH  # noqa: E402
import regime_entry_eval as RE  # noqa: E402
import research_harness as RH  # noqa: E402
import test_ms_pipeline_cli as TPC  # noqa: E402
from test_candidate_readiness import MARK_IV, TMH, _hist, offline  # noqa: E402,F401

DAY = 86_400_000
H4 = TMH.H8 // 2
VENUE = "api.bybit.com|linear"


@pytest.fixture()
def world(tmp_path, offline, monkeypatch):
    root, uni, spot = _hist(tmp_path)
    reg = str(tmp_path / "registry" / "history_holdout.jsonl")
    monkeypatch.setenv(HH.REGISTRY_ENV, reg)                 # the CLIs resolve the per-user registry from the environment
    return {"root": root, "uni": uni, "spot": spot, "reg": reg, "ldir": str(tmp_path / "ledgers"), "tmp": tmp_path}


def _rows(reg):
    return list(EC.rows_of(EC.read_lines(reg)[0])) if os.path.exists(reg) else []


def _window(w):
    req = json.load(open(os.path.join(w["root"], "funding", "manifest.json")))["request"]
    return req["start_ms"], req["end_ms"]


def _load(w, **kw):
    kw.setdefault("ledger_path", w["reg"])
    return HH.load(w["root"], universe_dir=w["uni"], spot_dir=w["spot"], mark_interval=MARK_IV, **kw)


def _reserve(w, **kw):
    kw.setdefault("ledger_dir", w["ldir"])
    return HH.reserve(w["reg"], w["root"], mark_interval=MARK_IV, **kw)


def _final(w):
    return HH.final_read(w["root"], universe_dir=w["uni"], spot_dir=w["spot"], mark_interval=MARK_IV, ledger_path=w["reg"])


def _last_ts(series):
    return max(max(ts for ts, _ in s.funding) for s in series), max(max(ts for ts, _ in s.mark) for s in series)


# ------------------------------------------------------------------ the cut itself
def test_the_cut_is_the_utc_day_boundary_at_seventy_percent_of_the_window():
    a, b = 1_700_000_000_000, 1_700_000_000_000 + 1000 * DAY
    cut = HH.cut_for(a, b, 0.30)
    assert cut % DAY == 0 and a < cut < b
    assert abs((cut - a) - 0.70 * (b - a)) < DAY
    assert HH.cut_for(a, b, 0.30) == cut                                  # deterministic: no clock, no randomness


@pytest.mark.parametrize("frac", [0.0, 0.19, 0.51, 1.0, 1e-9, float("nan"), True, "0.3"])
def test_a_reserved_fraction_outside_the_floor_and_the_ceiling_is_refused(frac):
    with pytest.raises(RH.HarnessError, match="reserved fraction"):
        HH.cut_for(0, 100 * DAY, frac)


@pytest.mark.parametrize("frac", [0.20, 0.30, 0.50])
def test_the_bounds_themselves_are_accepted(frac):
    assert HH.cut_for(0, 100 * DAY, frac) > 0


def test_a_window_too_short_to_leave_anything_to_explore_is_refused():
    with pytest.raises(RH.HarnessError, match="WINDOW_TOO_SHORT"):
        HH.cut_for(0, DAY // 2, 0.30)


# ------------------------------------------------------------------ nothing is readable before it is reserved
def test_an_evaluation_read_without_a_reservation_is_refused_with_the_exact_command(world):
    with pytest.raises(RH.HarnessError) as e:
        _load(world)
    msg = str(e.value)
    assert "HOLDOUT_NOT_RESERVED" in msg and "tools/history_holdout.py --reserve" in msg and "--unreserved-exploratory" in msg and world["reg"] in msg
    assert not os.path.exists(world["reg"]), "a refused read must not write anything"


def test_the_registry_argument_is_required_and_the_only_opt_out_is_the_explicit_sentinel(world):
    with pytest.raises(RH.HarnessError, match="HOLDOUT_LEDGER_REQUIRED"):
        HH.load(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_path=None)
    with pytest.raises(TypeError):                                        # a caller that forgets the argument cannot silently read the whole window
        CR.evaluate_cross_sectional(world["root"], universe_dir=world["uni"], ledger_path=str(world["tmp"] / "l.jsonl"), spot_dir=world["spot"], mark_interval=MARK_IV)
    series, info = _load(world, ledger_path=HH.UNGUARDED)
    h = info["holdout"]
    assert h["status"] == "NOT_GUARDED" and "not a holdout study" in h["label"]
    full, _ = CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV)
    assert _last_ts(series) == _last_ts(full)
    assert not os.path.exists(world["reg"]), "the explicit opt-out records nothing: it is for tests and embeddings, and a test forbids it in shipped code"


def test_the_reservation_binds_the_last_thirty_percent_of_the_funding_window_of_a_venue(world):
    r = _reserve(world)
    a, b = _window(world)
    assert r["kind"] == "reserve" and r["window_ms"] == [a, b] and r["from_ms"] == HH.cut_for(a, b, 0.30) and r["frac"] == 0.30 and r["from_ms"] % DAY == 0
    assert r["venue"] == VENUE and r["mark_dataset"] == "mark-kline-" + MARK_IV and r["reserved_days"] > 0
    rows = _rows(world["reg"])
    assert len(rows) == 1 and EC.verify(world["reg"])["ok"]


def test_reserving_twice_is_idempotent_and_never_moves_the_boundary(world):
    r1 = _reserve(world)
    r2 = _reserve(world)
    assert r2["from_ms"] == r1["from_ms"] and r2.get("already_reserved") is True
    assert len(_rows(world["reg"])) == 1
    r3 = _reserve(world, frac=0.50)
    assert r3["from_ms"] == r1["from_ms"] and r3["frac"] == 0.30 and r3["requested_fraction_ignored"] == 0.50


def test_a_mark_interval_that_does_not_exist_is_not_a_window(world):
    """'60' names no dataset in this root: the first version accepted it as a different dataset id and let the same funding window be reserved (and read) twice."""
    with pytest.raises(RH.HarnessError):
        HH.reserve(world["reg"], world["root"], mark_interval="60", ledger_dir=world["ldir"])
    assert not os.path.exists(world["reg"])


def test_the_boundary_in_force_is_the_ledgered_one_not_a_recomputed_one(world):
    """Data grows (the recorder keeps recording): a recomputed 70% would drift later and quietly hand the explorer yesterday's holdout. The registry row is the boundary."""
    r = _reserve(world)
    shifted = dict(_rows(world["reg"])[0], from_ms=r["from_ms"] - 20 * DAY)
    other = str(world["tmp"] / "other_registry.jsonl")
    EC.append(other, shifted)
    s, info = HH.load(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_path=other)
    assert info["holdout"]["cut_ms"] == r["from_ms"] - 20 * DAY
    assert _last_ts(s)[0] < r["from_ms"] - 20 * DAY


# ------------------------------------------------------------------ the evaluator never builds a row at or after the cut
def test_an_evaluation_load_returns_no_funding_or_mark_row_at_or_after_the_cut(world):
    full, _ = CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV)
    r = _reserve(world)
    cut = r["from_ms"]
    assert _last_ts(full)[0] >= cut and _last_ts(full)[1] >= cut, "the fixture must actually have rows in the reserved region or the test proves nothing"
    series, info = _load(world)
    f_last, m_last = _last_ts(series)
    assert f_last < cut and m_last < cut
    assert sum(len(s.funding) for s in series) < sum(len(s.funding) for s in full)
    h = info["holdout"]
    assert h["status"] == "RESERVED" and h["purpose"] == "EXPLORE" and h["cut_ms"] == cut and "never built" in h["note"] and h["venue"] == VENUE
    assert h["registry"] == world["reg"] and h["chain_head"]["n"] == len(_rows(world["reg"])), "every result stamps the registry and its chain head"
    assert h["reserved_rows_possible"] < 50 and "can REJECT" in h["warning"], "a reserved region this small must say it cannot support a verdict above INSUFFICIENT_DATA"
    assert info["history_window"]["to_ms"] == cut and info["funnel"]["window"]["to_ms"] == cut


def test_the_cut_flows_into_the_evaluation_result_and_each_family_carries_the_holdout_status(world):
    _reserve(world)
    out = CR.evaluate_cross_sectional(world["root"], universe_dir=world["uni"], ledger_path=str(world["tmp"] / "f.jsonl"), spot_dir=world["spot"], mark_interval=MARK_IV, holdout_ledger=world["reg"])
    assert out["input"]["holdout"]["status"] == "RESERVED" and out["input"]["history_window"]["to_ms"] == out["input"]["holdout"]["cut_ms"]
    assert out["holdout_status"] == "RESERVED" and all(f["holdout_status"] == "RESERVED" for f in out["families"].values())


def test_the_cut_must_leave_something_to_explore(world):
    start, _end = _window(world)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_CUT_BEFORE_WINDOW_START"):
        CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, cut_ms=start)


# ------------------------------------------------------------------ a late listing must not abort every explore load; the interval segments are clipped
def _late_listing_world(tmp_path, cut):
    """ETHUSDT is in the universe with an early launchTime but its first funding print and first mark candle fall AT the boundary (a late listing / PreLaunch->Trading switch)."""
    H8 = TMH.H8
    n_late = (TMH.BASE - cut) // H8 + 1
    delivery = TMH.BASE - 60 * H8
    syms = ("BTCUSDT", "ETHUSDT", "FASTUSDT", "OLDUSDT")
    root = tmp_path / "hist_late"
    os.mkdir(root, 0o700)
    uni = TMH._universe_dir(tmp_path, syms=syms, intervals={"FASTUSDT": 240}, bases={"FASTUSDT": "1000FAST"}, statuses={"OLDUSDT": "Closed"}, deliveries={"OLDUSDT": delivery})
    recs, info = MH.load_universe_dir(str(uni), include_statuses=["Trading", "Closed"])
    fdata = {("funding", "BTCUSDT"): TMH.grid(TMH.FIRST, TMH.N, H8), ("funding", "ETHUSDT"): TMH.grid(cut, n_late, H8),
             ("funding", "FASTUSDT"): TMH.grid(TMH.FIRST, 2 * TMH.N - 1, H4), ("funding", "OLDUSDT"): TMH.grid(TMH.FIRST, TMH.N - 60, H8)}
    TMH.run(root, MH.build_specs("funding", None, sorted(recs), TMH.START, TMH.END, recs), TMH.HV(fdata), universe=info)
    mdata = {("mark-kline", s): TMH.grid(TMH.FIRST, 2 * TMH.N - 1, H4) for s in syms if s not in ("OLDUSDT", "ETHUSDT")}
    mdata[("mark-kline", "ETHUSDT")] = TMH.grid(cut, (TMH.BASE - cut) // H4 + 1, H4)
    mdata[("mark-kline", "OLDUSDT")] = TMH.grid(TMH.FIRST, (delivery - TMH.FIRST) // H4 + 1, H4)
    TMH.run(root, MH.build_specs("mark-kline", MARK_IV, sorted(recs), TMH.START, TMH.BASE + 10, recs), TMH.HV(mdata), "mark-kline", MARK_IV, universe=info)
    res = MH.capture_spot_instruments(str(root), fetch=lambda u: TMH.spot_body([TMH.spot_row("BTC"), TMH.spot_row("ETH", "USDC"), TMH.spot_row("FAST"), TMH.spot_row("OLD", status="Delisted")]),
                                      clock=TMH.Clock(), sleep=lambda _s: None, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
    return str(root), str(uni), res["dir"]


def test_a_symbol_whose_first_row_is_at_the_boundary_is_dropped_and_listed_never_a_permanent_abort(tmp_path, offline):
    cut = HH.cut_for(TMH.START, TMH.END)
    root, uni, spot = _late_listing_world(tmp_path, cut)
    reg = str(tmp_path / "reg.jsonl")
    HH.reserve(reg, root, mark_interval=MARK_IV, ledger_dir=str(tmp_path / "led"))
    series, info = HH.load(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV, ledger_path=reg)
    assert "ETHUSDT" not in {s.symbol for s in series} and {"BTCUSDT", "FASTUSDT"} <= {s.symbol for s in series}
    assert info["holdout_dropped_no_rows_before_cut"] == ["ETHUSDT"]
    assert {"symbol": "ETHUSDT", "reason": "no_rows_before_the_holdout_cut"} in info["excluded"]
    assert any("late listings" in w for w in info["warnings"])
    everything, _ = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    assert "ETHUSDT" in {s.symbol for s in everything}, "control: the uncut load does include it"


def test_an_interval_change_inside_the_reserved_region_does_not_reach_the_explore_series(tmp_path, offline):
    """The 8h -> 4h regime change a funding cap produces is a fact about the reserved region; the segments the series carries end at the boundary."""
    cut = HH.cut_for(TMH.START, TMH.END)
    H8 = TMH.H8
    ts8 = TMH.grid(TMH.FIRST, ((cut + 10 * DAY) - TMH.FIRST) // H8 + 1, H8)
    ts4 = [ts8[-1] + (i + 1) * H4 for i in range((TMH.BASE - ts8[-1]) // H4)]
    root, uni, spot = _hist(tmp_path, funding_override={"BTCUSDT": ts8 + ts4})
    reg = str(tmp_path / "reg.jsonl")
    HH.reserve(reg, root, mark_interval=MARK_IV, ledger_dir=str(tmp_path / "led"))
    uncut, _ = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    assert any(seg[2] == 240 for seg in {s.symbol: s for s in uncut}["BTCUSDT"].funding_intervals), "the fixture must contain the regime change or the test proves nothing"
    series, _info = HH.load(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV, ledger_path=reg)
    segs = {s.symbol: s for s in series}["BTCUSDT"].funding_intervals
    assert segs and all(seg[1] < cut for seg in segs) and all(seg[2] == 480 for seg in segs) and segs[-1][1] == cut - 1


# ------------------------------------------------------------------ an exploratory read of unreserved history is stamped, recorded, and BURNS the time it read
def test_an_unreserved_exploratory_read_is_stamped_recorded_and_burns_the_time_it_covered(world):
    series, info = _load(world, unreserved_exploratory=True, reader="unit")
    h = info["holdout"]
    assert h["status"] == "UNRESERVED_EXPLORATORY" and "NOT_A_HOLDOUT_STUDY" in h["label"] and h["cut_ms"] is None
    full, _ = CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV)
    assert _last_ts(series) == _last_ts(full)                                     # it really did read everything: that is why it is recorded
    rows = _rows(world["reg"])
    assert [(r["kind"], r["purpose"], r["venue"]) for r in rows] == [("read", "UNRESERVED_EXPLORE", VENUE)]
    with pytest.raises(RH.HarnessError, match="ALREADY_READ") as e:
        _reserve(world)
    assert "must grow to at least" in str(e.value) and "unit" in str(e.value)


def test_the_same_history_reached_through_another_root_or_ledger_directory_is_the_same_history(world, tmp_path):
    """Review finding 2: the first version kept its state beside the ledger directory, so an unreserved read through one directory left another directory's reservation free."""
    _load(world, unreserved_exploratory=True, reader="a")
    copy = str(tmp_path / "copy_of_the_root")
    shutil.copytree(world["root"], copy)
    with pytest.raises(RH.HarnessError, match="ALREADY_READ"):
        HH.reserve(world["reg"], copy, mark_interval=MARK_IV, ledger_dir=str(tmp_path / "a_completely_different_ledger_dir"))
    assert HH.main(["--reserve", "--history-root", world["root"], "--mark-interval", MARK_IV, "--ledger-dir", str(tmp_path / "yet_another")]) == 2
    assert [r["kind"] for r in _rows(world["reg"])] == ["read"]


def test_a_reservation_after_a_stated_early_read_is_allowed_and_after_a_late_one_is_not(world):
    a, b = _window(world)
    cut = HH.cut_for(a, b)
    with pytest.raises(RH.HarnessError, match="ALREADY_READ"):
        _reserve(world, prior_reads_through_ms=cut + DAY)
    assert [r["purpose"] for r in _rows(world["reg"])] == ["PRIOR_STATED"], "the statement is recorded even when it makes the reservation impossible"
    reg2 = str(world["tmp"] / "reg2.jsonl")
    r = HH.reserve(reg2, world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"], prior_reads_through_ms=a + 10 * DAY)
    assert r["from_ms"] == cut and [x["purpose"] if x["kind"] == "read" else "reserve" for x in _rows(reg2)] == ["PRIOR_STATED", "reserve"]


def test_the_exploratory_escape_hatch_is_idempotent_per_reader(world):
    _load(world, unreserved_exploratory=True, reader="a")
    _load(world, unreserved_exploratory=True, reader="a")
    assert len(_rows(world["reg"])) == 1
    _load(world, unreserved_exploratory=True, reader="b")
    assert len(_rows(world["reg"])) == 2


def test_the_escape_hatch_never_downgrades_an_existing_reservation(world):
    r = _reserve(world)
    series, info = _load(world, unreserved_exploratory=True)
    assert info["holdout"]["status"] == "RESERVED" and _last_ts(series)[0] < r["from_ms"]
    assert [x["kind"] for x in _rows(world["reg"])] == ["reserve"]


def test_an_unreserved_read_whose_record_cannot_be_written_returns_no_data(world, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(EC, "append", boom)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_LEDGER_WRITE_FAILED"):
        _load(world, unreserved_exploratory=True, reader="x")


def test_after_the_region_is_spent_even_the_exploratory_flag_is_refused(world):
    _reserve(world)
    _final(world)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_SPENT"):
        _load(world, unreserved_exploratory=True, reader="late")


# ------------------------------------------------------------------ reads that left no row: the research ledgers beside the data are the evidence
def test_evaluations_that_left_ledger_rows_beside_the_data_block_a_reservation_until_the_owner_states_how_far_they_read(world):
    os.makedirs(world["ldir"])
    led = RH.VariantLedger("cross_sectional_funding", path=os.path.join(world["ldir"], "cross_sectional_funding.jsonl"))
    led.record_rejected("F3|H7|K1|Cbase", {"formation_days": 3}, "only 2 rows")
    with pytest.raises(RH.HarnessError, match="PRIOR_EVALUATION_DETECTED") as e:
        _reserve(world)
    assert "cross_sectional_funding.jsonl" in str(e.value) and "--prior-reads-through" in str(e.value)
    assert not os.path.exists(world["reg"])
    a, _b = _window(world)
    ok = _reserve(world, prior_reads_through_ms=a + DAY)
    assert ok["prior_evaluations"] == [{"file": "cross_sectional_funding.jsonl", "rows": 1}]
    assert [r["purpose"] if r["kind"] == "read" else "reserve" for r in _rows(world["reg"])] == ["PRIOR_STATED", "reserve"]


def test_a_library_run_that_read_the_whole_window_blocks_a_later_reservation(world):
    """Review finding 1: library mode read the whole window, left no registry row, and reserve then succeeded. The family ledger the run wrote is the evidence."""
    os.makedirs(world["ldir"])
    CR.evaluate_cross_sectional(world["root"], universe_dir=world["uni"], ledger_path=os.path.join(world["ldir"], "cross_sectional_funding.jsonl"), spot_dir=world["spot"], mark_interval=MARK_IV,
                                holdout_ledger=HH.UNGUARDED)
    with pytest.raises(RH.HarnessError, match="PRIOR_EVALUATION_DETECTED"):
        _reserve(world)


def test_btc_corpus_ledgers_never_count_as_an_evaluation_of_a_history_root(world):
    os.makedirs(world["ldir"])
    RH.VariantLedger("cash_and_carry", path=os.path.join(world["ldir"], "cash_and_carry.jsonl")).record_rejected("h1", {"hold": 1}, "no data")
    assert HH.prior_evaluations(world["ldir"]) == []
    assert _reserve(world)["kind"] == "reserve"


def test_a_bad_prior_reads_statement_is_refused(world):
    for bad in (0, -5, True, 1.5):
        with pytest.raises(RH.HarnessError, match="prior_reads_through"):
            _reserve(world, prior_reads_through_ms=bad)


# ------------------------------------------------------------------ the reserved region is readable once
def test_the_final_read_is_granted_once_and_recorded(world):
    r = _reserve(world)
    series, info = _final(world)
    assert _last_ts(series)[0] >= r["from_ms"], "the final read must contain the reserved region"
    h = info["holdout"]
    assert h["purpose"] == "FINAL" and h["status"] == "SPENT" and h["evaluate_only_from_ms"] == r["from_ms"] and h["chain_head"]["n"] == 2
    rows = _rows(world["reg"])
    assert [(x["kind"], x.get("purpose")) for x in rows] == [("reserve", None), ("read", "FINAL")]
    assert rows[-1]["from_ms"] == r["from_ms"] and rows[-1]["to_ms"] >= r["from_ms"] and rows[-1]["venue"] == VENUE
    with pytest.raises(RH.HarnessError, match="FINAL_READ_ALREADY_SPENT"):
        _final(world)
    assert len(_rows(world["reg"])) == 2


def _race(reg, root, uni, spot, q):
    try:
        HH.final_read(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV, ledger_path=reg, reader="race")
        q.put("OK")
    except RH.HarnessError as exc:
        q.put(str(exc).split(":")[0])


def test_two_processes_cannot_both_spend_the_final_read(world):
    _reserve(world)
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=_race, args=(world["reg"], world["root"], world["uni"], world["spot"], q)) for _ in range(2)]
    for p in procs:
        p.start()
    results = sorted(q.get(timeout=180) for _ in procs)
    for p in procs:
        p.join(timeout=60)
    assert results == ["FINAL_READ_ALREADY_SPENT", "OK"], results
    assert [x.get("purpose") for x in _rows(world["reg"]) if x["kind"] == "read"] == ["FINAL"]


def test_a_final_read_whose_record_cannot_be_written_returns_no_data(world, monkeypatch):
    """The record comes first: a crash or a full disk between 'data returned' and 'read recorded' would be a free second look."""
    _reserve(world)
    real = EC.append

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(EC, "append", boom)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_LEDGER_WRITE_FAILED"):
        _final(world)
    monkeypatch.setattr(EC, "append", real)
    assert [x["kind"] for x in _rows(world["reg"])] == ["reserve"]
    series, _info = _final(world)                                                  # the failed attempt did not spend it
    assert series


def test_after_the_final_read_exploration_needs_a_new_reservation_over_later_data(world):
    _reserve(world)
    _final(world)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_SPENT"):
        _load(world)
    with pytest.raises(RH.HarnessError, match="RESERVATION_OVERLAPS_A_SPENT_HOLDOUT") as e:
        _reserve(world)
    assert "must grow to at least" in str(e.value)


def test_a_final_read_without_a_reservation_is_refused(world):
    with pytest.raises(RH.HarnessError, match="HOLDOUT_NOT_RESERVED"):
        _final(world)
    assert not os.path.exists(world["reg"])


def test_a_final_read_that_fails_to_verify_does_not_spend_the_holdout(world):
    _reserve(world)
    with pytest.raises(RH.HarnessError):
        HH.final_read(world["root"], universe_dir=str(world["tmp"] / "no-such-universe"), spot_dir=world["spot"], mark_interval=MARK_IV, ledger_path=world["reg"])
    assert [x["kind"] for x in _rows(world["reg"])] == ["reserve"]
    _final(world)                                                                 # still available


# ------------------------------------------------------------------ the registry is evidence
def test_a_tampered_registry_refuses_every_read(world):
    _reserve(world)
    text = open(world["reg"], encoding="utf-8").read()
    assert '"frac":0.3' in text
    open(world["reg"], "w", encoding="utf-8").write(text.replace('"frac":0.3', '"frac":0.2'))
    with pytest.raises(RH.HarnessError, match="HOLDOUT_LEDGER_UNVERIFIED"):
        _load(world)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_LEDGER_UNVERIFIED"):
        _final(world)


def test_a_deleted_registry_cannot_be_told_from_a_fresh_one_so_the_refusal_names_where_it_looked(world):
    _reserve(world)
    os.remove(world["reg"])
    with pytest.raises(RH.HarnessError) as e:
        _load(world)
    assert world["reg"] in str(e.value)


def test_a_rollback_to_an_older_consistent_state_is_visible_only_in_the_chain_head_every_result_stamps(world):
    """Documented limit: an edit or a torn row is caught, a consistent rollback is not. What the registry gives you is the head to compare."""
    _reserve(world)
    _series, info = _final(world)
    head_after_final = info["holdout"]["chain_head"]
    assert head_after_final["n"] == 2
    for path in (world["reg"], world["reg"] + ".chain"):
        lines = open(path, "rb").read().splitlines(keepends=True)
        open(path, "wb").write(b"".join(lines[:1]))
    first_link = json.loads(open(world["reg"] + ".chain").read().splitlines()[0])["h"]
    json.dump({"n": 1, "head": first_link}, open(world["reg"] + ".head", "w"), sort_keys=True)
    _series2, info2 = _load(world)                                                 # the rolled-back registry verifies and the holdout looks unspent
    assert info2["holdout"]["status"] == "RESERVED"
    assert info2["holdout"]["chain_head"]["n"] == 1 != head_after_final["n"] and info2["holdout"]["chain_head"]["head"] != head_after_final["head"]


def test_the_status_report_is_read_only_and_states_the_arithmetic_and_the_burn(world):
    st = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    assert st["state"] == "NOT_RESERVED" and st["reservable"] is True and not os.path.exists(world["reg"]) and st["chain_head"] is None
    assert st["venue"] == VENUE and st["burned_through_ms"] is None
    r = _reserve(world)
    st = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    assert st["state"] == "RESERVED" and st["from_ms"] == r["from_ms"] and st["chain_head"]["n"] == 1
    assert st["explore_days"] > 0 and st["reserved_days"] > 0 and st["standard_grid_days_required"] == 457
    assert st["explore_supports_standard_grid"] is False and st["reserved_rows_possible"] == int(st["reserved_days"] // 7) and st["oos_verdict_floor_rows"] == 50
    assert st["reserved_supports_a_verdict_above_insufficient"] is False and "INSUFFICIENT_DATA" in st["warning"]
    _final(world)
    assert HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])["state"] == "SPENT"


def test_the_status_of_a_burned_venue_says_why_it_cannot_be_reserved(world):
    _load(world, unreserved_exploratory=True, reader="a")
    st = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    assert st["state"] == "NOT_RESERVED" and st["reservable"] is False and "recorded reads already covered" in st["not_reservable_because"] and st["burned_through"]


# ------------------------------------------------------------------ the CLI
def test_the_cli_reserves_reports_and_refuses_a_bad_request(world, capsys):
    argv = ["--history-root", world["root"], "--mark-interval", MARK_IV, "--ledger-dir", world["ldir"]]
    assert HH.main(["--status", *argv]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "NOT_RESERVED"
    assert HH.main(["--reserve", *argv]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "RESERVED" and out["registry"] == world["reg"] and os.path.exists(world["reg"])
    assert HH.main(["--reserve", *argv, "--fraction", "0.9"]) == 2
    assert "reserved fraction" in capsys.readouterr().err
    assert len(_rows(world["reg"])) == 1, "a refused request must not have appended anything"


def test_the_cli_takes_the_owners_statement_of_earlier_reads_as_an_instant(world, capsys):
    os.makedirs(world["ldir"])
    RH.VariantLedger("funding_regime_conditioned_entry", path=os.path.join(world["ldir"], "funding_regime_conditioned_entry.jsonl")).record_rejected("reg-1", {"x": 1}, "short")
    argv = ["--history-root", world["root"], "--mark-interval", MARK_IV, "--ledger-dir", world["ldir"]]
    assert HH.main(["--reserve", *argv]) == 2 and "PRIOR_EVALUATION_DETECTED" in capsys.readouterr().err
    assert HH.main(["--reserve", *argv, "--prior-reads-through", "2026-07-01T00:00:00Z"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "RESERVED"
    with pytest.raises(SystemExit):
        HH.main(["--reserve", *argv, "--prior-reads-through", "not a date"])


def test_the_cli_cannot_spend_the_final_read(world, capsys):
    """There is no flag that opens the reserved region: the frozen-hypothesis evaluator that may call final_read does not exist yet, and a human typing a flag is exactly the failure this prevents."""
    with pytest.raises(SystemExit) as e:
        HH.main(["--final", "--history-root", world["root"]])
    assert e.value.code == 2 and "usage" in capsys.readouterr().err
    assert not os.path.exists(world["reg"])
    with pytest.raises(SystemExit) as e:
        HH.main(["--reserve", "--final", "--history-root", world["root"]])
    assert e.value.code == 2 and not os.path.exists(world["reg"])


def test_the_default_registry_is_per_user_and_independent_of_the_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv(HH.REGISTRY_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert HH.default_registry_path() == str(tmp_path / "home" / ".ai-agent-control" / "history_holdout.jsonl")
    monkeypatch.setenv(HH.REGISTRY_ENV, str(tmp_path / "x" / "r.jsonl"))
    assert HH.default_registry_path() == str(tmp_path / "x" / "r.jsonl")


# ------------------------------------------------------------------ the exploratory stamp is not cosmetic: it caps readiness
def _promising_evaluation(**over):
    ev = {"status": "WALK_FORWARD_DONE", "verdict": "INSUFFICIENT_DATA", "oos_mean_total_bps_per_row": 12.0, "p_adjusted": 0.01, "oos_independent_observations": 200}
    ev.update(over)
    return ev


def _levels(ev):
    return CR.readiness_levels(data_ready=True, pit_ok=True, survivorship=CR.DELISTINGS_OBSERVED, mechanism_ok=True, evaluation=ev)


def test_an_exploratory_evaluation_cannot_reach_promising_whatever_its_numbers():
    assert _levels(_promising_evaluation())["levels"]["PROMISING"]["reached"] is True, "control: a hand-built evaluation without the stamp is unchanged"
    for status in ("RESERVED", "SPENT"):
        assert _levels(_promising_evaluation(holdout_status=status))["levels"]["PROMISING"]["reached"] is True
    for status in ("UNRESERVED_EXPLORATORY", "NOT_GUARDED", None):
        lv = _levels(_promising_evaluation(holdout_status=status))["levels"]["PROMISING"]
        assert lv["reached"] is False and any("exploratory" in r for r in lv["reasons"]), (status, lv)


def test_the_competition_shows_an_exploratory_cross_section_and_never_a_winner(world):
    rep = CC_.run_competition(data_root=world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_dir=world["ldir"], unreserved_exploratory=True)
    x = next(r for r in rep["results"] if r["name"] == "cross_sectional_funding")
    assert x["holdout"]["status"] == "UNRESERVED_EXPLORATORY" and x["readiness_level"] != "PROMISING" and rep["winner"] is None


# ------------------------------------------------------------------ every research entry point goes through the guard
def _family_ledgers(ld):
    return sorted(f for f in os.listdir(ld) if f.startswith("cross_sectional_funding")) if os.path.isdir(ld) else []


def test_the_readiness_cli_refuses_an_unreserved_dataset_then_evaluates_the_explore_window(world, capsys):
    os.makedirs(world["ldir"], exist_ok=True)
    argv = ["--history-root", world["root"], "--universe-dir", world["uni"], "--spot-dir", world["spot"], "--mark-interval", MARK_IV, "--ledger", os.path.join(world["ldir"], "cross_sectional_funding.jsonl")]
    with pytest.raises(SystemExit) as e:
        CR.main(argv)
    assert e.value.code == 2 and "HOLDOUT_NOT_RESERVED" in capsys.readouterr().err
    assert _family_ledgers(world["ldir"]) == [], "a refused run must not enlarge the multiple-testing count"
    HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty")      # a ledger dir with no earlier evaluations
    assert CR.main(argv) == 0
    assert "ranked funding:" in capsys.readouterr().out and _family_ledgers(world["ldir"])


def test_the_competition_refuses_an_unreserved_dataset_says_so_in_the_row_and_counts_nothing(world):
    rep = CC_.run_competition(data_root=world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    x = next(r for r in rep["results"] if r["name"] == "cross_sectional_funding")
    assert x["verdict"] == "UNTESTED" and any("HOLDOUT_NOT_RESERVED" in r and "history_holdout.py --reserve" in r for r in x["rejection_reasons"]), x["rejection_reasons"]
    assert _family_ledgers(world["ldir"]) == []
    assert rep["winner"] is None


def test_the_competition_on_a_reserved_dataset_runs_the_explore_window_and_the_table_shows_survivorship_and_the_holdout(world):
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty")
    rep = CC_.run_competition(data_root=world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    x = next(r_ for r_ in rep["results"] if r_["name"] == "cross_sectional_funding")
    assert x["holdout"]["status"] == "RESERVED" and x["holdout"]["cut_ms"] == r["from_ms"]
    assert x["survivorship"] in (CR.SURVIVORS_ONLY, CR.DELISTINGS_OBSERVED)
    table = CC_.render_table(rep).splitlines()
    cols = table[0].split("\t")
    assert "survivorship" in cols and "holdout" in cols
    row = next(line for line in table[1:] if line.split("\t")[1] == "cross_sectional_funding").split("\t")
    assert row[cols.index("holdout")] == "RESERVED" and row[cols.index("survivorship")] == x["survivorship"]


def test_a_survivors_only_cross_section_is_visible_in_the_competition_table(world):
    rep = CC_.run_competition(data_root=world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_dir=world["ldir"], unreserved_exploratory=True)
    x = next(r_ for r_ in rep["results"] if r_["name"] == "cross_sectional_funding")
    assert x["holdout"]["status"] == "UNRESERVED_EXPLORATORY" and x["rejection_reasons"][0].startswith("HOLDOUT UNRESERVED_EXPLORATORY"), x["rejection_reasons"][:2]
    table = CC_.render_table(rep)
    assert "UNRESERVED_EXPLORATORY" in table and (CR.SURVIVORS_ONLY in table or CR.DELISTINGS_OBSERVED in table)


def test_the_regime_cli_refuses_an_unreserved_dataset_then_runs_the_explore_window(world, capsys):
    argv = ["--history-root", world["root"], "--universe-dir", world["uni"], "--spot-dir", world["spot"], "--mark-interval", MARK_IV, "--ledger-dir", world["ldir"], "--registration", "reg-2",
            "--json", str(world["tmp"] / "o.json")]
    assert RE.main(argv) == 2 and "HOLDOUT_NOT_RESERVED" in capsys.readouterr().err
    assert not os.path.exists(str(world["tmp"] / "o.json"))
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty")
    assert RE.main(argv) == 0
    out = json.load(open(str(world["tmp"] / "o.json")))
    assert out["input"]["holdout"]["status"] == "RESERVED" and out["input"]["history_window"]["to_ms"] == r["from_ms"]


def test_run_on_root_hands_the_cut_to_the_index_series_read(world, monkeypatch):
    """The basis measurement is a SECOND read of the same window (index-kline closes): the cut has to reach it through run_on_root, not only through the loader (a mutant that stopped passing it
    survived the first version's tests because the fixture has no index dataset)."""
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty")
    seen = {}

    def spy(root, interval, symbols, *, cut_ms=None):
        seen["cut_ms"] = cut_ms
        return {}, {"status": "UNMEASURED", "reason": "spy"}
    monkeypatch.setattr(RE, "load_index_series", spy)
    RE.run_on_root(world["root"], universe_dir=world["uni"], ledger_path=str(world["tmp"] / "led.jsonl"), holdout_ledger=world["reg"], spot_dir=world["spot"], mark_interval=MARK_IV)
    assert seen["cut_ms"] == r["from_ms"]


def test_the_index_series_the_regime_evaluator_reads_separately_is_cut_at_the_boundary_too(tmp_path, monkeypatch):
    iv = 240 * 60_000
    (tmp_path / "index-kline-240").mkdir()
    rows = [{"ts": t, "observation_time": t + iv, "v": {"close": "100.0"}} for t in range(0, 20 * iv, iv)]
    monkeypatch.setattr(RE.CR, "_verified_dataset", lambda root, name: ({"symbols": {"AAAUSDT": {"status": "COMPLETE"}}, "manifest_digest": "x"}, str(tmp_path / name)))
    monkeypatch.setattr(RE.CR, "_rows", lambda ddir, sym: list(rows))
    full, _ = RE.load_index_series(str(tmp_path), "240", ["AAAUSDT"])
    cut = 10 * iv
    cutd, info = RE.load_index_series(str(tmp_path), "240", ["AAAUSDT"], cut_ms=cut)
    assert max(t for t, _ in full["AAAUSDT"]) >= cut and max(t for t, _ in cutd["AAAUSDT"]) < cut
    assert info["status"] == "MEASURED"


def _pipeline_args(world, cmd="validate", **over):
    w = types.SimpleNamespace(root=world["root"], uni=world["uni"], spot=world["spot"], ledger=world["ldir"])
    return TPC.argv(w, str(world["tmp"] / "art"), cmd, **{"unreserved_exploratory": None, **over})


def test_the_pipeline_cli_refuses_an_unreserved_dataset_and_the_identity_says_which_window_was_read(world, capsys):
    code, doc = TPC.cli(capsys, _pipeline_args(world))
    assert code == 2 and doc["error"]["code"] == "HOLDOUT_NOT_RESERVED" and "history_holdout.py --reserve" in doc["error"]["message"]
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty")
    code, cut_doc = TPC.cli(capsys, _pipeline_args(world))
    assert code == 0, cut_doc
    assert cut_doc["data_parts"]["holdout"] == f"RESERVED:{r['from_ms']}"
    code, full_doc = TPC.cli(capsys, _pipeline_args(world, unreserved_exploratory=True))
    assert code == 0 and full_doc["data_parts"]["holdout"] == f"RESERVED:{r['from_ms']}", "the escape hatch never downgrades an existing reservation"


def test_the_pipeline_identity_differs_between_a_cut_run_and_a_whole_window_run(world, capsys, monkeypatch):
    code, full_doc = TPC.cli(capsys, _pipeline_args(world, unreserved_exploratory=True))
    assert code == 0 and full_doc["data_parts"]["holdout"] == "UNRESERVED_EXPLORATORY"
    monkeypatch.setenv(HH.REGISTRY_ENV, str(world["tmp"] / "second_user" / "history_holdout.jsonl"))
    HH.reserve(os.environ[HH.REGISTRY_ENV], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty")
    code, cut_doc = TPC.cli(capsys, _pipeline_args(world))
    assert code == 0 and cut_doc["data_parts"]["holdout"].startswith("RESERVED:")
    assert cut_doc["data_digest"] != full_doc["data_digest"], "a cut run and a full-window run read different data: the identity must differ"


def test_a_pipeline_given_no_registry_for_a_history_root_is_refused():
    import ms_pipeline as MP
    with pytest.raises(MP.PipelineRefused, match="HOLDOUT_LEDGER_REQUIRED"):
        MP.load_series("/nonexistent", universe_dir="/u", spot_dir=None, mark_interval=None)


# ------------------------------------------------------------------ one enforcement point: nobody but the guard (and the metadata functions) builds series from a history root
TARGETS = ("load_history_root", "_rows")
ALLOWED_REFERENCES = {("candidate_readiness.py", "dataset_report", "load_history_root"): "reads coverage and symbol lists only, never a return series",
                      ("candidate_readiness.py", "load_history_root", "_rows"): "the loader itself",
                      ("backfill_plan.py", "_run_loader", "load_history_root"): "diagnose checks that the loader and the funnel agree: metadata only",
                      ("history_holdout.py", "load", "load_history_root"): "THE guard (evaluation reads, cut at the boundary)",
                      ("history_holdout.py", "final_read", "load_history_root"): "THE guard (the one read of the reserved region)",
                      ("regime_entry_eval.py", "load_index_series", "_rows"): "the second read of the window (index closes): given the cut by run_on_root"}


def _imports_candidate_readiness(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[-1] == "candidate_readiness":
            return True
        if isinstance(n, ast.Import) and any(al.name.split(".")[-1] == "candidate_readiness" for al in n.names):
            return True
    return False


def _is_cr(node):
    """``CR`` / ``candidate_readiness`` / ``something.CR`` as the base of an attribute."""
    return (isinstance(node, ast.Name) and node.id in ("CR", "candidate_readiness")) or (isinstance(node, ast.Attribute) and node.attr in ("CR", "candidate_readiness"))


def _references():
    """Every reference to the unguarded loader (by any spelling) and to candidate_readiness's private row reader. ``_rows`` is a common helper name, so only the references that can
    resolve to candidate_readiness's are counted: an attribute of CR / candidate_readiness, a bare name inside candidate_readiness.py itself, an import of it, a getattr string in a module
    that imports candidate_readiness."""
    out = []
    for base in (BOT, os.path.join(BOT, "tools")):
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(base, name), encoding="utf-8").read())
            uses_cr = _imports_candidate_readiness(tree)
            parents = {ch: node for node in ast.walk(tree) for ch in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                hit = None
                if isinstance(node, ast.Attribute) and node.attr == "load_history_root":
                    hit = node.attr
                elif isinstance(node, ast.Name) and node.id == "load_history_root":
                    hit = node.id
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == "load_history_root":
                    hit = node.value                                              # getattr(CR, "load_history_root")
                elif isinstance(node, ast.alias) and node.name == "load_history_root":
                    hit = node.name                                               # from candidate_readiness import load_history_root as X
                elif isinstance(node, ast.Attribute) and node.attr == "_rows" and _is_cr(node.value):
                    hit = "_rows"
                elif isinstance(node, ast.Name) and node.id == "_rows" and name == "candidate_readiness.py":
                    hit = "_rows"
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == "_rows" and uses_cr:
                    hit = "_rows"
                elif isinstance(node, ast.alias) and node.name == "_rows" and isinstance(parents.get(node), ast.ImportFrom) and (parents[node].module or "").endswith("candidate_readiness"):
                    hit = "_rows"
                if hit:
                    p = node
                    while p in parents and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        p = parents[p]
                    out.append((name, getattr(p, "name", "<module>"), hit, getattr(node, "lineno", 0)))
    return out


def test_only_the_guard_and_the_metadata_functions_reach_the_history_rows():
    """(file, function, name) granularity, and every REFERENCE counts (an alias import, a getattr string, the private row reader), not only a direct call."""
    offenders, seen = [], set()
    for name, func, hit, line in _references():
        key = (name, func, hit)
        seen.add(key)
        if key not in ALLOWED_REFERENCES:
            offenders.append(f"{name}:{line} in {func} references {hit}")
    assert not offenders, f"these reach the history rows without the reserved-holdout guard: {offenders}; route them through history_holdout.load(...)"
    assert set(ALLOWED_REFERENCES) <= seen, f"the allow-list names a reference that no longer exists (delete the entry): {sorted(set(ALLOWED_REFERENCES) - seen)}"


def test_the_explicit_opt_out_never_appears_in_shipped_code():
    """A CODE reference (a name or an attribute), not a mention in a docstring or an error message."""
    offenders = []
    for base in (BOT, os.path.join(BOT, "tools")):
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py") or name == "history_holdout.py":
                continue
            for node in ast.walk(ast.parse(open(os.path.join(base, name), encoding="utf-8").read())):
                if (isinstance(node, ast.Name) and node.id == "UNGUARDED") or (isinstance(node, ast.Attribute) and node.attr == "UNGUARDED"):
                    offenders.append(f"{name}:{node.lineno}")
    assert not offenders, f"UNGUARDED is for tests and embeddings: {offenders}"
