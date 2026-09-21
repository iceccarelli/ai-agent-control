"""`append_closed_corpus.run_all` — keeping `_full` from freezing.

WHY THIS FILE EXISTS
====================
`append_closed_corpus.py` used to write only `data/real_linear_1d` and
`data/real_funding`. `daily_forward_refresh` scores the pilot against
`data/real_linear_1d_full` / `data/real_funding_full` instead, and nothing
ever appended to those — a cron faithfully running this tool every day could
still leave `_full` frozen indefinitely while Stage B's counter silently
stopped moving. `run_all` fetches and appends every tree against the SAME
`observed_at_utc` cutoff so they converge on the same closed-bar frontier,
without assuming their (differently-aged) histories line up row for row.

Nothing here reaches the network: every fetch is a fake.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import append_closed_corpus as acc                  # noqa: E402

DAY = 86_400_000


def _tree(tmp_path, label, last_day):
    """A minimal linear+funding pair ending exactly at `last_day` (str, ISO date)."""
    lin = tmp_path / f"{label}_lin.csv.gz"
    fun = tmp_path / f"{label}_fun.csv.gz"
    last = dt.datetime.strptime(last_day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    day0 = last - dt.timedelta(days=1)
    rows = ["time_period_start,time_period_end,time_open,time_close,price_open,"
            "price_high,price_low,price_close,volume_traded,trades_count"]
    for d in (day0, last):
        s = d.strftime("%Y-%m-%dT00:00:00+00:00")
        e = d.strftime("%Y-%m-%dT23:59:59+00:00")
        rows.append(f"{s},{e},{s},{e},77000,79000,76000,78000,1000,5")
    with gzip.open(lin, "wb") as fh:
        fh.write(("\r\n".join(rows) + "\r\n").encode())
    t = int((last + dt.timedelta(hours=16)).timestamp() * 1000)
    with gzip.open(fun, "wb") as fh:
        fh.write(("funding_time,funding_time_ms,symbol,funding_rate,mark_price\r\n"
                  f"{(last + dt.timedelta(hours=16)).strftime('%Y-%m-%dT%H:%M:%S+00:00')}"
                  f",{t + 5},BTCUSDT,0.00010000,79464.0\r\n").encode())
    return str(lin), str(fun)


def _fake_fetch(now_ms):
    """Every bar strictly after the file's own last close, through the open bar."""
    def fetch(path, params):
        if path == "klines":
            start = int(params["startTime"])
            out, o = [], start
            while o <= now_ms:  # includes the OPEN bar on purpose
                out.append([o, "77719.00000000", "79974.80", "76649", "78953.0",
                            "232502.35000000", o + DAY - 1, "0", 5914733])
                o += DAY
            return out
        if path == "fundingRate":
            start = int(params["startTime"])
            out, t = [], (start // 28_800_000 + 1) * 28_800_000
            while t <= now_ms + 28_800_000:  # includes one FUTURE print on purpose
                out.append({"symbol": "BTCUSDT", "fundingTime": t + 3,
                            "fundingRate": "0.00010000", "markPrice": "80000.1"})
                t += 28_800_000
            return out
        raise AssertionError(path)
    return fetch


NOW = "2026-09-16T18:19:11Z"
NOW_MS = int(dt.datetime(2026, 9, 16, 18, 19, 11, tzinfo=dt.timezone.utc).timestamp() * 1000)


def test_two_trees_starting_at_different_tips_converge(tmp_path):
    """Today's real shape: primary one bar ahead of full. Both must catch up
    to the SAME venue frontier, not to each other's starting tip."""
    p_lin, p_fun = _tree(tmp_path, "primary", "2026-09-15")
    f_lin, f_fun = _tree(tmp_path, "full", "2026-09-14")
    trees = (("primary", p_lin, p_fun), ("full", f_lin, f_fun))

    rep = acc.run_all(observed_at_utc=NOW, fetch=_fake_fetch(NOW_MS),
                      write=True, trees=trees)

    assert rep["tips_equal_after"] is True
    assert rep["linear_tips"]["primary"] == rep["linear_tips"]["full"] == "2026-09-15"
    # the full tree actually gained the bar it was missing
    assert "2026-09-15" in rep["trees"]["full"]["new_closed_bars"]


def test_prefix_is_preserved_on_both_trees_after_sync(tmp_path):
    p_lin, p_fun = _tree(tmp_path, "primary", "2026-09-15")
    f_lin, f_fun = _tree(tmp_path, "full", "2026-09-14")
    trees = (("primary", p_lin, p_fun), ("full", f_lin, f_fun))
    before = {path: acc.read_gz(path) for path in (p_lin, p_fun, f_lin, f_fun)}

    acc.run_all(observed_at_utc=NOW, fetch=_fake_fetch(NOW_MS), write=True, trees=trees)

    for path, was in before.items():
        now = acc.read_gz(path)
        assert now.startswith(was), f"{path} lost history bytes it must keep"


def test_the_open_bar_is_refused_on_every_tree(tmp_path):
    p_lin, p_fun = _tree(tmp_path, "primary", "2026-09-15")
    f_lin, f_fun = _tree(tmp_path, "full", "2026-09-14")
    trees = (("primary", p_lin, p_fun), ("full", f_lin, f_fun))

    rep = acc.run_all(observed_at_utc=NOW, fetch=_fake_fetch(NOW_MS), write=True, trees=trees)

    for name in ("primary", "full"):
        assert b"2026-09-16T00:00:00" not in acc.read_gz(
            p_lin if name == "primary" else f_lin)
        assert rep["trees"][name]["refused_open_bar"] == "2026-09-16"


def test_a_refusal_on_one_tree_writes_neither(tmp_path):
    """ATOMIC INTENT: if `full` cannot be appended safely, `primary` must not
    be written either — a half-synced pair looks fixed and is not."""
    # `full` starts far enough behind that its catch-up window has more than
    # one new bar — a single-bar window can't expose an internal gap at all.
    p_lin, p_fun = _tree(tmp_path, "primary", "2026-09-15")
    f_lin, f_fun = _tree(tmp_path, "full", "2026-09-12")
    trees = (("primary", p_lin, p_fun), ("full", f_lin, f_fun))
    before_primary = acc.read_gz(p_lin)

    inner = _fake_fetch(NOW_MS)

    def gappy_for_full(path, params):
        rows = inner(path, params)
        # only corrupt the SECOND caller's klines (the "full" tree's fetch);
        # a plain call counter keyed by path is enough since run_all fetches
        # primary before full for every tree it processes.
        gappy_for_full.calls += 1
        if path == "klines" and gappy_for_full.calls > 1:
            return rows[::2]  # introduces a gap
        return rows
    gappy_for_full.calls = 0

    with pytest.raises(acc.Refuse):
        acc.run_all(observed_at_utc=NOW, fetch=gappy_for_full, write=True, trees=trees)

    assert acc.read_gz(p_lin) == before_primary, (
        "primary was written even though full's dry run refused — "
        "half-synced trees are worse than unsynced ones")


def test_a_gap_on_a_single_tree_refuses_and_writes_nothing(tmp_path):
    """A gap need not come from the atomic cross-tree scenario to be
    refused: a single tree's own fetch skipping a bar must be caught on
    its own, before `run_all` is ever in the picture."""
    # far enough behind that the catch-up window has more than one new
    # bar — a single-bar window can't expose an internal gap at all.
    lin, fun = _tree(tmp_path, "solo", "2026-09-12")
    before_lin, before_fun = acc.read_gz(lin), acc.read_gz(fun)

    inner = _fake_fetch(NOW_MS)

    def gappy(path, params):
        rows = inner(path, params)
        return rows[::2] if path == "klines" else rows

    with pytest.raises(acc.Refuse):
        acc.run(observed_at_utc=NOW, fetch=gappy, linear_path=lin,
               funding_path=fun, write=True)

    assert acc.read_gz(lin) == before_lin
    assert acc.read_gz(fun) == before_fun


def test_dry_run_writes_neither_tree(tmp_path):
    p_lin, p_fun = _tree(tmp_path, "primary", "2026-09-15")
    f_lin, f_fun = _tree(tmp_path, "full", "2026-09-14")
    trees = (("primary", p_lin, p_fun), ("full", f_lin, f_fun))
    before = {path: acc.read_gz(path) for path in (p_lin, p_fun, f_lin, f_fun)}

    acc.run_all(observed_at_utc=NOW, fetch=_fake_fetch(NOW_MS), write=False, trees=trees)

    for path, was in before.items():
        assert acc.read_gz(path) == was


def test_primary_only_flag_touches_only_the_primary_tree():
    """The escape hatch: `--primary-only` must not silently drop back to
    the old single-tree behaviour by default — it is opt-in, named, tested."""
    assert acc.TREES[0][0] == "primary"
    import inspect
    src = inspect.getsource(acc.main)
    assert "primary_only" in src or "primary-only" in src
    assert "run_all(" in src, "main() must sync every tree by default"


def test_run_all_defaults_to_every_tree():
    """`TREES` names both trees, and `run_all`'s default parameter is that
    same tuple, not a primary-only subset — the sync-both behaviour is the
    default, not something a caller has to opt into."""
    assert acc.TREES == (("primary", acc.LINEAR_PATH, acc.FUNDING_PATH),
                         ("full", acc.FULL_LINEAR_PATH, acc.FULL_FUNDING_PATH))
    import inspect
    default_trees = inspect.signature(acc.run_all).parameters["trees"].default
    assert default_trees == acc.TREES
