"""tools/prospective_prints.py: the only producer of the registered prospective input. Synthetic data and temporary directories only; no request is made.

The expected numbers are written out here from the venue's documented raw shape, not read back from the module: a funding rate is a FRACTION per settlement
(``"0.0001"`` is 1 bp), the plan wants basis points, and a wrong unit silently reads as "no edge".
"""
from __future__ import annotations

import ast
import json
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

import account_review as AR  # noqa: E402
import carry_prospective as CP  # noqa: E402
import prospective_prints as PP  # noqa: E402
import statistical_power as SP  # noqa: E402
import trial_journal as TJ  # noqa: E402
from test_carry_prospective import CLOSED, window_prints  # noqa: E402

H8 = CP.SETTLE_MS
T0 = CP.WINDOW_START_MS
NOW = CLOSED + 10 * H8                                  # a clock after the window has closed and after the post-window rows below


def row(ts, fraction="0.0001", symbol="BTCUSDT"):
    r = {"fundingRate": fraction, "fundingRateTimestamp": str(ts)}
    if symbol is not None:
        r["symbol"] = symbol
    return r


def page(rows, ret=0, category="linear"):
    return {"retCode": ret, "retMsg": "OK", "result": {"category": category, "list": rows}, "retExtInfo": {}, "time": 1}


