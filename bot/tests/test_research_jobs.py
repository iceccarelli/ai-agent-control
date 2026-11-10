"""research_jobs: the durable, bounded job queue for the research/challenge agents. Offline (sqlite in tmp_path; never bot/state)."""
import ast
import os
import sqlite3
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]
import research_jobs as RJ  # noqa: E402

T0 = 1_794_312_000_000
H = "a" * 64
GIT = "0ee080b"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def adv(self, s):
        self.t += int(s * 1000)


@pytest.fixture
def q(tmp_path):
    c = Clock()
    jq = RJ.JobQueue(str(tmp_path / "jobs.sqlite"), c)
    jq.c = c
    return jq


def sub(q, key="k1", role="opportunity_research", kind="regime_eval", refs=None, **kw):
    return q.submit(role, kind, {"refs": refs if refs is not None else [{"name": "md-20261101", "sha256": H}], "params": {"window_days": 30}}, dedupe_key=key, code_version=GIT,
                    config={"seed": 1}, **kw)


def prop(role="opportunity_research", kind="HYPOTHESIS", verdict=None, **kw):
    p = {"role": role, "kind": kind, "summary": "funding persistence above the cost chain", "rationale": "median settlement exceeds the derived requirement in 3 of 5 windows",
         "parameters": {"windows_ok": 3}, "evidence_refs": ["md-20261101"], "created_ms": T0, **kw}
    if verdict:
        p["parameters"] = {"verdict": verdict}
    return p


def test_submit_is_idempotent_typed_and_names_the_exact_data_and_code(q):
    jid, new = sub(q)
    assert new and sub(q)[0] == jid and sub(q)[1] is False
    j = q.claim("w1")
    assert j["id"] == jid and j["code_version"] == GIT and len(j["config_digest"]) == 64 and j["inputs"]["refs"][0]["sha256"] == H and j["attempt"] == 1


@pytest.mark.parametrize("bad", [
    {"refs": [{"name": "../../etc/passwd", "sha256": H}]}, {"refs": [{"name": "x", "sha256": "zz"}]}, {"refs": [], "params": {"max_notional_usd": 500}},
    {"refs": [], "params": {"api_key": "x"}}, {"refs": [], "params": {"hold_s": 5}}, {"refs": [], "params": {"x": [1]}}, {"refs": [], "params": {"ok": True}}, {"url": "http://x"},
    {"refs": [{"name": "x", "sha256": H, "path": "/tmp/x"}]}])
def test_inputs_are_closed_and_never_carry_paths_urls_secrets_or_decision_inputs(q, bad):
    with pytest.raises(RJ.JobRefused):
        q.submit("opportunity_research", "regime_eval", bad, dedupe_key="b", code_version=GIT, config={})


def test_role_code_version_and_limits_are_validated(q):
    for kw in ({"role": "trader"}, {"code_version": "not-a-sha"}, {"max_attempts": 0}, {"timeout_s": 10**6}):
        args = dict(role="opportunity_research", kind="regime_eval", inputs={"refs": []}, dedupe_key="x", code_version=GIT, config={}) | kw
        with pytest.raises(RJ.JobRefused):
            q.submit(args.pop("role"), args.pop("kind"), args.pop("inputs"), **args)


def test_only_one_worker_gets_a_job_and_a_stale_token_can_never_overwrite_a_newer_attempt(q):
    sub(q, timeout_s=60)
    j1 = q.claim("w1")
    assert q.claim("w2") is None                                              # leased: nobody else gets it
    q.c.adv(61)                                                               # w1 hangs past its time limit
    q.c.adv(RJ.BACKOFF_MAX_S)                                                 # the expired attempt is re-queued with a backoff; wait it out
    j2 = q.claim("w2")
    assert j2 is not None and j2["id"] == j1["id"] and j2["token"] == j1["token"] + 1 and j2["attempt"] == 2
    assert q.complete(j1["id"], j1["token"], [prop()]) == "STALE"             # the slow worker is fenced out
    assert q.fail(j1["id"], j1["token"], "late") == "STALE" and q.heartbeat(j1["id"], j1["token"]) is False
    assert q.complete(j2["id"], j2["token"], [prop()]) == "SUCCEEDED"


