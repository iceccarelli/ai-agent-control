"""The path by which a book earns its own execution samples, proven end to end
over a TEST DOUBLE venue. This proves plumbing - that real fills flow from the
engine through the ledger into the estimator and move the gate from "cannot
price" to "priced, but no strategy evidence" - and nothing about any market:
a double's costs are not evidence of anything."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carry_engine as ce                                    # noqa: E402
import entry_benefit_gate as ebg                             # noqa: E402
import execution_cost_estimate as ece                        # noqa: E402
from market_snapshot import MarketSnapshot                   # noqa: E402
from test_paired_leg_recovery import FeeVenue, MARK, modern, stack  # noqa: E402

H8 = 8 * 3600 * 1000


class Driver:
    def __init__(self, tmp_path):
        self.store, self.venue, self.broker, self.eng = stack(
            str(tmp_path / "s.db"), FeeVenue(modern, spot_qty=0.0))
        self.eng.pair_risk.max_entries_per_day = 1000
        self.stamp = 0
        self.cycles = 0

    def tick(self, funding):
        self.stamp += H8
        self.eng.snapshot = MarketSnapshot(
            perp_mark=MARK, spot_mark=MARK, funding_bps=funding,
            margin_multiple=5.0, observed_at_s=time.time(),
            perp_book=(MARK - 5.0, MARK + 5.0), spot_book=(MARK - 5.0, MARK + 5.0),
            book_observed_s=time.time())
        return self.eng.on_candle(mark=MARK, funding_bps=funding, spot=MARK,
                                  funding_print_ms=self.stamp)

    def cycle(self):
        for _ in range(10):
            self.tick(3.0)
            if self.eng.position is not None:
                break
        assert self.eng.position is not None
        self.eng.position.negative_funding_streak = ce.NEGATIVE_FUNDING_EXIT_PRINTS
        self.tick(-3.0)
        assert self.eng.position is None
        self.cycles += 1

    def estimate(self):
        return ece.estimate_round_trip(
            self.eng.broker.journal.entries, as_of_ms=int(time.time() * 1000) + 5000,
            execution_mode="acquire", perp_symbol="BTCUSDT", spot_symbol="BTCUSDT",
            planned_notional_usd=self.eng.max_notional_usd)

    def gate(self, **kw):
        return ebg.EntryBenefitGate(**kw).check(
            now_ms=int(time.time() * 1000) + 5000,
            entries=self.eng.broker.journal.entries, execution_mode="acquire",
            perp_symbol="BTCUSDT", spot_symbol="BTCUSDT", mark=MARK, spot=MARK,
            funding_history=[3.0] * 5,
            last_print_ms=int(time.time() * 1000) - 3_600_000,
            round_trip_bps=self.eng.round_trip_bps(), borrow_apr=0.0,
            opportunity_cost_apr=0.0, notional_usd=self.eng.max_notional_usd)


def test_a_fresh_book_cannot_price_its_execution(tmp_path):
    d = Driver(tmp_path)
    est = d.estimate()
    assert est["status"] == ece.UNAVAILABLE and est["spread_bps"] is None
    assert d.gate().reason == ebg.BENEFIT_UNAVAILABLE
    d.store.close()


def test_its_own_round_trips_earn_it_entry_and_exit_samples(tmp_path):
    d = Driver(tmp_path)
    for _ in range(ece.DEFAULT_POLICY.min_samples + 2):
        d.cycle()
    est = d.estimate()
    assert est["status"] == ece.MEASURED, est["reasons"]
    legs = {l["leg"]: l for l in est["legs"]}
    for name in ("linear/Sell/market", "linear/Buy/market",
                 "spot/Buy/market", "spot/Sell/market"):
        assert legs[name]["n_in_size_band"] >= ece.DEFAULT_POLICY.min_samples, name
    d.store.close()


def test_with_samples_the_gate_moves_from_unpriced_to_unqualified(tmp_path):
    """Enough execution history changes the refusal from 'cannot price' to
    'priced, and no strategy evidence'. It does not make the entry pass."""
    d = Driver(tmp_path)
    for _ in range(ece.DEFAULT_POLICY.min_samples + 2):
        d.cycle()
    r = d.gate()
    assert r.trace["inputs"]["execution_cost"]["status"] == "MEASURED"
    assert r.trace["layers"]["entry_benefit"]["passed"] is True
    assert r.reason == ebg.EVIDENCE_INSUFFICIENT and not r.allowed
    d.store.close()


def test_fewer_cycles_than_the_policy_still_cannot_price(tmp_path):
    d = Driver(tmp_path)
    for _ in range(ece.DEFAULT_POLICY.min_samples - 1):
        d.cycle()
    assert d.estimate()["status"] == ece.UNAVAILABLE
    assert d.gate().reason == ebg.BENEFIT_UNAVAILABLE
    d.store.close()
