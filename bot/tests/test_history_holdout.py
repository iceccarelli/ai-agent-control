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
VENUE = "mainnet|linear"                                          # the manifest's request host mapped through bot/venues.py (api.bybit.com is the registered mainnet REST host)


@pytest.fixture(autouse=True)
def _hermetic_ledger_lookup(tmp_path, monkeypatch):
    """``--reserve`` looks for earlier evaluations in EVERY directory the ledger rule can resolve to, including $CANDIDATE_LEDGER_DIR and ``~/.cache/ai-agent-control/candidate_ledgers``. A developer's
    own research ledgers must not decide these tests (the independent re-review ran them with one ledger row in HOME: 37 of 76 failed with PRIOR_EVALUATION_DETECTED, green in CI, red on the
    owner's research machine). conftest isolates the registry; this isolates the lookup."""
    monkeypatch.delenv("CANDIDATE_LEDGER_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))


@pytest.fixture(autouse=True)
def _registered_bar_for_synthetic_fixtures(monkeypatch):
    """Round 19: ``--reserve`` refuses a dataset whose explore window cannot carry one registered test. The synthetic fixtures are ~200 days (139 left to explore after the cut), so the module runs
    against a LOWER registered bar: 14 rows = 7 + 30 + 14 x 7 = 135 days, which the explore window carries (15 rows = 142 does not: the refusal test below uses exactly that pair). The production
    bar (60 rows, 457 days) is pinned by ``test_the_production_bar_refuses_a_short_history_without_any_patching``."""
    monkeypatch.setattr(CR, "STATE_MIN_ROWS", 14)


@pytest.fixture()
def world(tmp_path, offline, monkeypatch):
    # Round 19: --reserve refuses a dataset that is not COMPLETE over its whole requested window, so the world is a COMPLETE window (the mark dataset asks for the funding dataset's window).
    root, uni, spot = _hist(tmp_path, complete_window=True)
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
    kw.setdefault("universe_dir", w["uni"])
    kw.setdefault("spot_dir", w["spot"])
    return HH.reserve(w["reg"], w["root"], mark_interval=MARK_IV, **kw)


def _final(w):
    return HH.final_read(w["root"], universe_dir=w["uni"], spot_dir=w["spot"], mark_interval=MARK_IV, ledger_path=w["reg"])


def _reserve_past_the_dataset_gate(monkeypatch, reg, root, uni, spot, led):
    """For tests whose subject is the LOADER's cut (a regime change or a late listing inside the reserved region). Those fixtures are partial datasets the Round 19 gate rightly refuses to
    reserve on (an unproven funding-interval change and a symbol whose data starts after its launch both read DATA_INCOMPLETE), so they reserve past the gate on purpose and only the gate."""
    monkeypatch.setattr(HH, "dataset_problem_for_a_reservation", lambda *a, **k: None)
    return HH.reserve(reg, root, mark_interval=MARK_IV, ledger_dir=led, universe_dir=uni, spot_dir=spot)


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
        HH.reserve(world["reg"], world["root"], mark_interval="60", ledger_dir=world["ldir"], universe_dir=world["uni"], spot_dir=world["spot"])
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


def test_a_symbol_whose_first_row_is_at_the_boundary_is_dropped_and_listed_never_a_permanent_abort(tmp_path, offline, monkeypatch):
    cut = HH.cut_for(TMH.START, TMH.END)
    root, uni, spot = _late_listing_world(tmp_path, cut)
    reg = str(tmp_path / "reg.jsonl")
    _reserve_past_the_dataset_gate(monkeypatch, reg, root, uni, spot, str(tmp_path / "led"))
    series, info = HH.load(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV, ledger_path=reg)
    assert "ETHUSDT" not in {s.symbol for s in series} and {"BTCUSDT", "FASTUSDT"} <= {s.symbol for s in series}
    assert info["holdout_dropped_no_rows_before_cut"] == ["ETHUSDT"]
    assert {"symbol": "ETHUSDT", "reason": "no_rows_before_the_holdout_cut"} in info["excluded"]
    assert any("late listings" in w for w in info["warnings"])
    everything, _ = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    assert "ETHUSDT" in {s.symbol for s in everything}, "control: the uncut load does include it"


def test_the_committed_btc_corpus_overlap_with_the_reserved_region_is_stated(world, tmp_path):
    """Independent re-review, finding 3: the committed BTCUSDT corpus is read in sample on every competition run and lives outside the registry, yet it covers part of the reserved region for the
    mainnet venue (136 of 198 days for a 660-day backfill ending 2026-11-10). The registry cannot cut it, so it SAYS it, with numbers."""
    import datetime as dt
    ms = lambda y, m, d: int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp() * 1000)          # noqa: E731
    o = HH.corpus_overlap("mainnet|linear", ms(2026, 4, 26), ms(2026, 11, 10))
    assert o and o["symbols"] == ["BTCUSDT"] and 135 < o["overlap_days"] < 138 and 0.67 < o["fraction_of_reserved_region"] < 0.71 and "ALREADY in sample" in o["consequence"], o
    assert o["corpus_last"].startswith("2026-09-09")
    assert HH.corpus_overlap("mainnet|linear", ms(2026, 9, 10), ms(2026, 11, 10)) is None, "a boundary after the corpus ends has no overlap"
    assert HH.corpus_overlap("testnet|linear", ms(2026, 4, 26), ms(2026, 11, 10)) is None, "another venue's reserved region is not the corpus's"
    assert HH.corpus_overlap("mainnet|linear", ms(2026, 4, 26), ms(2026, 11, 10), manifest_path=str(tmp_path / "absent.json")) is None, "an unreadable manifest states nothing rather than inventing a figure"
    (tmp_path / "bad.json").write_text("{not json")
    assert HH.corpus_overlap("mainnet|linear", ms(2026, 4, 26), ms(2026, 11, 10), manifest_path=str(tmp_path / "bad.json")) is None
    # the world's own window starts after the corpus ends: the status carries the field (None) and the reserve row records it
    r = _reserve(world)
    assert "btc_corpus_overlap" in r
    st = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    assert "btc_corpus_overlap" in st


