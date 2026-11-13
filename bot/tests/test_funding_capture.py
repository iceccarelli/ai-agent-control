"""funding_capture: offline only. The transport is injected; a socket guard fails any real connection."""
from __future__ import annotations

import json
import os
import socket
import stat
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import funding_capture as FC  # noqa: E402
import prospective_prints as PP  # noqa: E402
import venues  # noqa: E402

H8 = FC.SETTLE_MS
T0 = 1_600_000_000_000 // H8 * H8          # an arbitrary grid-aligned origin


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def page(stamps, rate="0.0001", **over):
    rows = [{"symbol": "BTCUSDT", "fundingRate": rate, "fundingRateTimestamp": str(t)} for t in stamps]
    doc = {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": rows}, "time": 1}
    doc.update(over)
    return json.dumps(doc).encode()


class Venue:
    """Serves newest-first pages of at most `size` rows from a fixed history, honouring endTime like the endpoint."""
    def __init__(self, stamps, size=3):
        self.stamps, self.size, self.urls = sorted(stamps), size, []

    def __call__(self, url):
        self.urls.append(url)
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        end = int(q["endTime"])
        rows = sorted((t for t in self.stamps if t <= end), reverse=True)[:self.size]
        return page(rows)


def run(tmp_path, venue, start, end, **kw):
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("now_ms", end + 10 * H8)
    return FC.capture(str(tmp_path / "cap"), start, end, fetch=venue, **kw)


def test_request_is_built_from_constants_on_the_registry_host():
    u = FC.request_url(T0, T0 + H8)
    assert u.startswith(venues.rest_for("mainnet") + "/v5/market/funding/history?")
    assert "category=linear" in u and "symbol=BTCUSDT" in u and "limit=200" in u


def test_http_fetch_refuses_any_other_url():
    for bad in ("https://evil.example/v5/market/funding/history?x=1", venues.rest_for("mainnet") + "/v5/order/create?x=1",
                venues.rest_for("mainnet") + "/v5/market/funding/history"):
        with pytest.raises(FC.CaptureError):
            FC.http_fetch(bad)


def test_pages_backwards_complete_history_no_gap_files_are_raw_and_private(tmp_path):
    stamps = [T0 + i * H8 for i in range(8)]
    v = Venue(stamps, size=3)
    m = run(tmp_path, v, T0, stamps[-1])
    assert m["distinct_settlements"] == 8 and m["missing_on_grid"] == [] and m["stopped_because"] == "reached_start"
    assert len(m["pages"]) == 3 and m["identical_duplicates_dropped"] == 0
    cap = tmp_path / "cap"
    for p in m["pages"]:
        f = cap / p["file"]
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert FC.hashlib.sha256(f.read_bytes()).hexdigest() == p["sha256"]
    # the saved pages are exactly what prospective_prints must accept as its input (round trip through the strict parser)
    rows = []
    for p in m["pages"]:
        rows += PP._rows(json.loads((cap / p["file"]).read_bytes()), p["file"])
    assert len(rows) >= 8


def test_cursor_moves_strictly_backwards(tmp_path):
    stamps = [T0 + i * H8 for i in range(7)]
    v = Venue(stamps, size=3)
    run(tmp_path, v, T0, stamps[-1])
    ends = [int(u.split("endTime=")[1].split("&")[0]) for u in v.urls]
    assert ends == sorted(ends, reverse=True) and len(set(ends)) == len(ends)


def test_gap_is_reported_never_filled(tmp_path):
    stamps = [T0 + i * H8 for i in range(6) if i != 2]
    m = run(tmp_path, Venue(stamps, size=50), T0, T0 + 5 * H8)
    assert m["missing_on_grid"] == [T0 + 2 * H8] and m["distinct_settlements"] == 5


def test_conflicting_duplicate_is_fatal_identical_is_counted(tmp_path):
    calls = {"n": 0}
    full = [T0 + 400 * H8 - i * H8 for i in range(200)]

    def fetch2(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return page(full, rate="0.0001")
        return page([full[-1], full[-1] - H8], rate="0.0002")          # re-states the oldest settlement with a different rate
    calls["n"] = 0
    with pytest.raises(FC.CaptureError, match="two different rates"):
        FC.capture(str(tmp_path / "c1"), T0, full[0], fetch=fetch2, sleep=lambda _s: None, now_ms=full[0] + H8)

    calls["n"] = 0

    def fetch3(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return page(full)
        if calls["n"] > 2:
            return page([])
        return page([full[-1], full[-1] - H8])                          # identical re-statement
    m = FC.capture(str(tmp_path / "c2"), T0, full[0], fetch=fetch3, sleep=lambda _s: None, now_ms=full[0] + H8)
    assert m["identical_duplicates_dropped"] == 1


def test_non_moving_cursor_stops_instead_of_looping(tmp_path):
    full = [T0 + 400 * H8 - i * H8 for i in range(200)]
    with pytest.raises(FC.CaptureError, match="cursor did not move"):
        FC.capture(str(tmp_path / "c"), T0, full[0], fetch=lambda u: page([full[0] + 5 * H8] + full[1:]), sleep=lambda _s: None, now_ms=full[0] + H8)


@pytest.mark.parametrize("body", [
    b"[]", b"not json", page([T0], retCode=10001), page([T0], retCode=True),
    json.dumps({"retCode": 0, "result": {"category": "spot", "list": []}}).encode(),
    json.dumps({"retCode": 0, "result": {"category": "linear", "list": [{"symbol": "ETHUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(T0)}]}}).encode(),
    json.dumps({"retCode": 0, "result": {"category": "linear", "list": [{"symbol": "BTCUSDT", "fundingRate": 0.0001, "fundingRateTimestamp": str(T0)}]}}).encode(),
    json.dumps({"retCode": 0, "result": {"category": "linear", "list": [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": "-5"}]}}).encode(),
])
def test_malformed_responses_are_refused(tmp_path, body):
    with pytest.raises(FC.CaptureError):
        run(tmp_path, lambda u: body, T0, T0 + H8)


def test_oversized_page_is_refused(tmp_path):
    with pytest.raises(FC.CaptureError, match="page limit"):
        run(tmp_path, lambda u: page([T0 + i * H8 for i in range(201)]), T0, T0 + 300 * H8)


def test_never_overwrites_and_requires_a_new_directory(tmp_path):
    stamps = [T0 + i * H8 for i in range(3)]
    run(tmp_path, Venue(stamps, size=50), T0, stamps[-1])
    with pytest.raises(FC.CaptureError, match="not empty"):
        run(tmp_path, Venue(stamps, size=50), T0, stamps[-1])


def test_future_range_is_clamped_and_wholly_future_refused(tmp_path):
    with pytest.raises(FC.CaptureError, match="future"):
        FC.capture(str(tmp_path / "c"), T0 + 5 * H8, T0 + 9 * H8, fetch=lambda u: page([]), sleep=lambda _s: None, now_ms=T0)


def test_protected_output_paths_are_refused(tmp_path):
    with pytest.raises(Exception):
        FC.capture(str(tmp_path / "state" / "trading_state.db"), T0, T0 + H8, fetch=lambda u: page([]), sleep=lambda _s: None, now_ms=T0 + 2 * H8)


def test_default_fetch_cannot_reach_the_network_in_tests(tmp_path):
    with pytest.raises(Exception):
        FC.capture(str(tmp_path / "c"), T0, T0 + H8, sleep=lambda _s: None, now_ms=T0 + 2 * H8)


def test_create_exclusive_never_overwrites_an_existing_file(tmp_path):
    f = tmp_path / "a.json"
    FC._create_exclusive(str(f), b"one")
    with pytest.raises(FC.CaptureError, match="never overwritten"):
        FC._create_exclusive(str(f), b"two")
    assert f.read_bytes() == b"one"


# ---- Round 19 (data specialist F3, reproduced): the prints builder never read the capture's manifest
def _in_window_capture(tmp_path, n=12, size=3, **kw):
    import carry_prospective as CP
    stamps = [CP.WINDOW_START_MS + i * H8 for i in range(n)]
    out = tmp_path / "cap"
    man = FC.capture(str(out), stamps[0], stamps[-1], fetch=Venue(stamps, size=size), sleep=lambda _s: None, now_ms=stamps[-1] + H8, **kw)
    files = sorted(str(out / p["file"]) for p in man["pages"])
    return out, man, files, stamps


def test_an_untouched_capture_passes_and_the_report_names_the_manifest(tmp_path):
    out, man, files, stamps = _in_window_capture(tmp_path)
    built = PP.build(files, now_ms=stamps[-1] + H8)
    m = built["report"]["manifest"]
    assert m["status"] == "VERIFIED" and m["pages"] == len(files) and m["stopped_because"] == "reached_start" and m["missing_on_grid"] == 0
    import hashlib
    assert m["sha256"] == hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest(), "the report carries the digest of the manifest it checked the pages against"
    assert PP.build(files, now_ms=stamps[-1] + H8, manifest_path=str(out))["report"]["manifest"]["sha256"] == m["sha256"], "an explicit --manifest (a directory) agrees"


def test_a_page_edited_after_the_manifest_was_written_is_refused(tmp_path):
    """Reproduced: replacing '0.0001' with '0.0003' in the newest page after capture moved the mean from 1.17 to 2.50 bps and build() accepted it."""
    out, man, files, stamps = _in_window_capture(tmp_path)
    newest = files[-1]
    body = open(newest, "rb").read()
    assert b"0.0001" in body
    open(newest, "wb").write(body.replace(b"0.0001", b"0.0003"))
    with pytest.raises(PP.PrintsError, match="PAGE_HASH_MISMATCH"):
        PP.build(files, now_ms=stamps[-1] + H8)


def test_a_dropped_page_or_a_stray_page_is_refused(tmp_path):
    """Reproduced: dropping a middle page was accepted and the report said coverage 1.0 while the manifest said missing_on_grid=1."""
    out, man, files, stamps = _in_window_capture(tmp_path)
    assert len(files) >= 3
    with pytest.raises(PP.PrintsError, match="PAGE_SET_DIFFERS.*missing"):
        PP.build(files[:1] + files[2:], now_ms=stamps[-1] + H8)
    stray = tmp_path / "stray.json"
    stray.write_bytes(open(files[0], "rb").read())
    with pytest.raises(PP.PrintsError, match="PAGE_SET_DIFFERS.*not in the manifest"):
        PP.build(files + [str(stray)], now_ms=stamps[-1] + H8, manifest_path=str(out))


def test_a_truncated_capture_is_refused(tmp_path):
    out, man, files, stamps = _in_window_capture(tmp_path, max_pages=2)
    assert man["stopped_because"] == "max_pages"
    with pytest.raises(PP.PrintsError, match="CAPTURE_TRUNCATED"):
        PP.build(files, now_ms=stamps[-1] + H8)


def test_hand_saved_inputs_without_a_manifest_say_so_in_the_report(tmp_path):
    out, man, files, stamps = _in_window_capture(tmp_path)
    loose = tmp_path / "loose"
    loose.mkdir()
    copies = []
    for f in files:
        c = loose / os.path.basename(f)
        c.write_bytes(open(f, "rb").read())
        copies.append(str(c))
    m = PP.build(copies, now_ms=stamps[-1] + H8)["report"]["manifest"]
    assert m["status"] == "NO_MANIFEST_HAND_SAVED_INPUTS" and "cannot be shown" in m["note"], "no lineage is claimed for files nothing recorded"


def test_a_capture_with_gaps_is_verified_with_gaps_not_plain_verified(tmp_path):
    """Independent review: status read VERIFIED even when the manifest listed settlements missing on the grid (only the count was reported)."""
    import carry_prospective as CP
    stamps = [CP.WINDOW_START_MS + i * H8 for i in range(12)]
    gappy = [t for i, t in enumerate(stamps) if i != 5]
    out = tmp_path / "cap"
    man = FC.capture(str(out), stamps[0], stamps[-1], fetch=Venue(gappy, size=3), sleep=lambda _s: None, now_ms=stamps[-1] + H8)
    assert man["missing_on_grid"] == [stamps[5]]
    m = PP.build(sorted(str(out / p["file"]) for p in man["pages"]), now_ms=stamps[-1] + H8)["report"]["manifest"]
    assert m["status"] == "VERIFIED_WITH_GAPS" and m["missing_on_grid"] == 1


def test_an_unrelated_manifest_beside_hand_saved_pages_is_not_a_crash_and_a_glob_gets_a_hint(tmp_path):
    out, man, files, stamps = _in_window_capture(tmp_path)
    loose = tmp_path / "loose"
    loose.mkdir()
    copies = []
    for f in files:
        c = loose / os.path.basename(f)
        c.write_bytes(open(f, "rb").read())
        copies.append(str(c))
    (loose / "manifest.json").write_text(json.dumps({"schema": "market_history", "dataset": "funding"}))          # some OTHER tool's manifest: no `pages`
    m = PP.build(copies, now_ms=stamps[-1] + H8)["report"]["manifest"]
    assert m["status"] == "NO_MANIFEST_HAND_SAVED_INPUTS" and "not a funding_capture manifest" in m["note"]
    with pytest.raises(PP.PrintsError, match="MANIFEST_UNREADABLE"):
        PP.build(copies, now_ms=stamps[-1] + H8, manifest_path=str(loose / "manifest.json"))                      # asked for explicitly: refused, not ignored
    with pytest.raises(PP.PrintsError, match="PAGE_SET_DIFFERS.*manifest.json.*list the page"):
        PP.build(files + [str(out / "manifest.json")], now_ms=stamps[-1] + H8)
