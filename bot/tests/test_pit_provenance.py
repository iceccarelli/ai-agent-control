"""Point-in-time PROVENANCE: a universe is point in time only if it is rebuilt from raw captured responses that verify; a hand-made claim never is.

Every capture directory here is SYNTHETIC (tests/pit_fixtures.py builds it in a tmp dir). Nothing in this file is an exchange observation. The harness is
checked against its OWN re-verification: the injected ``verify_capture_dir`` stand-in always says OK unless a test is about the verifier itself.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import random
import sys
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, HERE)

import pit_fixtures as PF  # noqa: E402
import research_harness as RH  # noqa: E402
import strategy_verdict as SV  # noqa: E402

CAND = os.path.join(BOT, "research", "candidates")
SHA = hashlib.sha256(b"pit provenance fixture input data").hexdigest()
CLEAN = RH.LeakAudit(True, 10, ())
RAW = "RAW_RESPONSES_VERIFIED"


def build(dirs, *, lo=0, hi=10_000, verify=PF.ok_verifier, **kw):
    return RH.PointInTimeUniverse.from_capture_dirs(dirs, study_from_ms=lo, study_to_ms=hi, verify=verify, **kw)


@pytest.fixture()
def caps(tmp_path):
    return PF.standard_captures(str(tmp_path))


def planted(n=2000, mean=5.0, sd=10.0, seed=1):
    r = random.Random(seed)
    return [r.gauss(mean, sd) for _ in range(n)]


def spec_for(ledger):
    with open(os.path.join(CAND, "cross_sectional_funding.json"), encoding="utf-8") as f:
        sp = json.load(f)
    sp["bias_declarations"] = {k: True for k in SV.BIAS_DECLARATIONS}
    for d in sp["required_data"]:
        d["repo_has"] = True                                    # the COPY pretends, to exercise the evidence machinery only
    sp["multiple_testing_family"] = ledger.family
    return sp


def evidence_for(universe, leak=CLEAN, returns=None):
    L = RH.VariantLedger("t_" + uuid.uuid4().hex)
    st = L.record("v", {"k": 1}, returns if returns is not None else planted())
    return RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=universe, leak=leak, data_sha256=SHA), spec_for(L)


# ------------------------------------------------------------------ positive control
def test_verified_captures_give_a_point_in_time_universe_with_a_raw_basis(caps):
    u = build(caps)
    assert u.pit_basis == RAW and u.survivorship_problems() == [] and u.point_in_time
    assert [s["label"] for s in u.segments] == [RAW] and u.unverified_segments == []
    assert len(u.capture_manifest_sha256s) == 4 and all(len(h) == 64 for h in u.capture_manifest_sha256s)
    ev, sp = evidence_for(u)
    assert ev.result == SV.PASS and SV.counted(ev, sp) == (True, None), ev.metrics
    m = ev.metrics
    assert m["universe_pit_basis"] == RAW and m["universe_pit_independently_verified"] is True
    assert m["universe_pit_verified_against_exchange"] is False                       # a hash shows what was read, not who produced it
    assert m["universe_capture_manifest_sha256"] == list(u.capture_manifest_sha256s)


# ------------------------------------------------------------------ a claim is not provenance
def test_a_hand_made_attestation_with_plausible_hashes_cannot_manufacture_point_in_time():
    h1, h2 = hashlib.sha256(b"snapshot-1").hexdigest(), hashlib.sha256(b"snapshot-2").hexdigest()
    att = {"no_delistings_observed_in_window": True, "window_from_ms": 0, "window_to_ms": 10_000, "snapshot_sha256": [h1, h2]}
    for src in RH.PIT_SOURCE_KINDS:
        u = RH.PointInTimeUniverse({"AAA": {"listed_ms": 0, "delisted_ms": 5_000}, "BBB": {"listed_ms": 0}}, membership_source=src, includes_delisted=True,
                                   observed_from_ms=0, observed_to_ms=10_000, no_delistings_attestation=att)
        assert not u.point_in_time and u.pit_basis == "ASSERTED", src
        assert any(p.startswith("PIT_NEEDS_VERIFIED_RAW_CAPTURES") for p in u.survivorship_problems())
        ev, sp = evidence_for(u)
        assert ev.result == SV.INCONCLUSIVE and ev.metrics["universe_point_in_time"] is False
        assert ev.metrics["universe_pit_independently_verified"] is False and ev.metrics["universe_pit_source_assertion"] == src
        forced = dataclasses.replace(ev, result=SV.PASS)
        assert SV.counted(forced, sp) == (False, "UNIVERSE_NOT_POINT_IN_TIME")


def test_counted_requires_the_raw_verified_basis_not_just_the_boolean(caps):
    ev, sp = evidence_for(build(caps))
    assert SV.counted(ev, sp) == (True, None)
    for basis in ("ASSERTED", "PARTIALLY_VERIFIED", "attested_no_delistings_unverified", None):
        m = dict(ev.metrics, universe_pit_basis=basis)
        assert SV.counted(dataclasses.replace(ev, metrics=m), sp) == (False, "UNIVERSE_PIT_NOT_RAW_VERIFIED"), basis


def test_no_verifier_or_a_failed_one_refuses(caps, monkeypatch):
    monkeypatch.setattr(RH, "_resolve_verifier", lambda: None)
    u = RH.PointInTimeUniverse.from_capture_dirs(caps, study_from_ms=0, study_to_ms=10_000)           # default: look the verifier up, find none
    assert not u.point_in_time and "VERIFY_CAPTURE_DIR_UNAVAILABLE" in u.survivorship_problems()
    f = build(caps, verify=PF.failing_verifier)
    assert not f.point_in_time and any(p.startswith("VERIFY_CAPTURE_DIR_FAILED") for p in f.survivorship_problems())
    with pytest.raises(RH.UniverseError):
        f.active_at(1000)                                                                    # an unverified universe answers no membership question


def test_a_verifier_that_raises_or_returns_junk_is_a_refusal(caps):
    def boom(_p):
        raise RuntimeError("x")
    for v in (boom, lambda p: None, lambda p: {"ok": "yes"}, lambda p: {"ok": 1}):
        assert not build(caps, verify=v).point_in_time


def test_manifest_digest_must_match_what_the_verifier_reported(caps):
    def lying(path):
        r = PF.ok_verifier(path)
        r["manifest_sha256"] = "ab" * 32
        return r
    u = build(caps, verify=lying)
    assert not u.point_in_time and any(p.startswith("MANIFEST_SHA256_DIFFERS_FROM_VERIFIER") for p in u.survivorship_problems())


# ------------------------------------------------------------------ membership (survivorship, listings, closed, renames)
def test_delisted_symbol_is_a_member_before_its_delisting_and_drops_out_after(caps):
    u = build(caps)
    assert u.active_at(0) == ["AAAUSDT", "BBBUSDT"]
    assert u.active_at(4_999) == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert u.active_at(5_000) == ["BBBUSDT", "CCCUSDT"]                                       # deliveryTime is exclusive: gone AT the delivery time
    assert u.active_at(9_000) == ["BBBUSDT"]
    u.require_active("AAAUSDT", 4_000)
    with pytest.raises(RH.UniverseError):
        u.require_active("AAAUSDT", 5_000)
    assert "AAAUSDT" in u.symbols                                                               # the delisted name is KEPT (survivors-only would lose it)
    assert u.delisted_between(0, 10_000) == ["AAAUSDT", "CCCUSDT"]


def test_a_symbol_observed_in_no_snapshot_before_t_is_not_in_the_universe_at_t(tmp_path):
    root = str(tmp_path)
    b = PF.row("BBBUSDT", "Trading", 0)
    late = PF.row("NEWUSDT", "Trading", 3_000)                                                 # launched at 3000 but first SEEN in the t=6000 snapshot
    caps = [PF.write_capture(root, "c0", 0, {"Trading": [b]}), PF.write_capture(root, "c1", 6_000, {"Trading": [b, late]})]
    u = build(caps)
    assert u.point_in_time
    assert u.active_at(4_000) == ["BBBUSDT"]                                                    # launchTime <= t is not enough: nobody had recorded it yet
    assert u.active_at(6_000) == ["BBBUSDT", "NEWUSDT"]


def test_new_listing_is_not_a_member_before_launch_even_if_recorded_later(tmp_path):
    root = str(tmp_path)
    pre = PF.row("PREUSDT", "PreLaunch", 8_000)
    caps = [PF.write_capture(root, "c0", 0, {"PreLaunch": [pre]})]
    u = build(caps, lo=0, hi=9_000)
    assert u.active_at(7_999) == [] and u.active_at(8_000) == ["PREUSDT"]


def test_closed_instrument_partition_is_what_makes_delisting_visible(tmp_path):
    root = str(tmp_path)
    closed = PF.row("OLDUSDT", "Closed", 0, 4_000)
    caps = [PF.write_capture(root, "c0", 0, {"Closed": [closed], "Trading": [PF.row("BBBUSDT", "Trading", 0)]})]
    u = build(caps, lo=0, hi=9_000)
    assert u.point_in_time and u.active_at(3_999) == ["BBBUSDT", "OLDUSDT"] and u.active_at(4_000) == ["BBBUSDT"]
    only_trading = [PF.write_capture(root, "c1", 0, {"Trading": [PF.row("BBBUSDT", "Trading", 0)]}, statuses=("PreLaunch", "Trading", "Delivering"))]
    miss = build(only_trading, lo=0, hi=9_000)
    assert not miss.point_in_time and "MISSING_STATUS_PARTITION:Closed" in miss.survivorship_problems()


def test_non_perpetual_or_non_usdt_rows_are_not_universe_members(tmp_path):
    root = str(tmp_path)
    fut = PF.row("BTCUSDT-27DEC24", "Trading", 0, 99_999, contract="LinearFutures")
    caps = [PF.write_capture(root, "c0", 0, {"Trading": [PF.row("BBBUSDT", "Trading", 0), fut]})]
    assert build(caps, lo=0, hi=5_000).symbols == ("BBBUSDT",)


def test_renamed_symbol_keeps_one_lineage_and_is_not_a_delisting(tmp_path):
    root = str(tmp_path)
    old_closed = PF.row("OLDUSDT", "Closed", 0, 4_000, symbol_id="lin-7")
    new = PF.row("NEWUSDT", "Trading", 4_000, symbol_id="lin-7")
    caps = [PF.write_capture(root, "c0", 5_000, {"Closed": [old_closed], "Trading": [new, PF.row("BBBUSDT", "Trading", 0, symbol_id="lin-2")]})]
    u = build(caps, lo=5_000, hi=9_000)
    assert u.point_in_time and u.renames() == [("OLDUSDT", "NEWUSDT")]
    assert u.lineage_of("OLDUSDT") == u.lineage_of("NEWUSDT") == "lin-7" and u.lineage_of("BBBUSDT") == "lin-2"
    assert u.delisted_between(0, 9_000) == []                                                   # a rename is not a survivorship event
    assert u.active_lineages_at(5_000) == ["lin-2", "lin-7"]


def test_one_symbol_with_two_lineage_ids_is_contradictory(tmp_path):
    root = str(tmp_path)
    caps = [PF.write_capture(root, "c0", 0, {"Trading": [PF.row("AAAUSDT", "Trading", 0, symbol_id="x")]}),
            PF.write_capture(root, "c1", 1_000, {"Trading": [PF.row("AAAUSDT", "Trading", 0, symbol_id="y")]})]
    u = build(caps, lo=0, hi=2_000)
    assert not u.point_in_time and "LINEAGE_CONTRADICTION:AAAUSDT" in u.survivorship_problems()


# ------------------------------------------------------------------ contradictory / vanishing snapshots
def test_contradictory_snapshots_are_refused(tmp_path):
    root = str(tmp_path)
    a1, a2 = PF.row("AAAUSDT", "Trading", 100), PF.row("AAAUSDT", "Trading", 200)
    u = build([PF.write_capture(root, "c0", 0, {"Trading": [a1]}), PF.write_capture(root, "c1", 1_000, {"Trading": [a2]})], lo=0, hi=2_000)
    assert not u.point_in_time and "LAUNCHTIME_CONTRADICTION:AAAUSDT" in u.survivorship_problems()
    d1, d2 = PF.row("DDDUSDT", "Closed", 0, 500), PF.row("DDDUSDT", "Closed", 0, 600)
    u2 = build([PF.write_capture(root, "c2", 0, {"Closed": [d1]}), PF.write_capture(root, "c3", 1_000, {"Closed": [d2]})], lo=0, hi=2_000)
    assert "DELIVERYTIME_CONTRADICTION:DDDUSDT" in u2.survivorship_problems()
    r1, r2 = PF.row("RRRUSDT", "Closed", 0, 500), PF.row("RRRUSDT", "Trading", 0, 0)
    u3 = build([PF.write_capture(root, "c4", 0, {"Closed": [r1]}), PF.write_capture(root, "c5", 1_000, {"Trading": [r2]})], lo=0, hi=2_000)
    assert "DELIVERYTIME_RESCINDED:RRRUSDT" in u3.survivorship_problems()


def test_a_row_whose_status_differs_from_its_partition_is_contradictory(tmp_path):
    root = str(tmp_path)
    d = PF.write_capture(root, "c0", 0, {"Trading": [PF.row("AAAUSDT", "Closed", 0, 10)]})
    u = build([d], lo=0, hi=1_000)
    assert not u.point_in_time and any(p.startswith("ROW_STATUS_DIFFERS_FROM_PARTITION") for p in u.survivorship_problems())


def test_a_symbol_in_two_partitions_of_one_capture_is_contradictory(tmp_path):
    root = str(tmp_path)
    d = PF.write_capture(root, "c0", 0, {"Trading": [PF.row("AAAUSDT", "Trading", 0)], "Closed": [PF.row("AAAUSDT", "Closed", 0, 10)]})
    u = build([d], lo=0, hi=1_000)
    assert not u.point_in_time and "SYMBOL_IN_TWO_PARTITIONS:AAAUSDT" in u.survivorship_problems()


def test_a_symbol_that_vanishes_without_a_delisting_record_poisons_the_universe(tmp_path):
    root = str(tmp_path)
    a, b = PF.row("AAAUSDT", "Trading", 0), PF.row("BBBUSDT", "Trading", 0)
    u = build([PF.write_capture(root, "c0", 0, {"Trading": [a, b]}), PF.write_capture(root, "c1", 1_000, {"Trading": [b]})], lo=0, hi=2_000)
    assert not u.point_in_time and "SYMBOL_VANISHED_WITHOUT_RECORD:AAAUSDT" in u.survivorship_problems()


def test_two_captures_with_the_same_snapshot_time_are_refused(tmp_path):
    root = str(tmp_path)
    b = PF.row("BBBUSDT", "Trading", 0)
    u = build([PF.write_capture(root, "c0", 0, {"Trading": [b]}), PF.write_capture(root, "c1", 0, {"Trading": [b]})], lo=0, hi=1_000)
    assert not u.point_in_time and any(p.startswith("DUPLICATE_SNAPSHOT_TIME") for p in u.survivorship_problems())


# ------------------------------------------------------------------ partial, tampered, wrong-schema captures
def _p(u):
    return u.survivorship_problems()


def test_tampered_raw_page_is_caught_by_the_harness_even_if_the_verifier_says_ok(tmp_path):
    root = str(tmp_path)
    d = PF.write_capture(root, "c0", 0, {"Trading": [PF.row("BBBUSDT", "Trading", 0)]})
    assert build([d], lo=0, hi=1_000).point_in_time                                            # control
    page = os.path.join(d, "linear_Trading_page0001.json")
    with open(page, "rb") as f:
        body = f.read()
    with open(page, "wb") as f:
        f.write(body.replace(b"BBBUSDT", b"ZZZUSDT"))                                          # swap the symbol after capture
    u = build([d], lo=0, hi=1_000, verify=PF.ok_verifier)                                       # the stand-in verifier still says OK: the harness must not
    assert not u.point_in_time and any(p.startswith("RAW_PAGE_SHA256_MISMATCH") for p in _p(u))
    assert "ZZZUSDT" not in u.symbols                                                           # a tampered capture contributes nothing


def test_tampered_manifest_hash_edit_is_caught(tmp_path):
    root = str(tmp_path)
    d = PF.write_capture(root, "c0", 0, {"Trading": [PF.row("BBBUSDT", "Trading", 0)]})
    m = PF.read_manifest(d)
    PF.part(m, "Trading")["pages"][0]["sha256"] = "cd" * 32
    PF.write_manifest(d, m)
    assert any(p.startswith("RAW_PAGE_SHA256_MISMATCH") for p in _p(build([d], lo=0, hi=1_000)))


def test_missing_page_file_missing_manifest_and_not_a_directory(tmp_path):
    root = str(tmp_path)
    d = PF.write_capture(root, "c0", 0, {"Trading": [PF.row("BBBUSDT", "Trading", 0)]})
    os.remove(os.path.join(d, "linear_Trading_page0001.json"))
    assert any(p.startswith("RAW_PAGE_MISSING") for p in _p(build([d], lo=0, hi=1_000)))
    e = PF.write_capture(root, "c1", 0, {"Trading": [PF.row("BBBUSDT", "Trading", 0)]})
    os.remove(PF.manifest_path(e))
    assert any(p.startswith("MANIFEST_MISSING_OR_UNREADABLE") for p in _p(build([e], lo=0, hi=1_000, verify=lambda p: {"ok": True, "coverage": {"dataset_status": "COMPLETE", "delisting_observability": "OBSERVED", "partitions": [{"category": "linear", "status": x, "complete": True} for x in PF.STATUSES]}})))
    assert any(p.startswith("CAPTURE_DIR_MISSING") for p in _p(build([os.path.join(root, "nope")], lo=0, hi=1_000)))


def test_manifest_page_name_cannot_escape_the_capture_directory(tmp_path):
    root = str(tmp_path)
    outside = tmp_path / "outside.json"
    d = PF.write_capture(root, "c0", 0, {"Trading": [PF.row("BBBUSDT", "Trading", 0)]})
    body = (tmp_path / "c0" / "linear_Trading_page0001.json").read_bytes()
    outside.write_bytes(body)
    m = PF.read_manifest(d)
    PF.part(m, "Trading")["pages"][0]["file"] = "../outside.json"
    PF.write_manifest(d, m)
    assert any(p.startswith("RAW_PAGE_NAME_UNSAFE") for p in _p(build([d], lo=0, hi=1_000)))


def test_incomplete_partition_and_broken_cursor_chain_are_partial_captures(tmp_path):
    root = str(tmp_path)
    many = [PF.row(f"S{i:02d}USDT", "Trading", 0) for i in range(5)]
    d = PF.write_capture(root, "c0", 0, {"Trading": many}, page_rows=2)
    assert build([d], lo=0, hi=1_000).point_in_time                                            # control: a 3-page chain verifies
    m = PF.read_manifest(d)
    PF.part(m, "Trading")["pages"][-1]["cursor_out"] = "more-pages-exist"                        # the chain ends on a cursor: the universe is INCOMPLETE
    PF.write_manifest(d, m)
    assert any(p.startswith("CURSOR_CHAIN_NOT_TERMINATED") for p in _p(build([d], lo=0, hi=1_000)))
    m = PF.read_manifest(d)
    PF.part(m, "Trading")["pages"][-1]["cursor_out"] = ""
    PF.part(m, "Trading")["pages"][1]["cursor_in"] = "forged"
    PF.write_manifest(d, m)
    assert any(p.startswith("CURSOR_CHAIN_BROKEN") for p in _p(build([d], lo=0, hi=1_000)))
    e = PF.write_capture(root, "c1", 0, {"Trading": many}, complete=False)
    assert any(p.startswith(("PARTITION_NOT_COMPLETE", "MISSING_STATUS_PARTITION")) for p in _p(build([e], lo=0, hi=1_000)))
    full = lambda path: dict(PF.ok_verifier(path), coverage={"dataset_status": "COMPLETE", "delisting_observability": "OBSERVED",
                                                             "partitions": [{"category": "linear", "status": x, "complete": True} for x in PF.STATUSES]})
    assert any(p.startswith("PARTITION_NOT_COMPLETE") for p in _p(build([e], lo=0, hi=1_000, verify=full)))        # the harness's own re-check, whatever the verifier said
    m = PF.read_manifest(PF.write_capture(root, "c2", 0, {"Trading": many}, page_rows=2))
    pg = PF.part(m, "Trading")["pages"]
    pg[1]["cursor_in"] = pg[0]["cursor_out"] = pg[1]["cursor_out"] = "loop"
    PF.write_manifest(os.path.join(root, "c2"), m)
    assert any(p.startswith("CURSOR_") for p in _p(build([os.path.join(root, "c2")], lo=0, hi=1_000)))


def test_delisting_must_be_observable_and_the_schema_must_be_v2(tmp_path):
    root = str(tmp_path)
    b = {"Trading": [PF.row("BBBUSDT", "Trading", 0)]}
    n = build([PF.write_capture(root, "c0", 0, b, observability="NOT_OBSERVED")], lo=0, hi=1_000)
    assert not n.point_in_time and "DELISTING_NOT_OBSERVABLE" in _p(n)
    v1 = build([PF.write_capture(root, "c1", 0, b, schema="universe_capture/v1")], lo=0, hi=1_000)
    assert not v1.point_in_time and any(p.startswith("MANIFEST_SCHEMA_NOT_V2") for p in _p(v1))
    missing = PF.write_capture(root, "c2", 0, b)
    m = PF.read_manifest(missing)
    del m["delisting_observability"]
    PF.write_manifest(missing, m)
    assert "DELISTING_NOT_OBSERVABLE" in _p(build([missing], lo=0, hi=1_000))


def test_one_bad_capture_in_a_set_poisons_the_claim_and_contributes_nothing(caps):
    with open(os.path.join(caps[1], "linear_Trading_page0001.json"), "ab") as f:
        f.write(b" ")                                                                          # one trailing byte
    u = build(caps)
    assert not u.point_in_time and any(p.startswith("RAW_PAGE_SHA256_MISMATCH") for p in _p(u))
    assert u.pit_basis in ("PARTIALLY_VERIFIED", "ASSERTED") and u.pit_basis != RAW


# ------------------------------------------------------------------ the study window
def test_segments_before_the_first_capture_are_not_point_in_time_verified(caps):
    u = build(caps, lo=-5_000, hi=10_000)                                                        # the study starts 5s before the first capture
    assert not u.point_in_time and u.pit_basis == "PARTIALLY_VERIFIED"
    assert u.unverified_segments == [{"from_ms": -5_000, "to_ms": 0, "label": "NOT_POINT_IN_TIME_VERIFIED"}]
    assert u.segments[0]["label"] == "NOT_POINT_IN_TIME_VERIFIED" and u.segments[-1]["label"] == RAW
    with pytest.raises(RH.UniverseError):
        u.active_at(-1)
    assert u.active_at(0) == ["AAAUSDT", "BBBUSDT"]


def test_a_study_that_needs_unverified_segments_is_insufficient_data(caps):
    u = build(caps, lo=-5_000, hi=10_000)
    ev, sp = evidence_for(u)
    assert ev.result == SV.INCONCLUSIVE and ev.metrics["universe_point_in_time"] is False
    assert ev.metrics["universe_pit_unverified_segments"] == u.unverified_segments
    v = SV.evaluate(sp, [ev])
    assert v.verdict == SV.INSUFFICIENT_DATA, v.as_dict()


def test_coverage_stops_a_gap_after_the_last_capture_and_inside_big_gaps(caps):
    u = build(caps, lo=0, hi=20_000, max_snapshot_gap_ms=4_000)
    assert not u.point_in_time
    labels = [(s["from_ms"], s["to_ms"], s["label"]) for s in u.segments]
    assert labels[0] == (0, 13_500, RAW) and labels[-1] == (13_500, 20_000, "NOT_POINT_IN_TIME_VERIFIED"), labels         # last capture 9500 + gap 4000
    wide = build([caps[0], caps[3]], lo=0, hi=10_000, max_snapshot_gap_ms=3_000)                                          # 0 and 9500: a hole between
    assert [s["label"] for s in wide.segments] == [RAW, "NOT_POINT_IN_TIME_VERIFIED", RAW]
    assert not wide.point_in_time


def test_a_study_window_must_be_ordered_and_integer(caps):
    for lo, hi in ((10, 10), (10, 5)):
        with pytest.raises(RH.HarnessError):
            build(caps, lo=lo, hi=hi)
    with pytest.raises(RH.HarnessError):
        build(caps, lo="0", hi=5)


def test_required_statuses_can_not_be_emptied_to_skip_the_closed_partition(caps):
    with pytest.raises(RH.HarnessError):
        build(caps, required_statuses=())
    with pytest.raises(RH.HarnessError):
        build(caps, required_statuses=("Trading",))                                             # delistings are visible only through Closed


def test_no_capture_dirs_is_not_point_in_time():
    u = build([])
    assert not u.point_in_time and "NO_CAPTURE_DIRS" in _p(u) and u.pit_basis == "ASSERTED"


def test_gaps_no_longer_describe_a_shape_only_attestation():
    joined = " ".join(RH.GAPS)
    assert "SHAPE" not in joined and "fabricated attestation" not in joined and "IGNORED" in joined
    assert any("raw" in g and "who produced" in g for g in RH.GAPS), RH.GAPS
    assert not hasattr(RH, "MIN_ATTESTATION_SNAPSHOTS")


# ------------------------------------------------------------------ look-ahead: decision time vs LOCAL RECEIVE time
def test_a_series_whose_value_arrives_after_the_decision_is_invisible_to_asof():
    ts = [0, 100, 200, 300]
    recv = [5, 105, 450, 305]                                                                    # the print stamped 200 only reached us at 450
    s = RH.PITSeries(ts, [1.0, 2.0, 3.0, 4.0], recv_ms=recv)
    assert s.asof(300).values() == [1.0, 2.0]                                                    # decision at 300: the 200-stamped value is NOT yet received
    assert s.asof(450).values() == [1.0, 2.0, 3.0, 4.0]
    v = s.asof(300)
    with pytest.raises(RH.LookAheadError):
        v.at(200)
    assert s.receive_time_bound is True and RH.PITSeries(ts, [1.0] * 4).receive_time_bound is False


def test_recv_ms_before_event_time_or_misaligned_is_refused():
    with pytest.raises(RH.HarnessError):
        RH.PITSeries([0, 100], [1.0, 2.0], recv_ms=[0, 99])                                      # received before it happened: a clock error or a forgery
    with pytest.raises(RH.HarnessError):
        RH.PITSeries([0, 100], [1.0, 2.0], recv_ms=[5])
    with pytest.raises(RH.HarnessError):
        RH.PITSeries([0, 100], [1.0, 2.0], recv_ms=[5, True])


def test_audit_receive_times_catches_a_deliberately_leaking_series():
    ts = list(range(0, 200, 10))
    recv_ok = [t + 2 for t in ts]
    decisions = [t + 5 for t in ts]                                                              # decide 5ms after the event: every value has been received
    assert RH.audit_receive_times(decisions, ts, recv_ok).passed
    leaking = list(recv_ok)
    leaking[7] = ts[7] + 500                                                                     # row 7 arrives 500ms late but the strategy decides on it at t+5
    a = RH.audit_receive_times(decisions, ts, leaking)
    assert not a.passed and a.mismatches == (7,)
    shifted = RH.audit_receive_times([d - 10 for d in decisions], ts, recv_ok)                   # deciding BEFORE the value's event time reads the future
    assert not shifted.passed
    with pytest.raises(RH.HarnessError):
        RH.audit_receive_times([1, 2], [1], [1])


def test_a_receive_time_leak_keeps_a_real_edge_from_counting(caps):
    u = build(caps)
    ts = list(range(0, 200, 10))
    leaking = [t + 2 for t in ts]
    leaking[3] = ts[3] + 900
    audit = RH.audit_receive_times([t + 5 for t in ts], ts, leaking)
    ev, sp = evidence_for(u, leak=audit)
    assert ev.result == SV.INCONCLUSIVE and ev.metrics["uses_future_information"] is True
    assert SV.counted(dataclasses.replace(ev, result=SV.PASS), sp) == (False, "LOOK_AHEAD_NOT_RULED_OUT")
    clean, sp2 = evidence_for(u, leak=RH.audit_receive_times([t + 5 for t in ts], ts, [t + 2 for t in ts]))
    assert clean.result == SV.PASS                                                                # control: same edge, causal receive times


# ------------------------------------------------------------------ every evidence declares its family and its correction
def test_evidence_declares_family_size_correction_and_deflated_sharpe(caps):
    u = build(caps)
    fam = "t_" + uuid.uuid4().hex
    L = RH.VariantLedger(fam)
    for i in range(6):
        L.record(f"v{i}", {"k": i}, planted(seed=10 + i, mean=0.1 * i))
    best = L.record("best", {"k": 99}, planted(seed=3, mean=6.0))
    sp = spec_for(L)
    ev = RH.historical_evidence(spec=sp, stats=best, ledger=L, universe=u, leak=CLEAN, data_sha256=SHA, returns=planted(seed=3, mean=6.0))
    m = ev.metrics
    assert m["multiple_testing_family"] == fam and m["n_variants_tested"] == 7 and m["multiple_testing_correction"] == "holm"
    assert m["deflated_sharpe"]["applicable"] is True and m["deflated_sharpe"]["n_trials"] == 7 and 0.0 <= m["deflated_sharpe"]["dsr"] <= 1.0
    bh = RH.historical_evidence(spec=sp, stats=best, ledger=L, universe=u, leak=CLEAN, data_sha256=SHA, method="bh")
    assert bh.metrics["multiple_testing_correction"] == "bh" and bh.metrics["deflated_sharpe"]["applicable"] is False
    assert "not supplied" in bh.metrics["deflated_sharpe"]["reason"]
    with pytest.raises(RH.HarnessError):                                                          # the returns must be the ledger's recorded series, not a prettier one
        RH.historical_evidence(spec=sp, stats=best, ledger=L, universe=u, leak=CLEAN, data_sha256=SHA, returns=planted(seed=3, mean=9.0))


# ------------------------------------------------------------------ mutation checks: each guard is load-bearing
def _mutant(old, new, count=1):
    path = os.path.join(BOT, "tools", "research_harness.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert src.count(old) >= 1, f"mutation anchor not found: {old!r}"
    import types
    mod = types.ModuleType("rh_mutant_" + uuid.uuid4().hex)
    mod.__file__ = path
    sys.modules[mod.__name__] = mod                                   # dataclasses resolves string annotations through sys.modules
    exec(compile(src.replace(old, new, count), path, "exec"), mod.__dict__)
    return mod


def _tampered(tmp_path):
    d = PF.write_capture(str(tmp_path), "c0", 0, {"Trading": [PF.row("BBBUSDT", "Trading", 0)]})
    page = os.path.join(d, "linear_Trading_page0001.json")
    with open(page, "ab") as f:
        f.write(b" ")
    return d


def test_mutation_without_its_own_hash_check_a_tampered_page_would_pass(tmp_path):
    d = _tampered(tmp_path)
    assert not build([d], lo=0, hi=1_000).point_in_time
    m = _mutant("if _sha256(raw) != declared:", "if False:")
    u = m.PointInTimeUniverse.from_capture_dirs([d], study_from_ms=0, study_to_ms=1_000, verify=PF.ok_verifier)
    assert u.point_in_time, "the mutant must differ: otherwise the hash check is not what stops a tampered page"


def test_mutation_if_the_verifier_result_were_ignored_a_failed_capture_would_pass(caps):
    assert not build(caps, verify=PF.failing_verifier).point_in_time
    m = _mutant('if v.get("ok") is not True:', "if False:")
    assert m.PointInTimeUniverse.from_capture_dirs(caps, study_from_ms=0, study_to_ms=10_000, verify=PF.failing_verifier).point_in_time


def test_mutation_if_an_attestation_were_honoured_a_hand_made_one_would_pass():
    att = {"no_delistings_observed_in_window": True, "window_from_ms": 0, "window_to_ms": 10_000,
           "snapshot_sha256": [hashlib.sha256(b"a").hexdigest(), hashlib.sha256(b"b").hexdigest()]}
    mk = lambda mod: mod.PointInTimeUniverse({"AAA": {"listed_ms": 0}}, membership_source=RH.PIT_SOURCE_KINDS[0], includes_delisted=True, observed_from_ms=0,
                                              observed_to_ms=10_000, no_delistings_attestation=att)
    assert not mk(RH).point_in_time
    m = _mutant("def survivorship_problems(self) -> List[str]:", "def survivorship_problems(self) -> List[str]:\n        if self._capture is None and self.no_delistings_attestation:\n            return []")
    assert mk(m).point_in_time


def test_mutation_without_the_first_observation_rule_an_unseen_listing_would_be_a_member(tmp_path):
    root = str(tmp_path)
    b, late = PF.row("BBBUSDT", "Trading", 0), PF.row("NEWUSDT", "Trading", 3_000)
    caps = [PF.write_capture(root, "c0", 0, {"Trading": [b]}), PF.write_capture(root, "c1", 6_000, {"Trading": [b, late]})]
    assert build(caps).active_at(4_000) == ["BBBUSDT"]
    m = _mutant("if first_seen is not None and first_seen > t:", "if False:")
    u = m.PointInTimeUniverse.from_capture_dirs(caps, study_from_ms=0, study_to_ms=10_000, verify=PF.ok_verifier)
    assert u.active_at(4_000) == ["BBBUSDT", "NEWUSDT"]


def test_mutation_without_the_unverified_segment_rule_an_early_study_would_pass(caps):
    assert not build(caps, lo=-5_000).point_in_time
    m = _mutant("and not self.unverified_segments", "")
    u = m.PointInTimeUniverse.from_capture_dirs(caps, study_from_ms=-5_000, study_to_ms=10_000, verify=PF.ok_verifier)
    assert u.point_in_time


def test_mutation_without_the_receive_time_filter_a_late_value_would_be_visible():
    s = RH.PITSeries([0, 100, 200], [1.0, 2.0, 3.0], recv_ms=[5, 105, 450])
    assert s.asof(300).values() == [1.0, 2.0]
    m = _mutant("avail = self._recv if self._recv is not None else self._ts", "avail = self._ts")
    s2 = m.PITSeries([0, 100, 200], [1.0, 2.0, 3.0], recv_ms=[5, 105, 450])
    assert s2.asof(300).values() == [1.0, 2.0, 3.0]


# ------------------------------------------------------------------ funding interval heterogeneity (interval design constraint is OWNER-OBSERVED, not verified here)
def test_funding_is_normalised_per_day_never_compared_per_print():
    assert RH.funding_bps_per_day(1.0, 480) == pytest.approx(3.0)
    assert RH.funding_bps_per_day(0.5, 240) == pytest.approx(3.0)                        # half the print, twice as often: the same carry
    assert RH.funding_bps_per_day(0.125, 60) == pytest.approx(3.0)
    assert RH.funding_bps_over_horizon(0.5, 240, 10) == pytest.approx(30.0)
    for bad in (0, -480, 7, True, 480.0, None):
        with pytest.raises(RH.HarnessError):
            RH.funding_bps_per_day(1.0, bad)
    with pytest.raises(RH.HarnessError):
        RH.funding_bps_per_day(float("nan"), 480)


def test_universe_records_carry_each_symbols_interval_and_a_missing_one_blocks_the_claim(tmp_path):
    root = str(tmp_path)
    a, b = PF.row("AAAUSDT", "Trading", 0, interval=240), PF.row("BBBUSDT", "Trading", 0)
    d = PF.write_capture(root, "c0", 0, {"Trading": [a, b]})
    u = build([d], lo=0, hi=1_000)
    assert u.point_in_time and u.funding_normalisation_known
    assert u.funding_interval_min("AAAUSDT", 10) == 240 and u.funding_interval_min("BBBUSDT", 10) == 480
    miss = PF.write_capture(root, "c1", 0, {"Trading": [PF.row("CCCUSDT", "Trading", 0, interval=None)]})
    u2 = build([miss], lo=0, hi=1_000)
    assert not u2.point_in_time and "FUNDING_INTERVAL_UNKNOWN:CCCUSDT" in u2.survivorship_problems()
    ok = build([miss], lo=0, hi=1_000, needs_funding_interval=False)                    # a non-funding study may opt out, and then cannot use funding_interval_min
    assert ok.point_in_time and not ok.funding_normalisation_known
    with pytest.raises(RH.UniverseError):
        ok.funding_interval_min("CCCUSDT", 10)


def test_counted_refuses_when_funding_normalisation_is_unknown(caps):
    ev, sp = evidence_for(build(caps))
    assert ev.metrics["universe_funding_normalisation_known"] is True
    m = dict(ev.metrics, universe_funding_normalisation_known=False)
    assert SV.counted(dataclasses.replace(ev, metrics=m), sp) == (False, "FUNDING_INTERVAL_NORMALISATION_UNKNOWN")


def test_gaps_state_the_owner_observed_closed_share_and_interval_mix():
    j = " ".join(RH.GAPS)
    assert "OWNER-OBSERVED" in j and "318" in j and "890" in j and "240m" in j and "deliveryTime" in j


# ------------------------------------------------------------------ binding to the real verifier's contract: ``ok`` is integrity only
def test_ok_integrity_is_not_enough_the_rederived_coverage_must_be_complete_and_observed(tmp_path):
    root = str(tmp_path)
    b = {"Trading": [PF.row("BBBUSDT", "Trading", 0)]}
    for i, status in enumerate(("PARTIAL", "CONFLICTED", "STALE", "UNVERIFIED", None)):
        d = PF.write_capture(root, f"p{i}", 0, b, dataset_status=status)
        u = build([d], lo=0, hi=1_000)                                                       # the stand-in verifier says ok=True: integrity holds
        assert not u.point_in_time and any(p.startswith("CAPTURE_DATASET_NOT_COMPLETE") for p in _p(u)), status
    assert build([PF.write_capture(root, "okc", 0, b)], lo=0, hi=1_000).point_in_time          # control: COMPLETE verifies


def test_trading_and_closed_partitions_must_be_complete_in_the_verifiers_coverage(tmp_path):
    root = str(tmp_path)
    b = {"Trading": [PF.row("BBBUSDT", "Trading", 0)]}
    def cov_without(status):
        def v(path):
            r = PF.ok_verifier(path)
            r["coverage"]["partitions"] = [p for p in r["coverage"]["partitions"] if p["status"] != status]
            return r
        return v
    d = PF.write_capture(root, "c0", 0, b)
    for st in ("Trading", "Closed"):
        u = build([d], lo=0, hi=1_000, verify=cov_without(st))
        assert not u.point_in_time and f"MISSING_STATUS_PARTITION:{st}" in _p(u), st


def test_the_real_capture_shape_default_partition_and_spot_universe_give_hedgeability(tmp_path):
    root = str(tmp_path)
    btc, perp_only = PF.row("BTCUSDT", "Trading", 0), PF.row("ARIAUSDT", "Trading", 0)
    d = PF.write_capture(root, "c0", 0, {"Trading": [btc, perp_only]}, default_partition=True, spot_symbols=["BTCUSDT", "ETHUSDT"])
    u = build([d], lo=0, hi=1_000)
    assert u.point_in_time, _p(u)                                                              # the same rows in 'default' and 'Trading' are one instrument
    assert u.spot_listed("BTCUSDT") is True and u.spot_listed("ARIAUSDT") is False
    nospot = build([PF.write_capture(root, "c1", 0, {"Trading": [btc]})], lo=0, hi=1_000)
    assert nospot.spot_listed("BTCUSDT") is None                                                # no spot partition captured: hedgeability UNKNOWN, never assumed
    assert RH.PointInTimeUniverse({"A": {"listed_ms": 0}}, membership_source="x", includes_delisted=True).spot_listed("A") is None


def test_the_default_partition_cannot_contradict_a_status_partition(tmp_path):
    root = str(tmp_path)
    d = PF.write_capture(root, "c0", 0, {"Trading": [PF.row("AAAUSDT", "Trading", 0)]}, default_partition=True)
    m = PF.read_manifest(d)
    assert build([d], lo=0, hi=1_000).point_in_time
    raw = os.path.join(d, "linear_default_page0001.json")
    doc = json.loads(open(raw, "rb").read())
    doc["result"]["list"][0]["launchTime"] = "5"
    body = json.dumps(doc, sort_keys=True).encode()
    open(raw, "wb").write(body)
    PF.part(m, None)["pages"][0]["sha256"] = PF.sha(body)
    PF.write_manifest(d, m)
    assert "SYMBOL_IN_TWO_PARTITIONS:AAAUSDT" in _p(build([d], lo=0, hi=1_000))


# ------------------------------------------------------------------ the REAL universe_capture (offline, injected fetch of SYNTHETIC responses)
def _real_row(sym, status, launch, delivery=0, interval=240):
    return {"symbol": sym, "contractType": "LinearPerpetual", "status": status, "baseCoin": sym[:-4], "quoteCoin": "USDT", "settleCoin": "USDT", "symbolType": "",
            "launchTime": str(launch), "deliveryTime": str(delivery), "fundingInterval": interval, "upperFundingRate": "0.0075", "lowerFundingRate": "-0.0075",
            "priceFilter": {"tickSize": "0.1", "minPrice": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "100"},
            "leverageFilter": {"minLeverage": "1", "maxLeverage": "50", "leverageStep": "0.01"}}


def test_the_real_universe_capture_verifier_feeds_the_harness(tmp_path):
    import urllib.parse
    import universe_capture as UC
    if not hasattr(UC, "verify_capture_dir") or not hasattr(UC, "capture_universe"):
        pytest.skip("universe_capture v2 not present in this tree")
    rows = [_real_row("BTCUSDT", "Trading", 1_000, interval=480), _real_row("OLDUSDT", "Closed", 1_000, 5_000, interval=240)]
    def fetch(url):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("tickers"):
            lst = [{"symbol": "BTCUSDT", "markPrice": "1", "indexPrice": "1", "lastPrice": "1", "fundingRate": "0.0001", "nextFundingTime": "1", "fundingIntervalHour": "8",
                    "openInterest": "1", "openInterestValue": "1", "volume24h": "1", "turnover24h": "1"}]
        else:
            lst = [r for r in rows if q.get("status") in (None, r["status"])]
        return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"category": q["category"], "list": lst, "nextPageCursor": ""}, "time": 1_700_000_000_000}).encode()
    d = str(tmp_path / "cap")
    UC.capture_universe(d, fetch=fetch, clock_ms=lambda: 1_700_000_000_000, sleep=lambda s: None)
    v = UC.verify_capture_dir(d)
    assert v["ok"] is True, v["problems"]
    u = RH.PointInTimeUniverse.from_capture_dirs([d], study_from_ms=1_700_000_000_000, study_to_ms=1_700_000_000_000 + 86_400_000)      # default verifier = the real one
    assert u.point_in_time, u.survivorship_problems()
    assert u.pit_basis == RAW and u.symbols == ("BTCUSDT", "OLDUSDT")
    assert u.funding_interval_min("OLDUSDT", 1_700_000_000_000) == 240 and u.funding_interval_min("BTCUSDT", 1_700_000_000_000) == 480
    assert u.delisted_between(0, 10_000) == ["OLDUSDT"]
    with open(os.path.join(d, "linear_Trading_page0001.json"), "ab") as f:
        f.write(b" ")
    t = RH.PointInTimeUniverse.from_capture_dirs([d], study_from_ms=1_700_000_000_000, study_to_ms=1_700_000_000_000 + 86_400_000)
    assert not t.point_in_time and any(p.startswith("VERIFY_CAPTURE_DIR_FAILED") for p in t.survivorship_problems())