def test_a_delisting_inside_the_reserved_region_is_hidden_from_the_explore_series(world):
    """Independent re-review, finding 8: ``series[].delivery_ms`` carried a delisting instant from the reserved region. A delisting decides which explore holds are 'terminal' (priced at the last mark
    or dropped as unpriced), so the explore result would depend on the reserved period. Hidden for a cut load, listed in the info, kept for a delisting before the boundary."""
    _reserve(world)
    series, info = _load(world)
    old = next(x for x in series if x.symbol == "OLDUSDT")
    assert old.delivery_ms is None and info["holdout_delivery_hidden"] == ["OLDUSDT"], "the explore series knows a delisting that happens in the reserved period"
    assert all(x.delivery_ms is None for x in series if x.symbol != "OLDUSDT")
    full, finfo = _load(world, ledger_path=HH.UNGUARDED)
    real = next(x for x in full if x.symbol == "OLDUSDT").delivery_ms
    assert isinstance(real, int) and real >= info["holdout_cut_ms"] and finfo.get("holdout_delivery_hidden") in (None, []), "control: the whole-window read sees the delivery"
    before, binfo = CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, cut_ms=real + DAY)
    assert next(x for x in before if x.symbol == "OLDUSDT").delivery_ms == real and binfo["holdout_delivery_hidden"] == [], "a delisting BEFORE the boundary is history and stays"


def test_an_interval_change_inside_the_reserved_region_does_not_reach_the_explore_series(tmp_path, offline, monkeypatch):
    """The 8h -> 4h regime change a funding cap produces is a fact about the reserved region; the segments the series carries end at the boundary."""
    cut = HH.cut_for(TMH.START, TMH.END)
    H8 = TMH.H8
    ts8 = TMH.grid(TMH.FIRST, ((cut + 10 * DAY) - TMH.FIRST) // H8 + 1, H8)
    ts4 = [ts8[-1] + (i + 1) * H4 for i in range((TMH.BASE - ts8[-1]) // H4)]
    root, uni, spot = _hist(tmp_path, funding_override={"BTCUSDT": ts8 + ts4})
    reg = str(tmp_path / "reg.jsonl")
    _reserve_past_the_dataset_gate(monkeypatch, reg, root, uni, spot, str(tmp_path / "led"))
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
        HH.reserve(world["reg"], copy, mark_interval=MARK_IV, ledger_dir=str(tmp_path / "a_completely_different_ledger_dir"), universe_dir=world["uni"], spot_dir=world["spot"])
    assert HH.main(["--reserve", "--history-root", world["root"], "--mark-interval", MARK_IV, "--universe-dir", world["uni"], "--ledger-dir", str(tmp_path / "yet_another")]) == 2
    assert [r["kind"] for r in _rows(world["reg"])] == ["read"]


def test_a_reservation_after_a_stated_early_read_is_allowed_and_after_a_late_one_is_not(world):
    a, b = _window(world)
    cut = HH.cut_for(a, b)
    with pytest.raises(RH.HarnessError, match="ALREADY_READ"):
        _reserve(world, prior_reads_through_ms=cut + DAY)
    assert [r["purpose"] for r in _rows(world["reg"])] == ["PRIOR_STATED"], "the statement is recorded even when it makes the reservation impossible"
    reg2 = str(world["tmp"] / "reg2.jsonl")
    r = HH.reserve(reg2, world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"], prior_reads_through_ms=a + 10 * DAY, universe_dir=world["uni"], spot_dir=world["spot"])
    assert r["from_ms"] == cut and [x["purpose"] if x["kind"] == "read" else "reserve" for x in _rows(reg2)] == ["PRIOR_STATED", "reserve"]


def test_the_exploratory_escape_hatch_is_idempotent_per_reader(world):
    _load(world, unreserved_exploratory=True, reader="a")
    _load(world, unreserved_exploratory=True, reader="a")
    assert len(_rows(world["reg"])) == 1
    _load(world, unreserved_exploratory=True, reader="b")
    assert len(_rows(world["reg"])) == 2


def test_a_reservation_that_lands_while_an_unreserved_read_is_loading_refuses_with_the_true_reason(world, monkeypatch):
    """Re-review finding 9 (S1): the refusal was correct but said 'no holdout is reserved' about a holdout that WAS reserved a moment earlier. The read is refused, nothing is returned, nothing is
    recorded, and the message says what happened."""
    real = CR.load_history_root

    def racing(*a, **k):
        out = real(*a, **k)
        monkeypatch.setattr(CR, "load_history_root", real)                    # the OTHER process has its own, unpatched loader (and reserve now loads the dataset itself under the registry lock)
        HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])      # another process reserves while this one loaded the whole window
        return out
    monkeypatch.setattr(CR, "load_history_root", racing)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_CHANGED_WHILE_LOADING") as e:
        _load(world, unreserved_exploratory=True)
    assert "no holdout is reserved" not in str(e.value) and "Nothing was returned and nothing was recorded" in str(e.value)
    assert [r["kind"] for r in _rows(world["reg"])] == ["reserve"], "the refused read must not have been recorded"


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
    assert ok["prior_evaluations"] == [{"dir": os.path.abspath(world["ldir"]), "file": "cross_sectional_funding.jsonl", "rows": 1}]
    assert os.path.abspath(world["ldir"]) in ok["ledger_dirs_scanned"], "the row records WHERE it looked"
    assert [r["purpose"] if r["kind"] == "read" else "reserve" for r in _rows(world["reg"])] == ["PRIOR_STATED", "reserve"]


def _earlier_row(directory, family="cross_sectional_funding"):
    os.makedirs(directory, exist_ok=True)
    RH.VariantLedger(family, path=os.path.join(directory, family + ".jsonl")).record_rejected("F3|H7|K1|Cbase", {"formation_days": 3}, "only 2 rows")


def test_an_earlier_evaluation_in_ANY_ledger_directory_the_rule_can_resolve_to_blocks_the_reservation(world, tmp_path, monkeypatch):
    """Independent re-review, finding 2: a ledger row in directory A made ``--reserve --ledger-dir A`` refuse, but the same call with a different ``--ledger-dir``, or with $CANDIDATE_LEDGER_DIR set,
    succeeded and recorded ``prior_evaluations: []``. The realistic form: ``candidate_readiness.py --history-root R`` (no --ledger-dir) writes to <parent of R>/candidate_ledgers while
    ``make reserve-holdout`` scanned $(DATA)/ledgers, so an earlier direct run was invisible."""
    monkeypatch.delenv("CANDIDATE_LEDGER_DIR", raising=False)
    parent_default = os.path.join(os.path.dirname(os.path.realpath(world["root"])), "candidate_ledgers")
    elsewhere = str(tmp_path / "some_other_ledger_dir")
    _earlier_row(parent_default)                                                          # the directory a direct `candidate_readiness.py --history-root R` run used
    with pytest.raises(RH.HarnessError, match="PRIOR_EVALUATION_DETECTED") as e:
        _reserve(world, ledger_dir=elsewhere)                                              # reserved through a DIFFERENT --ledger-dir
    assert parent_default in str(e.value) and elsewhere in str(e.value), "the refusal names every directory it looked in"
    assert not os.path.exists(world["reg"])
    st = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=elsewhere)
    assert any(r["dir"] == parent_default for r in st["prior_evaluations"]) and parent_default in st["ledger_dirs_scanned"]
    assert st["reservable"] is False and "PRIOR_EVALUATION_DETECTED" in st["not_reservable_because"], "status must not say 'reservable' while --reserve refuses (finding 9)"
    shutil.rmtree(parent_default)
    envdir = str(tmp_path / "env_ledgers")
    _earlier_row(envdir, "funding_regime_conditioned_entry")
    monkeypatch.setenv("CANDIDATE_LEDGER_DIR", envdir)                                      # ... or through the environment variable, with another explicit directory given
    with pytest.raises(RH.HarnessError, match="PRIOR_EVALUATION_DETECTED"):
        _reserve(world, ledger_dir=elsewhere)
    monkeypatch.delenv("CANDIDATE_LEDGER_DIR")
    shutil.rmtree(envdir)
    home_cache = os.path.join(os.path.expanduser("~"), ".cache", "ai-agent-control", "candidate_ledgers")
    assert str(tmp_path / "isolated_home") in home_cache, "the hermetic fixture must have moved HOME"
    _earlier_row(home_cache, "ms_pipeline_trials")                                          # ... or the home-cache default every research CLI falls back to
    with pytest.raises(RH.HarnessError, match="PRIOR_EVALUATION_DETECTED") as e2:
        _reserve(world, ledger_dir=elsewhere)
    assert home_cache in str(e2.value)
    shutil.rmtree(home_cache)
    clean = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=elsewhere)
    assert clean["prior_evaluations"] == [] and clean["reservable"] is True and "not_reservable_because" not in clean, "control: with no earlier ledger anywhere the same call is reservable"
    ok = _reserve(world, ledger_dir=elsewhere)
    assert ok["kind"] == "reserve" and ok["prior_evaluations"] == [] and parent_default in ok["ledger_dirs_scanned"] and elsewhere in ok["ledger_dirs_scanned"]


