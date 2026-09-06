"""Slice 40 — S1/S2/S3, the structural repair of the directed null.

THE DEFECT THESE TESTS PIN
==========================
The directed control was VALID on the truncated 890-date window (z ≈ +0.3) and
INVALID on the full 1,461-date one (z ≈ +2.1, on **both** symbols). Slice 39
removed the only known data defect and the control did not move by a
thousandth. The bias is structural, and it is in the direction that flatters
the signal.

Located before any code changed, identically on ETH and SOL:

    flagged bars                       153
    flags & eligible                   152      <- 2026-07-31 fails the embargo
    REAL entries, from `flags`         125
    NULL slots,   from `flags & eligible`  124
    direction_seq length               125  (70 long / 55 short)
    entries actually scored            124  (70 long / 54 short)

So the real schedule carried one entry whose five-bar horizon runs past the end
of the series. It was never scored — `barrier_r_for_all_bars` refuses to
produce an R for it — but it *was* ranked as an entry and it *was* in the
direction sequence the nulls recycle. Every replicate cycled 125 directions
over 124 slots.

WHAT IS ASSERTED HERE
=====================
S1  the embargo is applied to the flag array BOTH sides build from
S2  the direction sequence is exactly the entered, embargo-passing set —
    never the flagged set, never the incomplete one
S3  surrogate slots come from the same schedule builder on the same array,
    with a genuinely rotated direction sequence attached

The regression that matters most is `test_the_sequence_is_not_the_flagged_set`:
124 against 153 is the difference between this instrument and the slice-35 one
that scored 37 of 72 trades in the wrong direction.
"""
from __future__ import annotations

import ast
import inspect
import os
import sys

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import control_directed as cd  # noqa: E402
import edge_measurement as em  # noqa: E402
import market_data as md  # noqa: E402
import skill_test as sk  # noqa: E402
from signals import btc_alt_spillover_v1 as sp  # noqa: E402

WARMUP = 200


class Args:
    """The frozen geometry, as both tools pass it."""
    multiplier = sp.SHOCK_MULTIPLIER
    atr_period = sp.ATR_PERIOD
    horizon = sp.HORIZON
    stop_atr = sp.STOP_ATR
    take_profit_atr = sp.TAKE_PROFIT_ATR
    round_trip_bps = sp.ROUND_TRIP_BPS
    lockup = sp.LOCKUP


def corpus(symbol="ETHUSDT"):
    loaded, _b, _n = md.load_corpus(os.path.join(REPO, "data", "real_multi_1d"))
    alt = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
           for b in loaded[symbol]]
    btc_loaded, _bb, _bn = md.load_corpus(os.path.join(REPO, "data", "real_1d"))
    btc = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
           for b in btc_loaded[sorted(btc_loaded)[0]]]
    return alt, btc


def built(symbol="ETHUSDT"):
    alt, btc = corpus(symbol)
    return alt, cd.build_book_and_flags(alt, btc, warmup=WARMUP, args=Args())


# ---------------------------------------------------------------- S1 -------


