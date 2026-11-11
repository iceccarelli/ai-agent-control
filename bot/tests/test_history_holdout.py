"""A holdout is a promise about ignorance: nothing may have read the window before the one test that is allowed to.

``tools/reserved_holdout.py`` already records reads of the old Binance daily corpus and proved the point (slice 57 declared an "out-of-sample" window that a full-sample run had
read). The cross-sectional study reads a DIFFERENT store (a market_history root through ``candidate_readiness.load_history_root``), which that ledger never saw: ``--certify`` was
vacuous for it, and nothing stopped an evaluator from walking the whole window and then calling the last part "out of sample".

``tools/history_holdout.py`` closes that for the history path. Before the first evaluation read the last 30% of the funding window (UTC-day aligned) is RESERVED in an append-only,
hash-chained ledger; every evaluation load is CUT at that boundary inside the loader (``load_history_root(cut_ms=...)``), so the rows are not merely ignored, they are never built;
the reserved window is readable exactly once through ``final_read``, which records the read before returning a byte. No reservation, no read (with the exact command in the
message); an explicit ``unreserved_exploratory`` read is stamped NOT_A_HOLDOUT_STUDY and permanently forbids reserving that dataset.

Every dataset here is the repo's own real-writer fixture; no number is an exchange observation.
"""
from __future__ import annotations

import json
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
import ms_pipeline as MP  # noqa: E402
import regime_entry_eval as RE  # noqa: E402
import research_harness as RH  # noqa: E402
import test_ms_pipeline_cli as TPC  # noqa: E402
from test_candidate_readiness import MARK_IV, _hist, offline  # noqa: E402,F401

DAY = 86_400_000


