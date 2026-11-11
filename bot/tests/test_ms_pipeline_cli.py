"""ms_pipeline CLI: the multi-symbol pipeline as ONE command (REPLAY / PAPER on a simulated transport; no network, no credential, no order). Every test drives the real verifiers, scanner,
allocator, risk, authority, simulated venue, ledger and control centre. The dataset fixture is written by the REAL writers (``test_candidate_readiness._hist``).

The ``check_*`` functions are the proofs; the MUTATION tests at the bottom break one behaviour at a time and require the matching proof to FAIL.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE, os.path.join(BOT, "tools")):
    sys.path.insert(0, _p)

import control_centre as CCE  # noqa: E402
import history_holdout as HH  # noqa: E402
import ms_pipeline as MP  # noqa: E402
import ms_types as T  # noqa: E402
import strategy_verdict as SV  # noqa: E402
import test_candidate_readiness as TCR  # noqa: E402
import test_market_history as TMH  # noqa: E402

H8 = TMH.H8
START, END = TMH.FIRST + 20 * H8, TMH.FIRST + 60 * H8
REV = "a" * 40
CAND_PATH = os.path.join(BOT, "research", "candidates", "cash_and_carry.json")
CAND_SPEC = json.load(open(CAND_PATH, encoding="utf-8"))
CAND_ID = CAND_SPEC["candidate_id"]
PROMISING = {"candidate": CAND_ID, "name": "cash_and_carry", "verdict": "INSUFFICIENT_DATA", "readiness_level": "PROMISING", "rejection_reasons": []}
ALL_FILES = MP.ARTIFACT_FILES + (MP.SUMMARY_FILE, MP.RECONCILE_FILE)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


UNPROMOTED = {"candidate": CAND_ID, "name": "cash_and_carry", "verdict": "INSUFFICIENT_DATA", "readiness_level": "RESEARCH_READY", "rejection_reasons": ["stub: not promoted"]}


@pytest.fixture(autouse=True)
def fast_competition(monkeypatch):
    """The REAL candidate_competition is run once (the ``ran`` fixture); every other CLI test uses a stubbed, un-promoted row so the suite stays fast. ``allowed_competition`` overrides it."""
    monkeypatch.setattr(MP, "competition_row_for", lambda *a, **k: dict(UNPROMOTED))


class W:
    pass


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    d = tmp_path_factory.mktemp("cliworld")
    w = W()
    w.root, w.uni, w.spot = TCR._hist(d)
    w.ledger = str(d / "ledgers")
    w.base = d
    return w


@pytest.fixture(scope="module")
def tampered(tmp_path_factory):
    d = tmp_path_factory.mktemp("clitamper")
    w = W()
    w.root, w.uni, w.spot = TCR._hist(d)
    w.ledger = str(d / "ledgers")
    w.base = d
    for dp, _dn, fn in os.walk(os.path.join(w.root, "funding")):
        for f in fn:
            if f == "rows.jsonl":
                with open(os.path.join(dp, f), "ab") as fh:
                    fh.write(b" ")
                return w
    raise AssertionError("no rows.jsonl to tamper with")


def argv(w, art, cmd="run", **over):
    base = {"--history-root": w.root, "--universe-dir": w.uni, "--spot-dir": w.spot, "--mark-interval": TCR.MARK_IV, "--artifacts-dir": art, "--ledger-dir": w.ledger,
            "--start-ms": str(START), "--end-ms": str(END), "--candidate": CAND_PATH, "--execution-policy": "MAKER_FIRST", "--derive-risk-policy": True, "--mode": "REPLAY",
            "--clock-ms": str(END), "--code-revision": REV, "--hold-days": "2",
            "--unreserved-exploratory": True}            # these tests are about the CLI's other contracts; the reserved-holdout guard has its own file (test_history_holdout.py)
    for k, v in over.items():
        base["--" + k.replace("_", "-")] = v
    out = [cmd]
    for k, v in base.items():
        if v is None or (cmd == "validate" and k in ("--artifacts-dir",) and False):
            continue
        out.append(k)
        if v is not True:
            out.append(str(v))
    return out


def cli(capsys, args):
    capsys.readouterr()
    try:
        code = MP.main(args)
    except SystemExit as exc:
        code = int(exc.code or 0)
    out = capsys.readouterr().out.strip()
    try:
        doc = json.loads(out.splitlines()[-1]) if out else None
    except ValueError:
        doc = None
    return code, doc


def blobs(d):
    return {n: open(os.path.join(d, n), "rb").read() for n in ALL_FILES}


def arts(d):
    return {n: json.loads(b) for n, b in blobs(d).items()}


@pytest.fixture(scope="module")
def ran(world, tmp_path_factory):
    """One real run of the SCRIPT (``python3 tools/ms_pipeline.py run``, a subprocess: the real entry point) with the REAL candidate_competition on the real fixture: the candidate is not
    promoted, so the pipeline refuses to allocate: a valid empty result."""
    out = tmp_path_factory.mktemp("cliout") / "art"
    r = subprocess.run([sys.executable, os.path.join(BOT, "tools", "ms_pipeline.py")] + argv(world, str(out)), capture_output=True, text=True, timeout=900)
    doc = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
    assert r.returncode == 0, (r.stdout[-600:], r.stderr[-600:])
    return doc, str(out)


def allowed_competition(monkeypatch):
    """TEST-ONLY seam: the competition says PROMISING, so the pipeline may allocate (nothing real is PROMISING today)."""
    monkeypatch.setattr(MP, "competition_row_for", lambda *a, **k: dict(PROMISING))


# ------------------------------------------------------------------------------------------------ help and usage

def test_help_lists_the_subcommands_and_explicit_flags_and_has_no_mainnet_or_testnet_mode():
    r = subprocess.run([sys.executable, os.path.join(BOT, "tools", "ms_pipeline.py"), "--help"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    for word in ("run", "validate"):
        assert word in r.stdout
    r2 = subprocess.run([sys.executable, os.path.join(BOT, "tools", "ms_pipeline.py"), "run", "--help"], capture_output=True, text=True, timeout=120)
    for flag in ("--history-root", "--universe-dir", "--spot-dir", "--collector-root", "--artifacts-dir", "--ledger-dir", "--start-ms", "--end-ms", "--candidate", "--mark-interval",
                 "--min-holding-rows", "--execution-policy", "--risk-policy", "--derive-risk-policy", "--mode", "--clock-ms", "--code-revision"):
        assert flag in r2.stdout, flag
    assert "mainnet" not in (r.stdout + r2.stdout).lower() and "--live" not in r2.stdout


# ------------------------------------------------------------------------------------------------ refusals (exit 2, machine readable, nothing written)

def refusal(capsys, args, code_expected, art_path=None):
    code, doc = cli(capsys, args)
    assert code == 2, (code, doc)
    assert doc is not None and doc["ok"] is False and doc["error"]["code"] == code_expected, doc
    assert isinstance(doc["error"]["message"], str) and doc["error"]["message"]
    if art_path is not None:
        assert not os.path.exists(art_path) or not os.listdir(art_path)              # a refusal leaves no artifact behind
    return doc


@pytest.mark.parametrize("mode,code", [("TESTNET", "MODE_NOT_ACCEPTED"), ("MAINNET", "MODE_NOT_ACCEPTED"), ("mainnet", "MODE_NOT_ACCEPTED"), ("LIVE", "MODE_NOT_ACCEPTED"),
                                       ("replay", "MODE_NOT_ACCEPTED"), ("", "MODE_NOT_ACCEPTED")])
def test_modes_other_than_replay_and_paper_are_refused_and_testnet_names_its_owner_tool(world, tmp_path, capsys, mode, code):
    doc = refusal(capsys, argv(world, str(tmp_path / "a"), mode=mode), code, str(tmp_path / "a"))
    if mode == "TESTNET":
        assert "ms_testnet" in doc["error"]["message"] and "not accept" in doc["error"]["message"].lower()


def test_there_is_no_flag_for_mainnet_or_a_venue(world, tmp_path, capsys):
    for extra in (["--mainnet"], ["--live"], ["--venue", "bybit"], ["--testnet"]):
        code, doc = cli(capsys, argv(world, str(tmp_path / "a")) + extra)
        assert code == 2 and doc is not None and doc["ok"] is False and doc["error"]["code"] == "ARGUMENT_ERROR"


def test_artifacts_dir_must_be_new_or_empty_outside_git_and_never_a_dataset(world, tmp_path, capsys):
    full = tmp_path / "full"
    full.mkdir()
    (full / "x.json").write_text("{}")
    refusal(capsys, argv(world, str(full)), "ARTIFACTS_DIR_NOT_EMPTY")
    assert os.listdir(full) == ["x.json"]
    inside_git = os.path.join(os.path.dirname(BOT), "zz_pipeline_refusal_probe")
    refusal(capsys, argv(world, inside_git), "ARTIFACTS_DIR_INSIDE_GIT")
    assert not os.path.exists(inside_git)
    refusal(capsys, argv(world, world.root), "ARTIFACTS_DIR_IS_AN_INPUT")                       # an existing dataset is never an output dir
    refusal(capsys, argv(world, os.path.join(world.root, "new_sub")), "ARTIFACTS_DIR_IS_AN_INPUT")
    refusal(capsys, argv(world, world.uni), "ARTIFACTS_DIR_IS_AN_INPUT")
    refusal(capsys, argv(world, str(tmp_path / "missing_parent" / "a")), "ARTIFACTS_DIR_PARENT_MISSING")
    link = tmp_path / "lnk"
    os.symlink(str(tmp_path / "empty_target"), str(link))
    refusal(capsys, argv(world, str(link)), "ARTIFACTS_DIR_IS_SYMLINK")
    empty = tmp_path / "empty"
    empty.mkdir()
    code, doc = cli(capsys, argv(world, str(empty), cmd="validate"))
    assert code == 0 and doc["ok"] is True                                                     # an existing EMPTY directory is acceptable


def test_state_directory_and_ledger_dir_inside_artifacts_are_refused(world, tmp_path, capsys):
    refusal(capsys, argv(world, str(tmp_path / "a"), ledger_dir=str(tmp_path / "a" / "led")), "LEDGER_DIR_INSIDE_ARTIFACTS_DIR")
    refusal(capsys, argv(world, str(tmp_path / "b"), ledger_dir=os.path.join(BOT, "state", "led")), "LEDGER_DIR_REFUSED")


def test_an_unverified_dataset_is_refused_by_the_real_verifier(tampered, tmp_path, capsys):
    refusal(capsys, argv(tampered, str(tmp_path / "a")), "INPUT_NOT_VERIFIED", str(tmp_path / "a"))


@pytest.mark.parametrize("over,code", [
    ({"history_root": "/nonexistent/hist"}, "HISTORY_ROOT_MISSING"), ({"universe_dir": "/nonexistent/uni"}, "UNIVERSE_DIR_MISSING"), ({"spot_dir": "/nonexistent/spot"}, "SPOT_DIR_MISSING"),
    ({"collector_root": "/nonexistent/col"}, "COLLECTOR_ROOT_MISSING"), ({"start_ms": str(END), "end_ms": str(START)}, "WINDOW_INVALID"), ({"start_ms": "1700000000"}, "WINDOW_INVALID"),
    ({"start_ms": "abc"}, "WINDOW_INVALID"), ({"start_ms": str(10 ** 12), "end_ms": str(10 ** 12 + 3 * 86_400_000)}, "WINDOW_OUTSIDE_DATA"),
    ({"candidate": "/nonexistent/cand.json"}, "CANDIDATE_MISSING"), ({"execution_policy": "MARKET"}, "EXECUTION_POLICY_INVALID"), ({"execution_policy": "MAKER_ONLY"}, "EXECUTION_POLICY_INVALID"),
    ({"derive_risk_policy": None}, "RISK_POLICY_REQUIRED"), ({"risk_policy": "/nonexistent/p.json"}, "RISK_POLICY_CONFLICT"), ({"min_holding_rows": "0"}, "MIN_HOLDING_ROWS_INVALID"),
    ({"hold_days": "0"}, "HOLD_DAYS_INVALID"), ({"code_revision": "not a sha"}, "CODE_REVISION_INVALID"), ({"clock_ms": "-5"}, "CLOCK_MS_INVALID"),
    ({"mark_interval": "999"}, "INPUT_NOT_VERIFIED"),
])
def test_every_bad_input_is_refused_with_a_machine_readable_code(world, tmp_path, capsys, over, code):
    refusal(capsys, argv(world, str(tmp_path / "a"), **over), code, str(tmp_path / "a"))


def test_candidate_outside_the_registry_needs_a_matching_digest_and_a_runnable_family(world, tmp_path, capsys):
    outside = tmp_path / "mine.json"
    outside.write_text(json.dumps(CAND_SPEC))
    refusal(capsys, argv(world, str(tmp_path / "a"), candidate=str(outside)), "CANDIDATE_DIGEST_REQUIRED")
    refusal(capsys, argv(world, str(tmp_path / "a"), candidate=str(outside), candidate_digest="0" * 64), "CANDIDATE_DIGEST_MISMATCH")
    code, doc = cli(capsys, argv(world, str(tmp_path / "a"), cmd="validate", candidate=str(outside), candidate_digest=SV.spec_digest(CAND_SPEC)))
    assert code == 0 and doc["candidate"]["spec_digest"] == SV.spec_digest(CAND_SPEC)
    refusal(capsys, argv(world, str(tmp_path / "a"), candidate=os.path.join(BOT, "research", "candidates", "passive_execution.json")), "CANDIDATE_NOT_RUNNABLE")


def test_risk_policy_file_is_validated_by_the_closed_schema_and_must_match_the_candidate(world, tmp_path, capsys):
    base = MP.derive_risk_policy(["BTCUSDT"], candidate_id=CAND_ID, spec_digest=SV.spec_digest(CAND_SPEC), portfolio=MP.PF.PortfolioPolicy(), equity_usd=100_000.0)

    def write(name, **chg):
        p = dict(base, **chg)
        f = tmp_path / name
        f.write_text(json.dumps(p))
        return str(f)
    refusal(capsys, argv(world, str(tmp_path / "a"), derive_risk_policy=None, risk_policy=write("wild.json", allowed_symbols=["*"])), "RISK_POLICY_REFUSED")
    refusal(capsys, argv(world, str(tmp_path / "a"), derive_risk_policy=None, risk_policy=write("mainnet.json", venue="mainnet")), "RISK_POLICY_REFUSED")
    refusal(capsys, argv(world, str(tmp_path / "a"), derive_risk_policy=None, risk_policy=write("bybit.json", venue="bybit")), "RISK_POLICY_REFUSED")
    refusal(capsys, argv(world, str(tmp_path / "a"), derive_risk_policy=None, risk_policy=write("extra.json", extra_key=1)), "RISK_POLICY_REFUSED")
    refusal(capsys, argv(world, str(tmp_path / "a"), derive_risk_policy=None, risk_policy=write("other.json", candidate_id="other@1")), "RISK_POLICY_CANDIDATE_MISMATCH")
    refusal(capsys, argv(world, str(tmp_path / "a"), derive_risk_policy=None, risk_policy=write("narrow.json")), "RISK_POLICY_ALLOW_LIST_LACKS")     # allow-list never widened
    (tmp_path / "bad.json").write_text("{not json")
    refusal(capsys, argv(world, str(tmp_path / "a"), derive_risk_policy=None, risk_policy=str(tmp_path / "bad.json")), "RISK_POLICY_UNREADABLE")


def test_collector_root_refusals_are_precise_never_silently_ignored(world, tmp_path, capsys, monkeypatch):
    import test_ms_microstructure as TMS
    col = tmp_path / "col"
    col.mkdir()
    code, doc = cli(capsys, argv(world, str(tmp_path / "a"), collector_root=str(col)))
    assert code == 2 and doc["error"]["code"] in ("COLLECTOR_EMPTY", "COLLECTOR_NOT_VERIFIED")
    v2 = tmp_path / "v2"                                                                      # a REAL collector run whose cohort was selected by a rule that reads funding
    TMS._cohort_root(str(v2), "cohort_v2")
    doc = refusal(capsys, argv(world, str(tmp_path / "b"), collector_root=str(v2)), "COLLECTOR_COHORT_FUNDING_SELECTED", str(tmp_path / "b"))
    assert "funding" in doc["error"]["message"]
    monkeypatch.setitem(sys.modules, "ms_microstructure", None)                               # the module is not importable
    refusal(capsys, argv(world, str(tmp_path / "c"), collector_root=str(col)), "COLLECTOR_PATH_UNAVAILABLE")


def test_collector_data_gives_measured_books_the_digest_enters_the_identity_and_limits_are_stated(tmp_path):
    import test_ms_microstructure as TMS
    import ms_microstructure as MM
    import ms_replay as RP
    root = str(tmp_path / "collect")
    TMS.build(root, n=3)
    ds = MM.load_cycles(root)
    c0 = TMS.T0
    H = 3_600_000
    marks = tuple((c0 - 2 * 86_400_000 + i * H, 100.0 + 0.05 * (i % 3)) for i in range(0, 4 * 24 + 1))
    fund = tuple((c0 - 2 * 86_400_000 + k * 8 * H, 5.0) for k in range(1, 7))
    btc = RP.ReplaySeries("BTCUSDT", fund, marks, 480, "BTC")
    zzz = RP.ReplaySeries("ZZZUSDT", fund, marks, 480, "ZZZ")
    c = TMP.cfg(start_ms=c0 - 86_400_000, end_ms=c0 + 86_400_000, hold_ms=86_400_000)
    pr = TMP.go(tmp_path, [btc, zzz], c, name="micro", micro=ds)
    s = pr.summary
    assert s["data_parts"]["collector"] == ds.data_digest() and s["identity"]["data_digest"] == MP.data_digest_of(s["data_parts"]) != "UNKNOWN"
    mi = s["evidence_basis"]["microstructure"]
    assert mi["symbols_with_micro"] == ["BTCUSDT"] and mi["symbols_without_micro_excluded"] == ["ZZZUSDT"] and s["symbols"] == ["BTCUSDT"]
    assert s["execution"]["books"].startswith("MEASURED_BOOK_TAPE") and s["execution"]["evidence"] == "MODELLED"
    assert any("NO_SPOT_ORDERBOOK_RECORDED" in x for x in mi["limits"]) and any("TAPE_GAP" in x for x in mi["limits"])


def test_the_library_refuses_a_funding_selected_cohort(tmp_path, monkeypatch):
    import test_ms_microstructure as TMS
    import ms_microstructure as MM
    root = str(tmp_path / "v2")
    TMS._cohort_root(root, "cohort_v2")
    ds = MM.load_cycles(root)
    with pytest.raises(MP.PipelineRefused, match="COLLECTOR_COHORT_FUNDING_SELECTED"):
        MP.apply_micro([], ds)


# ------------------------------------------------------------------------------------------------ validate, run, artifacts, identity

def test_validate_checks_inputs_only_and_writes_nothing(world, tmp_path, capsys):
    a = tmp_path / "a"
    code, doc = cli(capsys, argv(world, str(a), cmd="validate"))
    assert code == 0 and doc["ok"] is True and doc["command"] == "validate" and not a.exists()
    assert doc["data_parts"]["history_funding"] and doc["data_parts"]["collector"] == "ABSENT" and len(doc["data_digest"]) == 64


def check_identity(d):
    """The eight files carry ONE identity (the RunIdentity.as_dict() of the run) and agree with the summary."""
    a = arts(d)
    ids = {n: json.dumps(x.get("identity"), sort_keys=True) for n, x in a.items()}
    assert len(set(ids.values())) == 1 and all(v != "null" for v in ids.values()), ids
    ident = a[MP.SUMMARY_FILE]["identity"]
    T.RunIdentity(**ident)                                                                    # a well-formed RunIdentity
    assert set(ident) == {"run_id", "mode", "candidate_id", "spec_digest", "data_digest", "code_revision", "generated_ms"}
    assert all(x["generated_ms"] == ident["generated_ms"] for x in a.values())
    assert ident["run_id"] == T.derive_run_id(mode=ident["mode"], candidate_id=ident["candidate_id"], spec_digest=ident["spec_digest"], data_digest=ident["data_digest"], generated_ms=ident["generated_ms"])
    return ident


def test_the_real_cli_run_writes_seven_files_one_identity_and_the_control_centre_reads_them_without_error(ran, world):
    doc, d = ran
    assert doc["ok"] is True and doc["command"] == "run" and set(ALL_FILES) <= set(os.listdir(d)) and doc["identity"]["run_id"] == arts(d)[MP.SUMMARY_FILE]["identity"]["run_id"]
    assert all(os.path.isfile(os.path.join(d, n)) for n in ALL_FILES)
    ident = check_identity(d)
    assert ident["mode"] == "REPLAY" and ident["code_revision"] == REV and ident["candidate_id"] == CAND_ID and ident["spec_digest"] == SV.spec_digest(CAND_SPEC) and len(ident["data_digest"]) == 64
    st = CCE.build_state(data_root=None, universe_dir=None, artifacts_dir=d, env={}, now_ms=END + 1000)
    for sec in ("portfolio", "execution", "risk"):
        assert st[sec]["state"] == "OK", (sec, st[sec].get("error"))
    # the pipeline writes eight files; universe_scope / funnel / testnet_session come from other tools, so NO_DATA is correct for them, ERROR never is
    _other = {"universe_scope.json", "funnel.json", "testnet_session.json"}
    assert all(s["state"] == "OK" or (s["file"] in _other and s["state"] == "NO_DATA") for s in st["evidence"]["sources"]), [(s["file"], s["state"], s["error"]) for s in st["evidence"]["sources"] if s["state"] != "OK"]
    assert st["research"]["opportunities"]["state"] == "OK" and st["research"]["competition"]["state"] == "OK"


def test_a_candidate_the_competition_has_not_promoted_cannot_allocate_and_the_summary_says_why(ran):
    _doc, d = ran
    s = arts(d)[MP.SUMMARY_FILE]
    assert "NO_ALLOCATION" in s["status"]["codes"] and s["status"]["reasons"]["NO_ALLOCATION"]
    assert s["gate"]["allowed"] is False and s["gate"]["verdict"] in ("REJECTED", "INSUFFICIENT_DATA", "UNTESTED")
    a = arts(d)
    assert a["portfolio_state.json"]["positions"] == [] and a["replay_report.json"]["n_groups"] == 0
    assert s["identity"]["candidate_id"] == s["gate"]["candidate_id"] == CAND_ID                    # the competition row is the pipeline's candidate
    assert s["promotion_eligible"] is False and s["execution"]["evidence"] == "MODELLED" and "MEASURED" not in s["execution"]["evidence"].replace("MEASURED_BOOK", "")
    assert s["data_parts"]["collector"] == "ABSENT" and s["data_parts"]["history_funding"] != "ABSENT"
    for n in ALL_FILES:
        assert arts(d)[n].get("evidence_basis"), n


def test_data_digest_is_derived_from_the_verified_manifest_digests(ran, world):
    _doc, d = ran
    s = arts(d)[MP.SUMMARY_FILE]
    import candidate_readiness as CR
    _series, info = CR.load_history_root(world.root, universe_dir=world.uni, spot_dir=world.spot, mark_interval=TCR.MARK_IV)
    assert s["data_parts"]["history_funding"] == info["datasets"]["funding"]["manifest_digest"]
    assert s["data_parts"]["history_mark"] == info["datasets"]["mark-kline-240"]["manifest_digest"]
    assert s["data_parts"]["universe"] == info["universe"]["manifest_sha256"]
    assert s["identity"]["data_digest"] == MP.data_digest_of(s["data_parts"]) and s["identity"]["data_digest"] != "UNKNOWN"


def check_deterministic(world, tmp_path, capsys, **over):
    a, b = str(tmp_path / "d1"), str(tmp_path / "d2")
    c1, _ = cli(capsys, argv(world, a, **over))
    c2, _ = cli(capsys, argv(world, b, **over))
    assert c1 == 0 and c2 == 0
    assert blobs(a) == blobs(b)
    return a


def test_two_runs_with_a_fixed_clock_give_byte_identical_files(world, tmp_path, capsys):
    check_deterministic(world, tmp_path, capsys)


def test_a_different_clock_changes_only_the_stamp(world, tmp_path, capsys):
    a = check_deterministic(world, tmp_path, capsys)
    c = str(tmp_path / "d3")
    assert cli(capsys, argv(world, c, clock_ms=str(END + 5000)))[0] == 0
    x, y = arts(a), arts(c)
    assert y[MP.SUMMARY_FILE]["identity"]["generated_ms"] == END + 5000 and x[MP.SUMMARY_FILE]["identity"]["run_id"] != y[MP.SUMMARY_FILE]["identity"]["run_id"]
    check_identity(c)


def test_a_cli_run_that_is_allowed_to_allocate_trades_on_the_real_fixture_with_one_identity(world, tmp_path, capsys, monkeypatch):
    allowed_competition(monkeypatch)
    d = str(tmp_path / "t")
    code, doc = cli(capsys, argv(world, d, end_ms=str(END + 20 * H8)))
    assert code == 0 and doc["ok"] is True
    s = arts(d)[MP.SUMMARY_FILE]
    assert s["gate"]["allowed"] is True and "NO_ALLOCATION" not in s["status"]["codes"], s["status"]
    assert s["status"]["primary"] in MP.STATUS_CODES and set(s["status"]["codes"]) <= set(MP.STATUS_CODES)
    ident = check_identity(d)
    assert ident["mode"] == "REPLAY"
    st = CCE.build_state(data_root=None, universe_dir=None, artifacts_dir=d, env={}, now_ms=END + 21 * H8 + 1000)
    assert all(s_["state"] == "OK" or (s_["file"] in {"universe_scope.json", "funnel.json", "testnet_session.json"} and s_["state"] == "NO_DATA") for s_ in st["evidence"]["sources"]), [(s_["file"], s_["state"], s_["error"]) for s_ in st["evidence"]["sources"] if s_["state"] != "OK"]
    assert "INADEQUATE_HISTORY" in s["status"]["codes"] and s["history"]["required_days"] > s["history"]["available_days"]          # the exact required span is stated


def test_paper_mode_on_the_cli_uses_a_simulated_step_clock_and_stamps_paper(world, tmp_path, capsys, monkeypatch):
    allowed_competition(monkeypatch)
    d = str(tmp_path / "p")
    code, doc = cli(capsys, argv(world, d, mode="PAPER", end_ms=str(END + 6 * H8)))
    assert code == 0, doc
    assert check_identity(d)["mode"] == "PAPER"
    r = str(tmp_path / "r")
    assert cli(capsys, argv(world, r, end_ms=str(END + 6 * H8)))[0] == 0
    x, y = arts(d), arts(r)
    assert x["replay_report.json"]["net_pnl_usd"] == y["replay_report.json"]["net_pnl_usd"]


# ------------------------------------------------------------------------------------------------ the library seams (injected data; same code the CLI calls)

import test_ms_pipeline as TMP  # noqa: E402


def lib_run(tmp_path, row, name="lib", series=None, c=None, **kw):
    return TMP.go(tmp_path, series, c, name=name, competition_row=row, **kw)


def check_rejected_cannot_allocate(tmp_path):
    row = {"candidate": TMP.CAND, "name": "x", "verdict": "REJECTED", "readiness_level": "RESEARCH_READY", "rejection_reasons": ["loses after costs: mean net -3.0 bps"]}
    pr = lib_run(tmp_path, row, name="rej")
    assert pr.result.fills == [] and pr.result.states == {}
    s = pr.summary
    assert s["status"]["primary"] == "REJECTED_CANDIDATE" and "NO_ALLOCATION" in s["status"]["codes"] and "loses after costs" in s["status"]["reasons"]["NO_ALLOCATION"]
    comp = TMP.art(pr, "competition.json")["candidates"][0]
    assert comp["status"] == "REJECTED" and comp["reason"]
    assert TMP.art(pr, "opportunities.json")["items"]                                               # the scan itself still ran and is shown
    return pr


def test_a_rejected_candidate_gets_no_allocation_with_a_reason(tmp_path):
    check_rejected_cannot_allocate(tmp_path)


@pytest.mark.parametrize("verdict,status", [("UNTESTED", "RESEARCHING"), ("INSUFFICIENT_DATA", "INSUFFICIENT_EVIDENCE")])
def test_untested_and_insufficient_candidates_cannot_allocate_either(tmp_path, verdict, status):
    pr = lib_run(tmp_path, {"candidate": TMP.CAND, "name": "x", "verdict": verdict, "readiness_level": "RESEARCH_READY", "rejection_reasons": ["why"]}, name=verdict)
    assert pr.result.fills == [] and "NO_ALLOCATION" in pr.summary["status"]["codes"]
    assert TMP.art(pr, "competition.json")["candidates"][0]["status"] == status
    pr2 = lib_run(tmp_path, None, name="norow")
    assert pr2.result.fills == [] and pr2.summary["gate"]["reason"].startswith("COMPETITION_ROW_MISSING")


def test_a_promising_row_for_another_candidate_does_not_unlock_this_one(tmp_path):
    pr = lib_run(tmp_path, {**PROMISING, "candidate": "someone_else@1"}, name="other")
    assert pr.result.fills == [] and pr.summary["gate"]["allowed"] is False


def test_the_promoted_candidate_allocates_and_the_status_is_simulated_execution(tmp_path):
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="ok")
    assert pr.result.fills and "SIMULATED_EXECUTION" in pr.summary["status"]["codes"] and "NO_ALLOCATION" not in pr.summary["status"]["codes"]
    assert pr.summary["gate"]["allowed"] is True


def test_empty_results_have_explicit_status_codes_not_failure(tmp_path):
    ss = [TMP.ser("AAAUSDT", 480, 0.2), TMP.ser("BBBUSDT", 240, -1.0)]
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="noedge", series=ss, c=TMP.cfg(end_ms=TMP.T0 + 6 * TMP.D))
    s = pr.summary
    assert s["status"]["primary"] in ("NO_OPPORTUNITY", "INADEQUATE_HISTORY") and "NO_OPPORTUNITY" in s["status"]["codes"] and s["status"]["reasons"]["NO_OPPORTUNITY"]
    st = TMP.cc_state(pr)
    assert st["execution"]["state"] == "OK" and st["research"]["competition"]["state"] == "OK"
    assert TMP.art(pr, "opportunities.json")["est_net_usd_per_day"] is None


def test_inadequate_history_states_the_exact_required_span_and_inadequate_sample_the_counts(tmp_path):
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="hist", min_holding_rows=60)
    h = pr.summary["history"]
    assert "INADEQUATE_HISTORY" in pr.summary["status"]["codes"] and h["required_days"] == 7 + 3 + 60 * (TMP.cfg().hold_ms / TMP.D) and h["min_holding_rows"] == 60
    assert h["components"]["min_holding_rows"] == 60 and h["shortfall_days"] > 0 and "INADEQUATE_SAMPLE" in pr.summary["status"]["codes"] and pr.summary["sample"]["independent"] < 60


def test_an_unknown_ledger_component_is_a_stated_unknown_status_never_a_zero(tmp_path):
    from dataclasses import replace
    a = TMP.ser("AAAUSDT", 480, 6.0)
    gap = replace(a, mark=tuple((t, p) for t, p in a.mark if not (TMP.T0 + 2 * TMP.D + 2 * TMP.H < t < TMP.T0 + 4 * TMP.D)))
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="unk", series=[gap], c=TMP.cfg(end_ms=TMP.T0 + 6 * TMP.D))
    assert "UNKNOWN_OR_UNRECONCILED" in pr.summary["status"]["codes"]
    assert TMP.art(pr, "ledger_pnl.json")["net_usd"] is None and TMP.art(pr, "replay_report.json")["net_pnl_usd"] is None


def test_injected_data_has_unknown_data_digest_stated_and_still_one_identity(tmp_path):
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="inj")
    d = os.path.dirname(pr.files["replay_report.json"])
    ident = check_identity(d)
    assert ident["data_digest"] == "UNKNOWN" and arts(d)[MP.SUMMARY_FILE]["data_parts"] == {"injected": "UNKNOWN"} and ident["code_revision"] == "UNKNOWN"


# ------------------------------------------------------------------------------------------------ pure helpers

def test_git_revision_is_read_by_a_pure_helper_and_never_invented():
    assert MP.git_revision(reader=lambda *a, **k: "b" * 40 + "\n") == "b" * 40
    for bad in ("UNKNOWN_NOT_A_GIT_REPO", "UNKNOWN_GIT_UNAVAILABLE", "garbage", "B" * 40, "", "b" * 40 + "-dirty"):
        assert MP.git_revision(reader=lambda *a, bad=bad, **k: bad) == "UNKNOWN", bad

    def boom(*a, **k):
        raise FileNotFoundError("git")
    assert MP.git_revision(reader=boom) == "UNKNOWN"
    assert MP.git_revision() in ("UNKNOWN",) or len(MP.git_revision()) == 40


def test_data_digest_changes_with_every_part_and_unknown_only_when_nothing_is_known():
    p = {"history_funding": "a" * 64, "history_mark": "b" * 64, "universe": "c" * 64, "spot": "d" * 64, "collector": "ABSENT"}
    base = MP.data_digest_of(p)
    for k in p:
        assert MP.data_digest_of({**p, k: "e" * 64}) != base, k
    assert MP.data_digest_of(dict(reversed(list(p.items())))) == base
    assert MP.data_digest_of({"injected": "UNKNOWN"}) == "UNKNOWN" and MP.data_digest_of({}) == "UNKNOWN"


def test_the_gate_is_a_pure_closed_function_of_the_competition_row():
    g = MP.evaluate_gate(PROMISING, candidate_id=CAND_ID)
    assert g.allowed is True and g.verdict == "INSUFFICIENT_DATA"
    for bad in (None, {}, {"candidate": CAND_ID}, {**PROMISING, "verdict": "REJECTED"}, {**PROMISING, "candidate": "x"}, {**PROMISING, "verdict": "PASS"}, {**PROMISING, "readiness_level": "RESEARCH_READY"}):
        assert MP.evaluate_gate(bad, candidate_id=CAND_ID).allowed is False, bad


# ------------------------------------------------------------------------------------------------ MUTATIONS

def fails(fn, *a, **k):
    with pytest.raises(AssertionError):
        fn(*a, **k)


def test_mutation_skipping_verification_is_caught(world, tampered, tmp_path, capsys, monkeypatch):
    refusal(capsys, argv(tampered, str(tmp_path / "ok")), "INPUT_NOT_VERIFIED")
    good = MP.verify_inputs(world.root, universe_dir=world.uni, spot_dir=world.spot, mark_interval=TCR.MARK_IV, holdout_ledger=HH.UNGUARDED)
    monkeypatch.setattr(MP, "verify_inputs", lambda *a, **k: good)                                # what a CLI that does not re-verify the dataset in front of it would do
    fails(refusal, capsys, argv(tampered, str(tmp_path / "mut")), "INPUT_NOT_VERIFIED")


def test_mutation_dropping_the_identity_is_caught(world, tmp_path, capsys, monkeypatch):
    d = str(tmp_path / "id")
    assert cli(capsys, argv(world, d))[0] == 0
    check_identity(d)
    monkeypatch.setattr(MP, "stamp_identity", lambda arts_, ident: arts_)
    e = str(tmp_path / "noid")
    cli(capsys, argv(world, e))
    fails(check_identity, e)


def test_mutation_a_nondeterministic_ordering_is_caught(world, tmp_path, capsys, monkeypatch):
    check_deterministic(world, tmp_path, capsys)
    real = MP.derive_risk_policy
    calls = []

    def flip(*a, **k):
        out = real(*a, **k)
        calls.append(1)
        if len(calls) % 2 == 0:
            out["allowed_symbols"] = list(reversed(out["allowed_symbols"]))
        return out
    monkeypatch.setattr(MP, "derive_risk_policy", flip)
    sub = tmp_path / "m"
    sub.mkdir()
    fails(check_deterministic, world, sub, capsys)


def test_mutation_letting_a_rejected_candidate_allocate_is_caught(tmp_path, monkeypatch):
    check_rejected_cannot_allocate(tmp_path)
    monkeypatch.setattr(MP, "evaluate_gate", lambda row, *, candidate_id: MP.AllocationGate(True, "MUTANT", "REJECTED", "x", candidate_id))
    fails(check_rejected_cannot_allocate, tmp_path / "mut")


# ------------------------------------------------------------------------------------------------ reconciliation, hard loss limits, exit sequencing (library seams)

class PhantomVenue(TMP.EX.SimVenue):
    """A venue whose own position read shows a position nobody opened: every derived view disagrees with it."""
    def positions(self):
        out = super().positions()
        out["linear:AAAUSDT"] = out.get("linear:AAAUSDT", 0.0) + 1.0
        return out


def check_reconcile_halts(tmp_path, name="rec"):
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name=name, venue=PhantomVenue())
    r = TMP.art(pr, "reconcile_report.json") if os.path.exists(pr.files.get("reconcile_report.json", "")) else json.load(open(pr.files[MP.RECONCILE_FILE]))
    assert r["schema"] == "reconcile_report/v1" and r["halt_new_exposure"] is True and r["findings"] and r["ever_disagreed"] is True
    assert {"code", "key", "views", "detail"} <= set(r["findings"][0]) and r["identity"] == pr.summary["identity"]
    assert "UNKNOWN_OR_UNRECONCILED" in pr.summary["status"]["codes"] and pr.summary["reconcile"]["ever_disagreed"] is True
    assert pr.result.fills == [] and any(h.startswith("RECONCILE_DISAGREEMENT") for h in pr.result.engine.halts)             # new exposure was halted, nothing was overwritten
    return pr


def test_a_disagreement_between_the_venue_read_and_the_books_halts_new_exposure_and_is_reported(tmp_path):
    check_reconcile_halts(tmp_path)


def test_mutation_skipping_the_per_step_reconciliation_is_caught(tmp_path, monkeypatch):
    check_reconcile_halts(tmp_path)
    monkeypatch.setattr(MP.AuthorityGates, "reconcile_step", lambda self, now: None)
    fails(check_reconcile_halts, tmp_path / "mut", "mut")


def test_a_clean_run_reconciles_at_every_step_and_the_report_carries_the_identity(tmp_path):
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="clean")
    d = os.path.dirname(pr.files["replay_report.json"])
    r = json.load(open(os.path.join(d, MP.RECONCILE_FILE)))
    assert r["findings"] == [] and r["halt_new_exposure"] is False and r["n_steps_checked"] > 100 and r["ever_disagreed"] is False and r["compared"]["reference"] == "venue"
    assert pr.result.fills and "UNKNOWN_OR_UNRECONCILED" not in pr.summary["status"]["codes"]
    check_identity(d)


def test_the_artifacts_net_is_the_ledgers_canonical_net_and_null_when_incomplete(tmp_path):
    from dataclasses import replace
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="canon")
    assert TMP.art(pr, "replay_report.json")["net_pnl_usd"] == pr.ledger.ledger.canonical_net()["net"]
    a = TMP.ser("AAAUSDT", 480, 6.0)
    gap = replace(a, mark=tuple((t, p) for t, p in a.mark if not (TMP.T0 + 2 * TMP.D + 2 * TMP.H < t < TMP.T0 + 4 * TMP.D)))
    pr2 = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="canon2", series=[gap], c=TMP.cfg(end_ms=TMP.T0 + 6 * TMP.D))
    assert pr2.ledger.ledger.canonical_net()["net"] is None and TMP.art(pr2, "replay_report.json")["net_pnl_usd"] is None and TMP.art(pr2, "ledger_pnl.json")["net_usd"] is None


def test_hard_loss_limits_reach_the_policy_and_the_state_carries_what_they_need(tmp_path):
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="lim", max_daily_loss_usd=5_000.0, max_drawdown_frac=0.5)
    assert pr.policy["max_daily_loss_usd"] == 5_000.0 and pr.policy["max_drawdown_frac"] == 0.5
    assert pr.result.fills, pr.summary["decision_reason_counts"]
    assert not any(k.startswith(("DAILY_LOSS_INPUT_UNKNOWN", "DRAWDOWN_INPUT_UNKNOWN")) for k in pr.summary["decision_reason_counts"]), pr.summary["decision_reason_counts"]
    tiny = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="tiny", max_daily_loss_usd=0.01)
    assert len(tiny.result.groups) < len(pr.result.groups)                                      # the hard daily-loss limit actually bites once fees have been paid
    assert MP.derive_risk_policy(["AAAUSDT"], candidate_id="c", spec_digest="d", portfolio=MP.PF.PortfolioPolicy(), equity_usd=1e5)["max_drawdown_frac"] == MP.PF.PortfolioPolicy().max_drawdown_frac


def test_loss_limit_flags_are_validated_and_conflict_with_a_policy_file(world, tmp_path, capsys):
    refusal(capsys, argv(world, str(tmp_path / "a"), max_daily_loss_usd="0"), "LOSS_LIMIT_INVALID")
    refusal(capsys, argv(world, str(tmp_path / "a"), max_daily_loss_usd="abc"), "LOSS_LIMIT_INVALID")
    refusal(capsys, argv(world, str(tmp_path / "a"), max_drawdown_frac="1.5"), "LOSS_LIMIT_INVALID")
    f = tmp_path / "p.json"
    f.write_text(json.dumps(MP.derive_risk_policy(["BTCUSDT"], candidate_id=CAND_ID, spec_digest=SV.spec_digest(CAND_SPEC), portfolio=MP.PF.PortfolioPolicy(), equity_usd=1e5)))
    refusal(capsys, argv(world, str(tmp_path / "a"), derive_risk_policy=None, risk_policy=str(f), max_drawdown_frac="0.2"), "LOSS_LIMIT_NEEDS_DERIVED_POLICY")


def test_cli_loss_limits_land_in_the_summarys_policy(world, tmp_path, capsys, monkeypatch):
    allowed_competition(monkeypatch)
    d = str(tmp_path / "ll")
    assert cli(capsys, argv(world, d, max_daily_loss_usd="1234.5", max_drawdown_frac="0.2"))[0] == 0
    p = arts(d)[MP.SUMMARY_FILE]["risk_policy"]
    assert p["max_daily_loss_usd"] == 1234.5 and p["max_drawdown_frac"] == 0.2
    check_identity(d)


def test_the_perp_exit_is_filled_before_the_spot_exit_and_no_exit_is_refused_or_halted(tmp_path):
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="seq")
    ast = pr.gates.stats()
    assert ast["orders_refused"] == 0 and "SPOT_SELL_NOT_PROVEN_HEDGE_KEEPING" not in ast["order_refusal_codes"], ast
    assert not any(h.startswith("EXIT_ORDERS_REJECTED") for h in pr.result.engine.halts), pr.result.engine.halts
    by = {}
    for f in pr.result.fills:
        by.setdefault(f.group_id, []).append((f, pr.result.fills_cat.get(f.fill_id, "linear")))
    checked = 0
    for gid, fs in by.items():
        perp_close = [f.ts_ms for f, c in fs if c == "linear" and f.side == "Buy"]                  # the short perp is closed by a Buy
        spot_sell = [f.ts_ms for f, c in fs if c == "spot" and f.side == "Sell"]
        if perp_close and spot_sell:
            checked += 1
            assert min(spot_sell) >= min(perp_close), gid                                              # the spot sell only after the perp exit fill is on the venue book
    assert checked > 0


# ------------------------------------------------------------------------------------------------ ONE net per run; ledger slippage figures; unverified collector; honest limits text

def _flat_run(tmp_path, name):
    ss = [TMP.ser("AAAUSDT", 480, 6.0, n_days=10, delivery_day=8)]
    return lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name=name, series=ss, c=TMP.cfg(end_ms=TMP.T0 + 9 * TMP.D, hold_ms=2 * TMP.D))


def check_one_net(pr):
    """The competition's net (per closed group, ledger definition) adds up to the ledger's net for a run that ends flat."""
    comp = TMP.art(pr, "competition.json")["candidates"][0]
    assert comp["n_observations"] > 0 and not [k for k, v in pr.result.venue.positions().items() if abs(v) > 1e-9], "the fixture must end flat"
    ledger_net = TMP.art(pr, "ledger_pnl.json")["net_usd"]
    assert comp["net_usd_closed_groups"] == pytest.approx(ledger_net, rel=1e-6, abs=1e-6), (comp["net_usd_closed_groups"], ledger_net)
    assert comp["net_usd_closed_groups"] == pytest.approx(TMP.art(pr, "replay_report.json")["net_pnl_usd"], rel=1e-6, abs=1e-6)
    return comp


def test_the_competition_net_and_the_ledger_net_are_one_number_per_run(tmp_path):
    comp = check_one_net(_flat_run(tmp_path, "one"))
    assert "adverse selection" in comp["net_definition"]
    pr = lib_run(tmp_path, {**PROMISING, "candidate": TMP.CAND}, name="happy")                  # the happy fixture: adverse selection and taker slippage are charged in BOTH
    lp, rr = TMP.art(pr, "ledger_pnl.json"), TMP.art(pr, "replay_report.json")
    assert lp["adverse_selection_usd"] is not None and lp["adverse_selection_usd"] > 0 and lp["taker_slippage_usd"] is not None and lp["taker_slippage_usd"] >= 0
    st = TMP.cc_state(pr)
    comp_v = {c["key"]: c["value"] for c in st["accounting"]["components"]}
    assert comp_v["adverse_selection"]["v"] == lp["adverse_selection_usd"] and comp_v["adverse_selection"]["src"] == "ledger_pnl.json:adverse_selection_usd"
    assert comp_v["slippage"]["v"] == lp["taker_slippage_usd"] and comp_v["slippage"]["basis"] != "MISSING"
    assert not [s_ for s_ in st["evidence"]["sources"] if s_["state"] == "ERROR"]


def test_mutation_a_competition_net_that_omits_adverse_selection_is_caught(tmp_path, monkeypatch):
    check_one_net(_flat_run(tmp_path, "ok"))
    real = TMP.RP.GroupRecord
    monkeypatch.setattr(real, "adverse_usd", 0.0, raising=False)
    import ms_pipeline as M2
    src = M2._build_artifacts
    monkeypatch.setattr(M2, "_build_artifacts", lambda *a, **k: _zero_adverse(src(*a, **k)))
    fails(lambda: check_one_net(_flat_run(tmp_path, "mut")))


def _zero_adverse(arts_):
    c = arts_["competition.json"]["candidates"][0]
    if c["net_usd_closed_groups"] is not None:
        c["net_usd_closed_groups"] += 3.0                                                       # what a competition net that dropped the modelled costs would show
    return arts_


def test_an_unverified_collector_is_refused_and_never_labelled_measured(tmp_path):
    import test_ms_microstructure as TMS
    import ms_microstructure as MM
    root = str(tmp_path / "collect")
    TMS.build(root, n=3)
    ds = MM.load_cycles(root, verify=False)
    assert ds.verified is False
    with pytest.raises(MP.PipelineRefused, match="COLLECTOR_NOT_VERIFIED"):
        MP.apply_micro([TMP.RP.ReplaySeries("BTCUSDT", (), ((TMS.T0, 100.0),), 480, "BTC")], ds)
    good = MM.load_cycles(root)
    out, info = MP.apply_micro([TMP.RP.ReplaySeries("BTCUSDT", (), ((TMS.T0, 100.0),), 480, "BTC")], good)
    assert info["symbols_with_micro"] == ["BTCUSDT"]                                            # positive control: the verified dataset is accepted


def test_the_limits_text_states_what_the_replay_does_for_the_spot_leg_and_the_maker_markout():
    text = " ".join(MP.MICRO_LIMITS)
    assert "SYNTHETIC_BOOK_DEPTH_AND_SPREAD" in text and "2 bps" in text and "place_maker_from_micro" in text and "MODELLED" in text
    assert "REJECTED (NO_RECORDED_BOOK_FOR_CATEGORY)" not in text                              # the replay does not reject the spot leg; it gives it a synthetic book