def test_a_lost_lease_spends_the_attempt_and_the_job_dies_instead_of_looping_forever(q):
    sub(q, timeout_s=10, max_attempts=2)
    for _ in range(2):
        assert q.claim("w") is not None
        q.c.adv(11)
        q.c.adv(RJ.BACKOFF_MAX_S)                                             # past any backoff
    assert q.claim("w") is None and q.stats()["jobs"]["DEAD"] == 1


def test_failures_retry_with_exponential_backoff_then_go_dead(q):
    sub(q, max_attempts=3)
    j = q.claim("w")
    assert q.fail(j["id"], j["token"], "boom") == "QUEUED"
    assert q.claim("w") is None                                               # not yet: backoff 30 s
    q.c.adv(31)
    j = q.claim("w")
    assert j["attempt"] == 2 and q.fail(j["id"], j["token"], "boom") == "QUEUED"
    q.c.adv(40)
    assert q.claim("w") is None                                               # second backoff is 60 s
    q.c.adv(21)
    j = q.claim("w")
    assert j["attempt"] == 3 and q.fail(j["id"], j["token"], "boom") == "DEAD"
    assert [e["event"] for e in q.history(j["id"])].count("CLAIMED") == 3 and q.stats()["jobs"]["DEAD"] == 1


def test_a_malformed_output_is_quarantined_not_retried_and_not_stored(q):
    sub(q)
    j = q.claim("w")
    bad = prop(parameters={"max_notional_usd": 500})
    assert q.complete(j["id"], j["token"], [prop(), bad]) == "QUARANTINED"
    assert q.stats()["jobs"]["QUARANTINED"] == 1 and q.claim("w") is None
    row = sqlite3.connect(q.path).execute("select output_json, last_error from jobs").fetchone()
    assert row[0] is None and row[1].startswith("OUTPUT_QUARANTINED")


def test_an_output_that_names_another_role_is_quarantined(q):
    sub(q)
    j = q.claim("w")
    assert q.complete(j["id"], j["token"], [prop(role="red_team")]) == "QUARANTINED"


def test_events_are_append_only(q):
    jid, _ = sub(q)
    db = sqlite3.connect(q.path)
    for stmt in ("update events set detail='x'", "delete from events"):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(stmt)
    assert q.history(jid)[0]["event"] == "SUBMITTED"


def _produce(q, worker="researcher"):
    jid, _ = sub(q, key="prod")
    j = q.claim(worker)
    assert q.complete(j["id"], j["token"], [prop()]) == "SUCCEEDED"
    return jid, sqlite3.connect(q.path).execute("select output_digest from jobs where id=?", (jid,)).fetchone()[0]


def _challenge(q, digest, worker, verdict, role="quant_challenger", key="chal"):
    cid, _ = q.submit(role, "challenge", {"refs": [{"name": "candidate_output", "sha256": digest}]}, dedupe_key=key, code_version=GIT, config={})
    j = q.claim(worker, roles=[role])
    assert q.complete(j["id"], j["token"], [prop(role=role, kind="CHALLENGE", verdict=verdict)]) == "SUCCEEDED"
    return cid


def test_a_candidate_is_promotable_only_after_a_different_worker_challenges_it_and_it_survives(q):
    pid, digest = _produce(q)
    assert q.promotable(pid) is False                                          # nobody reviewed it: not promotable
    cid = _challenge(q, digest, "challenger", "SURVIVES")
    assert q.record_review(pid, cid) == "SURVIVES" and q.promotable(pid) is True


