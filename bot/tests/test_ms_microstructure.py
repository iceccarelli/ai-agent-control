"""ms_microstructure: every fixture is written by the REAL market_collector writer (run_once with injected fetch / clock / sleep); offline, tmp dirs only."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import market_collector as MC  # noqa: E402
import ms_microstructure as MS  # noqa: E402
import test_market_collector as TM  # noqa: E402

SYM = "BTCUSDT"
T0, STEP = TM.T0, TM.STEP


GLOBAL_TRADES = [T0 - 1000 + 30_000 * k for k in range(12)]       # one consistent venue tape; each cycle's file is the slice of it that its 60 s window and its receive time see


def tt(i):
    return T0 + i * STEP


def book(i, *, bids=None, asks=None, ts=None, **over):
    t = tt(i)
    ts = t - 100 if ts is None else ts
    kw = dict(bids=bids or [["100.0", "2"], ["99.9", "3"]], asks=asks or [["100.1", "1"], ["100.2", "4"]], u=10 + i, seq=10 + i, cts=ts)
    kw.update(over)
    return TM.book_body(SYM, ts, **kw)


def tape(i, rows=None):
    """rows: [(time, price, size, side)] in any order; default = the Sell trades at 100.0 of GLOBAL_TRADES that fall in [cycle - 61 s, cycle - 0.2 s] (so consecutive files overlap)."""
    t = tt(i)
    rows = rows if rows is not None else [(g, "100.0", "0.5", "Sell") for g in GLOBAL_TRADES if t - STEP - 1000 <= g <= t - 200]
    body = [TM.trade(r[0], SYM, price=r[1], size=r[2], side=r[3], time=str(r[0])) for r in sorted(rows, key=lambda r: -r[0])]      # execId e<time>, newest first
    return TM.trades_body(SYM, t, body)


def build(root, n=3, books=None, tapes=None, **cycle_kw):
    books, tapes = books or {}, tapes or {}
    out = []
    for i in range(n):
        v = TM.Venue()
        v.script[(MC.PATH_BOOK, SYM)] = [books[i] if i in books else book(i)]
        v.script[(MC.PATH_TRADES, SYM)] = [tapes[i] if i in tapes else tape(i)]
        res = TM.cycle(root, v, i, max_symbols=1, **cycle_kw)
        assert res["status"] in ("OK", "PARTIAL"), res                   # a defective book makes the REAL collector mark the cycle PARTIAL; it is still recorded
        out.append(res)
    return out


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "collect")


def snaps(ds):
    return ds.snapshots(SYM)


# ---------------------------------------------------------------- positive control and contract fields

def test_positive_control_a_verified_snapshot_carries_every_contract_field(root):
    build(root)
    ds = MS.load_cycles(root)
    assert ds.problems == () and ds.verified and all(c.problems == () for c in ds.cycles)
    s1 = snaps(ds)[1]
    d = s1.local_receive_ms + 10
    got = ds.snapshot_at(SYM, d)
    assert got is not None and got.cycle == f"cycle-{tt(1)}" and got.symbol == SYM and got.rejections == () and got.valid
    assert got.book_age_ms == 10 and got.local_receive_ms == s1.local_receive_ms <= d
    assert (got.ts, got.cts, got.u, got.seq) == (tt(1) - 100, tt(1) - 100, 11, 11)
    assert [(x.price, x.qty) for x in got.bids] == [(100.0, 2.0), (99.9, 3.0)] and [(x.price, x.qty) for x in got.asks] == [(100.1, 1.0), (100.2, 4.0)]
    assert got.spread_bps == pytest.approx(0.1 / 100.05 * 1e4) and got.mid == pytest.approx(100.05)
    assert got.depth_usd_bid == pytest.approx(100.0 * 2 + 99.9 * 3) and got.depth_usd_ask == pytest.approx(100.1 + 100.2 * 4)
    assert got.trades_window_from_ts == tt(0) - 100 and got.tape_complete is True
    assert got.trades == ((T0 + 29_000, 100.0, 0.5, "Sell"), (T0 + 59_000, 100.0, 0.5, "Sell")) and got.status == ()
    # the first snapshot has no previous one: its window is unknown, flagged, not guessed
    first = ds.snapshot_at(SYM, snaps(ds)[0].local_receive_ms + 5)
    assert first is not None and first.tape_complete is False and "NO_PREVIOUS_SNAPSHOT" in first.status and first.trades == ()


def test_cohort_and_identity_are_recorded(root):
    build(root)
    ds = MS.load_cycles(root)
    assert ds.cohort.kind == MS.COHORT_NARROW_L2 != MS.COHORT_BROAD_HISTORY and ds.cohort_id.startswith("narrow_l2:") and ds.cohort.symbols == (SYM,)
    assert ds.cohort.rules == (MC.SELECTION_RULE,) and len(ds.data_digest()) == 64
    assert MS.load_cycles(root).data_digest() == ds.data_digest() and MS.load_cycles(root).cohort_id == ds.cohort_id


# ---------------------------------------------------------------- decision-time eligibility

def test_stale_snapshot_is_rejected_with_a_reason(root):
    build(root)
    ds = MS.load_cycles(root)
    r = snaps(ds)[1].local_receive_ms
    assert ds.snapshot_at(SYM, r + 5_000) is not None
    s, why = ds.explain_at(SYM, r + 5_001)
    assert s is None and "STALE_SNAPSHOT" in why
    assert ds.snapshot_at(SYM, r + 5_001, max_age_ms=6_000) is not None                 # the bound is an explicit, stated parameter


def test_future_snapshot_is_never_attached_look_ahead(root):
    build(root)
    ds = MS.load_cycles(root)
    s0, s1 = snaps(ds)[0], snaps(ds)[1]
    s, why = ds.explain_at(SYM, s0.local_receive_ms - 1)
    assert s is None and why[0] == "NO_SNAPSHOT_AT_OR_BEFORE_DECISION" and "FUTURE_SNAPSHOT_NOT_USED" in why
    # between cycles the OLDER book is the only candidate, even when the age cap would allow either: the newer one is in the future
    got = ds.snapshot_at(SYM, s1.local_receive_ms - 1, max_age_ms=10**9)
    assert got is not None and got.cycle == s0.cycle and got.local_receive_ms <= s1.local_receive_ms - 1
    assert ds.snapshot_at(SYM, s1.local_receive_ms, max_age_ms=10**9).cycle == s1.cycle        # exactly at the receive time it is eligible


def test_unrelated_symbol_and_bad_decision_time_yield_none(root):
    build(root)
    ds = MS.load_cycles(root)
    assert ds.snapshot_at("ETHUSDT", snaps(ds)[1].local_receive_ms + 1) is None
    assert ds.explain_at(SYM, 0)[1] == ("BAD_DECISION_TIME",) and ds.explain_at(SYM, True)[1] == ("BAD_DECISION_TIME",)


# ---------------------------------------------------------------- per-snapshot validation

def _reasons_at(ds, i=1):
    return ds.explain_at(SYM, snaps(ds)[i].local_receive_ms + 1)


def test_crossed_book_is_rejected_and_not_repaired(root):
    build(root, books={1: book(1, bids=[["100.2", "1"]], asks=[["100.1", "1"]])})
    ds = MS.load_cycles(root)
    s, why = _reasons_at(ds)
    assert s is None and "CROSSED_BOOK" in why and "COLLECTOR_VALIDATION_FAILED" in why
    assert ds.snapshot_at(SYM, snaps(ds)[0].local_receive_ms + 1) is not None          # the neighbours are untouched


def test_equal_touch_is_crossed_too_strict_inequality(root):
    build(root, books={1: book(1, bids=[["100.1", "1"]], asks=[["100.1", "1"]])})
    assert "CROSSED_BOOK" in _reasons_at(MS.load_cycles(root))[1]


@pytest.mark.parametrize("bids,asks,code", [
    ([["99.9", "1"], ["100.0", "1"]], None, "NON_MONOTONE_BIDS"),
    (None, [["100.2", "1"], ["100.1", "1"]], "NON_MONOTONE_ASKS"),
    ([["100.0", "0"]], None, "BAD_LEVEL_BIDS"),
    ([["100.0", "-1"]], None, "BAD_LEVEL_BIDS"),
    ([["nan", "1"]], None, "BAD_LEVEL_BIDS"),
    (None, [["inf", "1"]], "BAD_LEVEL_ASKS"),
    ([["100.0", "1"], ["100.0", "2"]], None, "NON_MONOTONE_BIDS"),
])
def test_ladder_defects_are_rejected(root, bids, asks, code):
    build(root, books={1: book(1, bids=bids, asks=asks)})
    assert code in _reasons_at(MS.load_cycles(root))[1]


def test_empty_side_is_rejected(root):
    build(root, books={1: TM.book_body(SYM, tt(1) - 100, bids=[], u=11, seq=11, cts=tt(1) - 100)})
    s, why = _reasons_at(MS.load_cycles(root))
    assert s is None and "EMPTY_OR_BAD_BIDS" in why


def test_payload_symbol_must_equal_the_file_symbol(root):
    build(root, books={1: book(1, s="ETHUSDT")})
    s, why = _reasons_at(MS.load_cycles(root))
    assert s is None and "SYMBOL_MISMATCH" in why


def test_duplicate_symbol_in_a_cycle_rejects_every_copy(root):
    build(root)
    cdir = os.path.join(root, f"cycle-{tt(1)}")
    mp = os.path.join(cdir, "manifest.json")
    man = json.load(open(mp))
    man["artifacts"].append(dict(next(a for a in man["artifacts"] if a["kind"] == "book")))
    raw = (json.dumps(man, indent=2, sort_keys=True) + "\n").encode()
    open(mp, "wb").write(raw)
    open(os.path.join(cdir, "COMPLETE"), "w").write(MC._sha(raw) + "\n")                # a careful forger re-seals the marker
    ds = MS.load_cycles(root, verify=False)
    snaps1 = [s for s in snaps(ds) if s.cycle == f"cycle-{tt(1)}"]
    assert len(snaps1) == 2 and all("DUPLICATE_SYMBOL" in s.rejections for s in snaps1)
    assert ds.snapshot_at(SYM, snaps1[0].local_receive_ms + 1) is None
    dv = MS.load_cycles(root)                                                              # with verification the chain notices the re-seal as well
    assert any("CHAIN_MANIFEST_SHA_MISMATCH" in c.problems for c in dv.cycles)


def test_tampered_file_hash_rejects_the_whole_cycle(root):
    build(root)
    p = os.path.join(root, f"cycle-{tt(1)}", "books", f"{SYM}.json")
    raw = open(p, "rb").read()
    open(p, "wb").write(raw.replace(b'"100.0"', b'"100.05"', 1))                       # same length: only the hash notices
    ds = MS.load_cycles(root)
    s, why = _reasons_at(ds)
    assert s is None and any(w.startswith("CYCLE:HASH_MISMATCH") for w in why)
    assert ds.cycles[1].problems and ds.snapshot_at(SYM, snaps(ds)[0].local_receive_ms + 1) is not None
    assert not snaps(ds)[1].cycle_ok and snaps(ds)[1].tape_rows == ()                  # its tape is not usable evidence either
    unv = MS.load_cycles(root, verify=False)                                           # verify=False is an explicit opt-out and says so
    assert unv.verified is False and unv.snapshot_at(SYM, snaps(unv)[1].local_receive_ms + 1) is not None


def test_missing_manifest_and_missing_complete_marker(root):
    build(root)
    os.remove(os.path.join(root, f"cycle-{tt(1)}", "manifest.json"))
    ds = MS.load_cycles(root)
    assert any("MANIFEST_MISSING" in c.problems for c in ds.cycles)
    s, why = ds.explain_at(SYM, tt(1) + 100)
    assert s is None and any("MANIFEST_MISSING" in w for w in why)
    os.remove(os.path.join(root, f"cycle-{tt(2)}", "COMPLETE"))
    ds2 = MS.load_cycles(root)
    assert any("COMPLETE_MISSING" in c.problems for c in ds2.cycles) and snaps(ds2)[-1].rejections


def test_broken_or_missing_log_chain_rejects_everything(root):
    build(root)
    log = os.path.join(root, MC.LOG_NAME)
    raw = open(log, "rb").read()
    open(log, "wb").write(raw.replace(b'"status"', b'"statuz"', 1))
    ds = MS.load_cycles(root)
    assert any(p.startswith("LOG_CHAIN_BROKEN") for p in ds.problems) and all(s.rejections for s in snaps(ds))
    os.remove(log)
    ds2 = MS.load_cycles(root)
    assert "LOG_MISSING" in ds2.problems and all(s.rejections for s in snaps(ds2))


def test_unrecorded_and_missing_cycle_dirs_are_flagged(root):
    build(root)
    os.rename(os.path.join(root, f"cycle-{tt(2)}"), os.path.join(root, f"cycle-{tt(2) + 5}"))
    ds = MS.load_cycles(root)
    assert f"MISSING_CYCLE_DIR:cycle-{tt(2)}" in ds.problems


def test_disconnected_sequence_is_flagged_discontinuous_and_not_used(root):
    build(root, books={1: book(1, u=3, seq=11)})                                         # u regressed from 10 to 3
    ds = MS.load_cycles(root)
    s, why = _reasons_at(ds)
    assert s is None and "DISCONTINUOUS" in why and "DISCONTINUOUS" in snaps(ds)[1].status
    build_root2 = root + "2"
    build(build_root2, books={1: book(1, u=1, seq=11)})                                  # a reset to 1 after a larger u is a re-snapshot, not continuity
    assert "DISCONTINUOUS" in _reasons_at(MS.load_cycles(build_root2))[1]
    root3 = root + "3"
    build(root3, books={1: book(1, seq=2)})
    assert "DISCONTINUOUS" in _reasons_at(MS.load_cycles(root3))[1]


def test_u_step_bound_is_opt_in(root):
    build(root, books={1: book(1, u=10_000)})
    assert _reasons_at(MS.load_cycles(root))[0] is not None                              # REST polls cannot prove +1: no bound by default
    assert "DISCONTINUOUS" in _reasons_at(MS.load_cycles(root, params=MS.MicroParams(max_u_gap=100)))[1]


def test_exchange_timestamps_must_be_strictly_increasing(root):
    build(root, books={1: book(1, ts=tt(0) - 500, cts=tt(0) - 500)})
    assert "NON_MONOTONE_TS" in _reasons_at(MS.load_cycles(root))[1]
    root2 = root + "2"
    build(root2, books={1: book(1, ts=tt(0) - 100, cts=tt(0) - 100)})
    assert "DUPLICATE_BOOK_TS" in _reasons_at(MS.load_cycles(root2))[1]


def test_clock_consistency_between_exchange_and_local_receive(root):
    build(root, books={1: book(1, ts=tt(1) + 30_000, cts=tt(1) + 30_000)})
    assert "CLOCK_SKEW_EXCHANGE_AHEAD" in _reasons_at(MS.load_cycles(root))[1]
    root2 = root + "2"
    build(root2, books={1: book(1, ts=tt(1) - 60_000, cts=tt(1) - 60_000)}, tapes={})
    why = _reasons_at(MS.load_cycles(root2))[1]
    assert "STALE_AT_RECEIVE" in why
    root3 = root + "3"
    build(root3, books={1: book(1, cts=tt(1) + 30_000)})
    assert "CTS_AFTER_TS" in _reasons_at(MS.load_cycles(root3))[1]


# ---------------------------------------------------------------- the tape

def test_tape_between_complete_when_covered_and_gap_when_not(root):
    build(root)
    ds = MS.load_cycles(root)
    a, b = snaps(ds)[0], snaps(ds)[1]
    ok = ds.tape_between(SYM, a.ts, b.tape_recv_ms)
    assert ok.complete and [x[0] for x in ok.trades] == [T0 + 29_000, T0 + 59_000] and ok.reasons == ()
    far = ds.tape_between(SYM, tt(0) - 10 * STEP, tt(0))
    assert not far.complete and "TAPE_GAP" in far.reasons
    beyond = ds.tape_between(SYM, a.ts, snaps(ds)[2].tape_recv_ms + 1)
    assert not beyond.complete and "TAPE_GAP" in beyond.reasons                      # nothing recorded after the last file: unknown, not "no trades"
    assert ds.tape_between(SYM, 5, 5).complete


def test_a_tape_that_does_not_reach_back_to_the_previous_snapshot_is_a_gap(root):
    late = [(tt(1) - 3_000, "100.0", "0.5", "Sell"), (tt(1) - 200, "100.0", "0.5", "Sell")]          # oldest trade is AFTER the previous snapshot: older ones fell off the list
    build(root, tapes={1: tape(1, late)})
    ds = MS.load_cycles(root)
    s = snaps(ds)[1]
    assert s.tape_complete is False and "TAPE_GAP_BEFORE" in s.status
    assert not ds.tape_between(SYM, snaps(ds)[0].ts, s.tape_recv_ms).complete


def test_invalid_tape_is_unusable_never_repaired(root):
    bad = TM.trades_body(SYM, tt(1), [TM.trade(1, "ETHUSDT", time=str(tt(1) - 100))])             # a trade of another symbol in this symbol's tape
    build(root, tapes={1: bad})
    ds = MS.load_cycles(root)
    s = snaps(ds)[1]
    assert s.tape_ok is False and "TAPE_INVALID" in s.status and s.trades == () and s.tape_rows == ()
    assert s.valid                                                                       # the book itself is fine; the tape is simply not evidence


def test_conflicting_duplicate_exec_ids_across_files_make_the_tape_incomplete(root):
    r0 = [(tt(0) - 500, "100.0", "0.5", "Sell"), (tt(0) - 300, "100.0", "0.5", "Sell")]
    build(root, tapes={0: tape(0, r0), 1: tape(1, [(tt(0) - 500, "100.0", "0.5", "Sell"), (tt(0) - 300, "99.0", "0.5", "Sell"), (tt(1) - 200, "100.0", "0.5", "Sell")])})
    ds = MS.load_cycles(root)
    t = ds.tape_between(SYM, tt(0) - 1000, tt(0) - 100)
    assert not t.complete and "TAPE_CONFLICT" in t.reasons


# ---------------------------------------------------------------- cohorts

def _cohort_root(root, rule, n=2):
    syms = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    for i in range(n):
        v = TM.Venue(pages={"": [TM.inst(s) for s in syms]}, tickers=[TM.ctick("BTCUSDT", "9000000"), TM.ctick("ETHUSDT", "8000000"), TM.ctick("SOLUSDT", "7000000")])
        res = TM.cycle(root, v, i, max_symbols=2, selection_rule=rule)
        assert res["status"] in ("OK", "PARTIAL"), res                   # a defective book makes the REAL collector mark the cycle PARTIAL; it is still recorded


def test_cohort_v2_selection_never_evaluates_a_funding_edge(root):
    _cohort_root(root, "cohort_v2")
    ds = MS.load_cycles(root, verify=True)
    assert ds.cohort.rules == ("cohort_v2",) and ds.cohort.kind == MS.COHORT_NARROW_L2 and ds.cohort.funding_independent is False
    assert MS.evaluates_funding_edge_ok(ds) is False
    assert set(ds.symbols()) == {"BTCUSDT", "ETHUSDT"} and all(c[2] for c in ds.cohort.per_cycle)       # inputs digest recorded


def test_turnover_rule_does_not_read_funding_but_is_still_narrow(root):
    _cohort_root(root, MC.SELECTION_RULE)
    ds = MS.load_cycles(root)
    assert MS.evaluates_funding_edge_ok(ds) is True and ds.cohort.kind == MS.COHORT_NARROW_L2


def test_mixed_or_unknown_or_empty_cohorts_fail_closed(root):
    _cohort_root(root, MC.SELECTION_RULE, n=1)
    v = TM.Venue(pages={"": [TM.inst(s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]}, tickers=[TM.ctick("BTCUSDT", "9000000"), TM.ctick("ETHUSDT", "8000000"), TM.ctick("SOLUSDT", "7000000")])
    assert TM.cycle(root, v, 1, max_symbols=2, selection_rule="cohort_v2")["status"] == "OK"
    assert MS.evaluates_funding_edge_ok(MS.load_cycles(root)) is False                # one funding-scored cycle taints the dataset
    empty = root + "e"
    os.makedirs(empty)
    assert MS.evaluates_funding_edge_ok(MS.load_cycles(empty, verify=False)) is False
    with pytest.raises(ValueError):
        MS.load_cycles(os.path.join(root, "nope"))


def test_symbols_filter_and_hostile_manifest_never_raise(root):
    build(root)
    assert MS.load_cycles(root, symbols=["ETHUSDT"]).symbols() == ()
    p = os.path.join(root, f"cycle-{tt(1)}", "manifest.json")
    open(p, "w").write("[1,2,3]")
    ds = MS.load_cycles(root)
    assert any(c.problems for c in ds.cycles)


def test_module_is_offline_and_credential_free():
    import ast
    src = open(os.path.join(BOT, "tools", "ms_microstructure.py")).read()
    mods = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(ast.parse(src)) for a in (n.names if isinstance(n, (ast.Import, ast.ImportFrom)) else [])
            if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert not mods & {"socket", "requests", "urllib", "urllib.request", "http.client", "websockets", "hmac", "subprocess"}
    for bad in ("os.environ", "getenv", "bot/state", ".env", "api_key", "secret"):
        assert bad not in src