class TestS1HardHorizonEmbargo:
    """An entry whose horizon cannot complete is not ranked, on either side."""

    def test_the_embargoed_flag_is_removed_from_the_shared_array(self):
        _alt, (flags, eligible, *_rest) = built()
        tradable = em.tradable_flags(flags, eligible)
        removed = [i for i in range(flags.size) if flags[i] and not tradable[i]]
        assert removed, "the corpus must still contain the tail flag this pins"
        for index in removed:
            assert not eligible[index]

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_the_real_schedule_no_longer_contains_it(self, symbol):
        """125 -> 124, and the lost one is the unscorable tail entry."""
        _alt, (flags, eligible, directions, side_lookup, *_r) = built(symbol)
        before = sk.simulate_schedule(flags, warmup=WARMUP, lockup=Args.lockup)
        after = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                     warmup=WARMUP, lockup=Args.lockup)
        assert set(before) - set(after)
        for lost in set(before) - set(after):
            assert lost not in side_lookup[directions[lost]]
        assert set(after) - set(before) == set()

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_every_surviving_entry_can_actually_be_scored(self, symbol):
        """The property, not the count: no entry is left that has no R."""
        _alt, (flags, eligible, directions, side_lookup, *_r) = built(symbol)
        entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                       warmup=WARMUP, lockup=Args.lockup)
        for entry in entries:
            assert entry in side_lookup[directions[entry]]

    def test_both_sides_build_from_one_array(self):
        """S1's point: the real path and the nulls start from the same flags."""
        _alt, (flags, eligible, *_rest) = built()
        real = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                    warmup=WARMUP, lockup=Args.lockup)
        region = em.tradable_flags(flags, eligible)[WARMUP:]
        unrotated = np.zeros_like(flags)
        unrotated[WARMUP:] = np.roll(region, region.size)   # identity roll
        null = sk.simulate_schedule(unrotated, warmup=WARMUP,
                                    lockup=Args.lockup)
        assert real == null

    def test_an_incomplete_barrier_is_never_scored_as_zero(self):
        """A synthetic short tail: the last bars cannot resolve a horizon.

        The failure this forbids is subtle — scoring an unresolved trade as
        0.0 R rather than dropping it. A zero is a *result*, and a null full
        of manufactured zeros has a mean pulled toward the middle while the
        observed side keeps its real spread.
        """
        bars = [bt.Bar(i * 86_400_000, 100.0, 101.0, 99.0, 100.0 + 0.01 * i,
                       10.0) for i in range(60)]
        idx, net, _u = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=sp.TAKE_PROFIT_ATR, stop_atr=sp.STOP_ATR,
            horizon=sp.HORIZON, atr_period=sp.ATR_PERIOD,
            round_trip_bps=sp.ROUND_TRIP_BPS, side="long",
            entry_on=sp.ENTRY_ON)
        # Nothing within a full horizon of the end may appear AT ALL. Not
        # "appears with a 0.0" — a zero is a result, and a null padded with
        # manufactured zeros has its mean pulled toward the middle while the
        # observed side keeps its real spread.
        assert idx.size
        assert int(idx.max()) < len(bars) - sp.HORIZON - 1
        assert net.size == idx.size
        embargoed = set(range(len(bars) - sp.HORIZON - 1, len(bars)))
        assert not (set(int(i) for i in idx) & embargoed)

    def test_the_embargo_scales_with_the_horizon(self):
        """Not a hard-coded seven bars: a longer horizon embargoes more."""
        bars = [bt.Bar(i * 86_400_000, 100.0, 101.0, 99.0, 100.0 + 0.01 * i,
                       10.0) for i in range(80)]
        last = {}
        for horizon in (5, 10, 20):
            idx, _net, _u = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=sp.TAKE_PROFIT_ATR,
                stop_atr=sp.STOP_ATR, horizon=horizon,
                atr_period=sp.ATR_PERIOD,
                round_trip_bps=sp.ROUND_TRIP_BPS, side="long",
                entry_on=sp.ENTRY_ON)
            last[horizon] = int(idx.max())
        assert last[5] > last[10] > last[20]


# ---------------------------------------------------------------- S2 -------


class TestS2DirectionSequenceIsScoredEntriesOnly:

    def _sequence(self, symbol="ETHUSDT"):
        _alt, (flags, eligible, directions, side_lookup, side_net,
               lookup, net_r) = built(symbol)
        entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                       warmup=WARMUP, lockup=Args.lockup)
        seq = [directions[e] for e in entries if e in directions]
        return flags, entries, seq, directions, side_lookup

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_the_sequence_length_equals_the_scored_entry_count(self, symbol):
        _flags, entries, seq, directions, side_lookup = self._sequence(symbol)
        scored = [e for e in entries if e in side_lookup[directions[e]]]
        assert len(seq) == len(scored) == len(entries)

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_the_sequence_is_not_the_flagged_set(self, symbol):
        """The regression the mission asks for, stated as a number.

        153 flagged bars, 124 entries. One-trade-per-run drops the rest. In
        slice 35 this exact confusion scored 37 of 72 ETH trades in the wrong
        direction.
        """
        flags, entries, seq, _d, _sl = self._sequence(symbol)
        assert int(flags.sum()) > len(seq)
        assert len(seq) == len(entries)

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_the_sequence_excludes_the_incomplete_horizon_entry(self, symbol):
        """Before slice 40 the last element was a SHORT no trade realised."""
        _alt, (flags, eligible, directions, side_lookup, *_r) = built(symbol)
        old_entries = sk.simulate_schedule(flags, warmup=WARMUP,
                                           lockup=Args.lockup)
        new_entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                           warmup=WARMUP, lockup=Args.lockup)
        old_seq = [directions[e] for e in old_entries if e in directions]
        new_seq = [directions[e] for e in new_entries if e in directions]
        assert len(old_seq) == len(new_seq) + 1
        assert new_seq == old_seq[:len(new_seq)]

    def test_the_order_is_preserved(self):
        """"Recycles the ORDERED directions" — not a multiset."""
        _flags, entries, seq, directions, _sl = self._sequence()
        assert seq == [directions[e] for e in sorted(entries)]

    def test_the_tool_builds_the_sequence_from_the_embargoed_entries(self):
        """Structural: neither tool may rebuild it from `flags`."""
        for source in (inspect.getsource(cd.main),
                       inspect.getsource(em.main)):
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.keyword):
                    continue
                if node.arg != "direction_seq":
                    continue
                names = {n.id for n in ast.walk(node.value)
                         if isinstance(n, ast.Name)}
                assert "entries" in names, ast.dump(node.value)[:160]
                assert "flags" not in names, ast.dump(node.value)[:160]