def test_the_candidate_ledger_dir_rule_has_one_owner_and_its_first_candidate_is_its_answer(tmp_path, monkeypatch):
    monkeypatch.delenv("CANDIDATE_LEDGER_DIR", raising=False)
    root = str(tmp_path / "data" / "md")
    os.makedirs(root)
    default = os.path.join(os.path.dirname(os.path.realpath(root)), "candidate_ledgers")
    assert CR.ledger_dir_for(root) == CR.ledger_dir_candidates(root)[0] == default
    assert CR.ledger_dir_for(root, "x") == CR.ledger_dir_candidates(root, "x")[0] == os.path.abspath("x") and default in CR.ledger_dir_candidates(root, "x")
    monkeypatch.setenv("CANDIDATE_LEDGER_DIR", str(tmp_path / "env"))
    assert CR.ledger_dir_for(root) == str(tmp_path / "env") and CR.ledger_dir_candidates(root)[:2] == [str(tmp_path / "env"), default]
    monkeypatch.delenv("CANDIDATE_LEDGER_DIR")
    cands = CR.ledger_dir_candidates(None)
    assert cands == [os.path.join(os.path.expanduser("~"), ".cache", "ai-agent-control", "candidate_ledgers")] and CR.ledger_dir_for(None) == cands[0]
    assert len(CR.ledger_dir_candidates(root, default)) == len(set(CR.ledger_dir_candidates(root, default))), "no directory is listed twice"


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


