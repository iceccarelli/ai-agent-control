"""Slice 31 — the process must not be able to imply edge it does not have.

`project_status.py` is a small module and these are a lot of tests for it. The
ratio is deliberate. The field `cleared_edge_signal` is the single place in this
codebase where a false claim would be both easy to introduce and hard to notice:
it is one string, it would look plausible, and every downstream reader — the
startup log, the health payload, the stored session record — would repeat it
faithfully.

So the tests attack it from every direction: can the artefact reader be made to
accept an ABSENT signal? can a near-miss at 94.9 get through? can M1 alone carry
it? can the snapshot be mutated after it is built? can the claim string be edited
without a failure? The answer needs to be no each time.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import json
import os
import re
import shutil
import sys
import tempfile

import registration_invariant as ri

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import bybit_connection as bc  # noqa: E402
import config as _config  # noqa: E402
import main as m  # noqa: E402
import project_status as ps  # noqa: E402
import session_log as sl  # noqa: E402
import trading_engine as te  # noqa: E402
from fake_bybit import API_KEY, API_SECRET, FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402

EQUITY = 100_000.0
ARTIFACTS = os.path.join(REPO, "artifacts")


class Cfg:
    USE_TESTNET = True
    PAPER_TRADING = True
    ENTRIES_ENABLED = True
    PAPER_SESSION_LOG_PATH = ""
    LIVE_AUTHORIZED = False
    POLICY_MODE = "off"
    BYBIT_API_KEY = API_KEY
    BYBIT_API_SECRET = API_SECRET
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    MAX_POSITION_SIZE_PCT = 0.02
    MAX_TOTAL_EXPOSURE_PCT = 0.10
    MAX_DAILY_LOSS_PCT = 0.02
    MAX_DRAWDOWN_PCT = 0.10
    STOP_LOSS_PCT = 0.02
    RISK_PER_TRADE_PCT = 0.005
    MAX_OPEN_POSITIONS = 4
    MAX_CONSECUTIVE_LOSSES = 4
    MIN_RISK_REWARD_RATIO = 2.0
    MIN_CONFIDENCE = 0.60
    MAX_CORRELATION_EXPOSURE_PCT = 0.30
    MAX_LEVERAGE_EU = 1
    DEFAULT_LEVERAGE = 1
    TRADE_COOLDOWN_SECONDS = 0
    CIRCUIT_BREAKER_HOURS = 2.0
    BREAKEVEN_AFTER_FIRST_TP = True
    TRADING_SYMBOLS = ("BTCUSDT",)
    LOOP_INTERVAL_SECONDS = 0.01
    ENABLE_HEALTH_SERVER = False
    HEALTHCHECK_PORT = 18163
    LIVE_TRADING_ACK = ""


@pytest.fixture()
def exchange():
    return FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=EQUITY)


def build(tmp_path, exchange, cfg=None, strategy=None):
    cfg = cfg or Cfg()
    store = StateStore(str(tmp_path / "state.db"))
    store.update_equity(EQUITY)
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg,
    )
    return m.TradingBot(config=cfg, store=store, client=client,
                        risk_manager=risk, engine=engine, strategy=strategy)


def summary(**over):
    """A well-formed edge_measurement summary, POSITIVE unless overridden."""
    base = {
        "symbol": "BTCUSD", "bars": 2564, "signal": "some_future_signal",
        "observed": {"n_trades": 77, "mean_r": 0.4274},
        "m1": {"percentile": 97.0, "bar": 95.0, "passed": True},
        "m2": {"percentile": 96.0, "bar": 95.0, "passed": True,
               "delta": 0.269, "ci_low": 0.24, "ci_high": 0.30},
        "control_validated": True,
        "m3": {"folds": 4, "positive": 4, "soft_gate": 3, "soft_gate_met": True},
        "verdict": "EDGE_EVIDENCE_POSITIVE",
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


@contextlib.contextmanager
def _refusals_disabled(*signals):
    """Temporarily lift every reason the hook has to refuse `signals`.

    A guard that would refuse anyway for some other reason is not a guard, so a
    test that wants to prove a refusal is load-bearing has to be able to remove
    it and watch the answer change.

    Slice 55 only needed the universe mappings lifted. Slice 56 froze
    `funding_carry_fade_v1`, so the same artefact is now refused TWICE — and a
    helper that lifted only the universe rules would silently start proving
    nothing. Both are lifted here, and the freeze is lifted per-name so a test
    cannot accidentally disable the whole deny-list.
    """
    dual, multi = ps.DUAL_SYMBOL_REQUIREMENTS, ps.MULTI_SYMBOL_MINIMUMS
    frozen_map, frozen_tuple = ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS
    thinner = {k: v for k, v in frozen_map.items() if k not in signals}
    ps.DUAL_SYMBOL_REQUIREMENTS, ps.MULTI_SYMBOL_MINIMUMS = {}, {}
    ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS = thinner, tuple(thinner)
    try:
        yield
    finally:
        ps.DUAL_SYMBOL_REQUIREMENTS, ps.MULTI_SYMBOL_MINIMUMS = dual, multi
        ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS = frozen_map, frozen_tuple


@contextlib.contextmanager
def _refusals_disabled_except_universe(*signals):
    """Lift the FREEZE for `signals`, leaving the universe rules in force.

    Used to test what the k-of-n rule says when it is the guard actually doing
    the refusing. Without this the deny-list short-circuits first and the
    universe rule's message would never be emitted — the test would pass on the
    wrong log line.
    """
    frozen_map, frozen_tuple = ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS
    thinner = {k: v for k, v in frozen_map.items() if k not in signals}
    ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS = thinner, tuple(thinner)
    try:
        yield
    finally:
        ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS = frozen_map, frozen_tuple


def write_summary(directory, name="x_summary.json", **over):
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary(**over), handle)
    return path


# ---------------------------------------------------------------------------


class TestTheSnapshotTellsTheTruth:

    def test_every_field_is_present_and_correctly_typed(self):
        status = ps.current(Cfg())
        expected = {
            "timing_skill_research": str,
            # Was type(None) until slice 57 registered one under a
            # pre-declared OOS gate. The FIELD's contract is unchanged; what
            # changed is that the repository now has something to put in it.
            # registration_invariant keeps the VALUE honest.
            "cleared_edge_signal": (str, type(None)),
            "execution_mode": str,
            "policy_mode": str,
            "no_edge_claim": str,
            "entries_enabled": bool,
            "live_authorized": bool,
            "models_current_present": bool,
        }
        record = status.as_dict()
        assert set(record) == set(expected)
        for name, kind in expected.items():
            assert isinstance(record[name], kind), (name, record[name])

    def test_the_stock_values(self):
        status = ps.current(_config.load({}))
        assert status.timing_skill_research == "CLOSED"
        # Was None until slice 57 registered one under a pre-declared OOS
        # gate. Pinned to what the hook actually returns, with
        # registration_invariant keeping that value honest — a frozen name or
        # an ungated name here is still a failure.
        assert status.cleared_edge_signal == ri.stock_cleared_edge_signal()
        ri.assert_registration_is_sound()
        assert status.execution_mode == "paper"
        assert status.policy_mode == "off"
        assert status.live_authorized is False
        assert status.models_current_present is False

    def test_the_summary_line_states_the_mode(self):
        line = ps.current(Cfg()).summary_line()
        assert "timing_skill_research=CLOSED" in line
        expected = ri.stock_cleared_edge_signal() or "none"
        assert f"cleared_edge_signal={expected}" in line
        assert "execution_mode=paper" in line
        assert sl.NO_EDGE_CLAIM in line

    def test_entries_enabled_is_reported_not_assumed(self):
        cfg = Cfg()
        cfg.ENTRIES_ENABLED = False
        assert ps.current(cfg).entries_enabled is False

    def test_a_missing_config_does_not_crash_and_reports_paper(self):
        """Fail closed: no config must never read as armed."""
        status = ps.current(None)
        assert status.execution_mode == "paper"
        assert status.live_authorized is False
        assert status.policy_mode == "off"


class TestNoAbsentSignalCanBeClearedEdge:
    """The load-bearing test in this file."""

    def test_the_live_snapshot_names_neither(self):
        assert ps.current(_config.load({})).cleared_edge_signal not in ps.ABSENT_SIGNALS

    def test_the_real_artifacts_directory_yields_none(self):
        """Pointed at every artefact this project has ever produced: None.

        This is the whole claim of the slice, measured rather than asserted.
        """
        assert os.path.isdir(ARTIFACTS)
        summaries = [n for n in os.listdir(ARTIFACTS) if n.endswith("_summary.json")]
        assert summaries, "no edge summaries found — the test would be vacuous"
        ri.assert_registration_is_sound(ARTIFACTS)

    def test_no_real_summary_can_carry_a_claim(self):
        """WHY the answer is None — and it is no longer one single reason.

        Until slice 35 every artefact recorded ABSENT, so "nothing passed" was
        the whole explanation. Slice 35 produced a genuine
        `EDGE_EVIDENCE_POSITIVE` on ETH (M1 97.3 / M2 98.0) from a run whose
        directed control had FAILED. It is still on disk, deliberately.

        Slice 55 added the third reason, and it is the strongest of them.
        `slice55_edge_funding_carry_fade_BTCUSDT_summary.json` is POSITIVE at
        97.0 / 97.5 on 85 trades **with** a validated-control attestation:
        genuine, unforged, and measured with a ruler that passed its own
        pre-declared check. It registers nothing because
        `funding_carry_fade_v1`'s design note (EDGE.md §36e, committed before
        any number existed) declared a three-symbol universe needing two, and
        ETHUSDT (47.7 / 48.5) and SOLUSDT (2.3 / 1.0) did not clear.

        This test previously asserted the disjunction "ABSENT, or POSITIVE
        without an attestation", and its own docstring said that if an attested
        POSITIVE ever appeared it would be a human decision rather than a test
        fix. It appeared. The assertion is therefore **narrowed, not dropped**:
        an attested POSITIVE is tolerated ONLY where a registered universe rule
        explains why it does not promote, and the class-level assertion that the
        hook returns None still stands next to it. A POSITIVE + attested
        artefact for a signal with no universe rule, or one whose universe rule
        is actually satisfied, still fails here — which is exactly the case that
        would be a genuine cleared edge and a human's call.
        """
        seen = 0
        attested_positives = []
        for name in sorted(os.listdir(ARTIFACTS)):
            if not name.endswith("_summary.json"):
                continue
            seen += 1
            with open(os.path.join(ARTIFACTS, name), encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("verdict") != "EDGE_EVIDENCE_POSITIVE":
                continue
            if data.get("control_validated") is not True:
                continue
            signal = str(data.get("signal") or "")
            attested_positives.append((name, signal))
            assert (signal in ps.MULTI_SYMBOL_MINIMUMS
                    or signal in ps.DUAL_SYMBOL_REQUIREMENTS
                    or signal in ps.OOS_GATED_REGISTRATION
                    or signal in ps.ABSENT_SIGNALS), (
                f"{name} claims a cleared edge with an attested control and "
                f"{signal!r} is under no universe rule, no OOS gate and no "
                "freeze, so nothing stops it registering; if that is genuine "
                "it is a human decision, not a test fix")
        assert seen, "no summaries found — the test would be vacuous"
        # And the reason above is load-bearing rather than decorative: with the
        # universe rules removed, at least one of these WOULD register. If this
        # ever stops being true the narrowing above has gone slack.
        if attested_positives:
            ri.assert_registration_is_sound(ARTIFACTS)
            # Every attested POSITIVE that is NOT the one permitted to register
            # must be held back by something real. Lifting its guards has to
            # change the answer, or those guards are decorative. The permitted
            # one is excluded because it is not being held back at all — it
            # cleared, and battery 3 of tools/registration_discipline.py breaks
            # each clause of ITS gate in turn instead.
            blocked = [(name, signal) for name, signal in attested_positives
                       if signal != ri.PERMITTED_CLEARED_SIGNAL]
            # One at a time, each in a directory holding only its own artefact.
            # Lifting several at once and reading the whole directory would
            # produce TWO competing claims, the hook would refuse to adjudicate
            # between them, and the answer would come back None for a reason
            # that has nothing to do with the guard under test — the check
            # would silently stop checking anything.
            for name, signal in blocked:
                with tempfile.TemporaryDirectory() as solo:
                    shutil.copy(os.path.join(ARTIFACTS, name),
                                os.path.join(solo, "x_summary.json"))
                    with _refusals_disabled(signal):
                        promoted = ps.cleared_edge_signal_from_artifacts(solo)
                assert promoted == signal, (
                    f"{name} is an attested POSITIVE for {signal!r}, but "
                    "lifting its universe rule and its freeze changes nothing, "
                    "so neither is what is holding the claim back and this "
                    "test proves nothing")

    def test_an_absent_signal_is_refused_even_if_an_artefact_claims_positive(
        self, tmp_path
    ):
        """A forged artefact must not be able to promote a measured failure.

        A summary that says POSITIVE for `donchian_breakout_v1` contradicts
        `artifacts/slice29_edge_donchian_regression_summary.json`, which recorded
        91.2/91.5. Whatever produced it, the answer is no.
        """
        write_summary(tmp_path, signal="donchian_breakout_v1")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    @pytest.mark.parametrize("signal", ps.ABSENT_SIGNALS)
    def test_no_absent_signal_can_be_promoted_by_any_artefact(self, tmp_path, signal):
        """Belt to the braces on the bars check.

        Without this, one hand-written JSON file dropped into `artifacts/` would
        be enough to make the process announce a cleared edge for a signal that
        scored 91.2 against a bar of 95.
        """
        write_summary(tmp_path, signal=signal)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_module_has_no_setter_or_override_for_the_field(self):
        """No setter, no force/override parameter — checked by AST.

        Not by text: the module docstring says "no override ... no force
        argument" precisely to explain the absence, and a raw-text ban would
        force it to stop saying so.
        """
        tree = ast.parse(inspect.getsource(ps))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert "cleared" not in node.name or node.name == \
                    "cleared_edge_signal_from_artifacts", node.name
                names = [a.arg for a in node.args.args + node.args.kwonlyargs]
                for arg in names:
                    assert arg.lower() not in {"force", "override", "unsafe"}, \
                        (node.name, arg)

    def test_no_environment_variable_can_influence_it(self):
        """The module must not read the environment at all."""
        tree = ast.parse(inspect.getsource(ps))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"getenv", "environ"}, node.attr

    def test_no_module_assigns_the_field_directly(self):
        """Only the dataclass constructor may populate it."""
        for name in sorted(os.listdir(REPO)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(REPO, name), encoding="utf-8") as handle:
                try:
                    tree = ast.parse(handle.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute):
                            assert target.attr != "cleared_edge_signal", name


class TestTheRegistrationHookRefuses:

    def test_it_accepts_only_a_genuine_positive(self, tmp_path):
        """The control. Without it, "always None" could mean "always broken"."""
        write_summary(tmp_path)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) == \
            "some_future_signal"

    @pytest.mark.parametrize("verdict", [
        "EDGE_EVIDENCE_ABSENT", "INCONCLUSIVE", "", "positive",
        "edge_evidence_positive",
    ])
    def test_only_the_exact_positive_verdict_counts(self, tmp_path, verdict):
        write_summary(tmp_path, verdict=verdict)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    @pytest.mark.parametrize("m1_pct", [94.9, 91.2, 76.1, 0.0])
    def test_a_near_miss_on_m1_is_refused(self, tmp_path, m1_pct):
        write_summary(tmp_path, m1={"percentile": m1_pct})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    @pytest.mark.parametrize("m2_pct", [94.9, 91.5, 77.5, 0.0])
    def test_a_near_miss_on_m2_is_refused(self, tmp_path, m2_pct):
        write_summary(tmp_path, m2={"percentile": m2_pct})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_m1_alone_cannot_carry_a_claim(self, tmp_path):
        """M2 is mandatory. A long-only rule on an appreciating asset ranks
        well against nulls that move entries without removing exposure."""
        write_summary(tmp_path, m1={"percentile": 99.9},
                      m2={"percentile": 99.9, "passed": False})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_bars_match_the_measurement_tool(self):
        source = open(os.path.join(REPO, "tools", "edge_measurement.py"),
                      encoding="utf-8").read()
        assert f"M1_PERCENTILE_BAR = {ps.M1_BAR}" in source
        assert f"M2_PERCENTILE_BAR = {ps.M2_BAR}" in source

    @pytest.mark.parametrize("payload", ["{not json", "[]", "null", "{}",
                                         '{"verdict": "EDGE_EVIDENCE_POSITIVE"}'])
    def test_a_malformed_artefact_is_skipped_not_guessed_at(self, tmp_path, payload):
        with open(tmp_path / "bad_summary.json", "w", encoding="utf-8") as handle:
            handle.write(payload)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_a_missing_directory_is_none(self):
        assert ps.cleared_edge_signal_from_artifacts("/no/such/dir") is None
        assert ps.cleared_edge_signal_from_artifacts("") is None

    def test_an_unnamed_signal_is_refused(self, tmp_path):
        write_summary(tmp_path, signal="")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_two_competing_claims_refuse_rather_than_choose(self, tmp_path):
        """A state for a human to adjudicate, not to resolve by picking one."""
        write_summary(tmp_path, name="a_summary.json", signal="alpha")
        write_summary(tmp_path, name="b_summary.json", signal="beta")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_files_that_are_not_summaries_are_ignored(self, tmp_path):
        with open(tmp_path / "notes.txt", "w") as handle:
            handle.write("EDGE_EVIDENCE_POSITIVE")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None


class TestAValidatedControlIsRequired:
    """Slice 35: the hook accepted a real POSITIVE from an unvalidated ruler.

    `btc_alt_spillover_v1` produced a genuine, unforged
    `EDGE_EVIDENCE_POSITIVE` on ETH (M1 97.3 / M2 98.0) from a run whose
    directed control had FAILED its pre-declared validation — and whose
    companion symbol had failed outright at 85.1 / 81.5. The hook checked the
    verdict and the bars, found both fine, and `ProjectStatus` announced a
    cleared edge.

    Checking the number is not enough. The ruler has to have been checked too.
    """

    def test_a_positive_without_the_attestation_is_refused(self, tmp_path):
        write_summary(tmp_path, control_validated=False)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_a_missing_attestation_field_is_refused(self, tmp_path):
        payload = summary()
        payload.pop("control_validated", None)
        with open(os.path.join(str(tmp_path), "x_summary.json"), "w") as handle:
            json.dump(payload, handle)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    @pytest.mark.parametrize("value", ["true", 1, "yes", [], {}, None])
    def test_only_a_real_boolean_true_counts(self, tmp_path, value):
        """`is not True` — a truthy string must not arm a claim."""
        write_summary(tmp_path, control_validated=value)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_attested_control_is_accepted(self, tmp_path):
        """The control: without this, "always refuses" could mean "is broken"."""
        write_summary(tmp_path, control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) == \
            "some_future_signal"

    def test_the_real_slice35_artefact_is_refused(self):
        """The specific artefact that caused this, still on disk."""
        path = os.path.join(
            ARTIFACTS, "slice35_edge_btc_alt_spillover_ETHUSDT_summary.json")
        if not os.path.exists(path):
            pytest.skip("slice 35 artefact not present")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["verdict"] == "EDGE_EVIDENCE_POSITIVE"
        assert payload["m1"]["percentile"] >= 95.0
        assert payload["m2"]["percentile"] >= 95.0
        assert payload["control_validated"] is False
        ri.assert_registration_is_sound(ARTIFACTS)


class TestTheDualSymbolRuleIsEnforcedInCode:
    """Slice 37: the anti-cherry-pick rule used to live only in prose.

    `btc_alt_spillover_v1`'s intake declared a two-symbol universe and said one
    symbol passing while the other fails is ABSENT. The hook did not know that.
    Slice 35 produced exactly that shape — ETH 97.3/98.0, SOL 85.1/81.5 — so the
    rule is now enforced rather than trusted to a reader.
    """

    def test_the_requirement_is_declared(self):
        assert ps.DUAL_SYMBOL_REQUIREMENTS["btc_alt_spillover_v1"] == \
            ("ETHUSDT", "SOLUSDT")

    def test_one_symbol_alone_cannot_register(self, tmp_path):
        write_summary(tmp_path, name="eth_summary.json",
                      signal="btc_alt_spillover_v1", symbol="ETHUSDT",
                      control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_both_symbols_together_are_now_refused_by_the_freeze(self, tmp_path):
        """Slice 41 inverted this test, and that inversion is the freeze.

        Until slice 40 this asserted that two qualifying artefacts DID
        register — the control proving "always refuses" did not mean "is
        broken". `btc_alt_spillover_v1` has since been measured ABSENT on the
        full history under a validated control and frozen by human decision, so
        the dual-symbol rule is no longer the last gate it has to pass: the
        deny-list refuses it first, whatever the artefacts say.

        The proof that the machinery still works is
        `test_the_dual_symbol_rule_still_registers_a_signal_that_qualifies`
        below, which exercises it on a name that is not frozen.
        """
        for sym in ("ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{sym}_summary.json",
                          signal="btc_alt_spillover_v1", symbol=sym,
                          control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_dual_symbol_rule_still_registers_a_signal_that_qualifies(
            self, tmp_path, monkeypatch):
        """The guard keeps its teeth after the freeze.

        Freezing the only name in DUAL_SYMBOL_REQUIREMENTS would otherwise
        leave the rule permanently short-circuited and untested — every case
        passing for the wrong reason. So the mechanism is exercised on a
        hypothetical future signal instead.
        """
        monkeypatch.setattr(ps, "DUAL_SYMBOL_REQUIREMENTS",
                            {"some_future_signal": ("ETHUSDT", "SOLUSDT")})
        write_summary(tmp_path, name="eth_summary.json",
                      signal="some_future_signal", symbol="ETHUSDT",
                      control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

        write_summary(tmp_path, name="sol_summary.json",
                      signal="some_future_signal", symbol="SOLUSDT",
                      control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) == \
            "some_future_signal"

    def test_a_failing_companion_still_blocks_it(self, tmp_path):
        """ETH POSITIVE, SOL below the bars — the actual slice-35 shape.

        Refused twice over since slice 41: by the freeze and by the missing
        companion. Belt and braces are the point.
        """
        write_summary(tmp_path, name="eth_summary.json",
                      signal="btc_alt_spillover_v1", symbol="ETHUSDT",
                      control_validated=True)
        write_summary(tmp_path, name="sol_summary.json",
                      signal="btc_alt_spillover_v1", symbol="SOLUSDT",
                      control_validated=True,
                      m1={"percentile": 85.1}, m2={"percentile": 81.5,
                                                   "passed": False},
                      verdict="EDGE_EVIDENCE_ABSENT")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_a_signal_with_no_declared_universe_is_unaffected(self, tmp_path):
        write_summary(tmp_path, signal="some_future_signal", symbol="BTCUSD",
                      control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) == \
            "some_future_signal"

    def test_the_missing_symbol_is_named_in_the_log(self, tmp_path, caplog):
        import logging
        write_summary(tmp_path, name="eth_summary.json",
                      signal="btc_alt_spillover_v1", symbol="ETHUSDT",
                      control_validated=True)
        with caplog.at_level(logging.WARNING):
            ps.cleared_edge_signal_from_artifacts(str(tmp_path))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "SOLUSDT" in blob


class TestTheKOfNUniverseRule:
    """Slice 55: the same failure as slice 37, one mapping shape later.

    `funding_carry_fade_v1`'s design note (EDGE.md §36e, committed at d18a3b8
    BEFORE any number existed) declared a three-symbol universe and required at
    least two of them to clear both bars, and said in terms that single-symbol
    claims are refused.

    Slice 55 then measured all three. BTCUSDT cleared at 97.0 / 97.5 on 85
    trades under a VALID control; ETHUSDT read 47.7 / 48.5 and SOLUSDT read
    2.3 / 1.0. The BTCUSDT artefact is **genuine and unforged** — and with only
    the all-of mapping in place it was enough on its own: the hook returned
    "funding_carry_fade_v1" and `ProjectStatus` announced a cleared edge.

    That is slice 35's failure recurring under a new name: a real positive on
    one symbol, its companions failing, and a universe rule that existed only
    in prose. These tests are the enforcement.
    """

    SIGNAL_UNDER_FREEZE = "funding_carry_fade_v1"

    def test_the_minimum_is_declared_and_matches_the_design_note(self):
        universe, floor = ps.MULTI_SYMBOL_MINIMUMS["funding_carry_fade_v1"]
        assert universe == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        assert floor == 2

    def test_the_real_slice55_btc_artefact_alone_registers_nothing(self):
        """The specific artefact that caused this, on disk, unedited.

        Slice 56 froze the family, so this artefact is now refused TWICE — by
        the deny-list first and by this universe rule second. The assertion
        `signal not in ABSENT_SIGNALS` was true when the test was written and is
        deliberately inverted rather than deleted: which guard catches it has
        changed, that it is caught has not, and the artefact itself is
        untouched.
        """
        path = os.path.join(
            ARTIFACTS, "slice55_edge_funding_carry_fade_BTCUSDT_summary.json")
        if not os.path.exists(path):
            pytest.skip("slice 55 artefact not present")
        with open(path, encoding="utf-8") as handle:
            real = json.load(handle)
        # It really is a qualifying artefact in every other respect. If this
        # block ever stops holding, this test has become vacuous.
        assert real["verdict"] == "EDGE_EVIDENCE_POSITIVE"
        assert real["m1"]["percentile"] >= ps.M1_BAR
        assert real["m2"]["percentile"] >= ps.M2_BAR
        assert real["m2"]["passed"] is True
        assert real["control_validated"] is True
        assert real["signal"] in ps.ABSENT_SIGNALS          # frozen, slice 56
        assert real["signal"] in ps.MULTI_SYMBOL_MINIMUMS   # and out-numbered
        ri.assert_registration_is_sound(ARTIFACTS)

    def test_the_universe_rule_alone_still_refuses_it_without_the_freeze(self):
        """The slice-55 property, preserved after the slice-56 freeze.

        Lifting the freeze must NOT be enough to register a single-symbol
        clear. If this ever goes red, the k-of-n rule has quietly stopped being
        what refused the artefact in the first place and the freeze is carrying
        it alone.
        """
        with _refusals_disabled_except_universe(self.SIGNAL_UNDER_FREEZE):
            ri.assert_registration_is_sound(ARTIFACTS)

    def test_one_of_three_is_refused(self, tmp_path):
        write_summary(tmp_path, name="btc_summary.json",
                      signal="funding_carry_fade_v1", symbol="BTCUSDT",
                      control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_two_of_three_are_now_refused_by_the_freeze(self, tmp_path):
        """Slice 56 inverted this test, and that inversion IS the freeze.

        Until slice 56 this asserted that two qualifying artefacts DID register
        — the control proving "always refuses" did not mean "is broken".
        `funding_carry_fade_v1` has since been frozen ABSENT by human decision,
        so the k-of-n rule is no longer the last gate it must pass: the
        deny-list refuses it first, however many symbols a forger supplies.

        The proof that the k-of-n machinery still works is
        `test_the_k_of_n_rule_still_registers_a_signal_that_qualifies` below,
        which exercises it on a name that is not frozen. Same shape as slice
        41's inversion of the dual-symbol tests.
        """
        for sym in ("BTCUSDT", "ETHUSDT"):
            write_summary(tmp_path, name=f"{sym}_summary.json",
                          signal="funding_carry_fade_v1", symbol=sym,
                          control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_k_of_n_rule_still_registers_a_signal_that_qualifies(
            self, tmp_path, monkeypatch):
        """The guard keeps its teeth after the freeze.

        Freezing the only name in MULTI_SYMBOL_MINIMUMS would otherwise leave
        the rule permanently short-circuited and untested — every case passing
        for the wrong reason. So the mechanism is exercised on a hypothetical
        future signal instead.
        """
        monkeypatch.setattr(
            ps, "MULTI_SYMBOL_MINIMUMS",
            {"some_future_signal": (("BTCUSDT", "ETHUSDT", "SOLUSDT"), 2)})
        write_summary(tmp_path, name="btc_summary.json",
                      signal="some_future_signal", symbol="BTCUSDT",
                      control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

        write_summary(tmp_path, name="eth_summary.json",
                      signal="some_future_signal", symbol="ETHUSDT",
                      control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) == \
            "some_future_signal"

    def test_a_symbol_outside_the_declared_universe_does_not_count(self, tmp_path):
        """Two artefacts, only one of them from the pre-declared universe."""
        write_summary(tmp_path, name="btc_summary.json",
                      signal="funding_carry_fade_v1", symbol="BTCUSDT",
                      control_validated=True)
        write_summary(tmp_path, name="doge_summary.json",
                      signal="funding_carry_fade_v1", symbol="DOGEUSDT",
                      control_validated=True)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_a_failing_companion_does_not_count_toward_the_minimum(self, tmp_path):
        """The real slice-55 shape: one clears, two fail their bars."""
        write_summary(tmp_path, name="btc_summary.json",
                      signal="funding_carry_fade_v1", symbol="BTCUSDT",
                      control_validated=True)
        for sym, m1, m2 in (("ETHUSDT", 47.7, 48.5), ("SOLUSDT", 2.3, 1.0)):
            write_summary(tmp_path, name=f"{sym}_summary.json",
                          signal="funding_carry_fade_v1", symbol=sym,
                          control_validated=True,
                          m1={"percentile": m1, "passed": False},
                          m2={"percentile": m2, "passed": False},
                          verdict="EDGE_EVIDENCE_ABSENT")
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_an_unvalidated_control_does_not_count_toward_the_minimum(self, tmp_path):
        """Two POSITIVE artefacts, one measured with an unchecked ruler."""
        write_summary(tmp_path, name="btc_summary.json",
                      signal="funding_carry_fade_v1", symbol="BTCUSDT",
                      control_validated=True)
        write_summary(tmp_path, name="eth_summary.json",
                      signal="funding_carry_fade_v1", symbol="ETHUSDT",
                      control_validated=False)
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_refusal_names_the_shortfall(self, tmp_path, caplog):
        """With the freeze lifted for this one name, so the k-of-n message is
        the one under test rather than the deny-list's."""
        import logging
        write_summary(tmp_path, name="btc_summary.json",
                      signal="funding_carry_fade_v1", symbol="BTCUSDT",
                      control_validated=True)
        with _refusals_disabled_except_universe("funding_carry_fade_v1"):
            with caplog.at_level(logging.WARNING):
                ps.cleared_edge_signal_from_artifacts(str(tmp_path))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "funding_carry_fade_v1" in blob
        assert "BTCUSDT" in blob
        assert "at least 2" in blob