@pytest.fixture()
def world(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    return {"root": root, "uni": uni, "spot": spot, "led": str(tmp_path / "ledgers" / HH.LEDGER_NAME), "tmp": tmp_path}


def _rows(led):
    return [r for r in EC.rows_of(EC.read_lines(led)[0])] if os.path.exists(led) else []


def _load(w, **kw):
    kw.setdefault("ledger_path", w["led"])
    return HH.load(w["root"], universe_dir=w["uni"], spot_dir=w["spot"], mark_interval=MARK_IV, **kw)


def _reserve(w, **kw):
    return HH.reserve(w["led"], w["root"], mark_interval=MARK_IV, **kw)


def _final(w):
    return HH.final_read(w["root"], universe_dir=w["uni"], spot_dir=w["spot"], mark_interval=MARK_IV, ledger_path=w["led"])


def _last_ts(series):
    return max(max(ts for ts, _ in s.funding) for s in series), max(max(ts for ts, _ in s.mark) for s in series)


# ------------------------------------------------------------------ the cut itself
def test_the_cut_is_the_utc_day_boundary_at_seventy_percent_of_the_window():
    a, b = 1_700_000_000_000, 1_700_000_000_000 + 1000 * DAY
    cut = HH.cut_for(a, b, 0.30)
    assert cut % DAY == 0 and a < cut < b
    assert abs((cut - a) - 0.70 * (b - a)) < DAY
    assert HH.cut_for(a, b, 0.30) == cut                                  # deterministic: no clock, no randomness


@pytest.mark.parametrize("frac", [0.0, -0.1, 0.51, 1.0, float("nan"), True, "0.3"])
def test_a_reserved_fraction_outside_zero_to_one_half_is_refused(frac):
    with pytest.raises(RH.HarnessError, match="reserved fraction"):
        HH.cut_for(0, 100 * DAY, frac)


def test_a_window_too_short_to_leave_anything_to_explore_is_refused():
    with pytest.raises(RH.HarnessError, match="WINDOW_TOO_SHORT"):
        HH.cut_for(0, DAY // 2, 0.30)


# ------------------------------------------------------------------ nothing is readable before it is reserved
def test_an_evaluation_read_without_a_reservation_is_refused_with_the_exact_command(world):
    with pytest.raises(RH.HarnessError) as e:
        _load(world)
    msg = str(e.value)
    assert "HOLDOUT_NOT_RESERVED" in msg and "tools/history_holdout.py --reserve" in msg and "--unreserved-exploratory" in msg
    assert not os.path.exists(world["led"]), "a refused read must not write anything"


def test_the_reservation_binds_the_last_thirty_percent_of_the_funding_window(world):
    r = _reserve(world)
    man = json.load(open(os.path.join(world["root"], "funding", "manifest.json")))["request"]
    a, b = man["start_ms"], man["end_ms"]
    assert r["kind"] == "reserve" and r["window_ms"] == [a, b] and r["from_ms"] == HH.cut_for(a, b, 0.30) and r["frac"] == 0.30 and r["from_ms"] % DAY == 0
    assert r["dataset"] == "api.bybit.com|linear|funding+mark-kline-" + MARK_IV
    assert len(_rows(world["led"])) == 1 and EC.verify(world["led"])["ok"]


def test_reserving_twice_is_idempotent_and_never_moves_the_boundary(world):
    r1 = _reserve(world)
    r2 = _reserve(world)
    assert r2["from_ms"] == r1["from_ms"] and r2.get("already_reserved") is True
    assert len(_rows(world["led"])) == 1


def test_the_boundary_in_force_is_the_ledgered_one_not_a_recomputed_one(world):
    """Data grows (the recorder keeps recording): a recomputed 70% would drift later and quietly hand the explorer yesterday's holdout. The ledger row is the boundary."""
    r = _reserve(world)
    rows = _rows(world["led"])
    shifted = dict(rows[0], from_ms=r["from_ms"] - 20 * DAY)
    path = str(world["tmp"] / "other.jsonl")
    EC.append(path, shifted)
    s, info = HH.load(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_path=path)
    assert info["holdout"]["cut_ms"] == r["from_ms"] - 20 * DAY
    assert _last_ts(s)[0] < r["from_ms"] - 20 * DAY


# ------------------------------------------------------------------ the evaluator never builds a row at or after the cut
def test_an_evaluation_load_returns_no_funding_or_mark_row_at_or_after_the_cut(world):
    full, _ = CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV)
    r = _reserve(world)
    cut = r["from_ms"]
    assert _last_ts(full)[0] >= cut and _last_ts(full)[1] >= cut, "the fixture must actually have rows in the reserved window or the test proves nothing"
    series, info = _load(world)
    f_last, m_last = _last_ts(series)
    assert f_last < cut and m_last < cut
    assert [s.symbol for s in series] and sum(len(s.funding) for s in series) < sum(len(s.funding) for s in full)
    h = info["holdout"]
    assert h["status"] == "RESERVED" and h["purpose"] == "EXPLORE" and h["cut_ms"] == cut and "never built" in h["note"]
    assert info["history_window"]["to_ms"] == cut, "the study window the evaluator reports must end at the cut, not at the end of the data"


def test_the_cut_flows_into_the_evaluation_result(world):
    _reserve(world)
    out = CR.evaluate_cross_sectional(world["root"], universe_dir=world["uni"], ledger_path=str(world["tmp"] / "f.jsonl"), spot_dir=world["spot"], mark_interval=MARK_IV,
                                      holdout_ledger=world["led"])
    assert out["input"]["holdout"]["status"] == "RESERVED" and out["input"]["history_window"]["to_ms"] == out["input"]["holdout"]["cut_ms"]


def test_a_symbol_that_only_exists_inside_the_reserved_window_is_not_even_listed(world):
    """The funnel is judged on the explore window: a contract that lists inside the reserved window must not shape which symbols the explorer sees."""
    r = _reserve(world)
    _s, info = _load(world)
    assert info["funnel"]["window"]["to_ms"] == r["from_ms"]
    full, finfo = CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV)
    assert finfo["funnel"]["window"]["to_ms"] > r["from_ms"]