# ---------------------------------------------------------------- S3 -------


class TestS3NullUsesTheSameScheduleBuilder:

    def test_the_rotation_null_calls_the_shared_builder(self):
        """AST, not prose: `simulate_schedule` on `tradable_flags`."""
        tree = ast.parse(inspect.getsource(em.rotation_replicates))
        called = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "simulate_schedule" in called
        assert "tradable_flags" in called

    def test_the_shape_matched_null_calls_the_shared_builder(self):
        tree = ast.parse(inspect.getsource(em.shape_matched_replicates))
        called = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "simulate_schedule" in called
        assert "tradable_flags" in called

    def test_directions_are_never_attached_to_raw_flag_indices(self):
        """The forbidden shape, forbidden structurally.

        "Do not rotate directions onto raw flag indices." Every direction in
        `"sequence"` mode is read positionally out of `direction_seq`, indexed
        by the enumeration of `entries` — never by a bar index.
        """
        tree = ast.parse(inspect.getsource(em.score_schedule))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            value = getattr(node.value, "attr", None)
            if value != "direction_seq":
                continue
            names = {n.id for n in ast.walk(node.slice)
                     if isinstance(n, ast.Name)}
            assert "bar" not in names, ast.dump(node)[:160]
            assert "position" in names, ast.dump(node)[:160]

    def _spy_entries(self, generator, **kwargs):
        """Every entry list the given null generator actually scores."""
        seen = []
        real = em.score_schedule

        def spy(entries, *args, **kw):
            seen.append(list(entries))
            return real(entries, *args, **kw)

        em.score_schedule = spy
        try:
            generator(**kwargs)
        finally:
            em.score_schedule = real
        return seen

    def test_no_rotation_replicate_slot_can_fail_the_embargo(self):
        """`np.roll` wraps, and that is how the defect got onto the null side.

        Rolling the raw post-warm-up region moves flags into the embargoed
        tail. Each one that lands there is dropped later by a lookup miss,
        costing the replicate a trade AND shifting the direction sequence's
        phase — the S1/S2 defect again, on the null side only, where the
        observed schedule never pays it because it is never rotated.

        Asserted against the real generator, not a reimplementation of it.
        """
        _alt, (flags, eligible, directions, side_lookup, side_net,
               lookup, net_r) = built()
        entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                       warmup=WARMUP, lockup=Args.lockup)
        book = em.DirectedBook(
            directions=directions,
            direction_seq=[directions[e] for e in entries if e in directions],
            lookup=side_lookup, net_r=side_net)
        seen = self._spy_entries(
            em.rotation_replicates, flags=flags, eligible=eligible,
            lookup=lookup, net_r=net_r, warmup=WARMUP, lockup=Args.lockup,
            runs=60, rng=np.random.default_rng(11), book=book)
        assert seen
        for schedule in seen:
            for entry in schedule:
                assert eligible[entry], entry

    def test_no_shape_matched_slot_can_fail_the_embargo(self):
        """Same for M2's generator, which tiles rather than rotates."""
        _alt, (flags, eligible, directions, side_lookup, side_net,
               lookup, net_r) = built()
        entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                       warmup=WARMUP, lockup=Args.lockup)
        book = em.DirectedBook(
            directions=directions,
            direction_seq=[directions[e] for e in entries if e in directions],
            lookup=side_lookup, net_r=side_net)
        seen = self._spy_entries(
            em.shape_matched_replicates, flags=flags, eligible=eligible,
            lookup=lookup, net_r=net_r, warmup=WARMUP, lockup=Args.lockup,
            schedules=40, rng=np.random.default_rng(5), book=book)
        assert seen
        for schedule in seen:
            for entry in schedule:
                assert eligible[entry], entry

    def test_the_rotation_preserves_the_flag_count(self):
        """Rotating in eligible space must not create or destroy a flag."""
        _alt, (flags, eligible, directions, side_lookup, side_net,
               lookup, net_r) = built()
        tradable = em.tradable_flags(flags, eligible)
        positions = np.nonzero(eligible[WARMUP:])[0]
        values = tradable[WARMUP:][positions]
        rng = np.random.default_rng(2)
        for _ in range(50):
            rolled = np.roll(values, int(rng.integers(1, values.size)))
            assert rolled.sum() == values.sum() == tradable.sum()

    def test_the_offset_rotates_the_sequence(self):
        """`direction_offset` must actually move the assignment."""
        seq = ["LONG_SETUP"] * 3 + ["SHORT_SETUP"] * 2
        book = em.DirectedBook(
            directions={}, direction_seq=seq,
            lookup={"LONG_SETUP": {0: 0, 1: 1}, "SHORT_SETUP": {0: 0, 1: 1}},
            net_r={"LONG_SETUP": np.array([1.0, 1.0]),
                   "SHORT_SETUP": np.array([-1.0, -1.0])})
        at_zero = em.score_schedule([0, 1], {}, np.array([]), book=book,
                                    directed_mode="sequence",
                                    direction_offset=0)
        at_three = em.score_schedule([0, 1], {}, np.array([]), book=book,
                                     directed_mode="sequence",
                                     direction_offset=3)
        assert at_zero.mean_r == pytest.approx(1.0)     # LONG, LONG
        assert at_three.mean_r == pytest.approx(-1.0)   # SHORT, SHORT

    def test_a_full_turn_of_the_offset_is_the_identity(self):
        seq = ["LONG_SETUP", "SHORT_SETUP", "LONG_SETUP"]
        book = em.DirectedBook(
            directions={}, direction_seq=seq,
            lookup={"LONG_SETUP": {0: 0, 1: 1}, "SHORT_SETUP": {0: 0, 1: 1}},
            net_r={"LONG_SETUP": np.array([2.0, 2.0]),
                   "SHORT_SETUP": np.array([-1.0, -1.0])})
        base = em.score_schedule([0, 1], {}, np.array([]), book=book,
                                 directed_mode="sequence", direction_offset=0)
        wrapped = em.score_schedule([0, 1], {}, np.array([]), book=book,
                                    directed_mode="sequence",
                                    direction_offset=len(seq))
        assert base.mean_r == wrapped.mean_r

    def test_the_offset_is_ignored_in_own_mode(self):
        """Observed bars carry their own direction; nothing may rotate them."""
        book = em.DirectedBook(
            directions={0: "LONG_SETUP", 1: "LONG_SETUP"},
            direction_seq=["SHORT_SETUP"],
            lookup={"LONG_SETUP": {0: 0, 1: 1}, "SHORT_SETUP": {0: 0, 1: 1}},
            net_r={"LONG_SETUP": np.array([1.0, 1.0]),
                   "SHORT_SETUP": np.array([-1.0, -1.0])})
        for offset in (0, 1, 7):
            scored = em.score_schedule([0, 1], {}, np.array([]), book=book,
                                       directed_mode="own",
                                       direction_offset=offset)
            assert scored.mean_r == pytest.approx(1.0)

    def test_replicates_do_not_all_share_one_phase(self):
        """Until slice 40 every replicate read from position 0.

        A fixed phase gives all 1,500 rotations of a surrogate the same
        long/short alignment — a constraint the observed side does not have.
        Asserted by watching the offsets the generator actually passes.
        """
        _alt, (flags, eligible, directions, side_lookup, side_net,
               lookup, net_r) = built()
        entries = sk.simulate_schedule(em.tradable_flags(flags, eligible),
                                       warmup=WARMUP, lockup=Args.lockup)
        book = em.DirectedBook(
            directions=directions,
            direction_seq=[directions[e] for e in entries if e in directions],
            lookup=side_lookup, net_r=side_net)

        seen = []
        real = em.score_schedule

        def spy(*args, **kwargs):
            seen.append(kwargs.get("direction_offset", 0))
            return real(*args, **kwargs)

        em.score_schedule = spy
        try:
            em.rotation_replicates(
                flags, eligible, lookup, net_r, warmup=WARMUP,
                lockup=Args.lockup, runs=40,
                rng=np.random.default_rng(3), book=book)
        finally:
            em.score_schedule = real
        assert len(set(seen)) > 1, seen[:10]
        assert max(seen) < len(book.direction_seq)