def test_self_approval_a_wrong_role_and_a_review_of_other_data_are_refused(q):
    pid, digest = _produce(q, worker="same")
    cid = _challenge(q, digest, "same", "SURVIVES")
    assert q.record_review(pid, cid).startswith("REFUSED:the reviewer is the producer") and q.promotable(pid) is False
    other = _challenge(q, "b" * 64, "other", "SURVIVES", key="chal2")
    assert q.record_review(pid, other).startswith("REFUSED:the review job did not take the producer's output digest")
    # a challenge that is not about THIS producer cannot be reused, and a researcher cannot review a researcher
    rid, _ = sub(q, key="r2", role="market_intelligence")
    j = q.claim("m", roles=["market_intelligence"])
    q.complete(j["id"], j["token"], [prop(role="market_intelligence")])
    assert q.record_review(pid, rid).startswith("REFUSED:the reviewer must be a challenger")


def test_a_failing_ambiguous_or_missing_verdict_is_not_survival_and_one_failure_blocks_promotion(q):
    pid, digest = _produce(q)
    c1 = _challenge(q, digest, "c1", "FAILS", key="c1")
    assert q.record_review(pid, c1) == "FAILS" and q.promotable(pid) is False
    c2 = _challenge(q, digest, "c2", "SURVIVES", key="c2")
    assert q.record_review(pid, c2) == "SURVIVES" and q.promotable(pid) is False            # one SURVIVES does not erase a FAILS
    c3 = _challenge(q, digest, "c3", "maybe", key="c3")
    assert q.record_review(pid, c3) == "FAILS"


def test_external_model_jobs_wait_for_the_daily_budget_and_deterministic_jobs_never_do(tmp_path):
    c = Clock()
    jq = RJ.JobQueue(str(tmp_path / "j.sqlite"), c, budget_calls_per_day=2, budget_usd_per_day=1.0)
    for i in range(3):
        sub(jq, key=f"m{i}", needs_model=True, est_cost_usd=0.4)
    sub(jq, key="det", kind="deterministic_scan")
    got = [jq.claim("w") for _ in range(5)]
    assert sum(1 for g in got if g) == 3                                       # 2 model jobs (0.8 USD) + 1 deterministic; the third model job would cost 1.2 > 1.0
    assert jq.stats()["budget_today"]["calls"] == 2 and jq.stats()["jobs"]["QUEUED"] == 1
    c.adv(24 * 3600)                                                           # a new UTC day: the budget is fresh
    assert jq.claim("w") is not None


def test_no_budget_configured_means_no_external_model_at_all(q):
    sub(q, needs_model=True, est_cost_usd=0.01)
    assert q.claim("w") is None and q.stats()["jobs"]["QUEUED"] == 1


def test_the_queue_survives_a_restart_and_resumes_leases_and_state(tmp_path):
    c = Clock()
    p = str(tmp_path / "j.sqlite")
    a = RJ.JobQueue(p, c)
    jid, _ = sub(a, timeout_s=60)
    j = a.claim("w1")
    a.close()                                                                  # the service restarts
    b = RJ.JobQueue(p, c)
    assert b.claim("w2") is None                                               # the lease is still live across the restart
    assert b.heartbeat(j["id"], j["token"]) is True and b.complete(j["id"], j["token"], [prop()]) == "SUCCEEDED"
    assert RJ.JobQueue(p, c).stats()["jobs"]["SUCCEEDED"] == 1


def test_claims_can_be_limited_to_roles(q):
    sub(q, key="a", role="opportunity_research")
    sub(q, key="b", role="accounting_reporting", kind="reconcile")
    assert q.claim("w", roles=["accounting_reporting"])["role"] == "accounting_reporting"
    assert q.claim("w", roles=["accounting_reporting"]) is None


def test_the_module_can_reach_no_venue_credential_or_network_by_construction():
    tree = ast.parse(open(os.path.join(BOT, "tools", "research_jobs.py"), encoding="utf-8").read())
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"__future__", "hashlib", "json", "os", "re", "sqlite3", "time", "typing", "agent_contracts"}, mods
    src = open(os.path.join(BOT, "tools", "research_jobs.py"), encoding="utf-8").read().lower()
    assert "bybit" not in src.replace("exchange", "") and "environ" not in src and "socket" not in src and "requests" not in src