def test_a_boundary_the_served_data_cannot_support_is_refused_with_its_own_code_and_records_nothing(world, monkeypatch):
    """Independent re-review, finding 4 (E8, W1, W3, W4): funding that ended 120 days before the REQUEST's end still reserved 'the last 30%' and stamped every result RESERVED with zero rows withheld;
    a request that opened two years before the data put the boundary before the first row (explore_days -94, the whole dataset reserved); a dataset with no coverage was accepted. Each now has its own
    refusal, before anything is recorded, owner statement or not."""
    a, b = _window(world)
    sa, sb = HH.served_window(world["root"])

    def refused(code, **kw):
        with pytest.raises(RH.HarnessError, match=code) as e:
            _reserve(world, **kw)
        assert "Nothing was recorded" in str(e.value)
        with pytest.raises(RH.HarnessError, match=code):
            _reserve(world, prior_reads_through_ms=a + DAY, **kw)                       # an owner statement alongside must not leave a half-written registry
        assert not os.path.exists(world["reg"]), f"{code}: a refused reservation appended something"
        st = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
        assert st["reservable"] is False and code in st["not_reservable_because"], (code, st.get("not_reservable_because"))
    monkeypatch.setattr(HH, "_window", lambda root: (a, b + 400 * DAY))                    # the request runs 400 days past what the datasets served
    refused("DATA_ENDS_BEFORE_THE_REQUESTED_END")
    monkeypatch.setattr(HH, "_window", lambda root: (a, b + 3 * DAY))                      # even a few days: tolerance is two
    refused("DATA_ENDS_BEFORE_THE_REQUESTED_END")
    monkeypatch.setattr(HH, "_window", lambda root: (a - 700 * DAY, b))                    # the request opens long before the first served row: the cut lands before it
    refused("NO_EXPLORE_DATA_BEFORE_THE_BOUNDARY")
    monkeypatch.setattr(HH, "_window", lambda root: (a, b))
    monkeypatch.setattr(HH, "served_window", lambda root: None)                            # a verified dataset with no rows
    refused("NO_SERVED_COVERAGE")
    monkeypatch.undo()
    monkeypatch.setattr(CR, "STATE_MIN_ROWS", 14)                                          # undo() also reverted the module's fixture bar (autouse); the control below is about the window, not the bar
    monkeypatch.setenv(HH.REGISTRY_ENV, world["reg"])
    # the empty-region code, at the function that decides: a boundary less than a day before the last served row
    problem, _w = HH.reservation_problem(world["root"], sa, sb, sb - DAY // 2)
    assert problem.startswith("RESERVED_REGION_HAS_NO_DATA")
    assert HH.reservation_problem(world["root"], sa, sb, sb - 2 * DAY)[0] is None, "control: two days of data after the boundary is enough to be a (small) holdout"
    assert HH.reservation_problem(world["root"], sa, sb, sa + DAY // 2)[0].startswith("NO_EXPLORE_DATA_BEFORE_THE_BOUNDARY")
    assert HH.reservation_problem(world["root"], sa, sb + 2 * DAY, sb - 2 * DAY)[0] is None, "a shortfall inside the two-day tolerance is accepted (the last print precedes the request's end)"
    assert _reserve(world)["kind"] == "reserve", "control: the real window is reservable"


def test_the_arithmetic_counts_the_data_that_exists_not_the_window_that_was_requested(world, monkeypatch, tmp_path):
    a, b = _window(world)
    sa, sb = HH.served_window(world["root"])
    assert HH.effective_window(world["root"], a, b) == (max(a, sa), min(b, sb)) and sa >= a and sb <= b
    assert HH.effective_window(world["root"], a - 10 * DAY, b + 10 * DAY) == (sa, sb), "the request is clipped to what was served"
    control = HH.reserve(str(tmp_path / "control.jsonl"), world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"], universe_dir=world["uni"], spot_dir=world["spot"])
    assert round(control["reserved_days"], 3) == round((sb - control["from_ms"]) / DAY, 3) and control["explore_days"] == round((control["from_ms"] - max(a, sa)) / DAY, 3)
    assert "data_ends_before_the_requested_end_days" not in control, "control: a collection that reaches its request's end has nothing to warn about"
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"], universe_dir=world["uni"], spot_dir=world["spot"])           # reserved on the real window (a 40-day shortfall would now be REFUSED at reserve) ...
    monkeypatch.setattr(HH, "_window", lambda root: (a, b + 40 * DAY))                     # ... then the manifest's request is read as ending 40 days after the data (the stamps must not count them)
    requested_reserved = round(((b + 40 * DAY) - r["from_ms"]) / DAY, 3)
    served_reserved = round((min(b + 40 * DAY, sb) - r["from_ms"]) / DAY, 3)
    assert served_reserved < requested_reserved - 39, "the reserved days must be the data's, not the request's"
    ser, info = _load(world)
    h = info["holdout"]
    assert h["reserved_days"] == served_reserved and abs(h["data_ends_before_the_requested_end_days"] - 40) < 0.01 and "ends" in h["warning"] and "days before the requested window end" in h["warning"]
    st = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    assert st["reserved_days"] == served_reserved and st["served_window_ms"] == [max(a, sa), sb] and st["requested_window_ms"] == [a, b + 40 * DAY]


def test_the_venue_is_the_registered_price_source_not_the_string_the_manifest_spells(tmp_path):
    """Independent re-review, finding 7 (E2, V2, V3, N4): forging ``request.host`` as ``API.BYBIT.COM`` (digest recomputed) passed verification and gave a venue the burn did not apply to; the demo host
    (real mainnet prices) got its own clean slate; a category with a suffix or a zero-width character was a new venue; a manifest whose ``request`` was a string raised AttributeError. CLAUDE.md: venue
    identity comes from bot/venues.py, never a string comparison, and a holdout is a region of the PRICE series."""
    def root_with(name, host, cat="linear", raw=None):
        d = tmp_path / name
        (d / "funding").mkdir(parents=True)
        (d / "funding" / "manifest.json").write_text(raw if raw is not None else json.dumps({"request": {"host": host, "category": cat}}))
        return str(d)
    assert HH.venue_id(root_with("a", "api.bybit.com")) == "mainnet|linear"
    assert HH.venue_id(root_with("b", "API.BYBIT.COM")) == "mainnet|linear" == HH.venue_id(root_with("c", "  api.bybit.com  ", "LINEAR"))
    assert HH.venue_id(root_with("d", "api-testnet.bybit.com")) == "testnet|linear" != HH.venue_id(root_with("e", "api.bybit.com"))
    assert HH.venue_id(root_with("f", "api-demo.bybit.com", "spot")) == "mainnet|spot" == HH.venue_id(root_with("g", "api.bybit.com", "spot")), "demo trading is matched on real mainnet prices: the same region, one burn"
    for i, bad in enumerate(("api.bybit.eu", "evil.example.com", "api.bybit.com.evil.com", "", "   ", 7, None)):
        with pytest.raises(RH.HarnessError, match="VENUE_UNKNOWN"):
            HH.venue_id(root_with(f"bad{i}", bad))
    for i, cat in enumerate(("", "  ", None, 3, "linear2", "lin\u200bear", "linear ; drop", "Perpetual")):
        with pytest.raises(RH.HarnessError, match="VENUE_UNKNOWN"):
            HH.venue_id(root_with(f"cat{i}", "api.bybit.com", cat))
    for i, raw in enumerate(('{"request": "api.bybit.com"}', '[1, 2]', '"x"', 'null', '{"request": null}', '{"request": []}', '{"request": {"host": ["api.bybit.com"], "category": "linear"}}', "{not json")):
        with pytest.raises(RH.HarnessError, match="VENUE_UNKNOWN"):
            HH.venue_id(root_with(f"raw{i}", None, raw=raw))
    with pytest.raises(RH.HarnessError, match="VENUE_UNKNOWN"):
        HH.venue_id(str(tmp_path / "no_manifest_here"))


def test_a_registry_chosen_by_the_environment_is_said_on_the_status_and_on_every_load_stamp(world, monkeypatch):
    """Another registry file is a clean slate (E3: a second ``final_read`` through ``$HISTORY_HOLDOUT_LEDGER=<new>``). The floor stays a floor, but the override is stated, not inferred."""
    _reserve(world)
    st = HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    assert st["registry_source"] == "ENV:" + HH.REGISTRY_ENV and "REGISTRY_OVERRIDDEN_BY_ENV" in st["registry_warning"]
    _s, info = _load(world)
    assert info["holdout"]["registry_source"] == "ENV:" + HH.REGISTRY_ENV and "REGISTRY_OVERRIDDEN_BY_ENV" in info["holdout"]["registry_warning"]
    monkeypatch.delenv(HH.REGISTRY_ENV)
    assert HH.registry_source() == {"registry_source": "DEFAULT"} and HH.default_registry_path() == os.path.abspath(os.path.expanduser(HH.REGISTRY_DEFAULT))
    assert HH.registry_source(HH.default_registry_path()) == {"registry_source": "DEFAULT"}, "the default path itself is not an override"
    other = HH.registry_source(str(world["tmp"] / "some_other_registry.jsonl"))
    assert other["registry_source"] == "ARGUMENT" and "REGISTRY_OVERRIDDEN_BY_ARGUMENT" in other["registry_warning"], "the CLI's --registry is an override too (re-review R1)"
    st2 = HH.status(str(world["tmp"] / "some_other_registry.jsonl"), world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    assert st2["registry_source"] == "ARGUMENT" and "prior_evaluations_note" in st2 and "--prior-reads-through" in st2["prior_evaluations_note"]


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
    argv = ["--history-root", world["root"], "--mark-interval", MARK_IV, "--ledger-dir", world["ldir"], "--universe-dir", world["uni"], "--spot-dir", world["spot"]]
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
    argv = ["--history-root", world["root"], "--mark-interval", MARK_IV, "--ledger-dir", world["ldir"], "--universe-dir", world["uni"], "--spot-dir", world["spot"]]
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
    ev = {"status": "WALK_FORWARD_DONE", "verdict": "INSUFFICIENT_DATA", "oos_mean_total_bps_per_row": 12.0, "p_adjusted": 0.01, "oos_independent_observations": 200,
          "vs_cash": {"OKX_LENDING": {"status": "WALK_FORWARD_DONE", "verdict": "INSUFFICIENT_DATA", "oos_mean_excess_bps_of_capital_per_row": 6.0, "p_adjusted": 0.02, "oos_independent_observations": 200}}}
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
    HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])      # a ledger dir with no earlier evaluations
    assert CR.main(argv) == 0
    assert "ranked funding:" in capsys.readouterr().out and _family_ledgers(world["ldir"])


def test_the_competition_refuses_an_unreserved_dataset_says_so_in_the_row_and_counts_nothing(world):
    rep = CC_.run_competition(data_root=world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_dir=world["ldir"])
    x = next(r for r in rep["results"] if r["name"] == "cross_sectional_funding")
    assert x["verdict"] == "UNTESTED" and any("HOLDOUT_NOT_RESERVED" in r and "history_holdout.py --reserve" in r for r in x["rejection_reasons"]), x["rejection_reasons"]
    assert _family_ledgers(world["ldir"]) == []
    assert rep["winner"] is None


def test_the_competition_on_a_reserved_dataset_runs_the_explore_window_and_the_table_shows_survivorship_and_the_holdout(world):
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])
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
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])
    assert RE.main(argv) == 0
    out = json.load(open(str(world["tmp"] / "o.json")))
    assert out["input"]["holdout"]["status"] == "RESERVED" and out["input"]["history_window"]["to_ms"] == r["from_ms"]