class TestTheSignalItselfIsUntouched:
    """S4. The repair is to the instrument, never to what it measures."""

    def test_the_frozen_constants(self):
        assert sp.SHOCK_MULTIPLIER == 1.0
        assert sp.ATR_PERIOD == 14
        assert sp.STOP_ATR == 1.5
        assert sp.TAKE_PROFIT_R == 2.0
        assert sp.TAKE_PROFIT_ATR == 3.0
        assert sp.HORIZON == 5
        assert sp.LOCKUP == 1
        assert sp.ROUND_TRIP_BPS == 25.0
        assert sp.ENTRY_ON == "next_open"

    def test_the_control_clauses(self):
        assert cd.Z_ABS_MAX == 1.96
        assert cd.KS_MIN_P == 0.05
        assert cd.MAX_INCOMPLETE_SHARE == 0.05

    def test_the_bars(self):
        assert em.M1_PERCENTILE_BAR == 95.0
        assert em.M2_PERCENTILE_BAR == 95.0

    def test_the_exclusion_list_is_still_one_date(self):
        assert sp.EXCLUDED_BTC_UTC_DATES == ("2025-01-07",)

    @pytest.mark.parametrize("symbol", ["ETHUSDT", "SOLUSDT"])
    def test_the_flag_set_is_unchanged_by_the_repair(self, symbol):
        """S1-S3 change scheduling, never which bars the signal likes."""
        _alt, (flags, _eligible, *_rest) = built(symbol)
        assert int(flags.sum()) == 153


