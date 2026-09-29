"""tools/venue_evidence.py: redaction, environment-honesty, and the
append-only hash chain.

No network. Every record here is synthetic and clearly marked as such — see
the module docstring's "NO REAL VENUE EVIDENCE SHIPS WITH THIS MODULE".
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import venue_evidence as ve  # noqa: E402

VALID_GIT_COMMIT = "a" * 40


@pytest.fixture(autouse=True)
def _patched_git_identity(monkeypatch):
    monkeypatch.setattr(ve.prov, "git_commit", lambda *a, **kw: VALID_GIT_COMMIT)


class TestRedaction:
    def test_top_level_secret_is_redacted(self):
        out = ve.redact({"api_key": "topsecret", "symbol": "BTCUSDT"})
        assert out["api_key"] == ve._REDACTED
        assert out["symbol"] == "BTCUSDT"

    def test_nested_secret_is_redacted(self):
        out = ve.redact({"headers": {"X-BAPI-SIGN": "abc123",
                                     "Content-Type": "application/json"}})
        assert out["headers"]["X-BAPI-SIGN"] == ve._REDACTED
        assert out["headers"]["Content-Type"] == "application/json"

    def test_secret_inside_a_list_is_redacted(self):
        out = ve.redact({"args": ["key123", {"signature": "sig456"}]})
        assert out["args"][1]["signature"] == ve._REDACTED

    def test_key_matching_is_case_insensitive(self):
        out = ve.redact({"API_KEY": "x", "Api-Secret": "y"})
        # Api-Secret has a hyphen, not underscore — only exact known keys
        # (case-insensitive) are redacted, so this one is untouched; proves
        # redaction targets specific field names, not a fuzzy match.
        assert out["API_KEY"] == ve._REDACTED

    def test_redact_does_not_mutate_the_input(self):
        original = {"api_key": "topsecret"}
        ve.redact(original)
        assert original["api_key"] == "topsecret"

    def test_non_sensitive_payload_is_unchanged(self):
        payload = {"symbol": "BTCUSDT", "qty": "0.01", "side": "Buy"}
        assert ve.redact(payload) == payload


class TestEnvironmentClaim:
    def test_testnet_claim_matching_testnet_url_is_accepted(self):
        ve.validate_environment_claim(
            declared_environment="testnet",
            request_url="https://api-testnet.bybit.com/v5/order/create")

    def test_mainnet_claim_against_testnet_url_is_refused(self):
        """The concrete law: a testnet transcript cannot become mainnet
        evidence."""
        with pytest.raises(ve.EvidenceRefused):
            ve.validate_environment_claim(
                declared_environment="mainnet",
                request_url="https://api-testnet.bybit.com/v5/order/create")

    def test_simulation_claim_against_a_real_venue_url_is_refused(self):
        """A real network call cannot be filed as a simulation."""
        with pytest.raises(ve.EvidenceRefused):
            ve.validate_environment_claim(
                declared_environment="simulation",
                request_url="https://api-demo.bybit.com/v5/order/create")

    def test_networked_claim_with_no_url_is_refused(self):
        with pytest.raises(ve.EvidenceRefused):
            ve.validate_environment_claim(declared_environment="testnet",
                                          request_url="")

    def test_simulation_with_no_url_is_accepted(self):
        ve.validate_environment_claim(declared_environment="simulation",
                                      request_url="")

    def test_unknown_environment_name_is_refused(self):
        with pytest.raises(ve.EvidenceRefused):
            ve.validate_environment_claim(declared_environment="staging",
                                          request_url="")

    def test_demo_claim_against_mainnet_url_is_refused(self):
        with pytest.raises(ve.EvidenceRefused):
            ve.validate_environment_claim(
                declared_environment="demo",
                request_url="https://api.bybit.com/v5/order/create")

    def test_infer_environment_from_url_unknown_host(self):
        assert ve.infer_environment_from_url("https://example.com/x") == "unknown"

    def test_infer_environment_from_url_recognizes_each_venue(self):
        assert ve.infer_environment_from_url(
            "https://api.bybit.com/v5/order/create") == "mainnet"
        assert ve.infer_environment_from_url(
            "https://api-testnet.bybit.com/v5/order/create") == "testnet"
        assert ve.infer_environment_from_url(
            "https://api-demo.bybit.com/v5/order/create") == "demo"


class TestBuildRecord:
    def _valid(self, **overrides):
        kw = dict(
            venue="bybit",
            environment="testnet",
            method="POST",
            request_url="https://api-testnet.bybit.com/v5/order/create",
            request={"symbol": "BTCUSDT", "api_key": "shh"},
            response={"retCode": 0, "orderId": "abc123"},
            order_link_id="BB-entr-1-deadbeef",
        )
        kw.update(overrides)
        return ve.build_record(**kw)

    def test_record_has_expected_schema_fields(self):
        record = self._valid()
        for field in ("schema", "venue", "environment", "captured_at_utc",
                     "method", "request_url", "request", "response",
                     "order_link_id", "git_commit", "prev_hash",
                     "record_hash"):
            assert field in record

    def test_request_secrets_are_redacted_in_the_record(self):
        record = self._valid()
        assert record["request"]["api_key"] == ve._REDACTED

    def test_git_commit_is_stamped(self):
        record = self._valid()
        assert record["git_commit"] == VALID_GIT_COMMIT

    def test_default_prev_hash_is_genesis(self):
        record = self._valid()
        assert record["prev_hash"] == ve.GENESIS_PREV_HASH

    def test_record_hash_matches_its_own_content(self):
        record = self._valid()
        body = {k: v for k, v in record.items() if k != "record_hash"}
        assert record["record_hash"] == ve.content_hash(body)

    def test_environment_mismatch_refuses_to_build(self):
        with pytest.raises(ve.EvidenceRefused):
            self._valid(environment="mainnet")

    def test_from_order_result_duck_type(self):
        class FakeOrderResult:
            ok = True
            order_id = "OID-1"
            order_link_id = "BB-entr-1-deadbeef"
            reason = ""
            ret_code = 0
            raw = {"retCode": 0, "result": {"orderId": "OID-1"}}

        record = ve.build_record_from_order_result(
            FakeOrderResult(), venue="bybit", environment="testnet",
            request_url="https://api-testnet.bybit.com/v5/order/create",
            request={"symbol": "BTCUSDT", "sign": "shh"})
        assert record["response"]["ok"] is True
        assert record["response"]["order_id"] == "OID-1"
        assert record["order_link_id"] == "BB-entr-1-deadbeef"
        assert record["request"]["sign"] == ve._REDACTED


class TestAppendOnlyHashChain:
    def _record(self, prev_hash, **overrides):
        kw = dict(
            venue="bybit", environment="testnet", method="POST",
            request_url="https://api-testnet.bybit.com/v5/order/create",
            request={"symbol": "BTCUSDT"}, response={"retCode": 0},
            order_link_id="BB-entr-1", prev_hash=prev_hash,
        )
        kw.update(overrides)
        return ve.build_record(**kw)

    def test_first_append_uses_genesis_prev_hash(self, tmp_path):
        path = str(tmp_path / "evidence.jsonl")
        record = self._record(ve.last_record_hash(path))
        ve.append_evidence(path, record)
        assert ve.verify_chain(path) == []

    def test_second_append_chains_from_the_first(self, tmp_path):
        path = str(tmp_path / "evidence.jsonl")
        r1 = self._record(ve.last_record_hash(path))
        ve.append_evidence(path, r1)
        r2 = self._record(ve.last_record_hash(path), order_link_id="BB-entr-2")
        ve.append_evidence(path, r2)

        with open(path, encoding="utf-8") as h:
            lines = [json.loads(ln) for ln in h if ln.strip()]
        assert len(lines) == 2
        assert lines[1]["prev_hash"] == lines[0]["record_hash"]
        assert ve.verify_chain(path) == []

    def test_append_with_stale_prev_hash_is_refused(self, tmp_path):
        path = str(tmp_path / "evidence.jsonl")
        r1 = self._record(ve.last_record_hash(path))
        ve.append_evidence(path, r1)

        # A second record built against a STALE tip (never re-read after
        # r1 was appended) must be refused, not silently accepted.
        stale = self._record(ve.GENESIS_PREV_HASH, order_link_id="BB-entr-2")
        with pytest.raises(ve.EvidenceRefused):
            ve.append_evidence(path, stale)

        # And the file itself is untouched by the refused append.
        with open(path, encoding="utf-8") as h:
            lines = [ln for ln in h if ln.strip()]
        assert len(lines) == 1

    def test_tampered_record_hash_is_detected_by_verify_chain(self, tmp_path):
        path = str(tmp_path / "evidence.jsonl")
        r1 = self._record(ve.last_record_hash(path))
        ve.append_evidence(path, r1)

        with open(path, encoding="utf-8") as h:
            lines = h.readlines()
        tampered = json.loads(lines[0])
        tampered["response"] = {"retCode": 0, "orderId": "FORGED"}
        with open(path, "w", encoding="utf-8") as h:
            h.write(json.dumps(tampered) + "\n")

        reasons = ve.verify_chain(path)
        assert any("record_hash does not match" in r for r in reasons)

    def test_deleted_middle_record_breaks_the_chain(self, tmp_path):
        path = str(tmp_path / "evidence.jsonl")
        for i in range(3):
            r = self._record(ve.last_record_hash(path), order_link_id=f"BB-{i}")
            ve.append_evidence(path, r)

        with open(path, encoding="utf-8") as h:
            lines = [ln for ln in h if ln.strip()]
        del lines[1]
        with open(path, "w", encoding="utf-8") as h:
            h.writelines(lines)

        reasons = ve.verify_chain(path)
        assert any("does not chain from the prior record" in r for r in reasons)

    def test_verify_chain_on_missing_file_is_clean(self, tmp_path):
        assert ve.verify_chain(str(tmp_path / "nope.jsonl")) == []

    def test_last_record_hash_on_missing_file_is_genesis(self, tmp_path):
        assert ve.last_record_hash(str(tmp_path / "nope.jsonl")) == ve.GENESIS_PREV_HASH

    def test_append_never_truncates_existing_lines(self, tmp_path):
        path = str(tmp_path / "evidence.jsonl")
        r1 = self._record(ve.last_record_hash(path))
        ve.append_evidence(path, r1)
        before = open(path, encoding="utf-8").read()

        r2 = self._record(ve.last_record_hash(path), order_link_id="BB-2")
        ve.append_evidence(path, r2)
        after = open(path, encoding="utf-8").read()

        assert after.startswith(before)


class TestEvidenceCapturingTransport:
    """The real adapter integration point: wrapping a Transport captures
    evidence for every request that passes through it."""

    class _StubTransport:
        def __init__(self, responses):
            self._responses = list(responses)
            self.calls = []

        def request(self, method, url, *, headers, params=None, body=None,
                   timeout=10.0):
            self.calls.append({"method": method, "url": url,
                               "headers": dict(headers)})
            return self._responses.pop(0)

    def test_wrapped_request_still_returns_the_real_result(self, tmp_path):
        inner = self._StubTransport([(200, '{"retCode": 0}')])
        wrapped = ve.EvidenceCapturingTransport(
            inner, evidence_path=str(tmp_path / "evidence.jsonl"),
            venue="bybit", environment="testnet")

        status, text = wrapped.request(
            "POST", "https://api-testnet.bybit.com/v5/order/create",
            headers={"X-BAPI-API-KEY": "shh"}, body='{"symbol":"BTCUSDT"}')

        assert status == 200
        assert text == '{"retCode": 0}'

    def test_wrapped_request_appends_a_redacted_evidence_record(self, tmp_path):
        inner = self._StubTransport([(200, '{"retCode": 0, "result": {}}')])
        path = str(tmp_path / "evidence.jsonl")
        wrapped = ve.EvidenceCapturingTransport(
            inner, evidence_path=path, venue="bybit", environment="testnet")

        wrapped.request(
            "POST", "https://api-testnet.bybit.com/v5/order/create",
            headers={"X-BAPI-API-KEY": "shh", "X-BAPI-SIGN": "sig"},
            body='{"symbol":"BTCUSDT","api_key":"shh"}')

        with open(path, encoding="utf-8") as h:
            record = json.loads(h.readline())
        assert record["environment"] == "testnet"
        assert record["request"]["headers"]["X-BAPI-API-KEY"] == ve._REDACTED
        assert record["request"]["headers"]["X-BAPI-SIGN"] == ve._REDACTED
        assert record["response"]["_status_code"] == 200
        assert ve.verify_chain(path) == []

    def test_multiple_requests_chain_correctly(self, tmp_path):
        inner = self._StubTransport([
            (200, '{"retCode": 0, "call": 1}'),
            (200, '{"retCode": 0, "call": 2}'),
        ])
        path = str(tmp_path / "evidence.jsonl")
        wrapped = ve.EvidenceCapturingTransport(
            inner, evidence_path=path, venue="bybit", environment="testnet")

        wrapped.request("GET", "https://api-testnet.bybit.com/v5/order/list",
                        headers={})
        wrapped.request("GET", "https://api-testnet.bybit.com/v5/order/list",
                        headers={})

        assert ve.verify_chain(path) == []
        with open(path, encoding="utf-8") as h:
            lines = [json.loads(ln) for ln in h if ln.strip()]
        assert len(lines) == 2
        assert lines[1]["prev_hash"] == lines[0]["record_hash"]

    def test_environment_mismatch_does_not_raise_or_block_the_real_call(
            self, tmp_path, caplog):
        """A mismatched environment claim (this wrapper pointed at testnet,
        but told to label mainnet) must never surface as an exception to
        the caller -- capture failure degrades observability, not
        execution -- but it also must not be silent."""
        inner = self._StubTransport([(200, '{"retCode": 0}')])
        path = str(tmp_path / "evidence.jsonl")
        wrapped = ve.EvidenceCapturingTransport(
            inner, evidence_path=path, venue="bybit", environment="mainnet")

        status, text = wrapped.request(
            "POST", "https://api-testnet.bybit.com/v5/order/create",
            headers={})

        assert status == 200  # the real call still succeeded
        assert not os.path.exists(path) or open(path).read() == ""
        assert any("venue evidence capture failed" in r.message
                   for r in caplog.records)

    def test_non_json_response_is_captured_without_raising(self, tmp_path):
        inner = self._StubTransport([(503, "<html>maintenance</html>")])
        path = str(tmp_path / "evidence.jsonl")
        wrapped = ve.EvidenceCapturingTransport(
            inner, evidence_path=path, venue="bybit", environment="testnet")

        status, text = wrapped.request(
            "GET", "https://api-testnet.bybit.com/v5/market/time", headers={})

        assert status == 503
        with open(path, encoding="utf-8") as h:
            record = json.loads(h.readline())
        assert record["response"]["_raw_text"] == "<html>maintenance</html>"


class TestEvidenceCapturingTransportWiredIntoRealBybitClient:
    """Proves this is wired into the actual adapter, not exercised only in
    isolation: a real `bybit_connection.BybitClient` call, through the real
    signing/request path, against `FakeBybit`, produces real evidence."""

    def test_a_real_place_order_call_produces_an_evidence_record(
            self, tmp_path):
        sys.path.insert(0, REPO)
        import bybit_connection as bc
        from fake_bybit import API_KEY, API_SECRET, FakeBybit
        from persistence import StateStore

        class Cfg:
            USE_TESTNET = True
            PAPER_TRADING = False
            BYBIT_API_KEY = API_KEY
            BYBIT_API_SECRET = API_SECRET
            BYBIT_RECV_WINDOW_MS = 5000
            REQUEST_TIMEOUT_SECONDS = 5.0
            USE_LEVERAGE = False
            SYMBOL_FILTERS_TTL = 3600
            ORDERLINK_PREFIX = "BB"

        exchange = FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0},
                             equity=100_000.0)
        evidence_path = str(tmp_path / "evidence.jsonl")
        wrapped_transport = ve.EvidenceCapturingTransport(
            exchange, evidence_path=evidence_path, venue="bybit",
            environment="testnet")
        store = StateStore(str(tmp_path / "state.db"))
        client = bc.BybitClient(config=Cfg(), store=store,
                                transport=wrapped_transport)

        result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.01)

        assert result.ok
        assert os.path.exists(evidence_path)
        with open(evidence_path, encoding="utf-8") as h:
            records = [json.loads(ln) for ln in h if ln.strip()]
        assert len(records) >= 1, "no evidence captured for a real client call"
        assert any("order/create" in r["request_url"] for r in records)
        assert ve.verify_chain(evidence_path) == []
        # The client's real signing still redacted-through correctly: the
        # signature header itself is captured but redacted, never the
        # plaintext order parameters needed to reconstruct what happened.
        order_record = next(r for r in records if "order/create" in r["request_url"])
        assert order_record["request"]["headers"].get("X-BAPI-SIGN") == ve._REDACTED
        assert "BTCUSDT" in (order_record["request"].get("body") or "")