def test_run_on_root_hands_the_cut_to_the_index_series_read(world, monkeypatch):
    """The basis measurement is a SECOND read of the same window (index-kline closes): the cut has to reach it through run_on_root, not only through the loader (a mutant that stopped passing it
    survived the first version's tests because the fixture has no index dataset)."""
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])
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
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])
    code, cut_doc = TPC.cli(capsys, _pipeline_args(world))
    assert code == 0, cut_doc
    assert cut_doc["data_parts"]["holdout"] == f"RESERVED:{r['from_ms']}"
    code, full_doc = TPC.cli(capsys, _pipeline_args(world, unreserved_exploratory=True))
    assert code == 0 and full_doc["data_parts"]["holdout"] == f"RESERVED:{r['from_ms']}", "the escape hatch never downgrades an existing reservation"


def test_the_pipeline_identity_differs_between_a_cut_run_and_a_whole_window_run(world, capsys, monkeypatch):
    code, full_doc = TPC.cli(capsys, _pipeline_args(world, unreserved_exploratory=True))
    assert code == 0 and full_doc["data_parts"]["holdout"] == "UNRESERVED_EXPLORATORY"
    monkeypatch.setenv(HH.REGISTRY_ENV, str(world["tmp"] / "second_user" / "history_holdout.jsonl"))
    HH.reserve(os.environ[HH.REGISTRY_ENV], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])
    code, cut_doc = TPC.cli(capsys, _pipeline_args(world))
    assert code == 0 and cut_doc["data_parts"]["holdout"].startswith("RESERVED:")
    assert cut_doc["data_digest"] != full_doc["data_digest"], "a cut run and a full-window run read different data: the identity must differ"