class TestEveryDeclaredUniverseIsEnforcedInCode:
    """The generalisation, which is the part that matters after this slice.

    Twice now a multi-symbol pre-declaration has lived only in prose while the
    hook happily registered a single symbol — `btc_alt_spillover_v1` in slice
    35, `funding_carry_fade_v1` in slice 55. Both were caught by a human
    reading the artefact rather than by anything in the repository.

    A rule that is not transcribed into `project_status` is not enforced by
    anything. So this walks the stage-1 records on disk and asserts that every
    family declaring a multi-symbol requirement has that requirement registered
    in one of the two mappings. The next family cannot repeat this silently.
    """

    def _stage1_records(self):
        out = []
        if not os.path.isdir(ARTIFACTS):
            return out
        for name in sorted(os.listdir(ARTIFACTS)):
            if not (name.startswith("slice") and "_stage1_" in name
                    and name.endswith(".json")):
                continue
            with open(os.path.join(ARTIFACTS, name), encoding="utf-8") as h:
                try:
                    record = json.load(h)
                except ValueError:
                    continue
            if isinstance(record, dict):
                out.append((name, record))
        return out

    def test_there_are_stage1_records_to_check(self):
        """Otherwise this whole class passes by finding nothing."""
        assert self._stage1_records()

    def test_every_multi_symbol_declaration_is_registered(self):
        unenforced = []
        for name, record in self._stage1_records():
            required = record.get("positive_rule_symbols_required")
            signal = str(record.get("signal") or "")
            if not signal or not isinstance(required, int) or required < 2:
                continue
            if (signal in ps.DUAL_SYMBOL_REQUIREMENTS
                    or signal in ps.MULTI_SYMBOL_MINIMUMS
                    or signal in ps.ABSENT_SIGNALS):
                continue
            unenforced.append(f"{signal} (declared in {name})")
        assert not unenforced, (
            "these families declared a multi-symbol POSITIVE rule that no "
            "mapping in project_status enforces, so a single qualifying "
            "artefact would register a cleared edge: " + "; ".join(unenforced))