# ------------------------------------------------------------------ an exploratory read of unreserved data is stamped and is permanent
def test_an_unreserved_exploratory_read_is_stamped_recorded_and_forbids_a_later_reservation(world):
    series, info = _load(world, unreserved_exploratory=True, reader="unit")
    h = info["holdout"]
    assert h["status"] == "UNRESERVED_EXPLORATORY" and "NOT_A_HOLDOUT_STUDY" in h["label"] and h["cut_ms"] is None
    full, _ = CR.load_history_root(world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV)
    assert _last_ts(series) == _last_ts(full)                                     # it really did read everything: that is why it is recorded
    rows = _rows(world["led"])
    assert [(r["kind"], r["purpose"]) for r in rows] == [("read", "UNRESERVED_EXPLORE")]
    with pytest.raises(RH.HarnessError, match="ALREADY_READ"):
        _reserve(world)


def test_the_exploratory_escape_hatch_is_idempotent_per_reader(world):
    _load(world, unreserved_exploratory=True, reader="a")
    _load(world, unreserved_exploratory=True, reader="a")
    assert len(_rows(world["led"])) == 1
    _load(world, unreserved_exploratory=True, reader="b")
    assert len(_rows(world["led"])) == 2


def test_the_escape_hatch_never_downgrades_an_existing_reservation(world):
    r = _reserve(world)
    series, info = _load(world, unreserved_exploratory=True)
    assert info["holdout"]["status"] == "RESERVED" and _last_ts(series)[0] < r["from_ms"]
    assert [x["kind"] for x in _rows(world["led"])] == ["reserve"]


# ------------------------------------------------------------------ the reserved window is readable once
def test_the_final_read_is_granted_once_and_recorded(world):
    r = _reserve(world)
    series, info = _final(world)
    assert _last_ts(series)[0] >= r["from_ms"], "the final read must contain the reserved window"
    h = info["holdout"]
    assert h["purpose"] == "FINAL" and h["status"] == "SPENT" and h["evaluate_only_from_ms"] == r["from_ms"]
    rows = _rows(world["led"])
    assert [(x["kind"], x.get("purpose")) for x in rows] == [("reserve", None), ("read", "FINAL")]
    assert rows[-1]["from_ms"] == r["from_ms"] and rows[-1]["to_ms"] >= r["from_ms"]
    with pytest.raises(RH.HarnessError, match="FINAL_READ_ALREADY_SPENT"):
        _final(world)
    assert len(_rows(world["led"])) == 2


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
    assert [x["kind"] for x in _rows(world["led"])] == ["reserve"]
    series, _info = _final(world)                                                  # the failed attempt did not spend it
    assert series


def test_after_the_final_read_exploration_needs_a_new_reservation(world):
    _reserve(world)
    _final(world)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_SPENT"):
        _load(world)
    with pytest.raises(RH.HarnessError, match="RESERVATION_OVERLAPS_A_SPENT_HOLDOUT"):
        _reserve(world)                                                           # the same data has no new window to reserve


def test_a_final_read_without_a_reservation_is_refused(world):
    with pytest.raises(RH.HarnessError, match="HOLDOUT_NOT_RESERVED"):
        _final(world)
    assert not os.path.exists(world["led"])


def test_a_final_read_that_fails_to_verify_does_not_spend_the_holdout(world):
    _reserve(world)
    with pytest.raises(RH.HarnessError):
        HH.final_read(world["root"], universe_dir=str(world["tmp"] / "no-such-universe"), spot_dir=world["spot"], mark_interval=MARK_IV, ledger_path=world["led"])
    assert [x["kind"] for x in _rows(world["led"])] == ["reserve"]
    _final(world)                                                                 # still available


