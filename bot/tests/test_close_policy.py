"""The close policy has no defaults, cannot be enabled by filling in numbers, and
refuses what it cannot enforce."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import close_plan as CP                                         # noqa: E402
import close_policy as P                                        # noqa: E402

GOOD = {
    P.K_ATTEMPTS: "3", P.K_WAITS: "12", P.K_LAG_WAITS: "5",
    P.K_RESEND: "NEVER", P.K_ORDER_TYPE: "MARKET_REDUCE_ONLY",
    P.K_TIMEOUT: "HALT", P.K_RESIDUAL: "HALT_AND_RECORD",
    P.K_ESCALATION: "HALT_AND_LOG_CRITICAL",
}


def cfg(**over):
    d = dict(GOOD)
    d.update(over)
    return SimpleNamespace(**{k: v for k, v in d.items() if v is not None})


def approved(**over):
    return cfg(**{P.K_APPROVED: P.APPROVAL_VALUE, **over})


def test_an_empty_config_is_unset_and_the_path_is_off():
    v = P.load_close_policy(SimpleNamespace())
    assert v.state == P.UNSET and v.policy is None and not v.enabled
    assert len(v.blockers()) >= 8


def test_there_are_no_defaults_every_decision_must_be_stated():
    for key in (P.K_ATTEMPTS, P.K_WAITS, P.K_LAG_WAITS, P.K_RESEND,
                P.K_ORDER_TYPE, P.K_TIMEOUT, P.K_RESIDUAL, P.K_ESCALATION):
        v = P.load_close_policy(approved(**{key: None}))
        assert v.state == P.UNSET and not v.enabled, key


def test_filling_in_every_value_does_not_turn_the_path_on():
    v = P.load_close_policy(cfg())
    assert v.state == P.SET and v.policy is not None
    assert v.approved is False and v.enabled is False
    assert any(P.K_APPROVED in b for b in v.blockers())


@pytest.mark.parametrize("approval", [None, "", "true", "True", "1", "yes",
                                      "approved_by_owner", "APPROVED", True])
def test_only_the_exact_approval_string_approves(approval):
    c = cfg(**{P.K_APPROVED: approval})
    assert P.load_close_policy(c).enabled is False


def test_the_exact_approval_with_every_value_turns_it_on():
    v = P.load_close_policy(approved())
    assert v.enabled and v.policy.max_attempts == 3 and v.policy.max_waits == 12


def test_the_approval_alone_without_values_does_nothing():
    v = P.load_close_policy(SimpleNamespace(**{P.K_APPROVED: P.APPROVAL_VALUE}))
    assert v.enabled is False and v.state == P.UNSET


@pytest.mark.parametrize("key,bad", [
    (P.K_ATTEMPTS, "0"), (P.K_ATTEMPTS, "-1"), (P.K_ATTEMPTS, "2.5"),
    (P.K_ATTEMPTS, "nan"), (P.K_ATTEMPTS, "inf"), (P.K_ATTEMPTS, "abc"),
    (P.K_ATTEMPTS, str(P.CEILING_ATTEMPTS + 1)),
    (P.K_WAITS, "0"), (P.K_WAITS, str(P.CEILING_WAITS + 1)), (P.K_WAITS, "1e9"),
    (P.K_LAG_WAITS, "-1"), (P.K_LAG_WAITS, str(P.CEILING_LAG_WAITS + 1)),
    (P.K_RESEND, "ALWAYS"), (P.K_RESEND, "never"),
    (P.K_ORDER_TYPE, "LIMIT"), (P.K_ORDER_TYPE, "MARKET"),
    (P.K_TIMEOUT, "RETRY_FOREVER"), (P.K_TIMEOUT, "FLATTEN"),
    (P.K_RESIDUAL, "IGNORE"), (P.K_RESIDUAL, "BOOK_AS_CLOSED"),
    (P.K_ESCALATION, "NONE"),
])
def test_an_unusable_value_is_invalid_not_clamped(key, bad):
    v = P.load_close_policy(approved(**{key: bad}))
    assert v.state == P.INVALID and v.policy is None and not v.enabled


def test_zero_lag_waits_is_a_legal_owner_choice():
    assert P.load_close_policy(approved(**{P.K_LAG_WAITS: "0"})).enabled


def test_a_slippage_ceiling_is_refused_because_it_cannot_be_enforced():
    for value in ("5", "0", "50.5", "nan"):
        v = P.load_close_policy(approved(**{P.K_SLIPPAGE: value}))
        assert v.state == P.INVALID and not v.enabled, value
    assert any("cannot be enforced" in b for b in
               P.load_close_policy(approved(**{P.K_SLIPPAGE: "5"})).blockers())


def test_resending_the_same_id_needs_its_own_send_limit():
    v = P.load_close_policy(approved(**{P.K_RESEND: "SAME_LINK_ID"}))
    assert v.state == P.UNSET and not v.enabled
    ok = P.load_close_policy(approved(**{P.K_RESEND: "SAME_LINK_ID", P.K_SENDS: "2"}))
    assert ok.enabled and ok.policy.resend_same_link_id and ok.policy.max_sends == 2
    bad = P.load_close_policy(approved(**{P.K_RESEND: "SAME_LINK_ID", P.K_SENDS: "99"}))
    assert bad.state == P.INVALID


def test_never_resending_ignores_a_stray_send_limit_and_resends_nothing():
    v = P.load_close_policy(approved(**{P.K_SENDS: "4"}))
    assert v.enabled and v.policy.resend_same_link_id is False and v.policy.max_sends == 1


def test_the_planner_policy_carries_exactly_the_owner_values():
    pol = P.load_close_policy(approved()).policy
    cp = P.planner_policy(pol, qty_step=0.001, min_qty=0.001)
    assert (cp.max_attempts, cp.max_waits, cp.max_lag_waits) == (3, 12, 5)
    assert cp.resend_same_link_id is False
    assert (cp.qty_step, cp.min_qty) == (0.001, 0.001)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), None, True, "0.001"])
def test_lot_rules_that_are_not_positive_finite_numbers_are_refused(bad):
    pol = P.load_close_policy(approved()).policy
    with pytest.raises(ValueError):
        P.planner_policy(pol, qty_step=bad, min_qty=0.001)
    with pytest.raises(ValueError):
        P.planner_policy(pol, qty_step=0.001, min_qty=bad)


def test_nothing_in_the_module_supplies_a_default_for_an_owner_value():
    import inspect
    src = inspect.getsource(P)
    for key in (P.K_ATTEMPTS, P.K_WAITS, P.K_LAG_WAITS):
        assert f'getattr(cfg, "{key}", ' not in src
    assert "max_attempts: int =" not in src and "max_waits: int =" not in src


def test_status_lines_say_off_when_off_and_never_invent_a_value():
    off = "\n".join(P.status_lines(P.load_close_policy(SimpleNamespace())))
    assert "CLOSE PATH OFF" in off and "UNSET" in off
    on = "\n".join(P.status_lines(P.load_close_policy(approved())))
    assert "CLOSE PATH ON" in on


def test_the_exposed_decisions_name_every_owner_choice():
    assert set(P.DECISIONS) == set(P.ALL_KEYS)