def test_a_reserved_run_may_not_extend_past_the_boundary_through_the_cli(world, capsys):
    """Independent re-review, finding 1: ``validate`` returned code 0 for ``--end-ms`` thirty days past the boundary with ``data_parts.holdout = RESERVED:<cut>``: a run stamped as keeping the reserved
    region unread whose window reached into it. The window may now end at the boundary at the latest."""
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])
    cut = r["from_ms"]
    DAYS = 86_400_000
    for cmd in ("validate", "run"):
        code, doc = TPC.cli(capsys, _pipeline_args(world, cmd, end_ms=cut + 30 * DAYS, clock_ms=cut + 30 * DAYS))
        assert code == 2 and doc["error"]["code"] == "WINDOW_PAST_THE_HOLDOUT_BOUNDARY", (cmd, doc)
        assert str(cut) in doc["error"]["message"] and f"--end-ms {cut}" in doc["error"]["message"]
    code, doc = TPC.cli(capsys, _pipeline_args(world, "validate", end_ms=cut + 1, clock_ms=cut + 1))
    assert code == 2 and doc["error"]["code"] == "WINDOW_PAST_THE_HOLDOUT_BOUNDARY", "one millisecond past the boundary is past it"
    code, doc = TPC.cli(capsys, _pipeline_args(world, "validate", end_ms=cut, clock_ms=cut))
    assert code == 0, f"a window that ends AT the boundary is allowed (the reserved region starts there): {doc}"
    # an exploratory run on an unreserved root has no boundary to respect (and says so in its identity)
    code, doc = TPC.cli(capsys, _pipeline_args(world, "validate", unreserved_exploratory=True, end_ms=cut + 30 * DAYS, clock_ms=cut + 30 * DAYS))
    assert code == 2 and doc["error"]["code"] == "WINDOW_PAST_THE_HOLDOUT_BOUNDARY", "the escape hatch never downgrades an existing reservation, so the boundary still binds"


def test_the_pipeline_library_refuses_a_reserved_window_past_the_boundary_and_attaches_no_collector_book_at_or_after_it(world):
    """The same rule at the chokepoint every caller shares (``run``), and the collector's books and tape: a second source of prices for the same calendar time (finding 1: a replay over a series cut
    at the boundary, with books that continued past it, filled and valued its exit from the reserved period; net -425 with calm post-cut books and +1570 when they crashed)."""
    import ms_pipeline as MP
    import ms_replay as RP
    import types as _t
    r = HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"] + "_empty", universe_dir=world["uni"], spot_dir=world["spot"])
    cut = r["from_ms"]
    H1 = 3_600_000

    def snap(t, px, cycle):
        return _t.SimpleNamespace(valid=True, local_receive_ms=t, cycle=cycle, bids=((px - 0.05, 100.0),), asks=((px + 0.05, 100.0),), tape_complete=True, trades=((t - 1, px, 1.0), (t + 1, px, 1.0)))

    snaps = [snap(cut - 3 * H1, 100.0, 1), snap(cut - H1, 100.0, 2), snap(cut - 1, 100.0, 6), snap(cut, 90.0, 3), snap(cut + H1, 90.0, 4), snap(cut + 33 * H1, 90.0, 5)]     # the snapshot at cut - 1 carries a trade stamped AT the boundary
    micro = _t.SimpleNamespace(verified=True, problems=(), cohort_id="broad", snapshots=lambda sym: list(snaps), data_digest=lambda: "d" * 64, evaluates_funding_edge_ok=lambda: True)
    import ms_microstructure as MM
    orig = MM.evaluates_funding_edge_ok
    MM.evaluates_funding_edge_ok = lambda m: True
    try:
        series = [RP.ReplaySeries("AAAUSDT", ((cut - 8 * H1, 1.0),), ((cut - 4 * H1, 100.0), (cut - H1, 100.0)), 480, "AAA")]
        out_cut, info_cut = MP.apply_micro(series, micro, cut_ms=cut)
        assert [b[0] for b in out_cut[0].books] == [cut - 3 * H1, cut - H1, cut - 1], "a book at or after the boundary reached the replay"
        stamps = [getattr(t, "ts_ms", None) if getattr(t, "ts_ms", None) is not None else t[0] for t in out_cut[0].ticks]
        assert stamps and max(stamps) < cut, f"a trade stamped at or after the boundary reached the replay: {stamps}"
        assert info_cut["holdout_cut_ms"] == cut and info_cut["book_snapshots_dropped_at_or_after_the_cut"] == 3
        out_all, info_all = MP.apply_micro(series, micro)
        assert len(out_all[0].books) == 6 and info_all["holdout_cut_ms"] is None and info_all["book_snapshots_dropped_at_or_after_the_cut"] == 0, "control: with no boundary every book is attached"
        assert max(getattr(t, "ts_ms", None) if getattr(t, "ts_ms", None) is not None else t[0] for t in out_all[0].ticks) >= cut, "control: the late trades exist when nothing is cut"
        only_late = _t.SimpleNamespace(verified=True, problems=(), cohort_id="broad", snapshots=lambda sym: [snap(cut + H1, 90.0, 9)], data_digest=lambda: "d" * 64)
        with pytest.raises(MP.PipelineRefused, match="BEFORE THE RESERVED HOLDOUT BOUNDARY"):
            MP.apply_micro(series, only_late, cut_ms=cut)
    finally:
        MM.evaluates_funding_edge_ok = orig
    assert MP._window_past_boundary(cut, cut) is None and MP._window_past_boundary(cut - 1, cut) is None and MP._window_past_boundary(cut + 1, cut).startswith("WINDOW_PAST_THE_HOLDOUT_BOUNDARY")
    assert MP._window_past_boundary(10 ** 15, None) is None
    loaded = MP.verify_inputs(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, holdout_ledger=world["reg"])
    assert MP.holdout_cut_of(loaded.info) == cut
    # the chokepoint itself: ``run`` refuses a window that ends after the boundary even when a caller skipped the CLI's validation
    import ms_portfolio as PF
    import test_ms_pipeline as TMP
    DAYS = 86_400_000
    late = RP.ReplayConfig(start_ms=cut - 40 * H1, end_ms=cut + DAYS, fill=TMP.FILL, hold_ms=2 * DAYS, decision_every_ms=8 * H1, spec_digest=TMP.DIGEST,
                           portfolio=PF.PortfolioPolicy(max_wait_ms=3 * H1, style="MAKER_FIRST"))
    with pytest.raises(MP.PipelineRefused, match="WINDOW_PAST_THE_HOLDOUT_BOUNDARY"):
        MP.run(loaded, spec=TMP.SPEC, candidate_id=TMP.CAND, spec_digest=TMP.DIGEST, replay_cfg=late, artifacts_dir=str(world["tmp"] / "late_art"), competition_row=TMP.PROMOTED_ROW)
    assert not os.path.exists(str(world["tmp"] / "late_art")), "a refused run writes nothing"


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
                      ("history_holdout.py", "dataset_problem_for_a_reservation", "load_history_root"): "the reservation gate (Round 19): coverage state only, run BEFORE a boundary exists; it returns a refusal text, never a series",
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