class TestLiveIsNotArmed:

    def test_stock_env_is_paper(self):
        status = ps.current(_config.load({}))
        assert status.live_authorized is False
        assert status.execution_mode == "paper"

    @pytest.mark.parametrize("env", [
        {"USE_TESTNET": "0", "PAPER_TRADING": "0"},
        {"USE_TESTNET": "0", "PAPER_TRADING": "0", "LIVE_TRADING_ACK": "yes"},
        {"USE_TESTNET": "0", "PAPER_TRADING": "0",
         "LIVE_TRADING_ACK": "I_UNDERSTAND"},
        {"USE_TESTNET": "0", "PAPER_TRADING": "0", "BYBIT_API_KEY": "k",
         "BYBIT_API_SECRET": "s"},
    ])
    def test_partial_arming_never_reports_live(self, env):
        status = ps.current(_config.load(env))
        assert status.live_authorized is False
        assert status.execution_mode == "paper"

    def test_execution_mode_is_derived_not_configurable(self):
        """There must be no EXECUTION_MODE key anybody can set."""
        assert not hasattr(_config.load({}), "EXECUTION_MODE")
        source = inspect.getsource(ps)
        assert 'getattr(config, "EXECUTION_MODE"' not in source


class TestNoPromotedModel:

    def test_models_current_does_not_exist(self):
        assert not os.path.exists(os.path.join(REPO, "models", "current"))
        assert ps.current(Cfg()).models_current_present is False

    def test_the_field_reflects_the_filesystem(self, tmp_path):
        os.makedirs(tmp_path / "models" / "current")
        status = ps.current(Cfg(), repo_root=str(tmp_path))
        assert status.models_current_present is True

    def test_policy_mode_is_off(self):
        assert ps.current(_config.load({})).policy_mode == "off"


class TestTheClaimIsNotSilentlyEditable:

    def test_it_is_the_session_log_constant_not_a_copy(self):
        assert ps.current(Cfg()).no_edge_claim is sl.NO_EDGE_CLAIM

    def test_the_string_is_exact(self):
        assert sl.NO_EDGE_CLAIM == "NO EDGE CLAIM — timing-skill research CLOSED"

    def test_the_module_does_not_retype_it(self):
        source = inspect.getsource(ps)
        assert "NO EDGE CLAIM —" not in source, (
            "the claim must be imported from session_log, not re-declared; two "
            "copies of one sentence is how they drift apart")

    def test_research_closed_is_a_constant(self):
        assert ps.RESEARCH_CLOSED == "CLOSED"


class TestItIsFrozen:

    def test_a_field_cannot_be_reassigned(self):
        status = ps.current(Cfg())
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.cleared_edge_signal = "donchian_breakout_v1"

    @pytest.mark.parametrize("field", ["timing_skill_research", "execution_mode",
                                       "live_authorized", "no_edge_claim"])
    def test_no_field_can_be_reassigned(self, field):
        status = ps.current(Cfg())
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(status, field, "anything")

    def test_mutating_the_dict_does_not_mutate_the_snapshot(self):
        status = ps.current(Cfg())
        record = status.as_dict()
        record["cleared_edge_signal"] = "donchian_breakout_v1"
        # Was None until slice 57 registered one under a pre-declared OOS
        # gate. Pinned to what the hook actually returns, with
        # registration_invariant keeping that value honest — a frozen name or
        # an ungated name here is still a failure.
        assert status.cleared_edge_signal == ri.stock_cleared_edge_signal()
        ri.assert_registration_is_sound()


class TestItRefusesToBecomeAPerformanceReport:

    def test_no_field_name_looks_like_a_metric(self):
        for name in ps.field_names():
            for marker in ps.FORBIDDEN_FIELD_MARKERS:
                assert marker not in name.lower(), (name, marker)

    def test_the_dataclass_source_declares_no_metric_field(self):
        tree = ast.parse(inspect.getsource(ps.ProjectStatus))
        declared = [node.target.id for node in ast.walk(tree)
                    if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)]
        assert declared, "no annotated fields found"
        for name in declared:
            for marker in ps.FORBIDDEN_FIELD_MARKERS:
                assert marker not in name.lower(), (name, marker)

    def test_no_credential_is_read(self):
        banned = {"BYBIT_API_KEY", "BYBIT_API_SECRET", "api_key", "api_secret",
                  "signature"}
        tree = ast.parse(inspect.getsource(ps))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned, node.attr
            if isinstance(node, ast.Call) and getattr(
                    node.func, "id", None) == "getattr":
                for arg in node.args[1:2]:
                    if isinstance(arg, ast.Constant):
                        assert arg.value not in banned, arg.value

    def test_no_credential_appears_in_a_snapshot(self):
        cfg = Cfg()
        cfg.BYBIT_API_KEY = "canary-key-31"
        cfg.BYBIT_API_SECRET = "canary-secret-31"
        blob = json.dumps(ps.current(cfg).as_dict(), ensure_ascii=False)
        assert "canary-key-31" not in blob
        assert "canary-secret-31" not in blob


