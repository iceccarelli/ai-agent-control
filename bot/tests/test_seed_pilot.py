"""The seed pilot's decision rule: bounded, owner-approved, fail closed, inert.

Every figure is a hand-built FIXTURE chosen for the test; none is an owner policy
value, and none is a recommendation. Files are written under pytest's tmp_path
(outside the repository), never next to the code.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import json
import math
import os
import re
import stat
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import promotion_gate as pg                                    # noqa: E402
import seed_pilot as sp                                        # noqa: E402
import seed_pilot_io as sio                                    # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400.0
WINDOW = (1_700_000_000.0, 1_750_000_000.0)      # fixture: ended before NOW


class Rig:
    """One charter, its files, and helpers to ask the question."""

    def __init__(self, tmp_path, **over):
        self.dir = tmp_path / "owner"
        self.dir.mkdir()
        os.chmod(self.dir, 0o700)
        self.approvals = str(self.dir / "approvals.json")
        self.log_path = str(self.dir / "intents.log")
        self.flag = str(self.dir / "STOP")
        self.charter_path = str(self.dir / "charter.json")
        base = dict(
            charter_id="pilot-1", owner="fixture-owner", strategy_id="btc_funding_carry",
            environment="mainnet", symbol="BTCUSDT", max_notional_usd=100.0,
            max_cycle_duration_s=7 * DAY, max_cycles=3,
            cumulative_loss_limit_usd=10.0, not_before_s=NOW - 3600.0,
            expires_at_s=NOW + 30 * DAY, max_plan_age_s=120.0,
            termination_flag_path=self.flag, intent_log_path=self.log_path,
            protective_exit_required=True)
        base.update(over)
        self.charter = sp.SeedPilotCharter(**base)
        self.log = sio.IntentLog(self.log_path, self.charter)
        self.now = NOW                      # the rig's clock; cycles advance it

    # -- files ---------------------------------------------------------------
    def write_charter(self, charter=None, *, mode=0o600):
        d = sp.charter_to_dict(charter or self.charter)
        with open(self.charter_path, "w") as h:
            json.dump(d, h)
        os.chmod(self.charter_path, mode)

    def approve(self, charter=None, *, mode=0o600):
        c = charter or self.charter
        with open(self.approvals, "w") as h:
            json.dump({"approved": {sp.approval_key(c.charter_id):
                                    sp.charter_digest(c)}}, h)
        os.chmod(self.approvals, mode)

    def init_log(self):
        self.log.initialise()

    # -- the question --------------------------------------------------------------
    def approval(self):
        return sio.load_approval(self.charter, self.approvals)

    def plan(self, **over):
        base = dict(venue_readback_verified=True, verification_ref="readback-1",
                    verified_at_s=self.now - 30.0, stop_price=50_000.0,
                    covered_notional_usd=100.0, max_loss_if_triggered_usd=2.0)
        base.update(over)
        return sp.ProtectivePlan(**base)

    def request(self, **over):
        base = dict(intent_id="c1", charter_id="pilot-1", strategy_id="btc_funding_carry",
                    environment="mainnet", symbol="BTCUSDT",
                    requested_notional_usd=100.0, planned_duration_s=3 * DAY,
                    requested_at_s=self.now, protective_plan=self.plan())
        base.update(over)
        return sp.CycleRequest(**base)

    def state(self, **over):
        events, _p = self.log.read()
        base = dict(termination_flag_present=sio.read_termination_flag(self.flag),
                    events=events, book_open_cycle_ids=(), open_cycle_loss_usd=0.0,
                    discovery_window_s=WINDOW)
        base.update(over)
        return sp.PilotState(**base)

    def decide(self, *, charter=None, approval="auto", state="auto", request=None,
               now=None):
        now = self.now if now is None else now
        return sp.decide(
            charter=self.charter if charter is None else charter,
            approval=self.approval() if approval == "auto" else approval,
            state=self.state() if state == "auto" else state,
            request=request or self.request(), now_s=now)

    def ready(self):
        """Approved, log initialised: the rig a real pilot would start from."""
        self.approve()
        self.init_log()
        return self

    def start_cycle(self, **req):
        """The two-step: ask, record the intent the decision named, ask again."""
        first = self.decide(request=self.request(**req))
        assert first.verdict == sp.REFUSE and first.intent_to_record, first.reasons
        self.log.record_intent(first)
        return self.decide(request=self.request(**req))


@pytest.fixture()
def rig(tmp_path):
    return Rig(tmp_path).ready()


def reasons(d):
    return " | ".join(d.reasons)


# -- the happy path is two calls, and the record comes first -------------------------

def test_a_new_cycle_is_refused_until_its_intent_is_durably_recorded(rig):
    first = rig.decide()
    assert first.verdict == sp.REFUSE
    assert first.reasons == (sp.INTENT_NOT_DURABLY_RECORDED,)
    assert first.intent_to_record["kind"] == "INTENT"
    assert first.intent_to_record["intent_id"] == "c1"
    assert rig.log.read()[0] == ()            # the decision wrote nothing


def test_after_the_intent_is_recorded_the_same_request_is_allowed(rig):
    d = rig.start_cycle()
    assert d.verdict == sp.ALLOW and d.allowed, d.reasons
    ev = rig.log.read()[0]
    assert [e.kind for e in ev] == ["INTENT"] and ev[0].intent_id == "c1"


def test_allow_grants_nothing_but_the_cycle(rig):
    d = rig.start_cycle()
    assert d.grants_allocation is False and d.grants_promotion is False
    assert d.grants_live is False
    out = d.as_dict()
    assert out["grants_allocation"] is out["grants_promotion"] is out["grants_live"] is False
    for flag in ("grants_allocation", "grants_promotion", "grants_live"):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(d, flag, True)
        with pytest.raises(TypeError):
            sp.PilotDecision(sp.ALLOW, (), {}, None, False, **{flag: True})


def test_the_decision_is_pure_and_repeatable(rig):
    rig.start_cycle()
    before = open(rig.log_path, "rb").read()
    a, b = rig.decide(), rig.decide()
    assert a == b and open(rig.log_path, "rb").read() == before


def test_a_decision_cannot_be_used_to_write_anything_but_its_own_intent(rig):
    allowed = rig.start_cycle()
    with pytest.raises(sio.IntentLogError):
        rig.log.record_intent(allowed)
    other = rig.decide(request=rig.request(intent_id="c2", requested_notional_usd=1e9))
    with pytest.raises(sio.IntentLogError):
        rig.log.record_intent(other)


# -- the charter ---------------------------------------------------------------------------

BAD_CHARTER = [
    ("charter_id", ""), ("charter_id", "has space"), ("charter_id", "../x"),
    ("charter_id", 7), ("owner", ""), ("owner", None), ("strategy_id", ""),
    ("environment", "testnet"), ("environment", "paper"), ("environment", "Mainnet"),
    ("environment", "demo"), ("symbol", ""), ("symbol", "btcusdt"),
    ("max_notional_usd", 0), ("max_notional_usd", -1.0), ("max_notional_usd", True),
    ("max_notional_usd", float("nan")), ("max_notional_usd", float("inf")),
    ("max_notional_usd", "100"), ("max_notional_usd", None),
    ("max_cycle_duration_s", 0), ("max_cycle_duration_s", float("nan")),
    ("max_cycle_duration_s", sp.MAX_CYCLE_DURATION_CEILING_S + 1),
    ("max_cycles", 0), ("max_cycles", -1), ("max_cycles", 1.5), ("max_cycles", True),
    ("max_cycles", sp.MAX_CYCLES_CEILING + 1),
    ("cumulative_loss_limit_usd", 0), ("cumulative_loss_limit_usd", -5.0),
    ("cumulative_loss_limit_usd", float("nan")), ("cumulative_loss_limit_usd", None),
    ("cumulative_loss_limit_usd", 100.01),             # more than one cycle's notional
    ("not_before_s", 0), ("not_before_s", float("nan")),
    ("expires_at_s", 0), ("expires_at_s", float("inf")), ("expires_at_s", None),
    ("expires_at_s", NOW - 3600.0 - 1),                # before it starts
    ("expires_at_s", NOW + 365 * DAY),                 # beyond the span ceiling
    ("max_plan_age_s", 0), ("max_plan_age_s", sp.MAX_PLAN_AGE_CEILING_S + 1),
    ("termination_flag_path", "relative/STOP"), ("termination_flag_path", ""),
    ("termination_flag_path", "/tmp/../etc/STOP"),
    ("intent_log_path", "log.txt"), ("intent_log_path", None),
    ("protective_exit_required", False), ("protective_exit_required", 1),
    ("protective_exit_required", None),
]


@pytest.mark.parametrize("field,value", BAD_CHARTER)
def test_an_unbounded_or_malformed_charter_refuses_everything(rig, field, value):
    bad = dataclasses.replace(rig.charter, **{field: value})
    assert sp.charter_problems(bad)
    d = rig.decide(charter=bad, approval=sp.ApprovalStatus(sp.charter_digest(rig.charter)))
    assert d.verdict == sp.REFUSE
    assert not d.intent_to_record


def test_the_flag_and_the_log_may_not_be_the_same_file(rig):
    bad = dataclasses.replace(rig.charter, termination_flag_path=rig.log_path)
    assert sp.charter_problems(bad)


@pytest.mark.parametrize("extra", ["allows_live", "promote", "allocation",
                                   "grants_live", "live_trading_ack", "anything"])
def test_a_charter_with_any_unknown_key_is_invalid_never_ignored(rig, extra):
    d = sp.charter_to_dict(rig.charter)
    d[extra] = True
    charter, probs = sp.charter_from_dict(d)
    assert charter is None and any(f"unknown_key={extra}" in p for p in probs)


def test_a_charter_missing_a_key_or_with_a_wrong_schema_is_invalid(rig):
    d = sp.charter_to_dict(rig.charter)
    for key in list(d):
        broken = {k: v for k, v in d.items() if k != key}
        assert sp.charter_from_dict(broken)[0] is None, key
    assert sp.charter_from_dict({**d, "schema": "seed_pilot_charter/2"})[0] is None
    for junk in (None, [], "x", 3):
        assert sp.charter_from_dict(junk)[0] is None


def test_the_charter_round_trips_and_has_no_defaults(rig):
    c, probs = sp.charter_from_dict(sp.charter_to_dict(rig.charter))
    assert probs == [] and c == rig.charter
    assert all(f.default is dataclasses.MISSING and
               f.default_factory is dataclasses.MISSING
               for f in dataclasses.fields(sp.SeedPilotCharter))


def test_the_structural_ceilings_are_finite_and_a_charter_must_expire():
    for v in (sp.MAX_CHARTER_SPAN_S, sp.MAX_CYCLE_DURATION_CEILING_S,
              sp.MAX_CYCLES_CEILING, sp.MAX_PLAN_AGE_CEILING_S, sp.MAX_REQUEST_SKEW_S):
        assert math.isfinite(v) and v > 0


# -- the owner's approval ----------------------------------------------------------------------

def test_no_approval_file_no_approval(tmp_path):
    r = Rig(tmp_path)
    r.init_log()
    assert "APPROVAL_MISSING" in reasons(r.decide())
    assert "APPROVAL_MISSING" in reasons(r.decide(approval=None))
    assert "APPROVAL_MISSING" in reasons(r.decide(approval=sp.ApprovalStatus(None)))
    assert sio.load_approval(r.charter, "") == sp.ApprovalStatus(None, ())


def test_an_approval_of_a_different_charter_id_does_not_count(rig):
    other = dataclasses.replace(rig.charter, charter_id="pilot-2")
    rig.approve(other)
    assert sio.load_approval(rig.charter, rig.approvals).approved_digest is None
    assert "APPROVAL_MISSING" in reasons(rig.decide())


@pytest.mark.parametrize("field,value", [
    ("max_notional_usd", 101.0), ("cumulative_loss_limit_usd", 10.5),
    ("max_cycles", 4), ("expires_at_s", NOW + 31 * DAY), ("symbol", "ETHUSDT"),
    ("max_cycle_duration_s", 8 * DAY), ("owner", "someone-else"),
    ("termination_flag_path", "/tmp/elsewhere/STOP"), ("max_plan_age_s", 121.0),
])
def test_a_charter_edited_after_approval_is_refused_as_tampered(rig, field, value):
    """The owner approved one charter; someone loosens a bound afterwards. The
    approval no longer matches what is being run."""
    tampered = dataclasses.replace(rig.charter, **{field: value})
    assert not sp.charter_problems(tampered)
    d = rig.decide(charter=tampered)          # approval read for the ORIGINAL
    assert d.verdict == sp.REFUSE and sp.CHARTER_NOT_APPROVED_AS_WRITTEN in d.reasons


def test_a_charter_file_edited_on_disk_after_approval_is_refused(tmp_path):
    r = Rig(tmp_path)
    r.write_charter()
    r.approve()
    r.init_log()
    loaded, probs = sio.load_charter(r.charter_path)
    assert loaded == r.charter and not probs
    d = json.load(open(r.charter_path))
    d["max_notional_usd"] = 1_000_000.0
    d["cumulative_loss_limit_usd"] = 1_000_000.0
    json.dump(d, open(r.charter_path, "w"))
    edited, probs = sio.load_charter(r.charter_path)
    assert edited is not None and not probs
    dec = sp.decide(charter=edited, approval=sio.load_approval(edited, r.approvals),
                    state=r.state(), request=r.request(), now_s=NOW)
    assert dec.verdict == sp.REFUSE and sp.CHARTER_NOT_APPROVED_AS_WRITTEN in dec.reasons


@pytest.mark.parametrize("mode", [0o666, 0o662, 0o646, 0o664, 0o620])
def test_a_world_or_group_writable_approvals_file_approves_nothing(rig, mode):
    rig.approve(mode=mode)
    st = rig.approval()
    assert st.approved_digest is None and st.problems
    d = rig.decide()
    assert d.verdict == sp.REFUSE and sp.APPROVAL_UNSAFE in reasons(d)


def test_an_approvals_file_in_a_directory_others_can_write_approves_nothing(rig):
    os.chmod(rig.dir, 0o777)
    d = rig.decide()
    assert d.verdict == sp.REFUSE and sp.APPROVAL_UNSAFE in reasons(d)


def test_a_symlinked_approvals_file_approves_nothing(rig, tmp_path):
    real = tmp_path / "real-approvals.json"
    real.write_text(open(rig.approvals).read())
    os.chmod(real, 0o600)
    os.unlink(rig.approvals)
    os.symlink(real, rig.approvals)
    d = rig.decide()
    assert d.verdict == sp.REFUSE and sp.APPROVAL_UNSAFE in reasons(d)


def test_an_approvals_file_owned_by_someone_else_approves_nothing(rig, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    d = rig.decide()
    assert d.verdict == sp.REFUSE and sp.APPROVAL_UNSAFE in reasons(d)


def test_an_approvals_file_inside_the_repository_approves_nothing(rig, tmp_path,
                                                                monkeypatch):
    fake_root = tmp_path / "repo"
    (fake_root / "bot").mkdir(parents=True)
    os.chmod(fake_root / "bot", 0o700)
    monkeypatch.setattr(pg, "REPO_ROOT", str(fake_root))
    inside = str(fake_root / "bot" / "approvals.json")
    open(inside, "w").write(open(rig.approvals).read())
    os.chmod(inside, 0o600)
    st = sio.load_approval(rig.charter, inside)
    assert st.approved_digest is None and "inside the repository" in st.problems[0]
    assert "APPROVAL_UNSAFE" in reasons(rig.decide(approval=st))


@pytest.mark.parametrize("content", ["", "not json", "[]", "null", '{"approved": 3}',
                                     '{"approved": {"seed_pilot_charter:pilot-1": 7}}',
                                     '{"approved": {"seed_pilot_charter:pilot-1": ""}}'])
def test_a_malformed_approvals_file_approves_nothing(rig, content):
    open(rig.approvals, "w").write(content)
    os.chmod(rig.approvals, 0o600)
    d = rig.decide()
    assert d.verdict == sp.REFUSE and sp.APPROVAL_MISSING in reasons(d)


def test_a_wrong_digest_in_the_approvals_file_is_refused(rig):
    json.dump({"approved": {sp.approval_key("pilot-1"): "0" * 64}},
              open(rig.approvals, "w"))
    os.chmod(rig.approvals, 0o600)
    assert sp.CHARTER_NOT_APPROVED_AS_WRITTEN in rig.decide().reasons


def test_the_digest_is_the_promotion_gates_own_convention(rig):
    assert sp.charter_digest(rig.charter) == pg.approval_digest(
        sp.approval_key("pilot-1"), sp.charter_to_dict(rig.charter))


def test_a_world_writable_charter_file_is_not_loaded(tmp_path):
    r = Rig(tmp_path)
    r.write_charter(mode=0o666)
    c, probs = sio.load_charter(r.charter_path)
    assert c is None and probs
    assert sio.load_charter(str(tmp_path / "nope.json"))[0] is None
    open(r.charter_path, "w").write("{")
    os.chmod(r.charter_path, 0o600)
    assert sio.load_charter(r.charter_path)[0] is None


# -- time -------------------------------------------------------------------------------------------

def test_an_expired_or_not_yet_valid_charter_refuses(rig):
    assert sp.CHARTER_EXPIRED in reasons(rig.decide(
        now=rig.charter.expires_at_s, request=rig.request(
            requested_at_s=rig.charter.expires_at_s)))
    assert sp.CHARTER_EXPIRED in reasons(rig.decide(
        now=NOW + 400 * DAY, request=rig.request(requested_at_s=NOW + 400 * DAY)))
    early = rig.charter.not_before_s - 1
    assert sp.CHARTER_NOT_YET_VALID in reasons(rig.decide(
        now=early, request=rig.request(requested_at_s=early)))


@pytest.mark.parametrize("now", [None, 0, -1, float("nan"), float("inf"), "now", True])
def test_an_unusable_clock_refuses(rig, now):
    d = sp.decide(charter=rig.charter, approval=rig.approval(), state=rig.state(),
                  request=rig.request(), now_s=now)
    assert d.verdict == sp.REFUSE and sp.CLOCK_INVALID in reasons(d)


@pytest.mark.parametrize("delta", [-1000.0, 1000.0])
def test_a_request_stamped_away_from_the_clock_refuses(rig, delta):
    d = rig.decide(request=rig.request(requested_at_s=NOW + delta))
    assert sp.REQUEST_TIME_INVALID in reasons(d)


# -- the request ------------------------------------------------------------------------------------

@pytest.mark.parametrize("notional", [100.01, 1e9, math.inf])
def test_a_notional_above_the_charters_bound_refuses(rig, notional):
    d = rig.decide(request=rig.request(requested_notional_usd=notional,
                                       protective_plan=rig.plan(covered_notional_usd=1e12)))
    assert d.verdict == sp.REFUSE
    assert sp.NOTIONAL_ABOVE_BOUND in reasons(d) or "REQUEST_INVALID" in reasons(d)


@pytest.mark.parametrize("notional", [0, -5.0, float("nan"), None, "100", True])
def test_a_garbage_notional_refuses(rig, notional):
    d = rig.decide(request=rig.request(requested_notional_usd=notional))
    assert d.verdict == sp.REFUSE and "REQUEST_INVALID" in reasons(d)
    assert d.intent_to_record is None


def test_the_notional_bound_is_inclusive(rig):
    assert rig.decide(request=rig.request(requested_notional_usd=100.0)).reasons == \
        (sp.INTENT_NOT_DURABLY_RECORDED,)


def test_a_planned_duration_above_the_bound_refuses(rig):
    d = rig.decide(request=rig.request(planned_duration_s=7 * DAY + 1))
    assert sp.DURATION_ABOVE_BOUND in reasons(d)
    for bad in (0, -1, float("nan"), None):
        assert "REQUEST_INVALID" in reasons(rig.decide(
            request=rig.request(planned_duration_s=bad)))


@pytest.mark.parametrize("over", [
    {"environment": "testnet"}, {"environment": "paper"}, {"environment": "demo"},
    {"environment": "MAINNET"}, {"environment": ""}, {"environment": None}])
def test_only_mainnet_cycles_can_be_pilot_cycles(rig, over):
    d = rig.decide(request=rig.request(**over))
    assert d.verdict == sp.REFUSE and sp.NOT_MAINNET in reasons(d)


@pytest.mark.parametrize("over", [{"charter_id": "pilot-2"}, {"strategy_id": "other"},
                                  {"symbol": "ETHUSDT"}])
def test_a_request_for_another_charter_strategy_or_symbol_refuses(rig, over):
    assert sp.REQUEST_NOT_FOR_THIS_CHARTER in reasons(rig.decide(
        request=rig.request(**over)))


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "../etc", None, 5])
def test_a_bad_intent_id_refuses(rig, bad):
    d = rig.decide(request=rig.request(intent_id=bad))
    assert d.verdict == sp.REFUSE and d.intent_to_record is None


# -- manual termination ------------------------------------------------------------------------------------

def test_the_manual_termination_flag_stops_new_cycles(rig):
    open(rig.flag, "w").close()
    d = rig.decide()
    assert d.verdict == sp.REFUSE and sp.TERMINATION_FLAG_PRESENT in reasons(d)
    assert d.intent_to_record is None


def test_a_flag_of_any_kind_counts(rig):
    os.mkdir(rig.flag)
    assert sio.read_termination_flag(rig.flag) is True
    os.rmdir(rig.flag)
    os.symlink("/nonexistent-target", rig.flag)        # a dangling link is still there
    assert sio.read_termination_flag(rig.flag) is True


def test_an_unknowable_flag_refuses(rig, tmp_path):
    for st in (rig.state(termination_flag_present=None),
               rig.state(termination_flag_present="no"),
               rig.state(termination_flag_present=0)):
        assert sp.TERMINATION_FLAG_UNKNOWN in reasons(rig.decide(state=st))
    assert sio.read_termination_flag(str(tmp_path / "no-such-dir" / "STOP")) is None
    assert sio.read_termination_flag("relative") is None


def test_a_recorded_termination_is_permanent_even_after_the_flag_is_removed(rig):
    rig.log.record_termination(reason="owner stopped it", at_s=NOW)
    assert sio.read_termination_flag(rig.flag) is False
    d = rig.decide()
    assert d.verdict == sp.REFUSE and sp.PILOT_TERMINATED in reasons(d)
    assert d.intent_to_record is None


# -- the cumulative loss limit ------------------------------------------------------------------------------------

def run_cycle(rig, intent_id, net, outcome="CLOSED", **req):
    d = rig.start_cycle(intent_id=intent_id, **req)
    assert d.verdict == sp.ALLOW, d.reasons
    rig.now += 100.0
    rig.log.record_result(intent_id=intent_id, outcome=outcome, net_usd=net,
                          at_s=rig.now)
    rig.now += 100.0


def test_cumulative_losses_at_the_limit_terminate_the_pilot(rig):
    run_cycle(rig, "c1", -4.0)
    run_cycle(rig, "c2", -6.0)               # exactly the limit (fixture 10.0)
    d = rig.decide(request=rig.request(intent_id="c3"))
    assert d.verdict == sp.REFUSE and sp.CUMULATIVE_LOSS_LIMIT_REACHED in reasons(d)
    assert d.must_record_termination is True and d.intent_to_record is None
    assert d.details["cumulative_loss_usd"] == pytest.approx(10.0)


def test_a_win_never_extends_the_loss_budget(rig):
    run_cycle(rig, "c1", -9.0)
    run_cycle(rig, "c2", +500.0,             # a huge win does not offset the loss
              protective_plan=rig.plan(max_loss_if_triggered_usd=0.5))
    d = rig.decide(request=rig.request(intent_id="c3", protective_plan=rig.plan(
        max_loss_if_triggered_usd=1.5)))
    assert sp.PROTECTIVE_PLAN_LOSS_EXCEEDS_BUDGET in reasons(d)     # 1.5 > 1.0 left
    assert d.details["cumulative_loss_usd"] == pytest.approx(9.0)


def test_a_loss_past_the_limit_is_still_terminal(rig):
    run_cycle(rig, "c1", -250.0)
    d = rig.decide(request=rig.request(intent_id="c2"))
    assert sp.CUMULATIVE_LOSS_LIMIT_REACHED in reasons(d) and d.must_record_termination


def test_the_open_cycles_booked_loss_counts_against_the_limit(rig):
    assert rig.start_cycle().verdict == sp.ALLOW
    ok = rig.decide(state=rig.state(open_cycle_loss_usd=3.0,
                                    book_open_cycle_ids=("c1",)))
    assert ok.verdict == sp.ALLOW
    over = rig.decide(state=rig.state(open_cycle_loss_usd=10.0,
                                      book_open_cycle_ids=("c1",)))
    assert sp.CUMULATIVE_LOSS_LIMIT_REACHED in reasons(over) and over.must_record_termination


@pytest.mark.parametrize("loss", [None, -1.0, float("nan"), "3", True])
def test_an_unknown_open_loss_refuses(rig, loss):
    rig.start_cycle()
    d = rig.decide(state=rig.state(open_cycle_loss_usd=loss, book_open_cycle_ids=("c1",)))
    assert sp.OPEN_CYCLE_LOSS_UNKNOWN in reasons(d)


def test_an_aborted_attempts_loss_counts_too(rig):
    run_cycle(rig, "c1", -10.0, outcome="ABORTED")
    assert sp.CUMULATIVE_LOSS_LIMIT_REACHED in reasons(
        rig.decide(request=rig.request(intent_id="c2")))


# -- one open cycle at a time ----------------------------------------------------------------------------------------------

def test_a_second_concurrent_cycle_is_refused(rig):
    assert rig.start_cycle(intent_id="c1").verdict == sp.ALLOW
    d = rig.decide(request=rig.request(intent_id="c2"),
                   state=rig.state(book_open_cycle_ids=("c1",)))
    assert d.verdict == sp.REFUSE and sp.SECOND_CONCURRENT_CYCLE in reasons(d)
    assert d.intent_to_record is None
    # the cycle in flight is still allowed to continue
    assert rig.decide(request=rig.request(intent_id="c1"),
                      state=rig.state(book_open_cycle_ids=("c1",))).verdict == sp.ALLOW


def test_the_log_alone_proves_a_cycle_is_open_even_if_the_book_looks_flat(rig):
    """The book read says nothing is open (a stale or lying read); the durable
    record still has an intent with no result, so a second cycle is refused."""
    assert rig.start_cycle(intent_id="c1").verdict == sp.ALLOW
    d = rig.decide(request=rig.request(intent_id="c2"),
                   state=rig.state(book_open_cycle_ids=()))
    assert d.verdict == sp.REFUSE and sp.SECOND_CONCURRENT_CYCLE in reasons(d)
    assert d.intent_to_record is None


def test_a_second_cycle_is_allowed_once_the_first_has_a_result(rig):
    run_cycle(rig, "c1", +1.0)
    assert rig.start_cycle(intent_id="c2").verdict == sp.ALLOW


def test_a_cycle_open_in_the_book_that_the_log_does_not_know_refuses(rig):
    d = rig.decide(state=rig.state(book_open_cycle_ids=("stranger",)))
    assert sp.UNRECORDED_OPEN_CYCLE in reasons(d) and sp.SECOND_CONCURRENT_CYCLE in reasons(d)


@pytest.mark.parametrize("ids", [None, "c1", 5])
def test_an_unreadable_book_refuses(rig, ids):
    d = rig.decide(state=rig.state(book_open_cycle_ids=ids))
    assert sp.BOOK_STATE_UNKNOWN in reasons(d)


def test_the_cycle_count_is_bounded_and_aborted_attempts_count(rig):
    run_cycle(rig, "c1", +1.0)
    run_cycle(rig, "c2", +1.0)
    run_cycle(rig, "c3", -0.5, outcome="ABORTED")
    d = rig.decide(request=rig.request(intent_id="c4"))
    assert d.verdict == sp.REFUSE and sp.MAX_CYCLES_REACHED in reasons(d)


def test_a_consumed_intent_cannot_be_replayed(rig):
    run_cycle(rig, "c1", +1.0)
    d = rig.decide(request=rig.request(intent_id="c1"))
    assert d.verdict == sp.REFUSE and sp.INTENT_ALREADY_CONSUMED in reasons(d)


def test_a_recorded_intent_cannot_be_used_for_a_different_request(rig):
    rig.start_cycle(intent_id="c1")
    for over in ({"requested_notional_usd": 50.0}, {"planned_duration_s": DAY},
                 {"protective_plan": rig.plan(stop_price=1.0)},
                 {"protective_plan": rig.plan(verification_ref="other")}):
        d = rig.decide(request=rig.request(intent_id="c1", **over))
        assert sp.INTENT_MISMATCH in reasons(d), over


def test_an_open_cycle_older_than_the_charters_duration_gets_no_more_orders(rig):
    rig.start_cycle(intent_id="c1")
    later = NOW + 7 * DAY + 10
    d = rig.decide(now=later, request=rig.request(
        intent_id="c1", requested_at_s=later, protective_plan=rig.plan(verified_at_s=later - 5)))
    assert sp.CYCLE_DURATION_EXCEEDED in reasons(d)


# -- the protective exit plan ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("plan,needle", [
    (None, sp.PROTECTIVE_PLAN_MISSING),
    ("a stop", sp.PROTECTIVE_PLAN_MISSING),
    (dict(venue_readback_verified=False), "venue_readback_verified"),
    (dict(venue_readback_verified=1), "venue_readback_verified"),
    (dict(venue_readback_verified="yes"), "venue_readback_verified"),
    (dict(venue_readback_verified=None), "venue_readback_verified"),
    (dict(verification_ref=""), "verification_ref"),
    (dict(verification_ref="   "), "verification_ref"),
    (dict(verified_at_s=NOW - 121.0), "verification_stale"),
    (dict(verified_at_s=NOW + 1000.0), "verified_in_the_future"),
    (dict(verified_at_s=None), "verified_at_s"),
    (dict(verified_at_s=float("nan")), "verified_at_s"),
    (dict(stop_price=0), "stop_price"),
    (dict(stop_price=float("nan")), "stop_price"),
    (dict(stop_price=None), "stop_price"),
    (dict(covered_notional_usd=99.99), "covered_notional_usd"),
    (dict(covered_notional_usd=0), "covered_notional_usd"),
    (dict(covered_notional_usd=None), "covered_notional_usd"),
    (dict(max_loss_if_triggered_usd=None), "max_loss_if_triggered_usd"),
    (dict(max_loss_if_triggered_usd=-1.0), "max_loss_if_triggered_usd"),
    (dict(max_loss_if_triggered_usd=float("nan")), "max_loss_if_triggered_usd"),
    (dict(max_loss_if_triggered_usd=10.01), sp.PROTECTIVE_PLAN_LOSS_EXCEEDS_BUDGET),
])
def test_a_cycle_without_a_verified_protective_exit_is_refused(rig, plan, needle):
    if isinstance(plan, dict):
        plan = rig.plan(**plan)
    d = rig.decide(request=rig.request(protective_plan=plan))
    assert d.verdict == sp.REFUSE and needle in reasons(d), d.reasons
    assert d.intent_to_record is None        # no intent is written for a cycle with no exit


def test_a_plans_worst_case_must_fit_the_remaining_budget_exactly(rig):
    run_cycle(rig, "c1", -4.0)
    assert sp.PROTECTIVE_PLAN_LOSS_EXCEEDS_BUDGET not in reasons(rig.decide(
        request=rig.request(intent_id="c2", protective_plan=rig.plan(
            max_loss_if_triggered_usd=6.0))))
    assert sp.PROTECTIVE_PLAN_LOSS_EXCEEDS_BUDGET in reasons(rig.decide(
        request=rig.request(intent_id="c2", protective_plan=rig.plan(
            max_loss_if_triggered_usd=6.01))))


# -- the owner's discovery window (state, never defined here) ----------------------------------------------------------------------------

@pytest.mark.parametrize("w", [None, (), (1.0,), (5.0, 1.0), ("a", "b"), (float("nan"), 2.0),
                               "window", (1.0, 2.0, 3.0)])
def test_an_unknown_discovery_window_refuses(rig, w):
    d = rig.decide(state=rig.state(discovery_window_s=w))
    assert sp.DISCOVERY_WINDOW_UNKNOWN in reasons(d) and d.intent_to_record is None


def test_a_cycle_inside_or_at_the_end_of_the_window_refuses(rig):
    inside = rig.state(discovery_window_s=(NOW - 100.0, NOW + 100.0))
    assert sp.CYCLE_WITHIN_DISCOVERY_WINDOW in reasons(rig.decide(state=inside))
    edge = rig.state(discovery_window_s=(NOW - 100.0, NOW))
    assert sp.CYCLE_WITHIN_DISCOVERY_WINDOW in reasons(rig.decide(state=edge))
    after = rig.state(discovery_window_s=(NOW - 100.0, NOW - 1.0))
    assert sp.CYCLE_WITHIN_DISCOVERY_WINDOW not in reasons(rig.decide(state=after))


def test_the_loss_limit_above_the_evidence_drawdown_limit_warns_but_does_not_refuse(rig):
    d = rig.decide(state=rig.state(evidence_drawdown_limit_usd=5.0))
    assert d.reasons == (sp.INTENT_NOT_DURABLY_RECORDED,)
    assert d.details["warnings"]
    assert not rig.decide().details["warnings"]


# -- the log itself --------------------------------------------------------------------------------------------------------------------

def test_a_missing_log_is_unavailable_not_empty(tmp_path):
    r = Rig(tmp_path)
    r.approve()
    events, problem = r.log.read()
    assert events is None and "does not exist" in problem
    d = r.decide()
    assert d.verdict == sp.REFUSE and sp.INTENT_LOG_UNAVAILABLE in reasons(d)
    with pytest.raises(sio.IntentLogError):
        r.log.record_termination(reason="x", at_s=NOW)


def test_the_log_is_created_once_private_and_empty(tmp_path):
    r = Rig(tmp_path)
    r.init_log()
    assert stat.S_IMODE(os.stat(r.log_path).st_mode) == 0o600
    assert r.log.read() == ((), None)
    with pytest.raises(FileExistsError):
        r.log.initialise()


def test_the_log_refuses_to_live_in_the_repository(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    monkeypatch.setattr(pg, "REPO_ROOT", str(tmp_path))
    with pytest.raises(sio.IntentLogError):
        r.log.initialise()


def test_a_tampered_log_is_unavailable(rig):
    rig.start_cycle(intent_id="c1")
    rig.log.record_result(intent_id="c1", outcome="CLOSED", net_usd=-5.0, at_s=NOW + 10)
    good = open(rig.log_path).read()
    lines = good.splitlines()
    assert len(lines) == 2

    def check(text):
        open(rig.log_path, "w").write(text)
        os.chmod(rig.log_path, 0o600)
        events, problem = rig.log.read()
        assert events is None and problem
        d = rig.decide()
        assert d.verdict == sp.REFUSE and sp.INTENT_LOG_UNAVAILABLE in reasons(d)

    check(good.replace("-5.0", "5.0"))                     # a loss rewritten as a win
    check(lines[1] + "\n")                                  # the intent line removed
    check(lines[1] + "\n" + lines[0] + "\n")                # reordered
    check(good[:-1])                                        # truncated mid-record
    check(good + "garbage\n")
    check(good + good)                                      # replayed
    check(good.replace('"c1"', '"c9"', 1))
    open(rig.log_path, "w").write(good)
    os.chmod(rig.log_path, 0o600)
    assert rig.log.read()[1] is None                        # the original is fine


def test_a_forged_but_correctly_chained_event_under_another_charter_is_refused(rig):
    other = dataclasses.replace(rig.charter, cumulative_loss_limit_usd=99.0)
    forged = sio.IntentLog(rig.log_path, other)
    forged.record_termination(reason="forged", at_s=NOW + 5)
    events, problem = rig.log.read()
    assert events is None and sp.LOG_CHARTER_DIGEST_MISMATCH in problem


@pytest.mark.parametrize("mode", [0o666, 0o664, 0o620])
def test_a_writable_log_is_not_trusted(rig, mode):
    os.chmod(rig.log_path, mode)
    assert rig.log.read()[0] is None
    with pytest.raises(sio.IntentLogError):
        rig.log.record_termination(reason="x", at_s=NOW)


def test_a_symlinked_log_is_not_trusted(rig, tmp_path):
    real = tmp_path / "elsewhere.log"
    real.write_text("")
    os.chmod(real, 0o600)
    os.unlink(rig.log_path)
    os.symlink(real, rig.log_path)
    assert rig.log.read()[0] is None
    with pytest.raises(sio.IntentLogError):
        rig.log.record_termination(reason="x", at_s=NOW)


def test_events_that_make_no_sense_are_refused_at_append(rig):
    with pytest.raises(sio.IntentLogError):
        rig.log.record_result(intent_id="never-recorded", outcome="CLOSED",
                              net_usd=1.0, at_s=NOW + 5)
    rig.start_cycle()
    rig.log.record_result(intent_id="c1", outcome="CLOSED", net_usd=1.0, at_s=NOW + 5)
    for kw in (dict(intent_id="c1", outcome="CLOSED", net_usd=1.0, at_s=NOW + 6),   # twice
               dict(intent_id="c1", outcome="WON", net_usd=1.0, at_s=NOW + 7),
               dict(intent_id="c1", outcome="CLOSED", net_usd=float("nan"), at_s=NOW + 7),
               dict(intent_id="c1", outcome="CLOSED", net_usd=None, at_s=NOW + 7)):
        with pytest.raises((sio.IntentLogError, ValueError)):
            rig.log.record_result(**kw)
    with pytest.raises(sio.IntentLogError):                  # time going backwards
        rig.log.record_termination(reason="late", at_s=NOW - 1.0)
    with pytest.raises(sio.IntentLogError):
        rig.log.append({"kind": "INTENT"})
    assert len(open(rig.log_path).read().splitlines()) == 2


def test_each_append_is_flushed_before_it_returns(rig, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    first = rig.decide()
    assert synced == []                      # deciding writes nothing
    rig.log.record_intent(first)
    assert synced, "the intent was not fsynced before record_intent returned"
    assert open(rig.log_path).read().count("\n") == 1


def test_an_intent_is_on_disk_before_the_allow(rig):
    """The only way to an ALLOW is through a record that already exists."""
    d1 = rig.decide()
    assert d1.verdict == sp.REFUSE
    assert open(rig.log_path).read() == ""
    rig.log.record_intent(d1)
    on_disk = open(rig.log_path).read()
    assert '"intent_id":"c1"' in on_disk
    assert rig.decide().verdict == sp.ALLOW


# -- fail closed on junk ----------------------------------------------------------------------------------------------------------------

JUNK = [None, 0, 1, "", "x", [], {}, (), object(), float("nan"), True]


@pytest.mark.parametrize("junk", JUNK)
@pytest.mark.parametrize("slot", ["charter", "approval", "state", "request", "now_s"])
def test_garbage_in_any_slot_is_a_refusal_never_a_raise_or_an_allow(rig, junk, slot):
    kw = dict(charter=rig.charter, approval=rig.approval(), state=rig.state(),
              request=rig.request(), now_s=NOW)
    kw[slot] = junk
    d = sp.decide(**kw)
    assert d.verdict == sp.REFUSE and d.reasons
    assert d.intent_to_record is None


def test_state_fields_of_the_wrong_type_refuse(rig):
    for over in ({"events": "log"}, {"events": 5}, {"events": None},
                 {"events": ("not an event",)}, {"events": (None,)},
                 {"termination_flag_present": None}):
        d = rig.decide(state=rig.state(**over))
        assert d.verdict == sp.REFUSE, over


def test_a_request_with_unhashable_fields_refuses_with_its_reasons(rig):
    d = rig.decide(request=rig.request(requested_notional_usd=float("nan")))
    assert d.verdict == sp.REFUSE and "INTERNAL_ERROR" not in reasons(d)


def test_every_failure_is_reported_not_just_the_first(rig):
    open(rig.flag, "w").close()
    d = rig.decide(request=rig.request(environment="testnet", requested_notional_usd=1e6),
                   state=rig.state(termination_flag_present=True,
                                   discovery_window_s=None, book_open_cycle_ids=("x",)))
    for needle in (sp.TERMINATION_FLAG_PRESENT, sp.NOT_MAINNET, sp.NOTIONAL_ABOVE_BOUND,
                   sp.DISCOVERY_WINDOW_UNKNOWN, sp.SECOND_CONCURRENT_CYCLE):
        assert needle in reasons(d), needle


# -- monitoring and exits ------------------------------------------------------------------------------------------------------------------

def test_monitor_says_continue_inside_the_charter_and_exit_otherwise(rig):
    rig.start_cycle()
    ok = sp.monitor(charter=rig.charter, state=rig.state(book_open_cycle_ids=("c1",),
                                                          open_cycle_loss_usd=1.0),
                    now_s=NOW, intent_id="c1")
    assert ok.action == sp.CONTINUE
    def exits(**kw):
        st = kw.pop("state", rig.state(book_open_cycle_ids=("c1",), open_cycle_loss_usd=1.0))
        return sp.monitor(charter=rig.charter, state=st, intent_id="c1",
                          now_s=kw.pop("now", NOW))
    assert exits(state=rig.state(open_cycle_loss_usd=10.0)).must_record_termination
    assert exits(state=rig.state(open_cycle_loss_usd=10.0)).action == sp.EXIT_AND_TERMINATE
    assert exits(state=rig.state(termination_flag_present=True,
                                 open_cycle_loss_usd=0.0)).action == sp.EXIT_AND_TERMINATE
    assert exits(state=rig.state(termination_flag_present=None,
                                 open_cycle_loss_usd=0.0)).action == sp.EXIT_AND_TERMINATE
    assert exits(now=rig.charter.expires_at_s).action == sp.EXIT_AND_TERMINATE
    assert exits(now=NOW + 8 * DAY).action == sp.EXIT_AND_TERMINATE
    assert exits(state=rig.state(open_cycle_loss_usd=None)).action == sp.EXIT_AND_TERMINATE
    assert exits(state=sp.PilotState()).action == sp.EXIT_AND_TERMINATE
    for junk in JUNK:
        assert sp.monitor(charter=junk, state=junk, now_s=junk,
                          intent_id="c1").action == sp.EXIT_AND_TERMINATE


# -- inert: wired to nothing, can reach nothing -------------------------------------------------------------------------------------------------

PILOT_FILES = ("seed_pilot.py", "seed_pilot_io.py", os.path.join("tools", "seed_pilot_approve.py"))
BANNED = {"bybit_connection", "carry_broker", "trading_engine", "carry_engine", "main",
          "persistence", "config", "risk_management", "order_lifecycle", "order_recovery",
          "private_ws_consumer", "ws_transport", "capital_allocator", "carry_capital",
          "capital_reservations", "strategy_evidence", "carry_evidence", "entry_benefit_gate"}


def _tree(rel):
    return ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())


def _imports(tree, *, lazy=True):
    names = set()

    def walk(node, in_func):
        for ch in ast.iter_child_nodes(node):
            fn = in_func or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(ch, ast.Import) and (lazy or not in_func):
                names.update(a.name.split(".")[0] for a in ch.names)
            elif isinstance(ch, ast.ImportFrom) and ch.module and (lazy or not in_func):
                names.add(ch.module.split(".")[0])
            walk(ch, fn)
    walk(tree, False)
    return names


def test_the_pilot_imports_no_broker_engine_store_config_or_allocation_module():
    for rel in PILOT_FILES:
        assert _imports(_tree(rel)) & BANNED == set(), rel


def test_the_pure_module_imports_only_the_standard_library():
    assert _imports(_tree("seed_pilot.py")) <= {
        "__future__", "hashlib", "json", "math", "re", "dataclasses", "typing"}


def test_no_module_reachable_at_import_time_from_the_pilot_touches_a_broker():
    local = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")} | {"signals"}
    seen, stack = set(), ["seed_pilot_io", "seed_pilot"]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = (os.path.join(ROOT, "signals", "__init__.py") if mod == "signals"
                else os.path.join(ROOT, mod + ".py"))
        if os.path.isfile(path):
            stack += [m for m in _imports(ast.parse(open(path).read()), lazy=False)
                      if m in local]
    assert seen & {"bybit_connection", "carry_broker", "trading_engine", "carry_engine",
                   "main", "persistence", "config", "private_ws_consumer"} == set(), seen


def test_nothing_in_the_repository_imports_the_pilot_but_its_own_tool_and_tests():
    offenders = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", ".git", "venv")
                   and not d.startswith(".")]
        for f in files:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(base, f), ROOT)
            if rel in PILOT_FILES:
                continue
            tree = ast.parse(open(os.path.join(base, f), encoding="utf-8",
                                  errors="ignore").read())
            if _imports(tree) & {"seed_pilot", "seed_pilot_io", "seed_pilot_approve"}:
                offenders.append(rel)
    assert offenders == []


def test_the_pilot_is_not_in_any_container_image():
    for name in ("Dockerfile", "Dockerfile.drill"):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        assert "seed_pilot" not in text, name


def test_the_pilot_reads_no_environment_variable():
    for rel in PILOT_FILES:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                pytest.fail(f"{rel} reads the environment")
            if isinstance(node, ast.ImportFrom) and node.module == "os" and any(
                    a.name in ("environ", "getenv") for a in node.names):
                pytest.fail(f"{rel} reads the environment")


def test_the_pilot_never_assigns_or_names_a_live_arming_flag():
    for rel in PILOT_FILES:
        for node in ast.walk(_tree(rel)):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                for n in ast.walk(t):
                    name = getattr(n, "id", None) or getattr(n, "attr", None) or ""
                    assert "allows_live" not in name.lower(), rel
                    assert "live_authorized" not in name.lower(), rel
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "LIVE_TRADING_ACK" not in src.replace("`LIVE_TRADING_ACK`", "") \
            or rel == "seed_pilot.py"             # named in the docstring only, never read


def test_live_arming_is_untouched_by_the_whole_flow(rig):
    import config as _config
    cfg = _config.load({})
    before = _config.is_live_authorized(cfg)
    rig.start_cycle()
    rig.decide()
    assert _config.is_live_authorized(cfg) == before == (False, before[1])
    assert before[0] is False


def test_no_owner_policy_value_is_defined_in_the_pilot():
    for rel in PILOT_FILES:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert not re.search(r"\b1788739200\b|\b1793491199\b", src), rel


def test_a_successful_pilot_changes_no_evidence_or_allocation_state(rig):
    """Running a whole winning pilot leaves the evidence policy and verdict
    machinery exactly as it was: it produces cycles for the ordinary reports to
    judge, and nothing else."""
    import strategy_evidence as se
    before = (se.DEFAULT_POLICY, se.CASH_EVIDENCE_CLASS)
    run_cycle(rig, "c1", +5.0)
    run_cycle(rig, "c2", +5.0)
    assert (se.DEFAULT_POLICY, se.CASH_EVIDENCE_CLASS) == before
    v = se.evaluate_strategy_evidence(None, now_s=NOW)
    assert v.verdict == se.INSUFFICIENT and v.grants_owner_approval is False


# -- the approve tool -------------------------------------------------------------------------------------------------------------------------------

def _tool():
    import seed_pilot_approve as tool
    return tool


def test_the_approve_tool_shows_then_records_only_on_the_matching_digest(tmp_path, capsys,
                                                                       monkeypatch):
    r = Rig(tmp_path)
    r.write_charter()
    tool = _tool()
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: NOW)
    assert tool.main(["--charter", r.charter_path, "--out", r.approvals]) == 0
    out = capsys.readouterr().out
    digest = sp.charter_digest(r.charter)
    assert digest in out and "nothing written" in out and not os.path.exists(r.approvals)
    assert tool.main(["--charter", r.charter_path, "--out", r.approvals,
                      "--confirm", "0" * 64]) == 2
    assert not os.path.exists(r.approvals)
    assert tool.main(["--charter", r.charter_path, "--out", r.approvals,
                      "--confirm", digest]) == 0
    assert stat.S_IMODE(os.stat(r.approvals).st_mode) == 0o600
    assert sio.load_approval(r.charter, r.approvals).approved_digest == digest
    # extending keeps what is there
    other = dataclasses.replace(r.charter, charter_id="pilot-2")
    r.write_charter(other)
    assert tool.main(["--charter", r.charter_path, "--out", r.approvals,
                      "--confirm", sp.charter_digest(other)]) == 0
    assert sio.load_approval(r.charter, r.approvals).approved_digest == digest


def test_the_approve_tool_refuses_a_bad_expired_or_unsafe_request(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    tool = _tool()
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: NOW)
    assert tool.main(["--charter", str(tmp_path / "none.json"), "--out", r.approvals]) == 2
    r.write_charter(dataclasses.replace(r.charter, max_notional_usd=-1.0))
    assert tool.main(["--charter", r.charter_path, "--out", r.approvals]) == 2
    r.write_charter()
    monkeypatch.setattr(_time, "time", lambda: NOW + 365 * DAY)           # expired
    assert tool.main(["--charter", r.charter_path, "--out", r.approvals,
                      "--confirm", sp.charter_digest(r.charter)]) == 2
    monkeypatch.setattr(_time, "time", lambda: NOW)
    inside = str(tmp_path / "repo" / "a.json")
    os.makedirs(os.path.dirname(inside))
    monkeypatch.setattr(pg, "REPO_ROOT", str(tmp_path / "repo"))
    assert tool.main(["--charter", r.charter_path, "--out", inside,
                      "--confirm", sp.charter_digest(r.charter)]) == 2
    assert not os.path.exists(inside)
    monkeypatch.setattr(pg, "REPO_ROOT", "/nonexistent-root")
    os.chmod(r.dir, 0o777)             # charter and approvals now sit in an unsafe directory
    assert tool.main(["--charter", r.charter_path, "--out", r.approvals,
                      "--confirm", sp.charter_digest(r.charter)]) == 2
    assert not os.path.exists(r.approvals)