def _shipped_python(base=None):
    """``(basename, path)`` of every shipped .py under bot/. Tests, caches, hidden directories and real VIRTUAL ENVIRONMENTS (a directory holding ``pyvenv.cfg``, or ``site-packages`` /
    ``node_modules``) are not shipped; nothing is skipped by NAME otherwise (the re-review's leak planted under ``bot/lib`` passed both tripwires while ``lib``, ``data``, ``env`` and ``venv`` were
    skipped). A RECURSIVE walk: both tripwires used to read only bot/*.py and bot/tools/*.py, so a new subdirectory was a hiding place (independent re-review, finding 10)."""
    out = []
    for root, dirs, files in os.walk(base or BOT):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("tests", "site-packages", "node_modules", "__pycache__") and not os.path.exists(os.path.join(root, d, "pyvenv.cfg")))
        out.extend((name, os.path.join(root, name)) for name in sorted(files) if name.endswith(".py"))
    return out


def test_the_shipped_code_walker_skips_only_what_is_not_shipped(tmp_path):
    for rel, content in (("tools/a.py", ""), ("lib/b.py", ""), ("data/c.py", ""), ("env/d.py", ""), ("venv_named_but_real/e.py", ""), ("research/sub/f.py", ""),
                         ("tests/skip_t.py", ""), (".hidden/skip_h.py", ""), ("__pycache__/skip_c.py", ""), ("realvenv/skip_v.py", ""), ("realvenv/pyvenv.cfg", "home = /usr\n"),
                         ("x/site-packages/skip_s.py", ""), ("x/node_modules/skip_n.py", "")):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    got = sorted(os.path.relpath(p, str(tmp_path)) for _n, p in _shipped_python(str(tmp_path)))
    assert got == ["data/c.py", "env/d.py", "lib/b.py", "research/sub/f.py", "tools/a.py", "venv_named_but_real/e.py"], got


def _references():
    """Every reference to the unguarded loader (by any spelling) and to candidate_readiness's private row reader. ``_rows`` is a common helper name, so only the references that can
    resolve to candidate_readiness's are counted: an attribute of CR / candidate_readiness, a bare name inside candidate_readiness.py itself, an import of it, a getattr string in a module
    that imports candidate_readiness."""
    out = []
    for name, path in _shipped_python():
        if True:
            tree = ast.parse(open(path, encoding="utf-8").read())
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


def _opt_out_references(tree):
    """Line numbers of every CODE reference to the opt-out sentinel, by any spelling: a name, an attribute, a from-import (also with an alias), or the exact string handed to getattr. A mention
    inside a longer string (a docstring, an error message) is not a reference."""
    return sorted({node.lineno for node in ast.walk(tree)
                   if (isinstance(node, ast.Name) and node.id == "UNGUARDED") or (isinstance(node, ast.Attribute) and node.attr == "UNGUARDED")
                   or (isinstance(node, ast.alias) and node.name == "UNGUARDED") or (isinstance(node, ast.Constant) and node.value == "UNGUARDED")})


def test_the_opt_out_detector_sees_every_spelling_and_ignores_prose():
    planted = {
        "name": "UNGUARDED",
        "attribute": "import history_holdout as HH\nx = HH.UNGUARDED",
        "from_import": "from history_holdout import UNGUARDED",
        "aliased": "from history_holdout import UNGUARDED as _U\nuse(_U)",             # the spelling the previous tripwire missed (re-review, finding 10)
        "getattr": "import history_holdout as HH\nx = getattr(HH, 'UNGUARDED')",
        "dunder_import": "x = __import__('history_holdout').UNGUARDED",
    }
    for kind, src in planted.items():
        assert _opt_out_references(ast.parse(src)), f"the detector missed the {kind} spelling"
    prose = 'def f():\n    """pass history_holdout.UNGUARDED to read the whole window"""\n    raise ValueError("or the explicit history_holdout.UNGUARDED sentinel")'
    assert _opt_out_references(ast.parse(prose)) == []


def test_the_explicit_opt_out_never_appears_in_shipped_code():
    """A CODE reference (a name, an attribute, an import, a getattr string), not a mention in a docstring or an error message; every shipped file under bot/, recursively."""
    files = [(n, p) for n, p in _shipped_python() if n != "history_holdout.py"]
    assert len(files) > 100 and any(os.sep + "tools" + os.sep in p for _n, p in files), "the walker found no shipped code: the tripwire would pass vacuously"
    offenders = [f"{os.path.relpath(p, BOT)}:{line}" for _n, p in files for line in _opt_out_references(ast.parse(open(p, encoding="utf-8").read()))]
    assert not offenders, f"UNGUARDED is for tests and embeddings: {offenders}"


