"""ADVERSARIAL: no automated or owner-run RECOVERY path can introduce an unhedged or unauthorised position.

For every crash point of an `execute` (the process dies right after the venue accepted the perp leg, right after the spot leg, or the acknowledgement of either is lost), the venue is left
in whatever state it is in and the recovery commands run: a plain `reconcile` (adopt / cancel only) and the confirmed `reconcile --unwind`. A watcher on the fake venue records the position
before and after EVERY order those commands send. Invariants, checked on every order sent during recovery:
  * no ENTRY link is ever created again (the accepted order is adopted, never re-created);
  * the perp is only ever REDUCED (reduce-only, absolute size never grows) and the spot is only ever SOLD (the base balance never grows);
  * after the unwind the venue itself is flat, from a complete read.
Also: an engaged kill switch and a held account lock mean recovery sends no entry either."""
import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import ms_testnet_adapter as AD  # noqa: E402
from ms_fake_bybit_testnet import SimulatedCrash  # noqa: E402
from ms_testnet_world import World  # noqa: E402
import ms_executor_lock as XL  # noqa: E402

CREATE = "/v5/order/create"


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _watch(w):
    """Wrap the fake's order creation: record (link, reduceOnly, side, category, perp size before/after, base balance before/after) for every create after this point."""
    seen = []
    real = w.fake._create

    def create(d):
        before = (w.fake.positions.get("BTCUSDT", {"size": D(0)})["size"], w.fake.wallet["BTC"])
        out = real(d)
        after = (w.fake.positions.get("BTCUSDT", {"size": D(0)})["size"], w.fake.wallet["BTC"])
        seen.append({"link": d.get("orderLinkId", ""), "reduce": d.get("reduceOnly") is True, "side": d.get("side"), "category": d.get("category"), "before": before, "after": after, "ok": out.get("retCode") == 0})
        return out
    w.fake._create = create
    return seen


def _assert_only_reduces(seen, entry_links):
    for s in seen:
        assert s["link"] not in entry_links, f"an ENTRY link was created again during recovery: {s}"
        if s["category"] == "linear":
            assert s["reduce"] is True and s["side"] in ("Buy", "Sell"), s
            assert abs(s["after"][0]) <= abs(s["before"][0]), f"perp exposure GREW during recovery: {s}"
        else:
            assert s["side"] == "Sell" and s["after"][1] <= s["before"][1], f"spot base GREW during recovery: {s}"


def _ready(w):
    assert w.run("preflight") == 0 and w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 0
    return w.plan_doc()["plan_digest"]


@pytest.mark.parametrize("fault,leg", [("crash_after", "-p"), ("crash_after", "-s"), ("lose_response", "-p"), ("lose_response", "-s")])
def test_no_recovery_command_ever_grows_exposure_and_the_unwind_ends_flat(w, fault, leg):
    d = _ready(w)
    gid = w.plan_doc()["group"]["group_id"]
    entry_links = {gid + "-p", gid + "-s"}
    w.fake.inject(CREATE, fault, match=lambda b: b["orderLinkId"].endswith(leg))
    try:
        rc = w.run("execute", "--confirm-plan-digest", d, typed=d)
    except SimulatedCrash:
        rc = None
    created_before = dict(w.fake.create_calls)
    seen = _watch(w)
    w.run("reconcile")                                                                  # adopt / cancel only
    assert all(s["link"] not in entry_links for s in seen)
    w.run("reconcile")                                                                  # and again: idempotent, still no entry
    w.run("reconcile", "--unwind", "--confirm-plan-digest", d)
    w.run("reconcile", "--unwind", "--confirm-plan-digest", d)                         # a second unwind is a no-op, never a second exposure
    _assert_only_reduces(seen, entry_links)
    for link in entry_links:
        assert w.fake.create_calls.get(link, 0) == created_before.get(link, 0) or (created_before.get(link, 0) == 0 and w.fake.create_calls.get(link, 0) == 0), link
    assert all(p["size"] == 0 for p in w.fake.positions.values()) and w.fake.wallet["BTC"] == 0, "the venue is not flat after the unwind"
    assert w.record()["flat_verified"] is True
    assert rc in (None, 0, 1)


def test_an_engaged_kill_switch_means_no_entry_is_sent_by_a_fresh_execute_and_recovery_still_exits(w):
    d = _ready(w)
    AD.engage_kill_switch(os.path.join(w.state_dir, "MS_KILL"), "owner stop")
    seen = _watch(w)
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) in (0, 1, 2)
    gid = w.plan_doc()["group"]["group_id"]
    assert not [s for s in seen if s["link"] in (gid + "-p", gid + "-s") and s["ok"]], "an ENTRY order was accepted with the kill switch engaged"
    assert all(p["size"] == 0 for p in w.fake.positions.values())


def test_recovery_beside_a_running_session_is_refused_or_handed_over_never_a_second_writer(w, tmp_path):
    d = _ready(w)
    holder = XL.ExecutorLock(XL.lock_path(AD.DEFAULT_CAPABILITY_PATH), role="autopilot", state_dir=str(tmp_path / "other"), now_ms=1)
    assert holder.try_acquire()
    seen = _watch(w)
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 2 and seen == []              # a second executor: nothing sent
    assert w.run("reconcile") in (0, 2) and seen == []                                             # a second recoverer: nothing sent (0 = no session in this dir to reconcile)


def test_the_watcher_itself_detects_a_re_created_entry_a_growing_perp_and_a_growing_spot():
    z = D(0)
    ok = {"link": "g-x1", "reduce": True, "side": "Buy", "category": "linear", "before": (D("-1"), z), "after": (D("-0.5"), z), "ok": True}
    _assert_only_reduces([ok], {"g-p", "g-s"})
    for bad in ({**ok, "link": "g-p"}, {**ok, "reduce": False}, {**ok, "after": (D("-2"), z)}, {**ok, "category": "spot", "side": "Buy", "before": (z, D("1")), "after": (z, D("2"))},
                {**ok, "category": "spot", "side": "Sell", "before": (z, D("1")), "after": (z, D("2"))}):
        with pytest.raises(AssertionError):
            _assert_only_reduces([bad], {"g-p", "g-s"})
