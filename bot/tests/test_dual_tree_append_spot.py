"""`append_spot_corpus.run_all` — the same dual-tree fix W1b gave linear/funding,
applied to spot.

WHY THIS FILE EXISTS
====================
`corpus_health.py --corpus full` reported `perp_1d`/`funding` current after
the Binance catch-up (mission_W1b_catchup) but `spot_1d` still stale: nothing
kept `data/real_spot_full` moving the way `data/real_spot_btc` was. The
forward pilot itself does not read spot at all (see
test_the_forward_pilot_does_not_read_spot in test_daily_forward_refresh.py) —
this is book-health hygiene for corpus_health / carry_backtest's basis leg,
not a Stage B blocker. Fixed the same way as the linear/funding trees:
independent fetch-and-append per tree against the same cutoff, atomic
dry-run-first so a refusal on one tree writes neither.

Nothing here reaches the network: every fetch is fixed `klines`.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import append_spot_corpus as asc                    # noqa: E402

TODAY = dt.date(2026, 9, 20)


def kline(day: dt.date, price: float = 100_000.0):
    ms = int(dt.datetime.combine(day, dt.time(),
                                 dt.timezone.utc).timestamp() * 1000)
    return [ms, price, price, price, price, "1.0", 0, 0, "100"]


def _write_tree(tmp_path, rel_path, last_day):
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asc.to_row(kline(last_day - dt.timedelta(days=i)))
            for i in range(2, -1, -1)]               # 3 days ending last_day
    with gzip.open(path, "wb") as fh:
        fh.write(asc.encode(rows))
    return rel_path


@pytest.fixture
def repo(tmp_path):
    """Today's real shape: primary a few days ahead of full."""
    primary = _write_tree(tmp_path, asc.SPOT_PATH, dt.date(2026, 9, 15))
    full = _write_tree(tmp_path, asc.FULL_SPOT_PATH, dt.date(2026, 9, 12))
    trees = (("primary", primary), ("full", full))
    return str(tmp_path), trees


def test_two_trees_at_different_tips_converge(repo):
    repo_dir, trees = repo
    new_days = [kline(dt.date(2026, 9, d)) for d in range(13, 16)]  # 13,14,15
    rep = asc.run_all(repo_dir, today=TODAY, klines=new_days, write=True,
                      trees=trees)
    assert rep["tips_equal_after"] is True
    assert rep["tips"]["primary"] == rep["tips"]["full"] == "2026-09-15"
    assert "2026-09-13" in rep["trees"]["full"]["new_closed_days"]
    assert "2026-09-14" in rep["trees"]["full"]["new_closed_days"]
    assert rep["trees"]["primary"]["new_closed_days"] == []  # already current


def test_prefix_preserved_on_both_trees(repo):
    repo_dir, trees = repo
    before = {}
    for _, rel in trees:
        with open(os.path.join(repo_dir, rel), "rb") as fh:
            before[rel] = fh.read()

    new_days = [kline(dt.date(2026, 9, d)) for d in range(13, 16)]
    asc.run_all(repo_dir, today=TODAY, klines=new_days, write=True, trees=trees)

    for rel, was in before.items():
        with open(os.path.join(repo_dir, rel), "rb") as fh:
            now = fh.read()
        assert gzip.decompress(now).startswith(gzip.decompress(was))


def test_the_open_day_is_refused_on_every_tree(repo):
    repo_dir, trees = repo
    new_days = [kline(dt.date(2026, 9, d)) for d in range(13, 16)] + [kline(TODAY)]
    rep = asc.run_all(repo_dir, today=TODAY, klines=new_days, write=True, trees=trees)
    for name in ("primary", "full"):
        assert str(TODAY) not in rep["trees"][name]["new_closed_days"]
        assert rep["trees"][name]["refused_open_day"] == str(TODAY)


def test_a_history_rewrite_on_one_tree_writes_neither(repo, monkeypatch):
    """ATOMIC INTENT: if `full`'s prefix check would fail, `primary` must not
    be written either."""
    repo_dir, trees = repo
    primary_path = os.path.join(repo_dir, trees[0][1])
    with open(primary_path, "rb") as fh:
        before_primary = fh.read()

    real_run = asc.run
    calls = {"n": 0}

    def flaky_run(*args, **kwargs):
        calls["n"] += 1
        rep = real_run(*args, **kwargs)
        if calls["n"] == 2:  # the "full" tree's dry run
            rep["error"] = "APPEND WOULD REWRITE HISTORY — refusing (simulated)"
        return rep

    monkeypatch.setattr(asc, "run", flaky_run)
    new_days = [kline(dt.date(2026, 9, d)) for d in range(13, 16)]
    rep = asc.run_all(repo_dir, today=TODAY, klines=new_days, write=True, trees=trees)

    assert "refused" in rep and "full" in rep["refused"]
    with open(primary_path, "rb") as fh:
        assert fh.read() == before_primary, (
            "primary was written even though full's dry run refused")


def test_a_gap_on_one_tree_refuses_that_tree_and_writes_neither(repo):
    """A single tree skipping a day is refused on its own — `run_all`'s
    atomic dry-run-first then keeps the OTHER tree from being written too."""
    repo_dir, trees = repo
    primary_path = os.path.join(repo_dir, trees[0][1])
    full_path = os.path.join(repo_dir, trees[1][1])
    before_primary = open(primary_path, "rb").read()
    before_full = open(full_path, "rb").read()

    # primary's own tip is 09-15; offering 09-17 skips 09-16.
    gappy_days = [kline(dt.date(2026, 9, 17))]
    rep = asc.run_all(repo_dir, today=TODAY, klines=gappy_days, write=True,
                      trees=trees)

    assert "refused" in rep and "primary" in rep["refused"]
    assert "GAP" in rep["refused"]["primary"]
    assert open(primary_path, "rb").read() == before_primary
    assert open(full_path, "rb").read() == before_full


def test_dry_run_writes_neither_tree(repo):
    repo_dir, trees = repo
    before = {}
    for _, rel in trees:
        with open(os.path.join(repo_dir, rel), "rb") as fh:
            before[rel] = fh.read()

    new_days = [kline(dt.date(2026, 9, d)) for d in range(13, 16)]
    asc.run_all(repo_dir, today=TODAY, klines=new_days, write=False, trees=trees)

    for rel, was in before.items():
        with open(os.path.join(repo_dir, rel), "rb") as fh:
            assert fh.read() == was


def test_primary_only_stays_opt_in():
    assert asc.TREES[0][0] == "primary"
    import inspect
    src = inspect.getsource(asc.main)
    assert "primary_only" in src
    assert "run_all(" in src, "main() must sync every tree by default"
    default_trees = inspect.signature(asc.run_all).parameters["trees"].default
    assert default_trees == asc.TREES