class TestTheSlice40Artefacts:
    """The measurement this slice was allowed to run, pinned to its files.

    Both controls passed, so STEP 4 happened — the first fully interpretable
    reading of `btc_alt_spillover_v1` on the complete history. Both symbols
    read ABSENT. These tests exist so the artefacts cannot drift from what
    EDGE.md 21e says about them, and so a future session cannot mistake an
    ABSENT summary for a qualifying one.
    """

    import json as _json

    SYMBOLS = ("ETHUSDT", "SOLUSDT")

    def _summary(self, symbol):
        import json
        path = os.path.join(
            REPO, "artifacts",
            f"slice40_edge_btc_alt_spillover_{symbol}_summary.json")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_the_verdict_is_absent(self, symbol):
        assert self._summary(symbol)["verdict"] == "EDGE_EVIDENCE_ABSENT"

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_the_control_was_attested(self, symbol):
        """ABSENT under a VALID control is a measurement, not a shrug."""
        summary = self._summary(symbol)
        assert summary["control_validated"] is True
        attestation = os.path.join(REPO, summary["control_attestation"])
        assert "CONTROL: **VALID**" in open(attestation, encoding="utf-8").read()

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_both_bars_were_missed(self, symbol):
        summary = self._summary(symbol)
        assert summary["m1"]["bar"] == 95.0
        assert summary["m2"]["bar"] == 95.0
        assert summary["m1"]["passed"] is False
        assert summary["m2"]["passed"] is False
        assert summary["m1"]["percentile"] < 95.0
        assert summary["m2"]["percentile"] < 95.0

    @pytest.mark.parametrize("symbol", SYMBOLS)
    def test_the_measurement_used_the_full_window(self, symbol):
        assert self._summary(symbol)["bars"] == 1461

    def test_the_registration_hook_refuses_these_artefacts(self):
        """An ABSENT pair must register nothing, and must not raise doing it.

        Slice 57 registered `funding_carry_fade_btc_v1` under a pre-declared
        OOS gate, so "the hook returns None" stopped being the property. What
        this test was always protecting is narrower and survives: whatever
        clears, it is not one of these artefacts and it is not a frozen name.
        """
        import project_status as ps
        import registration_invariant as ri
        result = ps.cleared_edge_signal_from_artifacts(
            os.path.join(REPO, "artifacts"))
        assert result != "btc_alt_spillover_v1"
        assert result not in ps.ABSENT_SIGNALS
        ri.assert_registration_is_sound(os.path.join(REPO, "artifacts"))

    def test_eth_no_longer_reads_the_truncated_window_number(self):
        """Slice 35 scored ETH 97.3 / 98.0 on 890 dates with a biased ruler.

        On the full window, with S1-S3 applied, it is 94.3 / 92.0. The
        seductive number did not survive either the window or the repair, and
        that is the single most important comparison in this slice.
        """
        summary = self._summary("ETHUSDT")
        assert summary["m1"]["percentile"] < 95.0
        assert summary["m2"]["percentile"] < 95.0