def save(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    os.chmod(p, 0o600)
    return str(p)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(CP, "_now_ms", lambda: CLOSED + 10 * H8)
    monkeypatch.setattr(PP, "_now_ms", lambda: CLOSED + 10 * H8)


# ------------------------------------------------------------------ the unit

def test_the_venues_fraction_becomes_basis_points_here_and_nowhere_else(tmp_path):
    p = save(tmp_path, "a.json", page([row(T0, "0.0001"), row(T0 + H8, "0.000026"), row(T0 + 2 * H8, "-0.0000123"), row(T0 + 3 * H8, "0")]))
    got = dict(PP.build([p], now_ms=NOW)["prints"])
    assert got[T0] == 1.0 and got[T0 + 3 * H8] == 0.0                              # exact: decimal arithmetic, not float multiplication
    assert dict(PP.build([save(tmp_path, "e.json", page([row(T0, "0.0003")]))], now_ms=NOW)["prints"])[T0] == 3.0       # float 0.0003 * 1e4 is 2.9999999999999996
    assert got[T0] == pytest.approx(1.0, abs=1e-12)                               # the 1 bp baseline
    assert got[T0 + H8] == pytest.approx(0.26, abs=1e-12)                         # the repo's last-year mean level
    assert got[T0 + 2 * H8] == pytest.approx(-0.123, abs=1e-12)
    assert got[T0 + 3 * H8] == 0.0


def test_a_file_already_in_basis_points_is_not_the_raw_shape_and_is_refused(tmp_path):
    # The point of the tool: nobody types a unit. The prints shape ({"ts_ms","funding_bps"}) is NOT accepted as input.
    p = save(tmp_path, "bps.json", [{"ts_ms": T0, "funding_bps": 1.0}])
    with pytest.raises(PP.PrintsError, match="envelope"):
        PP.build([p], now_ms=NOW)
    q = save(tmp_path, "bps2.json", page([{"ts_ms": T0, "funding_bps": 1.0}]))                       # inside a valid envelope, still not a funding row
    with pytest.raises(PP.PrintsError, match="not a V5 funding row"):
        PP.build([q], now_ms=NOW)


# ------------------------------------------------------------------ refusals

@pytest.mark.parametrize("doc,match", [
    (page([row(T0)], ret=10001), "retCode"),
    (page([row(T0)], ret=True), "retCode"),
    (page([row(T0)], category="inverse"), "category"),
    (page([row(T0, symbol="ETHUSDT")]), "BTCUSDT only"),
    (page([row(T0, symbol=None)]), "BTCUSDT only"),
    (page([row(T0 + H8 // 2)]), "8-hour"),                                          # a 4h-interval print: the plan is an 8h plan
    (page([row(T0, "0.05")]), "plausible"),                                          # 500 bps
    (page([]), "no settlement inside"),                                              # an empty result is not a run that found data
    (page([row(T0, "nan")]), "plain decimal"),
    (page([row(T0, "1e999")]), "plain decimal"),
    (page([row(T0, "1e-4")]), "plain decimal"),
    (page([row(T0, " 0.0001 ")]), "plain decimal"),
    (page([row(T0, "1_0e-5")]), "plain decimal"),
    (page([row(T0, "0.0001\u0663")]), "plain decimal"),
    (page([row(T0, "abc")]), "plain decimal"),
    ([{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(T0)}], "envelope"),             # a bare list skips retCode/category
    (page([row(T0)], ret=0.0), "retCode"),
    (page([row(T0)], ret="0"), "retCode"),
    (page([{"fundingRate": 0.0001, "fundingRateTimestamp": str(T0), "symbol": "BTCUSDT"}]), "string"),
    (page([{"fundingRate": "0.0001", "fundingRateTimestamp": True, "symbol": "BTCUSDT"}]), "string|integer"),
    (page([{"fundingRate": "0.0001", "fundingRateTimestamp": "1.5e12", "symbol": "BTCUSDT"}]), "unreadable"),
    (page([{**row(T0), "extra": 1}]), "not a V5 funding row"),
    (page([row(T0 + i * H8) for i in range(201)]), "at most 200"),
    ({"retCode": 0, "result": {"category": "linear"}}, "no list"),
    ("nothing", "envelope"),
])
def test_anything_that_is_not_a_clean_btcusdt_eight_hour_print_is_refused_before_anything_is_written(tmp_path, doc, match):
    p = save(tmp_path, "a.json", doc)
    with pytest.raises(PP.PrintsError, match=match):
        PP.build([p], now_ms=NOW)
    out = tmp_path / "out.jsonl"
    assert PP.main(["--in", p, "--out", str(out)]) == 2 and not out.exists()


def test_a_print_dated_in_the_future_is_not_an_observation(tmp_path):
    p = save(tmp_path, "a.json", page([row(T0 + 10 * H8)]))
    with pytest.raises(PP.PrintsError, match="in the future"):
        PP.build([p], now_ms=T0 + 5 * H8)


def test_a_caller_cannot_claim_a_later_clock_than_the_real_one(tmp_path, monkeypatch):
    monkeypatch.setattr(PP, "_now_ms", lambda: T0 - 86_400_000)                       # the real clock: the day before the window
    p = save(tmp_path, "a.json", page([row(T0)]))
    with pytest.raises(PP.PrintsError, match="in the future"):
        PP.build([p], now_ms=CP.WINDOW_END_MS)                                         # claiming the window had closed does not help
    assert PP.main(["--in", p, "--out", str(tmp_path / "o.jsonl")]) == 2


@pytest.mark.parametrize("delta,ok", [(0, True), (1, False)])
def test_the_future_check_is_exact_at_the_clock_edge(tmp_path, delta, ok):
    p = save(tmp_path, "a.json", page([row(T0)]))
    if ok:
        assert PP.build([p], now_ms=T0 + delta)["report"]["written"] == 1              # a print stamped exactly now has settled
    else:
        with pytest.raises(PP.PrintsError, match="in the future"):
            PP.build([p], now_ms=T0 - delta)                                          # one millisecond early


def test_two_rates_that_differ_only_far_below_float_resolution_still_conflict(tmp_path):
    a = save(tmp_path, "a.json", page([row(T0, "0.0001")]))
    b = save(tmp_path, "b.json", page([row(T0, "0.00010000000000000001")]))
    with pytest.raises(PP.PrintsError, match="two different rates"):
        PP.build([a, b], now_ms=NOW)


def test_a_symlinked_output_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "dirlink"
    os.symlink(real, link)
    p = save(tmp_path, "a.json", page([row(T0)]))
    assert PP.main(["--in", p, "--out", str(link / "o.jsonl")]) == 2 and list(real.iterdir()) == []


def test_non_json_non_utf8_and_non_finite_documents_are_refused(tmp_path):
    for name, raw in (("a.json", b"{not json"), ("b.json", b"\xff\xfe"), ("c.json", b'{"retCode": NaN}')):
        p = tmp_path / name
        p.write_bytes(raw)
        os.chmod(p, 0o600)
        with pytest.raises(PP.PrintsError):
            PP.build([str(p)], now_ms=NOW)
    with pytest.raises(PP.PrintsError, match="no input"):
        PP.build([], now_ms=NOW)


# ------------------------------------------------------------------ merging and the window

def test_pages_in_any_order_merge_and_an_identical_overlap_is_dropped_but_a_conflict_is_an_error(tmp_path):
    a = save(tmp_path, "a.json", page([row(T0 + 3 * H8, "0.0003"), row(T0 + 2 * H8, "0.0002")]))          # descending, as a venue may return it
    b = save(tmp_path, "b.json", page([row(T0 + 2 * H8, "0.0002"), row(T0 + H8, "0.0001"), row(T0, "0.00005")]))
    built = PP.build([a, b], now_ms=NOW)
    assert [t for t, _ in built["prints"]] == [T0, T0 + H8, T0 + 2 * H8, T0 + 3 * H8]
    assert built["report"]["distinct_settlements_read"] == 4
    c = save(tmp_path, "c.json", page([row(T0 + 2 * H8, "0.0009")]))
    with pytest.raises(PP.PrintsError, match="two different rates"):
        PP.build([a, c], now_ms=NOW)


def test_history_before_the_window_is_counted_but_never_written_and_a_gap_is_reported_not_filled(tmp_path):
    rows = [row(T0 - 2 * H8), row(T0 - H8), row(T0), row(T0 + H8), row(T0 + 3 * H8), row(CP.WINDOW_END_MS), row(CP.WINDOW_END_MS + H8)]
    built = PP.build([save(tmp_path, "a.json", page(rows))], now_ms=NOW)
    r = built["report"]
    assert r["before_the_window_not_written"] == 2 and r["after_the_window_not_written"] == 2 and r["written"] == 3
    assert [t for t, _ in built["prints"]] == [T0, T0 + H8, T0 + 3 * H8]            # T0 + 2*H8 is missing and stays missing
    assert r["window"]["start"].startswith("2026-11-07") and r["coverage"]["missing"] > 0


# ------------------------------------------------------------------ the output file

def test_the_output_is_exclusive_private_and_in_the_registered_shape(tmp_path):
    p = save(tmp_path, "a.json", page([row(T0), row(T0 + H8, "0.0002")]))
    out = tmp_path / "prints.jsonl"
    assert PP.main(["--in", p, "--out", str(out)]) == 0
    assert oct(os.stat(out).st_mode & 0o777) == oct(0o600)
    assert [json.loads(x) for x in out.read_text().splitlines()] == [{"funding_bps": 1.0, "ts_ms": T0}, {"funding_bps": 2.0, "ts_ms": T0 + H8}]
    before = out.read_bytes()
    assert PP.main(["--in", p, "--out", str(out)]) == 2 and out.read_bytes() == before       # never overwritten
    link = tmp_path / "link.jsonl"
    os.symlink(tmp_path / "victim.txt", link)
    assert PP.main(["--in", p, "--out", str(link)]) == 2 and not (tmp_path / "victim.txt").exists()


@pytest.mark.parametrize("name", ["trading_state.db", ".env", "secrets.jsonl", "runs_evidence.jsonl"])
def test_protected_names_are_neither_read_nor_written(tmp_path, name):
    p = save(tmp_path, "a.json", page([row(T0)]))
    with pytest.raises(AR.RecordError):
        PP.write([(T0, 1.0)], str(tmp_path / name))
    protected_in = tmp_path / name
    protected_in.write_text(json.dumps(page([row(T0)])))
    with pytest.raises(AR.RecordError):
        PP.build([str(protected_in)], now_ms=NOW)
    assert PP.main(["--in", p, "--out", str(tmp_path / name)]) == 2


def test_inputs_that_are_links_or_other_than_single_link_regular_files_are_refused(tmp_path):
    real = save(tmp_path, "a.json", page([row(T0)]))
    link = tmp_path / "l.json"
    os.symlink(real, link)
    with pytest.raises(PP.PrintsError):
        PP.build([str(link)], now_ms=NOW)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(PP.PrintsError, match="regular file"):
        PP.build([str(fifo)], now_ms=NOW)                                             # does not hang: opened non-blocking, judged by fstat
    twin = tmp_path / "twin.json"
    os.link(real, twin)
    with pytest.raises((PP.PrintsError, AR.RecordError)):
        PP.build([real], now_ms=NOW)                                                  # now two links


# ------------------------------------------------------------------ the whole route

def test_raw_pages_to_the_journal_evaluate_to_the_same_result_as_the_pure_function(tmp_path):
    prints = window_prints(2.0, seed=3)                                               # the full registered window, in bps
    rows = [row(t, format(v / 1e4, ".12f")) for t, v in prints]                       # back to the venue's plain decimal fraction strings
    pages = [save(tmp_path, f"p{i}.json", page(list(reversed(rows[i:i + 200])))) for i in range(0, len(rows), 200)]
    built = PP.build(pages, now_ms=NOW)
    assert built["report"]["coverage"]["coverage"] == 1.0 and built["report"]["written"] == len(prints)
    for (t, v), (t2, v2) in zip(prints, built["prints"]):
        assert t == t2 and v2 == pytest.approx(v, abs=1e-6)                           # the round trip through the venue's string form
    out = tmp_path / "prints.jsonl"
    assert PP.main([*[x for p in pages for x in ("--in", p)], "--out", str(out)]) == 0
    loaded = CP._load(str(out))                                                       # the registered loader accepts the file
    assert len(loaded) == len(prints)
    pure = CP.evaluate(built["prints"], 10.0, SP.TrialLedger())
    logged = TJ.evaluate_logged(loaded, 10.0, str(tmp_path / "looks.jsonl"), first_look=True)
    logged.pop("journal")
    logged.pop("selection_sensitivity")
    assert json.dumps(logged, sort_keys=True, default=str) == json.dumps(pure, sort_keys=True, default=str)


def test_a_fraction_file_mistaken_for_bps_would_read_as_no_edge_which_is_why_the_tool_owns_the_unit():
    # Reproduced by the red team: the same series fed as fractions instead of bps. It is the reason this tool exists.
    prints = window_prints(2.0, seed=3)
    wrong = [(t, v / 1e4) for t, v in prints]
    assert "SUPPORTED" in CP.evaluate(prints, 10.0, SP.TrialLedger())["verdict"]
    assert "SUPPORTED" not in CP.evaluate(wrong, 10.0, SP.TrialLedger())["verdict"]


def test_the_module_makes_no_request_and_starts_no_process():
    tree = ast.parse(open(PP.__file__, encoding="utf-8").read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert imported <= {"__future__", "argparse", "decimal", "hashlib", "json", "math", "os", "re", "stat", "sys", "time", "typing", "account_review", "carry_prospective"}, imported
