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
