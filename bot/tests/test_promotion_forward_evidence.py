"""The forward-evidence item is derived from the artefact, not typed.

`forward_shadow_clean` used to carry a hand-typed sentence ("4 of 20 ... 51 of
180") in one gate file and "0 of 20" in another; neither was computed from
`forward_shadow_current.json`, and nothing noticed the artefact was a month old.
Now the item's evidence is read from the artefact by `forward_status` and the
item can complete only when ALL of these hold: the human flag and approval
digest are present (unchanged), the artefact is readable and FRESH, it is not
DETERIORATING, and it meets the gate's own minimum trades and days. Offline.
"""
from __future__ import annotations

import calendar
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import promotion_gate as pg                                  # noqa: E402
from test_forward_status import write, ep                    # noqa: E402

KEY = "forward_shadow_clean"
REAL_NOW = ep("2026-10-30T12:00:00Z")
HAND_TYPED = "APPEND-ONLY"          # a phrase from the typed gate-file evidence


def gate_with(tmp_path, entry):
    entries = {k: {"complete": False, "owner": "human", "evidence": f"ev-{k}"}
               for k, _d, owner in pg.CHECKLIST_ITEMS if owner != "machine"}
    entries[KEY] = entry
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({"schema": "promotion_gate/1",
                                "max_notional_usd": 100.0, "checklist": entries}))
    return str(path), entries


def approvals_for(tmp_path, entries, keys):
    p = tmp_path / "approvals.json"
    p.write_text(json.dumps({"approved": {k: pg.approval_digest(k, entries[k])
                                          for k in keys}}))
    os.chmod(p, 0o600)
    return str(p)


def item(verdict, key=KEY):
    return next(i for i in verdict.items if i.key == key)


def test_the_real_gate_reports_the_artefact_as_stale_with_derived_numbers():
    v = pg.evaluate_promotion_gate(now=REAL_NOW)
    it = item(v)
    assert it.complete is False
    assert "STALE" in it.evidence and "4 of 20" in it.evidence
    assert "-0.9367" in it.evidence
    assert HAND_TYPED not in it.evidence          # the typed sentence is gone
    assert v.allows_live is False


def test_the_same_artefact_is_not_stale_on_its_own_day():
    v = pg.evaluate_promotion_gate(now=ep("2026-09-30T12:00:00Z"))
    it = item(v)
    assert it.complete is False                    # still 4 of 20
    assert "STALE" not in it.evidence and "INSUFFICIENT_SAMPLE" in it.evidence


def passing_artefact(tmp_path, n=95):
    art, observed_epoch = write(tmp_path, [0.30 + 0.01 * (i % 3) for i in range(n)])
    return art, observed_epoch


def test_a_fresh_sufficient_stable_artefact_with_the_human_approval_completes(tmp_path):
    art, now = passing_artefact(tmp_path)
    flag = {"complete": True, "owner": "observation", "evidence": "typed"}
    path, entries = gate_with(tmp_path, flag)
    ap = approvals_for(tmp_path, entries, [KEY])
    v = pg.evaluate_promotion_gate(path=path, approvals_path=ap,
                                   forward_artifact=art, now=now + 3600)
    assert item(v).complete is True, item(v).evidence


def test_the_same_artefact_without_the_human_approval_does_not_complete(tmp_path):
    art, now = passing_artefact(tmp_path)
    path, entries = gate_with(tmp_path, {"complete": True, "owner": "observation",
                                         "evidence": "typed"})
    v = pg.evaluate_promotion_gate(path=path, approvals_path=str(tmp_path / "none"),
                                   forward_artifact=art, now=now + 3600)
    assert item(v).complete is False


def test_a_stale_artefact_does_not_complete_even_with_flag_and_approval(tmp_path):
    art, now = passing_artefact(tmp_path)
    path, entries = gate_with(tmp_path, {"complete": True, "owner": "observation",
                                         "evidence": "typed"})
    ap = approvals_for(tmp_path, entries, [KEY])
    v = pg.evaluate_promotion_gate(path=path, approvals_path=ap,
                                   forward_artifact=art, now=now + 30 * 86400)
    assert item(v).complete is False and "STALE" in item(v).evidence


def test_a_deteriorating_artefact_does_not_complete(tmp_path):
    rs = [0.5] * 50 + [-0.9] * 45
    art, now = write(tmp_path, rs)
    path, entries = gate_with(tmp_path, {"complete": True, "owner": "observation",
                                         "evidence": "typed"})
    ap = approvals_for(tmp_path, entries, [KEY])
    v = pg.evaluate_promotion_gate(path=path, approvals_path=ap,
                                   forward_artifact=art, now=now + 3600)
    assert item(v).complete is False and "DETERIORATING" in item(v).evidence


def test_too_few_trades_or_days_does_not_complete(tmp_path):
    art, now = write(tmp_path, [0.3] * 25)           # 25 trades, ~51 days
    path, entries = gate_with(tmp_path, {"complete": True, "owner": "observation",
                                         "evidence": "typed"})
    ap = approvals_for(tmp_path, entries, [KEY])
    v = pg.evaluate_promotion_gate(path=path, approvals_path=ap,
                                   forward_artifact=art, now=now + 3600)
    assert item(v).complete is False and "of 180" in item(v).evidence


@pytest.mark.parametrize("mode", ["missing", "garbage"])
def test_an_unavailable_artefact_does_not_complete(tmp_path, mode):
    art = str(tmp_path / "nope.json")
    if mode == "garbage":
        open(art, "w").write("{not json")
    path, entries = gate_with(tmp_path, {"complete": True, "owner": "observation",
                                         "evidence": "typed"})
    ap = approvals_for(tmp_path, entries, [KEY])
    v = pg.evaluate_promotion_gate(path=path, approvals_path=ap,
                                   forward_artifact=art, now=REAL_NOW)
    it = item(v)
    assert it.complete is False
    assert ("NO_DATA" in it.evidence) or ("UNREADABLE" in it.evidence)


def test_promotion_never_allows_live_in_any_of_these_cases(tmp_path):
    art, now = passing_artefact(tmp_path)
    path, entries = gate_with(tmp_path, {"complete": True, "owner": "observation",
                                         "evidence": "typed"})
    ap = approvals_for(tmp_path, entries, [KEY])
    assert pg.promotion_gate_allows_live(
        path=path, approvals_path=ap, forward_artifact=art, now=now + 3600) is False