# ------------------------------------------------------------------ the ledger is evidence
def test_a_tampered_ledger_refuses_every_read(world):
    _reserve(world)
    text = open(world["led"], encoding="utf-8").read()
    open(world["led"], "w", encoding="utf-8").write(text.replace('"frac":0.3', '"frac":0.1'))
    with pytest.raises(RH.HarnessError, match="HOLDOUT_LEDGER_UNVERIFIED"):
        _load(world)
    with pytest.raises(RH.HarnessError, match="HOLDOUT_LEDGER_UNVERIFIED"):
        _final(world)


def test_a_deleted_ledger_cannot_be_distinguished_from_a_fresh_one_so_the_tool_says_where_it_looked(world):
    _reserve(world)
    os.remove(world["led"])
    with pytest.raises(RH.HarnessError) as e:
        _load(world)
    assert world["led"] in str(e.value), "the refusal must name the ledger it looked in: a forgotten --ledger-dir is the commonest way to look in the wrong place"


def test_the_dataset_identity_does_not_depend_on_where_the_data_lives(world):
    copy = str(world["tmp"] / "copy")
    shutil.copytree(world["root"], copy)
    assert HH.dataset_id(copy, MARK_IV) == HH.dataset_id(world["root"], MARK_IV)
    assert HH.dataset_id(world["root"], "60") != HH.dataset_id(world["root"], MARK_IV)