class TestStartupAndHealthExposeIt:

    def test_the_health_payload_carries_the_snapshot(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()
        project = bot.health()["project"]
        assert project["timing_skill_research"] == "CLOSED"
        assert project["cleared_edge_signal"] == ri.stock_cleared_edge_signal()
        ri.assert_registration_is_sound()
        assert project["execution_mode"] == "paper"
        assert project["no_edge_claim"] == sl.NO_EDGE_CLAIM
        bot.shutdown()

    def test_startup_logs_the_mode(self, tmp_path, exchange, caplog):
        import logging
        bot = build(tmp_path, exchange, strategy=None)
        with caplog.at_level(logging.WARNING):
            bot.startup()
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "PROJECT MODE" in blob
        assert "timing_skill_research=CLOSED" in blob
        expected = ri.stock_cleared_edge_signal() or "none"
        assert f"cleared_edge_signal={expected}" in blob
        bot.shutdown()

    def test_the_session_record_carries_the_mode(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()
        record = bot.session.record()
        assert record["timing_skill_research"] == "CLOSED"
        assert record["cleared_edge_signal"] == ri.stock_cleared_edge_signal()
        assert record["no_edge_claim"] == sl.NO_EDGE_CLAIM
        bot.shutdown()

    def test_the_health_payload_still_has_no_metric_field(self, tmp_path, exchange):
        bot = build(tmp_path, exchange, strategy=None)
        bot.startup()
        for name in bot.health()["project"]:
            for marker in ps.FORBIDDEN_FIELD_MARKERS:
                assert marker not in name.lower(), name
        bot.shutdown()


class TestTheOperatorToolAgrees:

    def test_it_exits_zero_and_emits_the_declared_fields(self, capsys):
        import print_project_status as tool
        rc = tool.main(["--artifact-dir", ARTIFACTS, "--quiet"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["timing_skill_research"] == "CLOSED"
        assert payload["cleared_edge_signal"] == ri.stock_cleared_edge_signal()
        assert payload["execution_mode"] == "paper"
        assert payload["models_current_present"] is False

    def test_it_flags_an_incoherent_snapshot(self, tmp_path, capsys, monkeypatch):
        """The tripwire fires if an ABSENT signal ever appears as cleared."""
        import print_project_status as tool
        fake = ps.ProjectStatus(
            timing_skill_research="CLOSED",
            cleared_edge_signal="donchian_breakout_v1",
            execution_mode="paper", policy_mode="off",
            no_edge_claim=sl.NO_EDGE_CLAIM, entries_enabled=True,
            live_authorized=False, models_current_present=False,
        )
        monkeypatch.setattr(tool.ps, "current", lambda *a, **k: fake)
        assert tool.main(["--quiet"]) == 1
        assert "INCOHERENT" in capsys.readouterr().err


class TestEntriesEnabledIsStillNotAGate:

    def test_the_risk_manager_never_names_it(self):
        import risk_management
        assert "ENTRIES_ENABLED" not in inspect.getsource(risk_management)

    def test_the_readers_are_exactly_the_four_expected(self):
        """`project_status.py` joins the list; nothing else may.

        AST, so `bybit_connection.py`'s explanatory comment is not miscounted.
        """
        readers = set()
        for name in sorted(os.listdir(REPO)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(REPO, name), encoding="utf-8") as handle:
                try:
                    tree = ast.parse(handle.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "ENTRIES_ENABLED":
                    readers.add(name)
                elif isinstance(node, ast.Constant) and node.value == "ENTRIES_ENABLED":
                    readers.add(name)
        assert readers == {"config.py", "main.py", "session_log.py",
                           "project_status.py"}, readers


class TestTheSlice41Freeze:
    """Slice 41 — `btc_alt_spillover_v1` is frozen ABSENT by human decision.

    It was measured on the full 1,461-date history under a control that passed
    its own pre-declared three-clause rule, and it read ABSENT on both symbols:
    ETH 94.3/92.0, SOL 91.3/90.0, against a bar of 95.0.

    94.3 is not a near miss and this class exists so no future session can
    treat it as one. The freeze refuses a POSITIVE artefact for this signal
    even when that artefact is otherwise perfect — right verdict, both bars
    cleared, `m2.passed`, a genuine control attestation, and both symbols
    present. Reopening it means deleting an entry from `FROZEN_ABSENT`, which
    is a visible act with a reviewer attached.
    """

    def test_the_signal_is_in_the_frozen_set(self):
        assert "btc_alt_spillover_v1" in ps.ABSENT_SIGNALS
        assert "btc_alt_spillover_v1" in ps.FROZEN_ABSENT

    def test_all_three_measured_families_are_frozen(self):
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1"):
            assert name in ps.ABSENT_SIGNALS, name

    def test_the_deny_list_and_its_evidence_cannot_disagree(self):
        """Derived, not duplicated — the two are the same object's keys."""
        assert ps.ABSENT_SIGNALS == tuple(ps.FROZEN_ABSENT)

    def test_every_frozen_signal_carries_its_numbers(self):
        """A deny-list is only as trustworthy as the evidence behind it.

        What counts as "its numbers" depends on how the name was closed, and
        slice 47 made that explicit. An ABSENT family must cite the bar its
        percentiles missed (95.0). An INCONCLUSIVE one has no percentiles, so
        quoting 95.0 would imply a comparison that never happened — it must
        cite the control clauses it failed instead.
        """
        for name, why in ps.FROZEN_ABSENT.items():
            assert len(why) > 60, name        # not a placeholder
            if ps.FROZEN_STATUS[name] == "ABSENT":
                assert "95" in why, name      # the bar it was measured against
            else:
                # An INCONCLUSIVE entry must say which symbols were not read
                # and point at logs a reader can open. What it must NOT do is
                # invent a percentile for a symbol that has none.
                #
                # Three of these families have no numbers at all. The fourth,
                # compression_breakout_v1, has ONE trusted reading on BTCUSD
                # and none on the alts, so a blanket "NO M1 and NO M2" is
                # false for it and a blanket "M1/M2 =" ban would delete the
                # one number worth keeping. The durable rule is narrower:
                # say NOT MEASURED, cite artifacts, and carry no more
                # percentile pairs than the family actually has.
                assert "NOT MEASURED" in why, name
                assert "artifacts/" in why, name
                assert "NO M1 AND NO M2" in why.upper(), name

    def test_the_spillover_evidence_names_the_slice_40_numbers(self):
        why = ps.FROZEN_ABSENT["btc_alt_spillover_v1"]
        assert "94.3" in why and "92.0" in why
        assert "91.3" in why and "90.0" in why
        assert "control" in why.lower()

    def test_a_perfect_forged_positive_is_still_refused(self, tmp_path):
        """The decisive test. Everything a claim needs, and still no.

        Right verdict, M1 and M2 above the bars, `m2.passed`, a validated
        control, and BOTH declared symbols. Before the freeze this shape
        registered — `test_both_symbols_together_do_register` asserted exactly
        that until slice 41. The freeze is what refuses it now.
        """
        for sym in ("ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{sym}_summary.json",
                          signal="btc_alt_spillover_v1", symbol=sym,
                          control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_refusal_is_logged_with_its_reason(self, tmp_path, caplog):
        """An operator must not have to go looking for the closing numbers."""
        import logging
        for sym in ("ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{sym}_summary.json",
                          signal="btc_alt_spillover_v1", symbol=sym,
                          control_validated=True)
        with caplog.at_level(logging.ERROR):
            ps.cleared_edge_signal_from_artifacts(str(tmp_path))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "FROZEN" in blob
        assert "94.3" in blob

    def test_the_freeze_outranks_the_dual_symbol_rule(self, tmp_path):
        """Order matters: the deny-list must not be reachable only via it.

        With the dual-symbol requirement removed the artefacts would satisfy
        every other check, so this isolates the freeze as the thing refusing.
        """
        import pytest as _pytest
        monkey = _pytest.MonkeyPatch()
        monkey.setattr(ps, "DUAL_SYMBOL_REQUIREMENTS", {})
        try:
            write_summary(tmp_path, signal="btc_alt_spillover_v1",
                          symbol="ETHUSDT", control_validated=True)
            assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None
        finally:
            monkey.undo()

    def test_the_real_artifacts_directory_still_registers_nothing(self):
        """The repository as it actually stands, with slice-40 files present."""
        ri.assert_this_artefact_registers_nothing(
            ARTIFACTS, "btc_alt_spillover_v1")

    def test_the_slice_40_summaries_are_still_on_disk_and_still_absent(self):
        """Evidence stays. A freeze that deleted its own evidence is a cover-up."""
        for sym in ("ETHUSDT", "SOLUSDT"):
            path = os.path.join(
                ARTIFACTS, f"slice40_edge_btc_alt_spillover_{sym}_summary.json")
            assert os.path.exists(path), path
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
            assert summary["verdict"] == "EDGE_EVIDENCE_ABSENT"
            assert summary["control_validated"] is True

    def test_the_bars_were_not_lowered_to_reach_this_verdict(self):
        assert ps.M1_BAR == 95.0
        assert ps.M2_BAR == 95.0


class TestTheSlice44Freeze:
    """Slice 44 — `post_shock_fade_v1` is frozen ABSENT by human decision.

    BTCUSD read M1 1.6 / M2 2.0 against a bar of 95.0, on 41 trades, under a
    control that passed its own pre-declared three-clause rule. ETHUSDT and
    SOLUSDT were never measured: their controls were INVALID at 75.4% and 99.8%
    incomplete, and their 10 and 7 setups fall below the tool's 30-entry floor.
    The family verdict is INCONCLUSIVE under the intake's own multi-symbol
    rule, which needs two symbols at 50+ scored trades and got none.

    1.6 is the lowest reading any family has produced — the fade scored BELOW
    its own rotations. There is no version of this that reads as a near miss,
    and the tests below make sure no artefact can argue otherwise.
    """

    def test_the_signal_is_in_the_frozen_set(self):
        assert "post_shock_fade_v1" in ps.ABSENT_SIGNALS
        assert "post_shock_fade_v1" in ps.FROZEN_ABSENT

    def test_the_five_names_frozen_by_slice_44_are_all_present(self):
        """Membership, not a total.

        Slice 44 asserted `len == 5`, which was true then and went stale the
        moment slice 47 froze a sixth. The durable claim is that nothing
        already frozen quietly leaves the list; growth is legitimate and is
        counted by the owning slice's own test.
        """
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1",
                     "post_shock_fade_v1"):
            assert name in ps.ABSENT_SIGNALS, name
        assert len(ps.ABSENT_SIGNALS) >= 5

    def test_the_deny_list_and_its_evidence_still_cannot_disagree(self):
        assert ps.ABSENT_SIGNALS == tuple(ps.FROZEN_ABSENT)

    def test_the_evidence_cites_the_btc_numbers_and_the_artefact(self):
        why = ps.FROZEN_ABSENT["post_shock_fade_v1"]
        assert "1.6" in why and "2.0" in why
        assert "95" in why
        assert "slice43_edge_post_shock_fade_BTCUSD_summary.json" in why

    def test_the_evidence_says_why_eth_and_sol_were_not_measured(self):
        """An absent number needs a reason, or it reads as an omission."""
        why = ps.FROZEN_ABSENT["post_shock_fade_v1"]
        assert "NOT MEASURED" in why
        assert "75.4%" in why and "99.8%" in why
        assert "INCONCLUSIVE" in why

    def test_a_perfect_forged_positive_is_refused(self, tmp_path):
        """Everything a claim could want, on every universe symbol, and still no."""
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{symbol}_summary.json",
                          signal="post_shock_fade_v1", symbol=symbol,
                          control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_freeze_outranks_the_dual_symbol_rule(self, tmp_path):
        """Isolates the freeze: with the universe requirement emptied, the
        artefact satisfies every other check and is still refused."""
        import pytest as _pytest
        monkey = _pytest.MonkeyPatch()
        monkey.setattr(ps, "DUAL_SYMBOL_REQUIREMENTS", {})
        try:
            write_summary(tmp_path, signal="post_shock_fade_v1",
                          symbol="BTCUSD", control_validated=True)
            assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None
        finally:
            monkey.undo()

    def test_the_refusal_is_logged_with_its_reason(self, tmp_path, caplog):
        import logging
        write_summary(tmp_path, signal="post_shock_fade_v1", symbol="BTCUSD",
                      control_validated=True)
        with caplog.at_level(logging.ERROR):
            ps.cleared_edge_signal_from_artifacts(str(tmp_path))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "post_shock_fade_v1" in blob
        assert "FROZEN" in blob
        assert "1.6" in blob

    def test_the_slice_43_evidence_is_untouched(self):
        """A freeze that rewrote its own evidence would be worthless."""
        path = os.path.join(
            ARTIFACTS, "slice43_edge_post_shock_fade_BTCUSD_summary.json")
        assert os.path.exists(path), path
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
        assert summary["signal"] == "post_shock_fade_v1"
        assert summary["verdict"] == "EDGE_EVIDENCE_ABSENT"
        assert summary["control_validated"] is True
        assert summary["m1"]["percentile"] == pytest.approx(1.6, abs=0.05)
        assert summary["m2"]["percentile"] == pytest.approx(2.0, abs=0.05)
        assert summary["observed"]["n_trades"] == 41

    def test_no_alt_summary_was_ever_written(self):
        """ETH and SOL failed closed. Nothing may appear for them later."""
        import glob
        for symbol in ("ETHUSDT", "SOLUSDT"):
            assert glob.glob(os.path.join(
                ARTIFACTS,
                f"slice43_edge_post_shock_fade_{symbol}_summary.json")) == []

    def test_the_signal_constants_are_unchanged(self):
        """No retune arrived with the freeze."""
        import sys as _sys
        _sys.path.insert(0, REPO)
        from signals import post_shock_fade_v1 as fade
        assert fade.SHOCK_K == 2.0
        assert fade.STOP_ATR == 1.5
        assert fade.TAKE_PROFIT_R == 1.0
        assert fade.TAKE_PROFIT_ATR == 1.5
        assert fade.HORIZON == 3
        assert fade.LOCKUP == 1
        assert fade.ROUND_TRIP_BPS == 25.0
        assert fade.ENTRY_ON == "next_open"
        assert fade.ATR_PERIOD == 14

    def test_the_direction_was_not_flipped_after_the_loss(self):
        """The specific trap slice 44 exists to close.

        The fade lost badly (M1 1.6). The tempting move is to invert it and
        call the result an insight. A direction chosen because the opposite one
        underperformed is a parameter fitted to an observed result, so the
        mapping is pinned: an up-shock is still a SHORT.
        """
        import datetime as _dt
        import sys as _sys
        _sys.path.insert(0, REPO)
        import backtest as _bt
        from signals import post_shock_fade_v1 as fade
        epoch = int(_dt.datetime(2022, 1, 1,
                                 tzinfo=_dt.timezone.utc).timestamp() * 1000)
        bars, price = [], 100.0
        for i in range(60):
            if i == 40:
                price *= 1.30
            bars.append(_bt.Bar(epoch + i * 86_400_000, price, price + 1.0,
                                price - 1.0, price, 100.0))
        assert fade.shock_setups(bars).get(40) == fade.SHORT_SETUP

    def test_the_bars_were_not_lowered(self):
        assert ps.M1_BAR == 95.0
        assert ps.M2_BAR == 95.0

    def test_the_real_artifacts_directory_still_registers_nothing(self):
        ri.assert_registration_is_sound(ARTIFACTS)


class TestTheSlice47Freeze:
    """Slice 47 — `range_location_fade_v1` is frozen INCONCLUSIVE.

    Different in kind from the five before it. Those were measured by a trusted
    instrument and did not clear the bar. This one has **no M1 and no M2 at
    all**: the control failed its own pre-declared check on all three symbols
    (z +3.632 / +3.996 / +2.975, KS p 0.0004 / 0.0003 / 0.0122, 0% incompletes),
    so no reading was ever taken.

    The tests below therefore guard two things at once — that the freeze
    refuses, like every other, and that the record never acquires a percentile
    for a family that does not have one.
    """

    def test_the_signal_is_frozen(self):
        assert "range_location_fade_v1" in ps.ABSENT_SIGNALS
        assert "range_location_fade_v1" in ps.FROZEN_ABSENT

    def test_the_six_names_slice_47_knew_about_are_all_still_frozen(self):
        """Membership, not a total. The list grows as research closes lines.

        Slice 47 asserted `== 6`. Slice 50 froze a seventh, and an assertion
        that fails every time the programme legitimately advances is an
        assertion that gets edited rather than read.
        """
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1",
                     "post_shock_fade_v1", "range_location_fade_v1"):
            assert name in ps.ABSENT_SIGNALS, name
        assert len(ps.ABSENT_SIGNALS) >= 6

    def test_the_three_structures_cannot_disagree(self):
        assert ps.ABSENT_SIGNALS == tuple(ps.FROZEN_ABSENT)
        assert set(ps.FROZEN_STATUS) == set(ps.FROZEN_ABSENT)

    def test_the_status_map_keeps_absent_and_inconclusive_apart(self):
        """The distinction the human insisted on, held in code not just prose."""
        assert ps.FROZEN_STATUS["range_location_fade_v1"] == "INCONCLUSIVE"
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1",
                     "post_shock_fade_v1"):
            assert ps.FROZEN_STATUS[name] == "ABSENT", name

    def test_every_status_is_one_of_the_two_recognised_values(self):
        assert set(ps.FROZEN_STATUS.values()) <= {"ABSENT", "INCONCLUSIVE"}

    # -- the evidence string ------------------------------------------------

    def test_the_evidence_says_the_control_was_invalid(self):
        why = ps.FROZEN_ABSENT["range_location_fade_v1"]
        assert "CONTROL INVALID" in why

    def test_the_evidence_carries_the_three_z_values(self):
        why = ps.FROZEN_ABSENT["range_location_fade_v1"]
        for z in ("+3.632", "+3.996", "+2.975"):
            assert z in why, z

    def test_the_evidence_cites_the_control_logs(self):
        why = ps.FROZEN_ABSENT["range_location_fade_v1"]
        assert "slice46_control_range_location_fade" in why

    def test_the_evidence_says_edge_was_not_measured(self):
        why = ps.FROZEN_ABSENT["range_location_fade_v1"]
        assert "NOT MEASURED" in why
        assert "INCONCLUSIVE" in why

    def test_the_evidence_says_this_is_not_an_absent_reading(self):
        """Without this sentence a later reader files it with the other five."""
        why = ps.FROZEN_ABSENT["range_location_fade_v1"]
        assert "NOT an ABSENT percentile" in why

    def test_the_evidence_carries_the_trade_counts(self):
        """The design was measurable. The ruler was the problem."""
        why = ps.FROZEN_ABSENT["range_location_fade_v1"]
        for count in ("281", "112", "117"):
            assert count in why, count

    def test_the_evidence_invents_no_m1_or_m2(self):
        """The decisive honesty test.

        Every other frozen entry quotes a percentile pair because one exists.
        This family has none, so any "NN.N / NN.N" in its evidence would be a
        number somebody made up. The bar (95.0) and the z values are permitted
        and everything else numeric is checked by hand below.
        """
        import re
        why = ps.FROZEN_ABSENT["range_location_fade_v1"]
        assert "M1/M2 =" not in why
        pairs = re.findall(r"\d+\.\d+\s*/\s*\d+\.\d+", why)
        for pair in pairs:
            # the only slash-separated numeric pairs allowed are the KS
            # p-values, which are quoted as evidence of the control's failure
            assert pair.startswith("0.0"), pair

    def test_no_frozen_entry_is_a_placeholder(self):
        """Every entry carries checkable evidence of the kind its status implies."""
        for name, why in ps.FROZEN_ABSENT.items():
            assert len(why) > 60, name
            if ps.FROZEN_STATUS[name] == "ABSENT":
                assert "95" in why, name
            else:
                assert "NO M1 AND NO M2" in why.upper(), name
                assert "artifacts/" in why, name

    # -- the refusal --------------------------------------------------------

    def test_a_perfect_forged_positive_is_refused(self, tmp_path):
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{symbol}_summary.json",
                          signal="range_location_fade_v1", symbol=symbol,
                          control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_freeze_outranks_the_dual_symbol_rule(self, tmp_path):
        import pytest as _pytest
        monkey = _pytest.MonkeyPatch()
        monkey.setattr(ps, "DUAL_SYMBOL_REQUIREMENTS", {})
        try:
            write_summary(tmp_path, signal="range_location_fade_v1",
                          symbol="BTCUSD", control_validated=True)
            assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None
        finally:
            monkey.undo()

    def test_the_refusal_names_the_status_not_just_the_freeze(self, tmp_path,
                                                              caplog):
        """An operator reading the log must not infer "measured and failed"."""
        import logging
        write_summary(tmp_path, signal="range_location_fade_v1",
                      symbol="BTCUSD", control_validated=True)
        with caplog.at_level(logging.ERROR):
            ps.cleared_edge_signal_from_artifacts(str(tmp_path))
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "range_location_fade_v1" in blob
        assert "INCONCLUSIVE" in blob

    # -- the evidence on disk ----------------------------------------------

    @pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSDT", "SOLUSDT"])
    def test_the_slice_46_control_logs_are_untouched(self, symbol):
        path = os.path.join(
            ARTIFACTS,
            f"slice46_control_range_location_fade_{symbol}_n1000.log")
        assert os.path.exists(path), path
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "CONTROL: **INVALID" in text
        assert "CONTROL: **VALID**" not in text
        assert "0 of 1000 (0.0%)" in text

    def test_no_edge_summary_for_this_family_exists_anywhere(self):
        """It was never measured. Nothing may appear claiming otherwise."""
        import glob
        for path in glob.glob(os.path.join(ARTIFACTS, "*_summary.json")):
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
            assert summary.get("signal") != "range_location_fade_v1", path

    def test_the_signal_constants_are_unchanged(self):
        import sys as _sys
        _sys.path.insert(0, REPO)
        from signals import range_location_fade_v1 as rlf
        assert rlf.RANGE_BARS == 20
        assert rlf.UPPER == 0.90
        assert rlf.LOWER == 0.10
        assert rlf.STOP_ATR == 1.5
        assert rlf.TAKE_PROFIT_R == 1.0
        assert rlf.TAKE_PROFIT_ATR == 1.5
        assert rlf.HORIZON == 5
        assert rlf.LOCKUP == 1
        assert rlf.ROUND_TRIP_BPS == 25.0
        assert rlf.ENTRY_ON == "next_open"

    def test_the_null_was_not_redesigned_in_this_slice(self):
        """The trap slice 47 exists to close.

        Slice 46 recorded a hypothesis about WHY the control failed. Acting on
        it now — having seen which way the failure went — would be a
        construction chosen to fit an observed result. The three-clause rule
        and the scoring path are asserted unchanged.
        """
        import sys as _sys
        _sys.path.insert(0, os.path.join(REPO, "tools"))
        import control_directed as cd
        import edge_measurement as em
        assert cd.Z_ABS_MAX == 1.96
        assert cd.KS_MIN_P == 0.05
        assert cd.MAX_INCOMPLETE_SHARE == 0.05
        assert em.M1_PERCENTILE_BAR == 95.0
        assert em.M2_PERCENTILE_BAR == 95.0

    def test_the_real_artifacts_directory_still_registers_nothing(self):
        ri.assert_registration_is_sound(ARTIFACTS)


class TestTheSlice50FreezeOfOpenGapFade:
    """`open_gap_fade_v1` frozen INCONCLUSIVE — the seventh name.

    Two things are guarded here, and the second is the one that matters.

    The first is ordinary: a perfect forgery must be refused, like every other
    frozen family.

    The second is that this entry must never acquire a percentile. The family
    has no M1 and no M2 — none was ever computed — so a number in its evidence
    could only have been invented. That failure would be invisible to a later
    reader, who would have no way to tell a fabricated 91.4 from a measured one.
    """

    def test_the_signal_is_frozen(self):
        assert "open_gap_fade_v1" in ps.ABSENT_SIGNALS
        assert "open_gap_fade_v1" in ps.FROZEN_ABSENT
        assert ps.FROZEN_STATUS["open_gap_fade_v1"] == "INCONCLUSIVE"

    def test_the_seven_names_slice_50_knew_about_are_all_still_frozen(self):
        """Membership and no-shrink. Slice 51 froze an eighth.

        An assertion that fails every time the programme legitimately closes
        another line is an assertion that gets edited rather than read — the
        lesson slice 47's `== 6` and slice 50's `== 7` both taught.
        """
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1",
                     "post_shock_fade_v1", "range_location_fade_v1",
                     "open_gap_fade_v1"):
            assert name in ps.ABSENT_SIGNALS, name
        assert len(ps.ABSENT_SIGNALS) >= 7

    def test_the_three_structures_cannot_disagree(self):
        assert ps.ABSENT_SIGNALS == tuple(ps.FROZEN_ABSENT)
        assert set(ps.FROZEN_STATUS) == set(ps.FROZEN_ABSENT)

    # -- the evidence -------------------------------------------------------

    def test_the_evidence_quotes_no_percentile(self):
        """The decisive one. There is nothing to quote, so nothing may be."""
        import re
        why = ps.FROZEN_ABSENT["open_gap_fade_v1"]
        assert "M1/M2 =" not in why
        assert "NO M1 and NO M2" in why
        assert "EDGE WAS NOT MEASURED" in why
        # No bare "95.0"-shaped bar comparison: this family never met a bar.
        assert "vs 95" not in why
        assert "against a bar of 95" not in why
        for pair in re.findall(r"\d+\.\d+\s*/\s*\d+\.\d+", why):
            # the only slash-separated numeric pairs allowed are the corpus
            # percentages that explain the count, never percentiles
            assert "%" in why.split(pair)[1][:3] or pair.startswith("0."), pair

    def test_it_says_inconclusive_and_not_absent(self):
        why = ps.FROZEN_ABSENT["open_gap_fade_v1"]
        assert "INCONCLUSIVE" in why
        assert "NOT an ABSENT percentile reading" in why

    def test_the_evidence_carries_the_counts_and_the_control_result(self):
        why = ps.FROZEN_ABSENT["open_gap_fade_v1"]
        assert "1 / 0 / 0" in why
        assert ">= 50" in why
        assert "0 usable surrogates" in why
        assert "100% incompletes" in why

    def test_the_evidence_keeps_the_two_causes_apart(self):
        """Filing this INVALID as bias would overstate the slice-46 finding.

        Slice 46's instrument ran a thousand times per symbol at 0% incomplete
        and came back at z = +3.6. This one never ran. The evidence string must
        carry that distinction, because the deny-list is where a future reader
        will look first.
        """
        why = ps.FROZEN_ABSENT["open_gap_fade_v1"]
        assert "CONSTRUCTION MISMATCH" in why
        assert "NOT instrument bias" in why
        assert "surrogate_series" in why

    def test_the_evidence_points_at_logs_that_exist_and_say_so(self):
        why = ps.FROZEN_ABSENT["open_gap_fade_v1"]
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        count_log = os.path.join(repo, "artifacts",
                                 "slice49_count_finding_open_gap_fade.log")
        assert "slice49_count_finding_open_gap_fade.log" in why
        assert os.path.exists(count_log)
        with open(count_log, encoding="utf-8") as handle:
            text = handle.read()
        assert "BELOW the 50-trade target" in text
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            path = os.path.join(
                repo, "artifacts",
                f"slice49_control_open_gap_fade_{symbol}_n1000.log")
            assert os.path.exists(path), symbol
            with open(path, encoding="utf-8") as handle:
                assert "CONTROL: **INVALID" in handle.read()

    def test_the_signal_constants_were_not_touched(self):
        """A freeze is not a licence to tidy the module on the way past."""
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, repo)
        from signals import open_gap_fade_v1 as og
        assert og.GAP_K == 0.75
        assert og.ATR_PERIOD == 14
        assert og.STOP_ATR == 1.5
        assert og.TAKE_PROFIT_R == 1.0
        assert og.HORIZON == 4
        assert og.ROUND_TRIP_BPS == 25.0

    # -- the refusal --------------------------------------------------------

    def test_a_perfect_forged_positive_is_refused(self, tmp_path):
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{symbol}_summary.json",
                          signal="open_gap_fade_v1", symbol=symbol,
                          control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_refusal_names_the_status(self, tmp_path, caplog):
        write_summary(tmp_path, name="og_summary.json",
                      signal="open_gap_fade_v1", symbol="BTCUSD",
                      control_validated=True,
                      m1={"percentile": 99.9},
                      m2={"percentile": 99.9, "passed": True})
        with caplog.at_level("ERROR"):
            assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None
        assert "INCONCLUSIVE" in caplog.text


class TestTheSlice51FreezeOfTsMomentum:
    """`ts_momentum_v1` frozen INCONCLUSIVE — the eighth closed line.

    Three things are guarded, in order of how badly they would mislead a
    future reader if they broke.

    First, the entry must never acquire a percentile. There is no M1 and no M2
    for this family; a number in its evidence could only have been invented,
    and nothing downstream could tell it from a measured one.

    Second, the status must stay INCONCLUSIVE rather than drifting to ABSENT.
    Those words mean different things here — one family measured and lost, the
    other was never read — and collapsing them would overstate what this
    programme actually knows.

    Third, the root cause must stay stated. "The signal rarely fired" would be
    false: it flagged 2,934 bars on BTC and the corpora hold 441 / 209 / 201
    sign flips. What produced a 1-trade sample was the scheduler, and an
    intake written against the wrong diagnosis would waste another slice.
    """

    def test_the_signal_is_frozen(self):
        assert "ts_momentum_v1" in ps.ABSENT_SIGNALS
        assert "ts_momentum_v1" in ps.FROZEN_ABSENT
        assert ps.FROZEN_STATUS["ts_momentum_v1"] == "INCONCLUSIVE"

    def test_the_eight_names_slice_51_knew_about_are_all_still_frozen(self):
        """Membership and no-shrink. Slice 53 froze a ninth."""
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1",
                     "post_shock_fade_v1", "range_location_fade_v1",
                     "open_gap_fade_v1", "ts_momentum_v1"):
            assert name in ps.ABSENT_SIGNALS, name
        assert len(ps.ABSENT_SIGNALS) >= 8

    def test_the_three_structures_cannot_disagree(self):
        """The deny-list, its evidence and its statuses are one object's keys."""
        assert ps.ABSENT_SIGNALS == tuple(ps.FROZEN_ABSENT)
        assert set(ps.FROZEN_STATUS) == set(ps.FROZEN_ABSENT)

    def test_the_bars_did_not_move(self):
        assert ps.M1_BAR == 95.0
        assert ps.M2_BAR == 95.0

    # -- the evidence -------------------------------------------------------

    def test_the_evidence_quotes_no_percentile(self):
        """The decisive one. There is nothing to quote, so nothing may be."""
        import re
        why = ps.FROZEN_ABSENT["ts_momentum_v1"]
        assert "M1/M2 =" not in why
        assert "NO M1 and NO M2" in why
        assert "EDGE WAS NOT MEASURED" in why
        assert "vs 95" not in why
        assert "against a bar of 95" not in why
        # No percentile-shaped pair anywhere. The only slash-separated numbers
        # in this entry are the per-symbol counts, which are integers.
        for pair in re.findall(r"\d+\.\d+\s*/\s*\d+\.\d+", why):
            raise AssertionError(f"percentile-shaped pair in evidence: {pair}")

    def test_it_says_inconclusive_and_refuses_the_absent_label(self):
        why = ps.FROZEN_ABSENT["ts_momentum_v1"]
        assert "INCONCLUSIVE" in why
        assert "NOT an ABSENT percentile reading" in why
        assert "NO TRUSTED STAGE-1 NUMBER EXISTS" in why

    def test_the_evidence_carries_the_counts_and_the_control_result(self):
        why = ps.FROZEN_ABSENT["ts_momentum_v1"]
        assert "1 / 1 / 3" in why
        assert ">= 50" in why
        assert "0 usable surrogates" in why
        assert "100% incompletes" in why
        assert "INVALID" in why

    def test_the_evidence_states_the_state_versus_event_root_cause(self):
        why = ps.FROZEN_ABSENT["ts_momentum_v1"]
        assert "STATE signal" in why
        assert "EVENT-shaped" in why
        assert "ONE-TRADE-PER-RUN" in why
        assert "CONTIGUOUS RUNS" in why

    def test_the_evidence_records_that_the_thesis_is_not_rare(self):
        """The diagnosis the next intake will act on. It must not be wrong."""
        why = ps.FROZEN_ABSENT["ts_momentum_v1"]
        assert "NOT RARE" in why
        assert "441" in why and "209" in why and "201" in why
        assert "2934" in why

    def test_the_evidence_keeps_this_apart_from_slice_49_and_slice_46(self):
        """Three INVALIDs, three causes. Merging them overstates the bias case."""
        why = ps.FROZEN_ABSENT["ts_momentum_v1"]
        assert "CONSTRUCTION MISMATCH" in why
        assert "NOT instrument bias" in why
        assert "PRESERVED this trigger" in why
        # slice 49's entry must still say the opposite thing about its own null
        assert "deletes the trigger" in ps.FROZEN_ABSENT["open_gap_fade_v1"]

    def test_the_evidence_records_that_nothing_was_retuned(self):
        why = ps.FROZEN_ABSENT["ts_momentum_v1"]
        assert "LOOKBACK stays 10" in why
        assert "SKIP stays 1" in why
        assert "NOT altered after the count" in why

    def test_the_evidence_points_at_logs_that_exist_and_say_so(self):
        why = ps.FROZEN_ABSENT["ts_momentum_v1"]
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        count_log = os.path.join(repo, "artifacts",
                                 "slice50_count_finding_ts_momentum.log")
        assert "slice50_count_finding_ts_momentum.log" in why
        assert os.path.exists(count_log)
        with open(count_log, encoding="utf-8") as handle:
            text = handle.read()
        assert "BELOW the 50-trade target" in text
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            path = os.path.join(
                repo, "artifacts",
                f"slice50_control_ts_momentum_{symbol}_n1000.log")
            assert os.path.exists(path), symbol
            with open(path, encoding="utf-8") as handle:
                assert "CONTROL: **INVALID" in handle.read()
        assert "slice50_no_edge_run_attestation.log" in why
        assert os.path.exists(os.path.join(
            repo, "artifacts", "slice50_no_edge_run_attestation.log"))

    # -- nothing was retuned to make the freeze look better ------------------

    def test_the_signal_constants_were_not_touched(self):
        """A freeze is not a licence to tidy the module on the way past."""
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, repo)
        from signals import ts_momentum_v1 as tsm
        assert tsm.LOOKBACK == 10
        assert tsm.SKIP == 1
        assert tsm.STOP_ATR == 1.5
        assert tsm.TAKE_PROFIT_R == 2.0
        assert tsm.TAKE_PROFIT_ATR == 3.0
        assert tsm.HORIZON == 5
        assert tsm.LOCKUP == 1
        assert tsm.ROUND_TRIP_BPS == 25.0

    def test_the_scheduler_was_not_changed_after_the_count(self):
        """The sharpest forbidden repair, asserted against the source.

        Breaking flag runs at direction changes would turn a 1-trade sample
        into a several-hundred-trade one. Doing it now, after the count is
        known, would be a construction chosen to produce a sample.
        """
        import inspect
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(repo, "tools"))
        import skill_test as sk
        source = inspect.getsource(sk.simulate_schedule)
        assert "AT MOST ONE entry" in source
        assert "a contiguous run of flags contributes AT MOST ONE entry" in source

    def test_no_edge_artefact_for_this_family_exists(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        artifacts = os.path.join(repo, "artifacts")
        hits = [f for f in os.listdir(artifacts)
                if "ts_momentum" in f and "edge" in f]
        assert hits == [], hits

    # -- the refusal --------------------------------------------------------

    def test_a_perfect_forged_positive_is_refused(self, tmp_path):
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{symbol}_summary.json",
                          signal="ts_momentum_v1", symbol=symbol,
                          control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_refusal_names_the_status(self, tmp_path, caplog):
        write_summary(tmp_path, name="tsm_summary.json",
                      signal="ts_momentum_v1", symbol="BTCUSD",
                      control_validated=True,
                      m1={"percentile": 99.9},
                      m2={"percentile": 99.9, "passed": True})
        with caplog.at_level("ERROR"):
            assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None
        assert "INCONCLUSIVE" in caplog.text

    def test_the_real_artifact_directory_still_registers_nothing(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ri.assert_registration_is_sound(os.path.join(repo, "artifacts"))


class TestEveryFreezeRefusesAPerfectForgery:
    """The whole battery, in one place, parameterised over the live deny-list.

    Reading the names from `ps.ABSENT_SIGNALS` rather than listing them means a
    name added without evidence, or a name removed, changes what this test
    covers automatically instead of silently leaving a gap.
    """

    @pytest.mark.parametrize("signal", list(ps.ABSENT_SIGNALS))
    def test_a_perfect_forgery_is_refused(self, signal, tmp_path):
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{symbol}_summary.json",
                          signal=signal, symbol=symbol,
                          control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_deny_list_is_the_expected_size(self):
        """Updated deliberately each time a human freezes a name."""
        assert len(ps.ABSENT_SIGNALS) == 11

    @pytest.mark.parametrize("signal", list(ps.ABSENT_SIGNALS))
    def test_every_entry_carries_the_evidence_its_status_implies(self, signal):
        why = ps.FROZEN_ABSENT[signal]
        assert len(why) > 60, signal
        if ps.FROZEN_STATUS[signal] == "ABSENT":
            assert "95" in why, signal
        else:
            assert "NO M1 AND NO M2" in why.upper(), signal
            assert "NOT MEASURED" in why, signal
            assert "artifacts/" in why, signal


class TestTheSlice56FreezeOfFundingCarryFade:
    """`funding_carry_fade_v1` frozen ABSENT — the eleventh closed line.

    Every previous freeze closed a family whose artefacts all said ABSENT, or
    whose artefacts did not exist. **This one is closed while a genuine
    `EDGE_EVIDENCE_POSITIVE` for it sits in `artifacts/`** — M1 97.0, M2 97.5,
    85 trades, `control_validated: true`, measured with a ruler that passed its
    own pre-declared three clauses.

    So the honesty risk here is the sharpest the programme has produced, and it
    runs in two directions at once:

    * toward **inflation** — "BTC cleared, that is the result, the alts are
      noise". The pre-declaration said two of three, in git, before the loader
      produced a setup;
    * toward **erasure** — quietly deleting or downgrading the inconvenient
      artefact so the ledger reads cleanly. A repository that removes its one
      awkward file is one whose remaining files mean less.

    These tests refuse both. The artefact must stay, byte-for-byte, still saying
    POSITIVE; and it must register nothing.
    """

    SIGNAL = "funding_carry_fade_v1"
    SUMMARIES = {
        "BTCUSDT": ("EDGE_EVIDENCE_POSITIVE", 97.0, 97.5, 85),
        "ETHUSDT": ("EDGE_EVIDENCE_ABSENT", 47.7, 48.5, 76),
        "SOLUSDT": ("EDGE_EVIDENCE_ABSENT", 2.3, 1.0, 162),
    }

    def _summary(self, symbol):
        path = os.path.join(
            ARTIFACTS,
            f"slice55_edge_funding_carry_fade_{symbol}_summary.json")
        assert os.path.exists(path), path
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    # -- the freeze itself --------------------------------------------------

    def test_the_signal_is_frozen(self):
        assert self.SIGNAL in ps.ABSENT_SIGNALS
        assert self.SIGNAL in ps.FROZEN_ABSENT
        assert ps.FROZEN_STATUS[self.SIGNAL] == "ABSENT"

    def test_the_deny_list_is_eleven_and_internally_consistent(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert ps.ABSENT_SIGNALS == tuple(ps.FROZEN_ABSENT)
        assert set(ps.FROZEN_STATUS) == set(ps.FROZEN_ABSENT)

    def test_the_ten_earlier_names_are_all_still_frozen(self):
        """Membership and no-shrink. A freeze never costs an earlier freeze."""
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1",
                     "post_shock_fade_v1", "range_location_fade_v1",
                     "open_gap_fade_v1", "ts_momentum_v1",
                     "sign_flip_momentum_v1", "compression_breakout_v1"):
            assert name in ps.ABSENT_SIGNALS, name

    def test_the_bars_did_not_move(self):
        assert ps.M1_BAR == 95.0 and ps.M2_BAR == 95.0

    def test_it_is_absent_and_not_inconclusive(self):
        """Three readings were taken. Filing this with the empty closures
        would understate what is known; filing them with it would fabricate
        numbers they do not have."""
        why = ps.FROZEN_ABSENT[self.SIGNAL]
        assert ps.FROZEN_STATUS[self.SIGNAL] == "ABSENT"
        assert "NOTHING here is INCONCLUSIVE" in why
        assert "MEASURED refusal" in why
        assert "NO M1 AND NO M2" not in why.upper()

    # -- what the evidence must say ----------------------------------------

    def test_the_evidence_names_the_reason_the_family_closed(self):
        why = ps.FROZEN_ABSENT[self.SIGNAL]
        assert "FAMILY ABSENT" in why
        assert "2-SYMBOL CONJUNCTION FAILED" in why
        assert "positive_rule_met is false" in why

    def test_the_evidence_carries_all_three_readings_and_their_counts(self):
        why = ps.FROZEN_ABSENT[self.SIGNAL]
        for number in ("97.0/97.5", "47.7/48.5", "2.3/1.0",
                       "85", "76", "162", "95.0"):
            assert number in why, number

    def test_the_evidence_records_that_every_control_passed(self):
        """An ABSENT is only worth something if the ruler was checked first,
        and here all three were."""
        why = ps.FROZEN_ABSENT[self.SIGNAL]
        for fragment in ("+0.632", "+0.168", "+1.933", "0.0% incomplete",
                         "control_validated true"):
            assert fragment in why, fragment

    def test_the_evidence_does_not_hide_the_btc_positive(self):
        """The temptation is to omit it. The refusal is to state it plainly."""
        why = ps.FROZEN_ABSENT[self.SIGNAL]
        assert "EDGE_EVIDENCE_POSITIVE" in why
        assert "RETAINED UNEDITED" in why
        assert "artifacts/slice55_edge_funding_carry_fade_BTCUSDT_" in why

    def test_the_evidence_refuses_the_almost_positive_reading(self):
        """The single sentence most likely to be softened by a later reader."""
        why = ps.FROZEN_ABSENT[self.SIGNAL]
        assert "NOT AN 'ALMOST POSITIVE'" in why
        assert "disagree in SIGN" in why
        assert "14%" in why

    def test_the_evidence_records_that_registration_already_refused_it(self):
        why = ps.FROZEN_ABSENT[self.SIGNAL]
        assert "MULTI_SYMBOL_MINIMUMS" in why
        assert "TWICE OVER" in why

    def test_the_evidence_forbids_the_retune_and_the_universe_narrowing(self):
        """Two temptations this family creates that no earlier one did."""
        why = ps.FROZEN_ABSENT[self.SIGNAL]
        assert "FUND_ABS stays 0.0001" in why
        assert "no symbol was dropped from the universe after the fact" in why
        assert "no BTC-only rescue" in why

    # -- the freeze must actually bite -------------------------------------

    def test_a_perfect_single_symbol_forgery_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_summary(tmp, name="btc_summary.json", signal=self.SIGNAL,
                          symbol="BTCUSDT", control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
            assert ps.cleared_edge_signal_from_artifacts(tmp) is None

    def test_a_perfect_forgery_that_SATISFIES_the_universe_rule_is_refused(self):
        """The test that proves the freeze OUTRANKS the universe rule.

        Slice 55's `MULTI_SYMBOL_MINIMUMS` refused this family because only one
        of three symbols cleared. A forger who supplies all three would satisfy
        that rule completely — so if the freeze were merely redundant with it,
        this would register. It must not.
        """
        universe, minimum = ps.MULTI_SYMBOL_MINIMUMS[self.SIGNAL]
        assert len(universe) >= minimum  # the forgery below really does satisfy it
        with tempfile.TemporaryDirectory() as tmp:
            for symbol in universe:
                write_summary(tmp, name=f"{symbol}_summary.json",
                              signal=self.SIGNAL, symbol=symbol,
                              control_validated=True,
                              m1={"percentile": 99.9},
                              m2={"percentile": 99.9, "passed": True})
            assert ps.cleared_edge_signal_from_artifacts(tmp) is None

    def test_that_forgery_would_have_registered_without_the_freeze(self):
        """The control: a guard that would have refused anyway is not a guard.

        Same three artefacts, deny-list emptied of this one name. If this does
        not register, the previous test proves nothing about the freeze.
        """
        universe, _minimum = ps.MULTI_SYMBOL_MINIMUMS[self.SIGNAL]
        with tempfile.TemporaryDirectory() as tmp:
            for symbol in universe:
                write_summary(tmp, name=f"{symbol}_summary.json",
                              signal=self.SIGNAL, symbol=symbol,
                              control_validated=True,
                              m1={"percentile": 99.9},
                              m2={"percentile": 99.9, "passed": True})
            thinner = {k: v for k, v in ps.FROZEN_ABSENT.items()
                       if k != self.SIGNAL}
            original_map, original_tuple = ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS
            ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS = thinner, tuple(thinner)
            try:
                assert ps.cleared_edge_signal_from_artifacts(tmp) == self.SIGNAL
            finally:
                ps.FROZEN_ABSENT, ps.ABSENT_SIGNALS = original_map, original_tuple

    def test_the_refusal_quotes_the_evidence(self, caplog):
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            write_summary(tmp, name="btc_summary.json", signal=self.SIGNAL,
                          symbol="BTCUSDT", control_validated=True)
            with caplog.at_level(logging.ERROR):
                ps.cleared_edge_signal_from_artifacts(tmp)
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert self.SIGNAL in blob
        assert "97.0/97.5" in blob
        assert "2-SYMBOL CONJUNCTION FAILED" in blob

    # -- the slice-55 artefacts must survive, unedited ----------------------

    @pytest.mark.parametrize("symbol", sorted(SUMMARIES))
    def test_the_slice55_summary_is_still_on_disk_with_its_numbers(self, symbol):
        verdict, m1, m2, trades = self.SUMMARIES[symbol]
        data = self._summary(symbol)
        assert data["signal"] == self.SIGNAL
        assert data["symbol"] == symbol
        assert data["verdict"] == verdict
        assert round(data["m1"]["percentile"], 1) == m1
        assert round(data["m2"]["percentile"], 1) == m2
        assert data["observed"]["n_trades"] == trades
        assert data["control_validated"] is True

    def test_the_btc_artefact_still_says_positive(self):
        """Stated on its own, because this is the one a later slice would be
        tempted to soften. It is a true reading and it stays true on disk."""
        data = self._summary("BTCUSDT")
        assert data["verdict"] == "EDGE_EVIDENCE_POSITIVE"
        assert data["m1"]["passed"] is True and data["m2"]["passed"] is True
        assert data["m1"]["percentile"] >= ps.M1_BAR
        assert data["m2"]["percentile"] >= ps.M2_BAR

    def test_the_sol_artefact_still_records_a_negative_delta(self):
        """The counterweight to BTC's 97.0, and the reason the family is
        ABSENT rather than unlucky. Its CI excludes zero on the wrong side."""
        m2 = self._summary("SOLUSDT")["m2"]
        assert m2["delta"] < 0
        assert m2["ci_high"] < 0

    def test_the_stage1_record_still_says_the_rule_was_not_met(self):
        path = os.path.join(
            ARTIFACTS, "slice55_stage1_funding_carry_fade_v1.json")
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        assert record["positive_rule_met"] is False
        # A DATED artefact: it recorded a null cleared edge at the time it
        # was written and is never rewritten. Slice 57 registering a
        # different name later does not change what this record said.
        assert record["cleared_edge_signal"] is None
        assert record["edge_verdict"] == "ABSENT"
        assert record["single_symbol_claim_refused"] is True

    def test_the_real_artifacts_directory_still_registers_nothing(self):
        ri.assert_registration_is_sound(ARTIFACTS)
        assert ps.current().cleared_edge_signal == ri.stock_cleared_edge_signal()
        ri.assert_registration_is_sound()

    # -- and nothing was retuned -------------------------------------------

    def test_the_signal_constants_are_untouched(self):
        """A freeze that came with a parameter change would not be a freeze."""
        from signals import funding_carry_fade_v1 as fc
        assert (fc.FUND_ABS, fc.STOP_ATR, fc.TAKE_PROFIT_R, fc.TAKE_PROFIT_ATR,
                fc.HORIZON, fc.ATR_PERIOD, fc.LOCKUP, fc.ROUND_TRIP_BPS,
                fc.ENTRY_ON) == \
            (0.0001, 1.5, 1.0, 1.5, 5, 14, 1, 25.0, "next_open")

    def test_no_new_edge_or_control_artefact_was_produced_for_this_family(self):
        """Slice 56 re-ran nothing. Any slice56_* edge or control log for this
        family would mean a measurement happened after the numbers were known.
        """
        offending = [n for n in os.listdir(ARTIFACTS)
                     if n.startswith("slice56_")
                     and ("edge_funding" in n or "control_funding" in n)]
        assert not offending, offending

    def test_the_universe_rule_was_not_narrowed_to_the_symbol_that_passed(self):
        universe, minimum = ps.MULTI_SYMBOL_MINIMUMS[self.SIGNAL]
        assert universe == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        assert minimum == 2


class TestTheSlice56FreezeArtefact:
    """`artifacts/slice56_funding_carry_fade_freeze.json` must not flatter.

    A machine-readable freeze record is the thing a future reader trusts when
    they have not got time for EDGE.md. Its failure mode is not being wrong
    about the numbers — those are copied — but being *selectively* right: a
    record that states the family verdict and quietly omits that one symbol
    cleared both bars would be true in every field and misleading as a whole.

    So these tests check the awkward parts are present, not just the convenient
    ones.
    """

    def _payload(self):
        path = os.path.join(ARTIFACTS,
                            "slice56_funding_carry_fade_freeze.json")
        assert os.path.exists(path), path
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_records_the_freeze_truthfully(self):
        payload = self._payload()
        assert payload["schema"] == "research_freeze/1"
        assert payload["frozen_name"] == "funding_carry_fade_v1"
        assert payload["freeze"] is True
        assert payload["family_status"] == "ABSENT"
        assert payload["status"] == \
            ps.FROZEN_STATUS["funding_carry_fade_v1"] == "ABSENT"
        assert payload["freeze_reason"] == \
            "FAMILY_ABSENT_MULTI_SYMBOL_RULE_FAILED"
        assert payload["conjunction_failed"] is True
        assert payload["positive_rule_met"] is False
        # A DATED artefact: it recorded a null cleared edge at the time it
        # was written and is never rewritten. Slice 57 registering a
        # different name later does not change what this record said.
        assert payload["cleared_edge_signal"] is None

    def test_it_agrees_with_the_deny_list_it_was_written_from(self):
        payload = self._payload()
        assert payload["frozen_absent_count"] == len(ps.ABSENT_SIGNALS) == 11
        assert payload["frozen_absent"] == list(ps.ABSENT_SIGNALS)
        assert payload["frozen_status"] == dict(ps.FROZEN_STATUS)
        assert payload["bars"] == {"m1": ps.M1_BAR, "m2": ps.M2_BAR}
        assert payload["bars_changed_this_slice"] is False

    def test_it_states_the_btc_positive_rather_than_omitting_it(self):
        """The field this record exists to not quietly drop."""
        payload = self._payload()
        btc = payload["readings"]["BTCUSDT"]
        assert btc["verdict"] == "EDGE_EVIDENCE_POSITIVE"
        assert btc["m1"] == 97.0 and btc["m2"] == 97.5
        assert btc["clears_both_bars"] is True
        assert btc["control_validated"] is True
        assert payload["btc_artefact_preserved"] is True
        assert payload["positive_rule_symbols_clearing_both_bars"] == ["BTCUSDT"]

    def test_it_states_the_counterweight_too(self):
        """A record that named only BTC's 97.0 would be a press release."""
        payload = self._payload()
        sol = payload["readings"]["SOLUSDT"]
        assert sol["verdict"] == "EDGE_EVIDENCE_ABSENT"
        assert sol["m2_delta"] < 0 and sol["m2_ci"][1] < 0
        assert sol["n_trades"] > payload["readings"]["BTCUSDT"]["n_trades"]
        assert payload["readings"]["ETHUSDT"]["verdict"] == \
            "EDGE_EVIDENCE_ABSENT"

    def test_it_records_that_nothing_was_inconclusive(self):
        payload = self._payload()
        assert payload["all_symbols_measured"] is True
        assert payload["any_symbol_inconclusive"] is False
        for symbol, reading in payload["readings"].items():
            assert reading["control_validated"] is True, symbol
            assert reading["n_trades"] >= 50, symbol

    def test_it_records_that_nothing_was_retuned_or_re_run(self):
        payload = self._payload()
        assert payload["constants_unchanged"] is True
        assert payload["universe_unchanged"] is True
        assert payload["universe"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert payload["edge_or_control_rerun_in_this_slice"] is False
        assert payload["grid_or_sweep_run"] is False
        assert payload["new_signal_implemented"] is False
        from signals import funding_carry_fade_v1 as fc
        assert payload["constants"]["FUND_ABS"] == fc.FUND_ABS == 0.0001
        assert payload["constants"]["HORIZON"] == fc.HORIZON == 5

    def test_it_lists_what_is_forbidden_without_a_new_intake(self):
        payload = self._payload()
        joined = " ".join(payload["forbidden_without_a_new_human_intake"])
        assert "BTCUSDT alone" in joined
        assert "FUND_ABS" in joined
        assert "dropping ETHUSDT or SOLUSDT" in joined
        assert len(payload["forbidden_without_a_new_human_intake"]) >= 5

    def test_it_does_not_claim_progress_toward_profit(self):
        payload = self._payload()
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["model_blocked"] is True
        assert payload["live_blocked"] is True
        assert payload["models_current_present"] is False
        assert payload["execution_mode"] == "paper"

    def test_the_named_artefacts_all_exist(self):
        """A freeze record pointing at files that are gone proves nothing."""
        payload = self._payload()
        repo = os.path.dirname(ARTIFACTS)
        assert os.path.exists(os.path.join(repo, payload["btc_artefact_path"]))
        for reading in payload["readings"].values():
            assert os.path.exists(os.path.join(repo, reading["summary"]))
            assert os.path.exists(os.path.join(repo, reading["control_log"]))


class TestTheLedgerDocumentsAgreeWithTheDenyList:
    """Slice 51 STEP 3. The prose must not drift from the code.

    These are the documents a returning human reads first. A ledger that says
    seven while the hook refuses eight is not a small inaccuracy — it is the
    surface where "which lines are closed" gets decided by whoever read most
    recently.

    Only LIVING documents are asserted here. Dated artefacts (the slice-42/45/48
    JSON records) are deliberately left alone: a historical record edited to
    match today's code stops being a record.
    """

    DOCS = ("docs/RESEARCH_HOLD.md", "docs/RESEARCH_PROGRAM_FREEZE.md",
            "RESEARCH_STATUS.md", "HANDOFF.md")

    def _repo(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, name):
        with open(os.path.join(self._repo(), name), encoding="utf-8") as handle:
            return handle.read()

    @pytest.mark.parametrize("document", DOCS)
    def test_every_frozen_name_appears(self, document):
        text = self._read(document)
        for name in ps.ABSENT_SIGNALS:
            assert name in text, (document, name)

    @pytest.mark.parametrize("document", ("docs/RESEARCH_HOLD.md",
                                          "docs/RESEARCH_PROGRAM_FREEZE.md",
                                          "RESEARCH_STATUS.md"))
    def test_absent_and_inconclusive_stay_distinct(self, document):
        text = self._read(document)
        assert "ABSENT" in text and "INCONCLUSIVE" in text
        # Case-insensitively, and NOT via text.lower() — lowering the haystack
        # also lowers "M1", so `"no M1" in text.lower()` can never match. That
        # bug made this assertion vacuous on the first attempt.
        lowered = text.lower()
        assert "no m1" in lowered, document

    #: Spelled-out counts, so the prose assertion below tracks the deny-list
    #: instead of being rewritten by hand at every freeze. The word was
    #: hardcoded as "ten" until slice 56 froze an eleventh — the same
    #: cry-wolf failure as every `== N` count assertion in this suite, one
    #: layer up in the English.
    WORDS = {8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
             13: "thirteen", 14: "fourteen", 15: "fifteen"}

    def test_the_hold_counts_every_frozen_name(self):
        count = len(ps.ABSENT_SIGNALS)
        text = self._read("docs/RESEARCH_HOLD.md")
        assert f"count is **{count}**" in text
        word = self.WORDS.get(count)
        assert word is not None, (
            f"{count} frozen names and no spelled-out form registered; add it "
            "rather than dropping the prose check")
        assert f"{word} frozen names" in text

    def test_no_document_quotes_a_percentile_the_deny_list_does_not(self):
        """No prose may invent a reading, and the deny-list is the source.

        Two earlier attempts at this were wrong, and both are worth recording.

        A blacklist of phrases like "near miss" failed instantly against this
        repository's own "94.3 IS NOT A NEAR MISS" — the discipline working,
        banned by the test written to police it.

        Its replacement banned every percentile-shaped pair on any line naming
        an INCONCLUSIVE family. That held while all three such families had no
        numbers at all, and broke the moment `compression_breakout_v1` arrived
        with one real reading on one symbol. A rule that forbids stating a
        true, measured number is not a discipline.

        The version that survives both: a document may quote a percentile pair
        for a frozen family only if `project_status.FROZEN_ABSENT` — the
        deny-list, which is the single source of truth and is itself tested —
        quotes that same pair. Prose cannot get ahead of the code.
        """
        import re
        pair = re.compile(r"(\d{1,3}\.\d)\s*/\s*(\d{1,3}\.\d)")

        def pairs_on(line):
            if "z " in line or "z =" in line or "KS" in line:
                return []
            out = []
            for left, right in pair.findall(line):
                # z-values, KS p-values and small ratios are not skill
                # percentiles; only 0-100-range pairs are checked.
                if float(left) <= 5.0 or float(right) <= 5.0:
                    continue
                out.append((left, right))
            return out

        # (1) The two LEDGERS state outcomes, so every reading they attribute
        #     to a frozen family must be one the deny-list also carries. A row
        #     may name more than one family — the analyser shares a row with
        #     its alias — so the pair must be quoted by SOME family named.
        for document in ("docs/RESEARCH_HOLD.md",
                         "docs/RESEARCH_PROGRAM_FREEZE.md"):
            for line in self._read(document).splitlines():
                named = [n for n in ps.ABSENT_SIGNALS if n in line]
                if not named:
                    continue
                evidence = " ".join(ps.FROZEN_ABSENT[n] for n in named)
                for left, right in pairs_on(line):
                    quoted = f"{left}/{right}" in evidence or \
                        f"{left} / {right}" in evidence
                    assert quoted, (document, named, f"{left}/{right}",
                                    line[:120])

        # (2) There is deliberately NO equivalent check on RESEARCH_STATUS or
        #     HANDOFF, and the reason is worth the paragraph.
        #
        #     Those are narrative histories, and they legitimately quote
        #     SUPERSEDED numbers in order to forbid their use: "No interpreting
        #     btc_alt_spillover_v1's ETH reading. 97.3 / 98.0 came from a
        #     construction whose control did not validate." That sentence is
        #     the discipline working, and it is above the bar.
        #
        #     Three versions of this test have now banned an honest sentence:
        #     a phrase blacklist banned "94.3 IS NOT A NEAR MISS"; a
        #     ban on percentile pairs beside INCONCLUSIVE families banned
        #     compression_breakout_v1's one real reading; a ban on
        #     at-or-above-bar pairs banned the 97.3 prohibition above. The
        #     pattern is the same each time — ANY rule keyed on a number
        #     APPEARING in prose will eventually forbid a sentence whose whole
        #     purpose is to forbid that number.
        #
        #     So the check stays where it can be exact: the ledgers, against
        #     the deny-list. The narrative documents are covered by the
        #     membership and ABSENT-vs-INCONCLUSIVE tests in this class, and
        #     the deny-list evidence itself is tested per family. Ban the
        #     thing, not the word for it (EDGE.md §12c).

    def test_the_intake_is_either_empty_or_holds_exactly_one_open_thesis(self):
        """The durable rule, not a point-in-time state.

        Slice 51 asserted `WAITING — EMPTY`, which was true the moment it was
        written and false the moment a human filed the next thesis — the same
        cry-wolf failure as every `== N` count assertion this suite has had to
        generalise. What must hold at all times is weaker and more useful:
        the slot is empty, or it names exactly one family that is not frozen.
        """
        text = self._read("NEW_SIGNAL_INTAKE.md")
        # Read the intake's OWN declaration of what is open, not the contents
        # of `signals/`. Design-before-code means the slot can name a thesis
        # whose module does not exist yet — slice 53 hit exactly that, because
        # the human files the intake before any implementation.
        header = text.split("## Thesis", 1)[0]
        declared = re.findall(r"\*\*Signal name:\*\*\s*`([a-z0-9_]+)`", text)
        open_theses = [n for n in declared if n not in ps.ABSENT_SIGNALS]
        assert len(open_theses) <= 1, open_theses
        if not open_theses:
            assert "WAITING — EMPTY" in text
        else:
            assert "HUMAN-FILLED" in header, (
                "an open thesis must be presented as human-filled, so that a "
                "reader can tell who proposed it")

    #: Words that mark a family as closed. A vocabulary, not a magic phrase.
    CLOSED_MARKERS = ("COMPLETED", "CLOSED", "FROZEN", "ABSENT",
                      "INCONCLUSIVE", "do not reopen",
                      "must not be re-implemented")

    def test_no_frozen_family_is_presented_as_an_open_thesis(self):
        """A frozen name may appear in the intake only as a closed record.

        NARROWED AND STRENGTHENED IN SLICE 57, AFTER IT REJECTED A CORRECT
        DOCUMENT
        ---------------------------------------------------------------------
        This matched two literal phrases anywhere in the header. The slice-57
        human intake names `funding_carry_fade_v1` in a "Relationship to frozen
        research" table that reads "FROZEN ABSENT (11th line) | Multi-symbol
        >=2 rule failed. **Do not reopen.**" — which says exactly what the test
        wants, in the author's own words — and the test went red.

        That is EDGE.md §12c for the sixth time: a guard keyed on a specific
        string eventually forbids a sentence written to satisfy it. Worse, the
        old form was also WEAK: one "COMPLETED — CLOSED" anywhere in the header
        satisfied it for EVERY frozen name mentioned, so a document could close
        one family and quietly reopen another.

        The replacement is line-scoped and vocabulary-based: every line that
        names a frozen family must itself carry a closed marker. Strictly
        stronger than what it replaces, and it accepts a human who writes
        "FROZEN ABSENT ... do not reopen" instead of the exact incantation.
        """
        text = self._read("NEW_SIGNAL_INTAKE.md")
        header = text.split("## Thesis", 1)[0]
        for name in ps.ABSENT_SIGNALS:
            for line in header.splitlines():
                if name not in line:
                    continue
                assert any(m.lower() in line.lower()
                           for m in self.CLOSED_MARKERS), (name, line[:160])

    def test_that_guard_still_catches_a_frozen_name_offered_as_open(self):
        """The control. Without it the vocabulary above could be so broad that
        the check passes on anything."""
        offending = (
            "# NEW SIGNAL — INTAKE FORM (FILLED)\n\n"
            "**Signal name:** `donchian_breakout_v1`\n\n"
            "Let us try N = 20 this time.\n\n## Thesis\n")
        header = offending.split("## Thesis", 1)[0]
        hits = [line for line in header.splitlines()
                if "donchian_breakout_v1" in line
                and not any(m.lower() in line.lower()
                            for m in self.CLOSED_MARKERS)]
        assert hits, "the marker vocabulary has become vacuous"

    def test_every_implemented_signal_is_frozen_or_named_in_the_intake(self):
        """The durable invariant from slice 43, in the form that survives.

        A module in `signals/` that is neither frozen nor named by a human in
        the intake is a thesis nobody asked for. Slice 51 tightened this to
        "must be frozen" because the slot was empty at that moment; that is a
        state, not an invariant, and a human filing the next thesis makes it
        false. The version below holds in both worlds.
        """
        repo = self._repo()
        intake = self._read("NEW_SIGNAL_INTAKE.md")
        implemented = sorted(
            name[:-3] for name in os.listdir(os.path.join(repo, "signals"))
            if name.endswith(".py") and not name.startswith("__"))
        assert implemented
        for signal in implemented:
            assert signal in ps.ABSENT_SIGNALS or signal in intake, (
                f"{signal} is implemented but is neither frozen nor named in "
                f"the human intake")


class TestTheSlice51FreezeArtefact:
    """The machine-readable record must agree with the code it describes."""

    def _payload(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo, "artifacts", "slice51_ts_momentum_freeze.json")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_records_the_freeze_truthfully(self):
        """A dated record: membership and no-shrink, not equality.

        The artefact says 8 because 8 was true at slice 51. Slice 53 froze a
        ninth, and rewriting the record to match today's code would destroy
        the record.
        """
        payload = self._payload()
        assert payload["schema"] == "research_freeze/1"
        assert payload["frozen_name"] == "ts_momentum_v1"
        assert payload["status"] == ps.FROZEN_STATUS["ts_momentum_v1"] \
            == "INCONCLUSIVE"
        assert payload["freeze_reason"] == "INCONCLUSIVE_STATE_VS_EVENT_SCHEDULER"
        assert payload["frozen_absent_count"] == 8
        assert len(ps.ABSENT_SIGNALS) >= 8
        for name, status in payload["frozen_status"].items():
            assert name in ps.ABSENT_SIGNALS, name
            assert ps.FROZEN_STATUS[name] == status, name

    def test_it_quotes_no_percentile(self):
        payload = self._payload()
        assert payload["m1"] is None and payload["m2"] is None
        assert payload["no_trusted_stage1_number"] is True
        assert payload["is_absent_percentile_reading"] is False
        assert payload["edge_measurement_runs"] == 0

    def test_it_claims_nothing_about_progress(self):
        payload = self._payload()
        # A DATED artefact: it recorded a null cleared edge at the time it
        # was written and is never rewritten. Slice 57 registering a
        # different name later does not change what this record said.
        assert payload["cleared_edge_signal"] is None
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["live_authorized"] is False
        assert payload["models_current_present"] is False
        assert payload["new_signal_implemented"] is False
        assert payload["intake_slot"] == "WAITING — EMPTY"

    def test_the_constants_it_claims_unchanged_are_unchanged(self):
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, repo)
        from signals import ts_momentum_v1 as tsm
        declared = self._payload()["constants_unchanged"]
        assert declared["LOOKBACK"] == tsm.LOOKBACK == 10
        assert declared["SKIP"] == tsm.SKIP == 1
        assert declared["STOP_ATR"] == tsm.STOP_ATR
        assert declared["TAKE_PROFIT_R"] == tsm.TAKE_PROFIT_R
        assert declared["HORIZON"] == tsm.HORIZON
        assert self._payload()["scheduler_changed"] is False

    def test_every_evidence_log_it_cites_exists(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        payload = self._payload()
        assert payload["evidence_deleted_or_rewritten"] is False
        for path in payload["evidence_logs"]:
            assert os.path.exists(os.path.join(repo, path)), path

    def test_the_three_causes_are_kept_apart(self):
        causes = self._payload()["three_invalid_controls_three_causes"]
        assert set(causes) == {"range_location_fade_v1", "open_gap_fade_v1",
                               "ts_momentum_v1"}
        assert "ONLY instrument-bias evidence" in causes["range_location_fade_v1"]
        assert "DELETED" in causes["open_gap_fade_v1"]
        assert "PRESERVED" in causes["ts_momentum_v1"]


class TestTheSlice53FreezeOfSignFlipMomentum:
    """`sign_flip_momentum_v1` frozen ABSENT — the ninth closed line.

    This is the first freeze in four slices where the family actually has
    numbers, which inverts the honesty risk. The last three entries had to be
    guarded against a fabricated percentile. This one has three real readings,
    and the exposure runs the other way: ETH's 81.4 / 83.5 is the second-highest
    figure in the programme's history and is a FAILURE. The tests below exist so
    that it cannot drift into "nearly cleared", and so that `LOOKBACK` — the
    obvious lever toward it — cannot move without a human on the commit.
    """

    def test_the_signal_is_frozen(self):
        assert "sign_flip_momentum_v1" in ps.ABSENT_SIGNALS
        assert "sign_flip_momentum_v1" in ps.FROZEN_ABSENT
        assert ps.FROZEN_STATUS["sign_flip_momentum_v1"] == "ABSENT"

    def test_the_nine_names_slice_53_knew_about_are_all_still_frozen(self):
        """Membership and no-shrink. Slice 54 froze a tenth."""
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1",
                     "post_shock_fade_v1", "range_location_fade_v1",
                     "open_gap_fade_v1", "ts_momentum_v1",
                     "sign_flip_momentum_v1"):
            assert name in ps.ABSENT_SIGNALS, name
        assert len(ps.ABSENT_SIGNALS) >= 9
        assert ps.ABSENT_SIGNALS == tuple(ps.FROZEN_ABSENT)
        assert set(ps.FROZEN_STATUS) == set(ps.FROZEN_ABSENT)

    def test_the_bars_did_not_move(self):
        assert ps.M1_BAR == 95.0 and ps.M2_BAR == 95.0

    def test_it_is_absent_and_not_inconclusive(self):
        """The distinction the whole ledger turns on.

        Three families are INCONCLUSIVE because no reading was ever taken.
        This one has readings. Filing it with them would understate what is
        known; filing them with it would fabricate numbers they do not have.
        """
        why = ps.FROZEN_ABSENT["sign_flip_momentum_v1"]
        assert ps.FROZEN_STATUS["sign_flip_momentum_v1"] == "ABSENT"
        assert "NOT an INCONCLUSIVE closure" in why
        assert "percentiles EXIST here" in why
        assert "NO M1 and NO M2" not in why

    def test_the_evidence_carries_all_three_readings_and_their_counts(self):
        why = ps.FROZEN_ABSENT["sign_flip_momentum_v1"]
        for number in ("45.0/46.0", "81.4/83.5", "22.7/25.0",
                       "290", "134", "131", "95.0"):
            assert number in why, number

    def test_the_evidence_records_that_the_controls_passed(self):
        """An ABSENT is only worth anything if the ruler was checked first."""
        why = ps.FROZEN_ABSENT["sign_flip_momentum_v1"]
        assert "CONTROL THAT PASSED" in why
        assert "200/200 usable surrogates" in why
        assert "0% incompletes" in why
        assert "control_validated true" in why

    def test_the_evidence_calls_the_eth_reading_a_failure(self):
        """The one sentence most likely to be softened later."""
        why = ps.FROZEN_ABSENT["sign_flip_momentum_v1"]
        assert "is a FAILURE" in why
        assert "that is NOT the test" in why
        assert "45 / 81 / 23" in why

    def test_the_evidence_forbids_the_obvious_retune(self):
        why = ps.FROZEN_ABSENT["sign_flip_momentum_v1"]
        assert "LOOKBACK stays 10" in why
        assert "SKIP stays 1" in why
        assert "no second run" in why
        assert "no single-symbol rescue" in why

    def test_the_evidence_points_at_summaries_that_exist_and_agree(self):
        """Not a citation — the numbers are read back out of the artefacts.

        Compared at one decimal place, which is the precision the evidence
        quotes and the precision the measurement tool prints. ETH's stored
        percentile is 81.39999999999999; the entry says 81.4, and rounding is
        the correct relationship between them. Asserting exact float equality
        here would fail and would invite "correcting" the evidence to a
        fifteen-digit number that no reader wants.
        """
        import json
        why = ps.FROZEN_ABSENT["sign_flip_momentum_v1"]
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assert "slice52_edge_sign_flip_momentum_" in why
        expected = {"BTCUSD": (45.0, 46.0), "ETHUSDT": (81.4, 83.5),
                    "SOLUSDT": (22.7, 25.0)}
        for symbol, (m1, m2) in expected.items():
            path = os.path.join(
                repo, "artifacts",
                f"slice52_edge_sign_flip_momentum_{symbol}_summary.json")
            assert os.path.exists(path), symbol
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
            assert round(summary["m1"]["percentile"], 1) == m1, symbol
            assert round(summary["m2"]["percentile"], 1) == m2, symbol
            assert summary["verdict"] == "EDGE_EVIDENCE_ABSENT", symbol
            assert summary["control_validated"] is True, symbol
            control = os.path.join(
                repo, "artifacts",
                f"slice52_control_sign_flip_momentum_{symbol}_n1000.log")
            assert os.path.exists(control), symbol
            with open(control, encoding="utf-8") as handle:
                assert "CONTROL: **VALID**" in handle.read()

    def test_the_signal_constants_were_not_touched(self):
        """A freeze is not a licence to tidy the module on the way past."""
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, repo)
        from signals import sign_flip_momentum_v1 as sf
        assert sf.LOOKBACK == 10
        assert sf.SKIP == 1
        assert sf.STOP_ATR == 1.5
        assert sf.TAKE_PROFIT_R == 2.0
        assert sf.TAKE_PROFIT_ATR == 3.0
        assert sf.HORIZON == 5
        assert sf.LOCKUP == 1
        assert sf.ROUND_TRIP_BPS == 25.0

    def test_no_better_numbers_were_invented(self):
        """No reading above the bar may appear anywhere in this entry."""
        import re
        why = ps.FROZEN_ABSENT["sign_flip_momentum_v1"]
        pairs = re.findall(r"(\d{1,3}\.\d)\s*/\s*(\d{1,3}\.\d)", why)
        readings = [(float(a), float(b)) for a, b in pairs]
        assert (45.0, 46.0) in readings
        assert (81.4, 83.5) in readings
        assert (22.7, 25.0) in readings
        for m1, m2 in readings:
            if (m1, m2) == (95.0, 95.0):      # the bar itself, quoted as such
                continue
            assert m1 < 95.0 and m2 < 95.0, (m1, m2)

    # -- the refusal --------------------------------------------------------

    def test_a_perfect_forged_positive_is_refused(self, tmp_path):
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{symbol}_summary.json",
                          signal="sign_flip_momentum_v1", symbol=symbol,
                          control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_refusal_names_the_status(self, tmp_path, caplog):
        write_summary(tmp_path, name="sfm_summary.json",
                      signal="sign_flip_momentum_v1", symbol="BTCUSD",
                      control_validated=True,
                      m1={"percentile": 99.9},
                      m2={"percentile": 99.9, "passed": True})
        with caplog.at_level("ERROR"):
            assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None
        assert "ABSENT" in caplog.text

    def test_the_real_summaries_still_register_nothing(self):
        """The genuine slice-52 artefacts are ABSENT and must stay inert."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ri.assert_registration_is_sound(os.path.join(repo, "artifacts"))


class TestTheSlice54FreezeOfCompressionBreakout:
    """`compression_breakout_v1` frozen INCONCLUSIVE — the tenth closed line.

    This family is the mixed case, and it is the one most likely to be
    mis-filed later in either direction.

    Rounded UP to a dual-symbol ABSENT, it would imply a three-symbol refusal
    that was never performed — ETHUSDT and SOLUSDT produced no reading at all.
    Rounded DOWN to an empty INCONCLUSIVE, it would discard the one trustworthy
    number the slice produced: BTCUSD's 69.1 / 73.5 on 76 trades under a
    validated control.

    The tests below pin both halves at once, and pin the levers — `COMP_MAX`
    above all — that would turn 23 and 35 into passing counts.
    """

    def test_the_signal_is_frozen(self):
        assert "compression_breakout_v1" in ps.ABSENT_SIGNALS
        assert "compression_breakout_v1" in ps.FROZEN_ABSENT
        assert ps.FROZEN_STATUS["compression_breakout_v1"] == "INCONCLUSIVE"

    def test_the_ten_names_slice_54_knew_about_are_all_still_frozen(self):
        """Membership and no-shrink, not a dated count.

        This asserted `== 10` until slice 56 froze an eleventh. A freeze is
        permanent and the list only grows, so a test that pins its exact length
        goes red every time the programme does its job. The invariant worth
        holding is that none of slice 54's ten ever leaves.
        """
        for name in ("technical_analysis", "closed_analyser",
                     "donchian_breakout_v1", "btc_alt_spillover_v1",
                     "post_shock_fade_v1", "range_location_fade_v1",
                     "open_gap_fade_v1", "ts_momentum_v1",
                     "sign_flip_momentum_v1", "compression_breakout_v1"):
            assert name in ps.ABSENT_SIGNALS, name
        assert len(ps.ABSENT_SIGNALS) >= 10
        assert ps.ABSENT_SIGNALS == tuple(ps.FROZEN_ABSENT)
        assert set(ps.FROZEN_STATUS) == set(ps.FROZEN_ABSENT)

    def test_the_bars_did_not_move(self):
        assert ps.M1_BAR == 95.0 and ps.M2_BAR == 95.0

    # -- the two halves of the mixed case -----------------------------------

    def test_it_keeps_btcs_real_reading(self):
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        assert "69.1/73.5" in why
        assert "76 scheduled entries" in why
        assert "control_validated true" in why
        assert "95.0" in why

    def test_it_invents_no_reading_for_the_alts(self):
        """The decisive one. Two symbols have no number and must keep none."""
        import re
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        assert "NO M1 AND NO M2 FOR ETHUSDT OR SOLUSDT" in why
        assert "NOT MEASURED" in why
        pairs = re.findall(r"(\d{1,3}\.\d)\s*/\s*(\d{1,3}\.\d)", why)
        readings = {(a, b) for a, b in pairs}
        assert readings == {("69.1", "73.5")}, readings

    def test_it_refuses_both_wrong_labels(self):
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        assert ps.FROZEN_STATUS["compression_breakout_v1"] == "INCONCLUSIVE"
        assert "NOT a dual-symbol ABSENT closure" in why
        assert "Nor is it an empty INCONCLUSIVE" in why

    def test_it_records_that_positive_died_at_the_count_stage(self):
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        assert "AT THE COUNT STAGE" in why
        assert "23 and 35" in why
        assert ">= 50 hard gate" in why

    def test_it_records_the_controls_and_why_the_alts_failed(self):
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        assert "PASSED all three pre-declared clauses" in why
        assert "INVALID on incompletes" in why
        assert "11.5% and 5.5%" in why

    def test_it_does_not_blame_a_construction_mismatch(self):
        """Slices 49 and 50 failed for construction reasons; this did not."""
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        assert "SPARSE-SAMPLE failure" in why
        assert "NOT the slice-49 or slice-50 construction mismatch" in why

    def test_it_records_that_the_design_was_materially_different(self):
        """The filter earned the family its own name; that survives the freeze."""
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        assert "64-78% of channel breaks" in why
        assert "donchian_breakout_v1" in why

    def test_it_names_the_forbidden_levers(self):
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        assert "COMP_MAX stays 25" in why
        assert "BREAK_N stays 20" in why
        assert "no single-symbol claim" in why

    def test_the_evidence_points_at_artefacts_that_exist_and_agree(self):
        """The quoted reading is read back out of the summary it came from."""
        import json
        why = ps.FROZEN_ABSENT["compression_breakout_v1"]
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        summary = os.path.join(
            repo, "artifacts",
            "slice53_edge_compression_breakout_BTCUSD_summary.json")
        assert "slice53_edge_compression_breakout_BTCUSD_summary.json" in why
        assert os.path.exists(summary)
        with open(summary, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert round(payload["m1"]["percentile"], 1) == 69.1
        assert round(payload["m2"]["percentile"], 1) == 73.5
        assert payload["verdict"] == "EDGE_EVIDENCE_ABSENT"
        assert payload["control_validated"] is True

        count_log = os.path.join(
            repo, "artifacts", "slice53_count_finding_compression_breakout.log")
        assert "slice53_count_finding_compression_breakout.log" in why
        assert os.path.exists(count_log)
        with open(count_log, encoding="utf-8") as handle:
            text = handle.read()
        assert "MEETS the 50-trade target" in text
        assert "BELOW the 50-trade target" in text

        expected = {"BTCUSD": "CONTROL: **VALID**",
                    "ETHUSDT": "CONTROL: **INVALID",
                    "SOLUSDT": "CONTROL: **INVALID"}
        for symbol, marker in expected.items():
            path = os.path.join(
                repo, "artifacts",
                f"slice53_control_compression_breakout_{symbol}_n1000.log")
            assert os.path.exists(path), symbol
            with open(path, encoding="utf-8") as handle:
                assert marker in handle.read(), symbol

    def test_no_edge_artefact_exists_for_the_unmeasured_symbols(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        artifacts = os.path.join(repo, "artifacts")
        for symbol in ("ETHUSDT", "SOLUSDT"):
            hits = [f for f in os.listdir(artifacts)
                    if f.startswith(
                        f"slice53_edge_compression_breakout_{symbol}")]
            assert hits == [], (symbol, hits)

    def test_the_signal_constants_were_not_touched(self):
        """A freeze is not a licence to tidy the module on the way past."""
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, repo)
        from signals import compression_breakout_v1 as cb
        assert cb.COMP_MAX == 25.0
        assert cb.PCTILE_WINDOW == 50
        assert cb.BREAK_N == 20
        assert cb.ATR_PERIOD == 14
        assert cb.STOP_ATR == 1.5
        assert cb.TAKE_PROFIT_R == 2.0
        assert cb.TAKE_PROFIT_ATR == 3.0
        assert cb.HORIZON == 5
        assert cb.LOCKUP == 1
        assert cb.ROUND_TRIP_BPS == 25.0

    # -- the refusal --------------------------------------------------------

    def test_a_perfect_forged_positive_is_refused(self, tmp_path):
        for symbol in ("BTCUSD", "ETHUSDT", "SOLUSDT"):
            write_summary(tmp_path, name=f"{symbol}_summary.json",
                          signal="compression_breakout_v1", symbol=symbol,
                          control_validated=True,
                          m1={"percentile": 99.9},
                          m2={"percentile": 99.9, "passed": True})
        assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None

    def test_the_refusal_names_the_status(self, tmp_path, caplog):
        write_summary(tmp_path, name="cbv_summary.json",
                      signal="compression_breakout_v1", symbol="BTCUSD",
                      control_validated=True,
                      m1={"percentile": 99.9},
                      m2={"percentile": 99.9, "passed": True})
        with caplog.at_level("ERROR"):
            assert ps.cleared_edge_signal_from_artifacts(str(tmp_path)) is None
        assert "INCONCLUSIVE" in caplog.text

    def test_the_real_btc_summary_still_registers_nothing(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ri.assert_registration_is_sound(os.path.join(repo, "artifacts"))


class TestTheSlice54FreezeArtefact:
    """The machine-readable record must agree with the code it describes."""

    def _payload(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo, "artifacts",
                            "slice54_compression_breakout_freeze.json")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_it_records_the_freeze_truthfully(self):
        payload = self._payload()
        assert payload["schema"] == "research_freeze/1"
        assert payload["frozen_name"] == "compression_breakout_v1"
        assert payload["status"] == \
            ps.FROZEN_STATUS["compression_breakout_v1"] == "INCONCLUSIVE"
        assert payload["freeze_reason"] == \
            "INCONCLUSIVE_BTC_ABSENT_ALTS_NON_MEASURABLE"
        # A DATED artefact. It recorded the deny-list as it stood in slice 54
        # and it is never rewritten — a historical record edited to match
        # today's code stops being a record. So this asserts containment and
        # no-shrink rather than equality, which is what survived slice 56
        # adding an eleventh name.
        assert payload["frozen_absent_count"] == 10
        assert payload["frozen_absent_count"] <= len(ps.ABSENT_SIGNALS)
        assert set(payload["frozen_absent"]) <= set(ps.ABSENT_SIGNALS)
        for name, status in payload["frozen_status"].items():
            assert ps.FROZEN_STATUS[name] == status, name

    def test_it_keeps_the_measured_and_unmeasured_symbols_apart(self):
        payload = self._payload()
        assert set(payload["measured"]) == {"BTCUSD"}
        assert set(payload["not_measured"]) == {"ETHUSDT", "SOLUSDT"}
        btc = payload["measured"]["BTCUSD"]
        assert btc["control"] == "VALID" and btc["gate_met"] is True
        assert btc["m1"] < 95.0 and btc["m2"] < 95.0
        for symbol, entry in payload["not_measured"].items():
            assert entry["m1"] is None and entry["m2"] is None, symbol
            assert entry["control"] == "INVALID", symbol
            assert entry["gate_met"] is False, symbol

    def test_it_refuses_both_wrong_labels(self):
        payload = self._payload()
        assert payload["is_dual_symbol_absent"] is False
        assert payload["is_empty_inconclusive"] is False
        assert payload["family_positive_rule_met"] is False
        assert payload["positive_unreachable_at_count_stage"] is True

    def test_it_says_sixty_nine_is_not_almost_ninety_five(self):
        note = self._payload()["btc_reading_is_not_almost_95"]
        assert "26 and 22 points short" in note
        assert "not the test" in note

    def test_it_does_not_misattribute_the_alts_invalid(self):
        note = self._payload()["alts_invalid_cause"]
        assert "SPARSE SAMPLE" in note
        assert "not a construction mismatch" in note

    def test_it_claims_nothing_about_progress(self):
        payload = self._payload()
        # A DATED artefact: it recorded a null cleared edge at the time it
        # was written and is never rewritten. Slice 57 registering a
        # different name later does not change what this record said.
        assert payload["cleared_edge_signal"] is None
        assert payload["closer_to_autonomous_profit_agent"] is False
        assert payload["live_authorized"] is False
        assert payload["models_current_present"] is False
        assert payload["new_signal_implemented"] is False
        assert payload["intake_slot"] == "WAITING — EMPTY"
        assert payload["fabricated_percentiles"] is False
        assert payload["edge_or_control_rerun"] is False

    def test_the_constants_it_claims_unchanged_are_unchanged(self):
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, repo)
        from signals import compression_breakout_v1 as cb
        declared = self._payload()["constants_unchanged"]
        assert declared["COMP_MAX"] == cb.COMP_MAX == 25.0
        assert declared["BREAK_N"] == cb.BREAK_N == 20
        assert declared["PCTILE_WINDOW"] == cb.PCTILE_WINDOW
        assert declared["STOP_ATR"] == cb.STOP_ATR
        assert declared["TAKE_PROFIT_R"] == cb.TAKE_PROFIT_R
        assert declared["HORIZON"] == cb.HORIZON

    def test_every_evidence_log_it_cites_exists(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        payload = self._payload()
        assert payload["evidence_deleted_or_rewritten"] is False
        for path in payload["evidence_logs"]:
            assert os.path.exists(os.path.join(repo, path)), path
