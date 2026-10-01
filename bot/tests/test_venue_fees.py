"""venue_fees.extract_fee — THE one fee-extraction authority.

Before this module, `carry_broker.CarryBroker._fee` and
`trading_engine.venue_fee` were two separate implementations of the same
`cumExecFee` parsing rule. These tests prove there is now exactly one:
both consumers are checked for literal identity with (or a thin,
behaviour-preserving delegation to) `venue_fees.extract_fee`, so a second,
independently-reimplemented copy of the rule — however faithful — fails
these tests immediately.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import carry_broker  # noqa: E402
import trading_engine as te  # noqa: E402
import venue_fees as vf  # noqa: E402


class TestOneAuthority:
    def test_trading_engine_venue_fee_is_the_same_callable(self):
        """Not merely equivalent logic — the SAME function object."""
        assert te.venue_fee is vf.extract_fee

    def test_carry_broker_fee_delegates_to_the_same_rule(self):
        """`CarryBroker._fee` keeps its historical `Optional[float]`
        contract (every caller in that file expects a bare amount), but
        must derive it from `extract_fee`, not re-parse `cumExecFee`
        itself. Proven by agreement across the whole matrix below, not
        just a spot check."""
        rows = [
            {"cumExecFee": "0.0123"},
            {},
            {"cumExecFee": None},
            {"cumExecFee": ""},
            {"cumExecFee": "garbage"},
            {"cumExecFee": "nan"},
            {"cumExecFee": "inf"},
            {"cumExecFee": "0"},
            {"cumExecFee": "0.0", "feeCurrency": "BTC"},
        ]
        for row in rows:
            assert carry_broker.CarryBroker._fee(row) == vf.extract_fee(row).amount

    @staticmethod
    def _reads_cum_exec_fee_itself(path: str) -> bool:
        """True iff the source actually SUBSCRIPTS `cumExecFee` out of a
        row (`.get("cumExecFee")` / `["cumExecFee"]`), as opposed to just
        mentioning the field name in prose (a docstring/comment explaining
        why the shared helper exists). A bare substring check would flag
        every file that merely documents the convention; this checks the
        actual access pattern that would constitute a second parser.
        """
        import re
        src = open(path, encoding="utf-8").read()
        pattern = re.compile(r'''\[\s*["']cumExecFee["']\s*\]|\.get\(\s*["']cumExecFee["']''')
        return bool(pattern.search(src))

    def test_no_duplicate_parsing_logic_survives_in_trading_engine_source(self):
        """A regression guard against reintroducing a second copy of the
        parsing rule under a new name: `trading_engine.py` must not pull
        `cumExecFee` out of a row itself anywhere outside delegating to
        `venue_fees` (mentioning the field name in a comment/docstring is
        fine and expected documentation, not a second authority)."""
        assert not self._reads_cum_exec_fee_itself(te.__file__), (
            "trading_engine.py reads cumExecFee directly instead of going "
            "through venue_fees.extract_fee — a second authority has crept "
            "back in")

    def test_no_duplicate_parsing_logic_survives_in_carry_broker_source(self):
        assert not self._reads_cum_exec_fee_itself(carry_broker.__file__), (
            "carry_broker.py reads cumExecFee directly instead of going "
            "through venue_fees.extract_fee — a second authority has crept "
            "back in")


class TestExtractionMatrix:
    """The exact matrix the hardening pass calls for."""

    def test_valid_integer_like_fee(self):
        fee = vf.extract_fee({"cumExecFee": "5"})
        assert fee.amount == 5.0
        assert fee.known is True

    def test_valid_decimal_fee(self):
        fee = vf.extract_fee({"cumExecFee": "0.00001234"})
        assert fee.amount == 0.00001234
        assert fee.known is True

    def test_missing_cumExecFee(self):
        fee = vf.extract_fee({"orderStatus": "Filled"})
        assert fee.amount is None
        assert fee.known is False

    def test_empty_string_cumExecFee(self):
        fee = vf.extract_fee({"cumExecFee": ""})
        assert fee.known is False

    def test_whitespace_only_cumExecFee(self):
        assert vf.extract_fee({"cumExecFee": "   "}).known is False

    def test_malformed_string(self):
        assert vf.extract_fee({"cumExecFee": "not-a-number"}).known is False

    def test_nan(self):
        fee = vf.extract_fee({"cumExecFee": "nan"})
        assert fee.known is False
        assert fee.amount is None

    def test_infinity(self):
        assert vf.extract_fee({"cumExecFee": "inf"}).known is False
        assert vf.extract_fee({"cumExecFee": "-inf"}).known is False

    def test_none_row(self):
        assert vf.extract_fee(None) == vf.UNKNOWN_FEE

    def test_empty_row(self):
        assert vf.extract_fee({}) == vf.UNKNOWN_FEE

    def test_valid_amount_with_valid_currency(self):
        fee = vf.extract_fee({"cumExecFee": "0.000002355", "feeCurrency": "BTC"})
        assert fee.amount == 0.000002355
        assert fee.currency == "BTC"

    def test_valid_amount_with_lowercase_currency_is_normalised(self):
        fee = vf.extract_fee({"cumExecFee": "0.05", "feeCurrency": "usdt"})
        assert fee.currency == "USDT"

    def test_malformed_currency_is_unknown_not_guessed(self):
        for bad in ("", "   ", "this is not a ticker", "a" * 13, None):
            fee = vf.extract_fee({"cumExecFee": "0.01", "feeCurrency": bad})
            assert fee.amount == 0.01
            assert fee.currency is None, f"bad currency {bad!r} was not rejected"

    def test_missing_currency_with_known_amount_stays_unknown_currency(self):
        fee = vf.extract_fee({"cumExecFee": "0.01"})
        assert fee.amount == 0.01
        assert fee.currency is None

    def test_known_zero_is_not_unknown(self):
        """`known_zero != unknown` — the venue can genuinely charge 0."""
        fee = vf.extract_fee({"cumExecFee": "0", "feeCurrency": "BTC"})
        assert fee.known is True
        assert fee.amount == 0.0
        assert fee.currency == "BTC"

    def test_unexpected_payload_nesting_does_not_crash(self):
        """A nested dict under cumExecFee is not a number — refuse, don't
        raise and don't coerce."""
        fee = vf.extract_fee({"cumExecFee": {"amount": "0.01"}})
        assert fee.known is False

    def test_currency_never_inferred_from_symbol(self):
        """Even with `symbol: BTCUSDT` right there in the row, a missing
        `feeCurrency` must stay unknown — never guessed as BTC or USDT."""
        fee = vf.extract_fee({"cumExecFee": "0.01", "symbol": "BTCUSDT"})
        assert fee.currency is None

    def test_unit_conversion_never_happens_in_extraction(self):
        """extract_fee reports exactly what the venue said — converting a
        BTC amount into anything else is a separate, explicit operation
        this module refuses to perform implicitly."""
        fee = vf.extract_fee({"cumExecFee": "0.00001", "feeCurrency": "BTC"})
        assert fee.amount == 0.00001  # untouched, no price multiplied in