def test_the_status_report_is_read_only_and_states_the_arithmetic(world):
    st = HH.status(world["led"], world["root"], mark_interval=MARK_IV)
    assert st["state"] == "NOT_RESERVED" and not os.path.exists(world["led"])
    r = _reserve(world)
    st = HH.status(world["led"], world["root"], mark_interval=MARK_IV)
    assert st["state"] == "RESERVED" and st["from_ms"] == r["from_ms"]
    assert st["explore_days"] > 0 and st["reserved_days"] > 0 and st["standard_grid_days_required"] == 457
    assert st["explore_supports_standard_grid"] is False and st["reserved_rows_possible"] == int(st["reserved_days"] // 7)
    _final(world)
    assert HH.status(world["led"], world["root"], mark_interval=MARK_IV)["state"] == "SPENT"


# ------------------------------------------------------------------ the CLI
def test_the_cli_reserves_reports_and_refuses_a_bad_request(world, capsys):
    argv = ["--history-root", world["root"], "--mark-interval", MARK_IV, "--ledger-dir", os.path.dirname(world["led"])]
    assert HH.main(["--status", *argv]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "NOT_RESERVED"
    assert HH.main(["--reserve", *argv]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "RESERVED" and os.path.exists(world["led"])
    assert HH.main(["--reserve", *argv, "--fraction", "0.9"]) == 2
    assert "reserved fraction" in capsys.readouterr().err
    assert len(_rows(world["led"])) == 1, "a refused request must not have appended anything"


def test_a_later_request_for_a_different_fraction_never_moves_the_ledgered_boundary(world):
    r1 = _reserve(world, frac=0.30)
    r2 = _reserve(world, frac=0.20)
    assert r2["from_ms"] == r1["from_ms"] and r2["frac"] == 0.30 and r2["requested_fraction_ignored"] == 0.20


def test_the_cli_cannot_spend_the_final_read(world, capsys):
    """There is no flag that opens the reserved window: the frozen-hypothesis evaluator that may call final_read does not exist yet, and a human typing a flag is exactly the failure this prevents."""
    with pytest.raises(SystemExit) as e:
        HH.main(["--final", "--history-root", world["root"]])
    assert e.value.code == 2 and "usage" in capsys.readouterr().err
    assert not os.path.exists(world["led"])
    with pytest.raises(SystemExit) as e:
        HH.main(["--reserve", "--final", "--history-root", world["root"], "--ledger-dir", os.path.dirname(world["led"])])
    assert e.value.code == 2 and not os.path.exists(world["led"])


# ------------------------------------------------------------------ one enforcement point: nobody but the guard (and metadata tools) builds series from a history root
def test_only_the_guard_and_the_metadata_functions_call_the_unguarded_loader():
    """(file, function) granularity: a direct call inside an ALLOWED file but a different function (say a new evaluator added to candidate_readiness.py) is still a bypass."""
    import ast
    allowed = {("candidate_readiness.py", "dataset_report"): "reads coverage and symbol lists only, never a return series",
               ("backfill_plan.py", "_run_loader"): "diagnose checks that the loader and the funnel agree: metadata only",
               ("history_holdout.py", "load"): "THE guard (evaluation reads, cut at the boundary)",
               ("history_holdout.py", "final_read"): "THE guard (the one read of the reserved window)"}
    offenders, seen = [], set()
    for base in (BOT, os.path.join(BOT, "tools")):
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(base, name), encoding="utf-8").read())
            parents = {ch: node for node in ast.walk(tree) for ch in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "load_history_root":
                    p = node
                    while p in parents and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        p = parents[p]
                    key = (name, getattr(p, "name", "<module>"))
                    seen.add(key)
                    if key not in allowed:
                        offenders.append(f"{name}:{node.lineno} in {key[1]}")
    assert not offenders, f"these call load_history_root directly and so bypass the reserved holdout: {offenders}; route them through history_holdout.load(...)"
    assert set(allowed) <= seen, f"the allow-list names a call that no longer exists (delete the entry): {sorted(set(allowed) - seen)}"


# ------------------------------------------------------------------ every research entry point goes through the guard
def _family_ledgers(ld):
    return sorted(f for f in os.listdir(ld) if f.startswith("cross_sectional_funding")) if os.path.isdir(ld) else []


def test_the_readiness_cli_refuses_an_unreserved_dataset_then_evaluates_the_explore_window(world, capsys):
    ld = os.path.dirname(world["led"])
    argv = ["--history-root", world["root"], "--universe-dir", world["uni"], "--spot-dir", world["spot"], "--mark-interval", MARK_IV, "--ledger", os.path.join(ld, "cross_sectional_funding.jsonl")]
    with pytest.raises(SystemExit) as e:
        CR.main(argv)
    assert e.value.code == 2 and "HOLDOUT_NOT_RESERVED" in capsys.readouterr().err
    assert _family_ledgers(ld) == [], "a refused run must not enlarge the multiple-testing count"
    _reserve(world)
    assert CR.main(argv) == 0
    assert "ranked funding:" in capsys.readouterr().out and _family_ledgers(ld)


def test_the_competition_refuses_an_unreserved_dataset_says_so_in_the_row_and_counts_nothing(world):
    ld = os.path.dirname(world["led"])
    rep = CC_.run_competition(data_root=world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_dir=ld)
    x = next(r for r in rep["results"] if r["name"] == "cross_sectional_funding")
    assert x["verdict"] == "UNTESTED" and any("HOLDOUT_NOT_RESERVED" in r and "history_holdout.py --reserve" in r for r in x["rejection_reasons"]), x["rejection_reasons"]
    assert _family_ledgers(ld) == []
    assert rep["winner"] is None


def test_the_competition_on_a_reserved_dataset_runs_the_explore_window_and_the_table_shows_survivorship_and_the_holdout(world):
    ld = os.path.dirname(world["led"])
    r = _reserve(world)
    rep = CC_.run_competition(data_root=world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_dir=ld)
    x = next(r_ for r_ in rep["results"] if r_["name"] == "cross_sectional_funding")
    assert x["holdout"]["status"] == "RESERVED" and x["holdout"]["cut_ms"] == r["from_ms"]
    assert x["survivorship"] in (CR.SURVIVORS_ONLY, CR.DELISTINGS_OBSERVED)
    table = CC_.render_table(rep).splitlines()
    cols = table[0].split("\t")
    assert "survivorship" in cols and "holdout" in cols
    row = next(line for line in table[1:] if line.split("\t")[1] == "cross_sectional_funding").split("\t")
    assert row[cols.index("holdout")] == "RESERVED" and row[cols.index("survivorship")] == x["survivorship"]


def test_a_survivors_only_cross_section_is_visible_in_the_competition_table(world):
    rep = CC_.run_competition(data_root=world["root"], universe_dir=world["uni"], spot_dir=world["spot"], mark_interval=MARK_IV, ledger_dir=os.path.dirname(world["led"]),
                              unreserved_exploratory=True)
    x = next(r_ for r_ in rep["results"] if r_["name"] == "cross_sectional_funding")
    assert x["holdout"]["status"] == "UNRESERVED_EXPLORATORY" and x["rejection_reasons"][0].startswith("HOLDOUT UNRESERVED_EXPLORATORY"), x["rejection_reasons"][:2]
    table = CC_.render_table(rep)
    assert "UNRESERVED_EXPLORATORY" in table and (CR.SURVIVORS_ONLY in table or CR.DELISTINGS_OBSERVED in table)


def test_the_regime_cli_refuses_an_unreserved_dataset_then_runs_the_explore_window(world, capsys):
    ld = os.path.dirname(world["led"])
    argv = ["--history-root", world["root"], "--universe-dir", world["uni"], "--spot-dir", world["spot"], "--mark-interval", MARK_IV, "--ledger-dir", ld, "--registration", "reg-2",
            "--json", str(world["tmp"] / "o.json")]
    assert RE.main(argv) == 2 and "HOLDOUT_NOT_RESERVED" in capsys.readouterr().err
    assert not os.path.exists(str(world["tmp"] / "o.json"))
    r = _reserve(world)
    assert RE.main(argv) == 0
    out = json.load(open(str(world["tmp"] / "o.json")))
    assert out["input"]["holdout"]["status"] == "RESERVED" and out["input"]["history_window"]["to_ms"] == r["from_ms"]


def test_the_index_series_the_regime_evaluator_reads_separately_is_cut_at_the_boundary_too(tmp_path, monkeypatch):
    """The basis measurement is a SECOND read of the same window (index-kline closes): cutting only the funding/mark series would leave the reserved window readable through it."""
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
    w = types.SimpleNamespace(root=world["root"], uni=world["uni"], spot=world["spot"], ledger=os.path.dirname(world["led"]))
    return TPC.argv(w, str(world["tmp"] / "art"), cmd, **{"unreserved_exploratory": None, **over})


def test_the_pipeline_cli_refuses_an_unreserved_dataset_and_the_identity_says_which_window_was_read(world, capsys):
    code, doc = TPC.cli(capsys, _pipeline_args(world))
    assert code == 2 and doc["error"]["code"] == "HOLDOUT_NOT_RESERVED" and "history_holdout.py --reserve" in doc["error"]["message"]
    r = _reserve(world)
    code, cut_doc = TPC.cli(capsys, _pipeline_args(world))
    assert code == 0, cut_doc
    assert cut_doc["data_parts"]["holdout"] == f"RESERVED:{r['from_ms']}"
    fresh = {**world, "led": str(world["tmp"] / "other" / HH.LEDGER_NAME)}
    code, full_doc = TPC.cli(capsys, _pipeline_args(fresh, unreserved_exploratory=True, ledger_dir=os.path.dirname(fresh["led"])))
    assert code == 0, full_doc
    assert full_doc["data_parts"]["holdout"] == "UNRESERVED_EXPLORATORY"
    assert cut_doc["data_digest"] != full_doc["data_digest"], "a cut run and a full-window run read different data: the identity must differ"


def test_the_pipeline_cli_flag_does_not_defeat_an_existing_reservation(world, capsys):
    r = _reserve(world)
    code, doc = TPC.cli(capsys, _pipeline_args(world, unreserved_exploratory=True))
    assert code == 0 and doc["data_parts"]["holdout"] == f"RESERVED:{r['from_ms']}"