# ------------------------------------------------------------------ Round 19: the one-time holdout is never reserved or spent on data the dataset gate does not call complete and sufficient
def _bytes(path):
    return open(path, "rb").read() if os.path.exists(path) else None


def test_a_reservation_on_a_dataset_the_gate_calls_incomplete_is_refused_and_records_nothing(tmp_path, offline, monkeypatch):
    """Round 19 (data specialist F1, reproduced): the default fixture reads DATA_INCOMPLETE (three symbols PARTIAL over the funding window) and `--reserve` froze the boundary on it. It is
    irreversible: the reserved region can never be un-reserved, so a reservation on a partial cross-section spends the one holdout the owner has."""
    root, uni, spot = _hist(tmp_path)                                       # complete_window=False: the mark request ends 989 ms before the funding request
    _s, info = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    assert info["dataset_state"]["state"] == CR.DATA_INCOMPLETE
    reg = str(tmp_path / "registry" / "h.jsonl")
    with pytest.raises(RH.HarnessError, match="DATASET_NOT_COMPLETE_FOR_A_HOLDOUT.*DATA_INCOMPLETE"):
        HH.reserve(reg, root, mark_interval=MARK_IV, ledger_dir=str(tmp_path / "led"), universe_dir=uni, spot_dir=spot)
    assert not os.path.exists(reg), "a refused reservation must not create or touch the registry"
    assert HH.main(["--reserve", "--history-root", root, "--mark-interval", MARK_IV, "--universe-dir", uni, "--spot-dir", spot, "--registry", reg, "--ledger-dir", str(tmp_path / "led")]) == 2
    assert not os.path.exists(reg)


def test_a_reservation_without_the_universe_capture_is_refused_not_unguarded(world):
    for missing in (None, ""):
        with pytest.raises(RH.HarnessError, match="UNIVERSE_DIR_REQUIRED"):
            HH.reserve(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"], universe_dir=missing)
    assert HH.main(["--reserve", "--history-root", world["root"], "--mark-interval", MARK_IV, "--ledger-dir", world["ldir"]]) == 2
    assert not os.path.exists(world["reg"])


def test_a_window_that_carries_one_test_but_not_one_test_after_the_reserve_is_refused(world, monkeypatch):
    """The window is sufficient for ONE registered evaluation only BEFORE the cut (15 rows = 142 days against a 199.7-day window): after reserving 30% the explore window is 139 days. The
    historical plan arithmetic called this 'sufficient' (591 days study span against a 457-day need leaves 413 after the reserve) and the reservation went through."""
    monkeypatch.setattr(CR, "STATE_MIN_ROWS", 15)
    _s, whole = CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV)
    assert whole["dataset_state"]["sufficient"] is True, whole["dataset_state"]
    with pytest.raises(RH.HarnessError, match="EXPLORE_WINDOW_NOT_SUFFICIENT_AFTER_THE_RESERVE.*DATA_VALID_BUT_INSUFFICIENT"):
        _reserve(world)
    assert not os.path.exists(world["reg"])
    monkeypatch.setattr(CR, "STATE_MIN_ROWS", 14)                             # the same data against the bar its explore window carries (135 days <= 139): the reservation is made
    assert _reserve(world)["kind"] == "reserve"


def test_the_production_bar_refuses_a_short_history_without_any_patching(tmp_path, offline, monkeypatch):
    """The REGISTERED 60 rows (457 days), not the module's lowered fixture bar, against the 199.7-day fixture. The reservation that used to succeed here is refused as insufficient."""
    monkeypatch.setattr(CR, "STATE_MIN_ROWS", 60)
    assert (CR.STATE_FORMATION_DAYS, CR.STATE_HOLD_DAYS, CR.STATE_MIN_ROWS) == (30, 7, 60)
    root, uni, spot = _hist(tmp_path, complete_window=True)
    reg = str(tmp_path / "r.jsonl")
    with pytest.raises(RH.HarnessError, match="EXPLORE_WINDOW_NOT_SUFFICIENT_AFTER_THE_RESERVE"):
        HH.reserve(reg, root, mark_interval=MARK_IV, ledger_dir=str(tmp_path / "led"), universe_dir=uni, spot_dir=spot)
    assert not os.path.exists(reg)


def test_an_existing_reservation_is_still_returned_unchanged_whatever_the_gate_now_says(world, monkeypatch):
    first = _reserve(world)
    monkeypatch.setattr(CR, "STATE_MIN_ROWS", 60)                             # the gate would now refuse: an existing reservation is idempotent, never re-judged or moved
    again = _reserve(world)
    assert again["already_reserved"] is True and again["from_ms"] == first["from_ms"]


def test_the_final_read_is_not_spent_on_a_dataset_that_is_no_longer_complete(world, monkeypatch):
    """Round 19 (data specialist F1): `final_read` computed `info['dataset_state']` and recorded the one read whatever it said (DATA_INCOMPLETE on 3 of 4 symbols: spent, and a second read
    refused). The refusal comes before the record: the registry is byte-identical, the holdout is still reserved, and the read can still be made once the dataset is repaired."""
    _reserve(world)
    before = _bytes(world["reg"])
    real = CR.load_history_root

    def regressed(*a, **k):
        series, info = real(*a, **k)
        info["dataset_state"] = CR.dataset_state({"incomplete": ["3 loaded symbols are not COMPLETE over the whole required window"]})
        return series, info
    monkeypatch.setattr(CR, "load_history_root", regressed)
    with pytest.raises(RH.HarnessError, match="FINAL_READ_REFUSED_DATASET_NOT_COMPLETE.*DATA_INCOMPLETE"):
        _final(world)
    assert _bytes(world["reg"]) == before and HH.status(world["reg"], world["root"], mark_interval=MARK_IV, ledger_dir=world["ldir"])["state"] == "RESERVED"
    monkeypatch.setattr(CR, "load_history_root", real)                         # repaired: the read is still available, once
    series, info = _final(world)
    assert info["holdout"]["status"] == "SPENT" and series
    with pytest.raises(RH.HarnessError, match="FINAL_READ_ALREADY_SPENT"):
        _final(world)
