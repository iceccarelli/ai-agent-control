"""Three findings of the second independent review, each reproduced on the fake venue before the fix:

1. the headline guard "an unattended session is valid only under the autopilot loop" tested only that a pre-order HOOK exists; the owner's real command line (``guarded_main``) always supplies one
   (it only renews the lease), so ``execute --unattended`` ran a full session past ALERT / STOP / PAUSED, the capital book and the revocation of the grant;
2. the Testnet session record published fees and the price leg from a ledger whose own integrity flags said its inputs conflicted, and from a session whose incomplete-fills safeguard never ran
   because the measurement itself raised;
3. the glue that feeds the capital book (the autopilot's ``known`` / ``funding_was_read`` / ``flat_verified``) had no test that failed when it was broken: mutants (unknowns-as-zero, funding always
   read, always flat) survived every capital and autopilot test, and the one test that claimed to cover it re-implemented the formula in a lambda.

In-process fake Bybit testnet only: no network, no credential, no order outside the fake."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import ms_capital as CC  # noqa: E402
import ms_testnet_session as S  # noqa: E402
from ms_testnet_world import World  # noqa: E402
from test_ms_testnet_autopilot import _cycle_eng, _session_world  # noqa: E402

CREATE = "/v5/order/create"


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _ready(w, hold="60"):
    assert w.run("preflight") == 0
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--hold-seconds", hold) == 0
    return w.plan_doc()["plan_digest"]


# ------------------------------------------------------------------ 1. the owner's command line cannot run an unattended session
def test_the_owners_command_line_refuses_unattended_before_it_takes_the_lease_or_runs_anything(capsys):
    calls = {"acquire": 0, "run": 0}

    def acquire(_env):
        calls["acquire"] += 1
        raise AssertionError("the lease must not even be asked for")

    def run(*_a, **_k):
        calls["run"] += 1
        raise AssertionError("main must not run")
    rc = S.guarded_main(["--state-dir", "/nonexistent", "execute", "--confirm-plan-digest", "a" * 64, "--unattended"], env={}, acquire=acquire, run=run)
    out = capsys.readouterr().out
    assert rc == S.EXIT_REFUSED and calls == {"acquire": 0, "run": 0}
    assert "REFUSED" in out and "autopilot loop" in out and "no order was sent" in out


def test_the_attended_command_line_still_takes_the_lease_and_runs(capsys):
    seen = {}

    class Handle:
        def renew(self):
            return True

        def release(self):
            seen["released"] = True

    def run(argv, **kw):
        seen["hook"] = kw.get("pre_order_check")
        return 0
    rc = S.guarded_main(["--state-dir", "/nonexistent", "execute", "--confirm-plan-digest", "a" * 64], env={}, acquire=lambda _e: (Handle(), ""), run=run)
    assert rc == 0 and seen["released"] is True and callable(seen["hook"])


def test_a_bare_unattended_main_without_the_loops_hook_is_still_refused(w):
    d = _ready(w)
    assert w.run("execute", "--confirm-plan-digest", d, "--unattended") != 0
    assert "UNATTENDED_ONLY_VALID_UNDER_THE_AUTOPILOT_LOOP" in w.text()
    assert not [r for r in w.fake.requests if r[0] == "POST" and r[1] == CREATE]


# ------------------------------------------------------------------ 2. the session record never publishes a partial sum as a measurement
def test_a_raising_quality_measurement_is_unknown_not_a_lower_bound(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("the measurement broke")
    monkeypatch.setattr(S.EQ, "leg_quality", boom)
    d = _ready(world)
    assert world.run("execute", "--confirm-plan-digest", d, typed=d) == 0, world.text()[-300:]
    net = world.record()["net"]
    assert net["fees_usd"] is None and net["price_pnl_usd"] is None, f"the safeguard against an unlisted fill never ran, yet the record published {net}"


def test_a_ledger_that_flags_a_conflict_in_its_own_inputs_publishes_no_fees_and_no_price_leg(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    real = S.AC.Ledger.canonical_net

    def flagged(self, *a, **k):
        out = real(self, *a, **k)
        out["flags"] = list(out.get("flags", [])) + ["DUPLICATE_FILL_CONFLICT:E1"]
        return out
    monkeypatch.setattr(S.AC.Ledger, "canonical_net", flagged)
    d = _ready(world)
    assert world.run("execute", "--confirm-plan-digest", d, typed=d) == 0, world.text()[-300:]
    net = world.record()["net"]
    assert net["fees_usd"] is None and net["price_pnl_usd"] is None


def test_a_session_that_ends_not_flat_publishes_its_fees_but_no_price_leg(tmp_path, monkeypatch):
    """The price leg of an open position is a mark-to-market, not a realised figure: the record publishes it only after the venue proved flat (a mutant that published it always survived every test)."""
    world = World(tmp_path, monkeypatch)
    d = _ready(world)
    world.fake.inject(CREATE, "rate_limit", times=10_000, match=lambda body: bool(body.get("reduceOnly")))       # every reduce-only exit is refused: the hedge stays open
    world.run("execute", "--confirm-plan-digest", d, typed=d)
    rec = world.record()
    assert rec["flat_verified"] is False, "control: the venue did not end flat"
    assert rec["net"]["price_pnl_usd"] is None, f"an open position's mark-to-market was published as a price leg: {rec['net']}"
    assert rec["net"]["fees_usd"] is not None, "the fees of the fills that did happen are measured and stay published"


def test_a_conflicted_funding_figure_is_not_published_but_a_mere_valuation_flag_does_not_hide_it(tmp_path, monkeypatch):
    """Independent re-review, finding 12: fees and the price leg were nulled on an integrity flag but ``funding_usd`` stayed: two funding prints for one settlement give FUNDING_CONFLICT,
    ``net_complete`` False and a funding sum (the first print booked) that is not a measurement."""
    real = S.AC.Ledger.canonical_net

    def with_flags(*flags):
        def patched(self, *a, **k):
            out = real(self, *a, **k)
            out["funding"] = 1.25
            out["flags"] = list(out.get("flags", [])) + list(flags)
            return out
        return patched
    monkeypatch.setattr(S.AC.Ledger, "canonical_net", with_flags("FUNDING_CONFLICT:BTCUSDT:1500"))
    world = World(tmp_path, monkeypatch)
    d = _ready(world)
    assert world.run("execute", "--confirm-plan-digest", d, typed=d) == 0, world.text()[-300:]
    net = world.record()["net"]
    assert net["funding_usd"] is None and net["fees_usd"] is None and net["price_pnl_usd"] is None, net
    monkeypatch.setattr(S.AC.Ledger, "canonical_net", with_flags("MID_FALLBACK"))                      # a valuation convention is not an integrity conflict
    world2 = World(tmp_path / "second", monkeypatch)
    d2 = _ready(world2)
    assert world2.run("execute", "--confirm-plan-digest", d2, typed=d2) == 0, world2.text()[-300:]
    assert world2.record()["net"]["funding_usd"] == 1.25, "a flag that is not an integrity conflict must not hide a measured funding figure"
    # unlisted executions null the fees and the price leg (a lower bound is not a measurement) AND the funding (round-10 specialist H1): the held window starts at the first VISIBLE perp fill, so a fill the venue's
    # list does not show can leave that start wrong, a settlement the pair really was held through falls before it, and "no settlement while held" is a DERIVED zero. An unknown window cannot be a zero.
    # (This was pinned the other way round, 1.25, before: that expectation was the defect, not a control.)
    monkeypatch.setattr(S.AC.Ledger, "canonical_net", with_flags())
    world3 = World(tmp_path / "third", monkeypatch)
    for i in range(1, 9):
        world3.fake.hidden_execs.add("E%06d" % i)
    d3 = _ready(world3)
    assert world3.run("execute", "--confirm-plan-digest", d3, typed=d3) == 0, world3.text()[-300:]
    n3 = world3.record()["net"]
    assert n3["fees_usd"] is None and n3["price_pnl_usd"] is None and n3["funding_usd"] is None, f"incomplete executions leave the held window unknown: a funding figure ({n3}) is a derived zero or a partial sum, not a measurement"


def test_a_session_with_a_clean_ledger_keeps_its_measured_net(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    d = _ready(world)
    assert world.run("execute", "--confirm-plan-digest", d, typed=d) == 0, world.text()[-300:]
    net = world.record()["net"]
    assert net["fees_usd"] is not None and net["price_pnl_usd"] is not None


# ------------------------------------------------------------------ 3. the glue that feeds the capital book
def _pending(eng):
    return CC.CapitalBook(eng).load()["pending"]


def test_a_filled_order_whose_executions_are_not_listed_parks_an_unknown_net_never_a_zero(w):
    _session_world(w)
    orig = w.fake._fill

    def hide(o, qty, px, *, taker, dup=False):
        orig(o, qty, px, taker=taker, dup=dup)
        if o["orderLinkId"].endswith("-p"):
            w.fake.hidden_execs.add(w.fake.execs[-1]["execId"])                 # the perp ENTRY fill: the order row says Filled, the execution list does not carry it
    w.fake._fill = hide
    res, eng = _cycle_eng(w)
    p = _pending(eng)
    assert p is not None and p["session_id"] == os.path.basename(res["state_dir"])
    assert p["ledger_net_known_usd"] is None, f"an unlisted fill was parked as a measured net: {p}"


def test_a_session_that_ends_with_the_venue_not_flat_is_parked_as_not_flat(w):
    _session_world(w)
    w.fake.inject(CREATE, "rate_limit", times=10_000, match=lambda d: bool(d.get("reduceOnly")))      # every reduce-only exit is refused: the hedge cannot be closed
    res, eng = _cycle_eng(w)
    assert res["result"] != "DONE", res
    p = _pending(eng)
    assert p is not None and p["flat_verified"] is False, f"a session the venue did not prove flat must never be parked as flat: {p}"


def test_a_session_whose_funding_was_not_recognised_is_parked_as_funding_not_read(w):
    _session_world(w)
    res, eng = _cycle_eng(w)                                                    # no settlement printed during the hold: the venue's funding rows say nothing
    rec = json.load(open(os.path.join(res["state_dir"], "testnet_session.json")))
    assert rec["net"]["funding_usd"] is None
    p = _pending(eng)
    assert p["funding_was_read"] is False, f"funding that was never read was parked as read: {p}"
    assert p["ledger_net_known_usd"] is not None, "control: fees and the price leg were measured, so the (funding-free) net is known"
