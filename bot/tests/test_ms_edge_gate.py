"""The expected-NET-edge gate: a funding rate is not a profit. Built on the one economic model (economic_model.evaluate_chain)."""
from __future__ import annotations

import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import economic_model as EM  # noqa: E402
import ms_edge_gate as EG  # noqa: E402
from ms_testnet_world import World  # noqa: E402

NOW = 1_794_312_000_000
PERP_ROW = {"symbol": "BTCUSDT", "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT", "fundingInterval": "480", "upperFundingRate": "0.0075", "lowerFundingRate": "-0.0075",
            "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}}
SPOT_ROW = {"symbol": "BTCUSDT", "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT", "priceFilter": {"tickSize": "0.01"},
            "lotSizeFilter": {"basePrecision": "0.000001", "minOrderAmt": "1"}}


def book(mid="75000", half="0.05", depth="20", ts=NOW):
    m, h, q = D(mid), D(half), D(depth)
    return EM.Book(bids=((m - h, q), (m - h - 1, q)), asks=((m + h, q), (m + h + 1, q)), ts_ms=ts)


DAY_MS = 86_400_000
#: the owner's STATED alternative return on capital (4%/yr, stated yesterday). The gate no longer prices an unstated cost of capital as free: every test that is not ABOUT the cost of capital states one.
STATED = EG.CashAlternative(apr=D("0.04"), source="OWNER_STATED", stated_ms=NOW - DAY_MS)


def inputs(rate="0.0001", measured=True, **over):
    kw = dict(symbol="BTCUSDT", notional_usd=D("375"), now_ms=NOW, funding_rate=D(rate), next_funding_ms=NOW + 600_000, hold_s=720, perp_row=PERP_ROW, spot_row=SPOT_ROW,
              perp_book=book(), spot_book=book(), leverage=1,
              perp_fee=(D("0.0002"), D("0.00055")) if measured else None, spot_fee=(D("0.001"), D("0.001")) if measured else None, fee_observed_ms=NOW if measured else None,
              cash_alternative=STATED)
    kw.update(over)
    return EG.EdgeInputs(**kw)


def test_the_typical_testnet_funding_of_one_bp_is_uneconomic_and_the_required_funding_is_derived():
    v = EG.evaluate_edge(inputs("0.0001"))
    assert v.session_class == EG.REJECTED and v.settlements_in_hold == 1
    assert D(v.net_edge_bps) < 0 and D(v.expected_net_usd) < 0                       # about -31 bps of fees alone against 0.7 bps of expected funding
    assert D(v.costs_bps["fees"]) == D("31")                                         # spot 0.1% + perp 0.055%, entry and exit
    assert D(v.required_funding_bps) > D("40")                                       # the funding that WOULD pay: derived from the chain, not typed in
    assert any(r.startswith("NET_EDGE_") for r in v.reasons)


def test_the_required_funding_is_the_exact_boundary_between_rejected_and_profit_candidate():
    base = EG.evaluate_edge(inputs("0.0001"))
    req = D(base.required_funding_bps) / EG.BPS                                      # fraction per settlement
    above = EG.evaluate_edge(inputs(str(req * D("1.02"))))
    below = EG.evaluate_edge(inputs(str(req * D("0.98"))))
    assert above.session_class == EG.PROFIT_CANDIDATE and below.session_class == EG.REJECTED
    assert D(above.net_edge_bps) >= EG.MIN_NET_EDGE_BPS > D(below.net_edge_bps)


def test_an_assumed_fee_tier_can_never_pass_even_when_the_funding_is_huge():
    v = EG.evaluate_edge(inputs("0.0100", measured=False))                            # 100 bps: gross alone would clear every cost
    assert v.session_class == EG.INCONCLUSIVE and "fees" in v.assumed_inputs
    assert any(r.startswith("ASSUMED_INPUTS_CANNOT_PASS") for r in v.reasons) and D(v.net_edge_bps) > 0


def test_a_stale_fee_read_is_not_current():
    v = EG.evaluate_edge(inputs("0.0100", fee_observed_ms=NOW - 2 * EG.MAX_FEE_AGE_MS))
    assert v.session_class == EG.INCONCLUSIVE and v.net_edge_bps is None


def test_negative_funding_is_a_negative_expectation_for_a_short():
    v = EG.evaluate_edge(inputs("-0.0005"))
    assert v.session_class == EG.REJECTED and D(v.expected_funding_bps) < 0


def test_no_settlement_inside_the_hold_is_no_funding_at_all():
    v = EG.evaluate_edge(inputs("0.0100", next_funding_ms=NOW + 4_000_000, hold_s=720))
    assert v.session_class == EG.REJECTED and v.reasons == ("NO_SETTLEMENT_INSIDE_THE_HOLD",)


@pytest.mark.parametrize("over,why", [
    ({"perp_book": book(depth="0.0001")}, "BOOK_TOO_THIN"),
    ({"spot_book": book(ts=NOW - 60_000)}, "STALE_BOOK"),
    ({"perp_row": dict(PERP_ROW, status="Settling")}, "NOT_TRADING"),
])
def test_a_thin_stale_or_untradable_market_is_inconclusive_never_a_pass(over, why):
    v = EG.evaluate_edge(inputs("0.0100", **over))
    assert v.session_class == EG.INCONCLUSIVE and any(why in r for r in v.reasons)


def test_a_malformed_instrument_row_is_inconclusive():
    v = EG.evaluate_edge(inputs("0.0100", perp_row={"symbol": "BTCUSDT"}))
    assert v.session_class == EG.INCONCLUSIVE and v.reasons[0].startswith("INSTRUMENT_INVALID")


def test_the_verdict_is_deterministic_and_carries_a_digest():
    a, b = EG.evaluate_edge(inputs("0.0050")), EG.evaluate_edge(inputs("0.0050"))
    assert a == b and a.digest == b.digest and EG.evaluate_edge(inputs("0.0051")).digest != a.digest


def test_the_hurdle_is_positive_and_not_lowerable_by_the_caller():
    assert EG.MIN_NET_EDGE_BPS > 0 and EG.FUNDING_HAIRCUT > 0
    v = EG.evaluate_edge(inputs("0.0100"))
    assert v.session_class == EG.PROFIT_CANDIDATE and D(v.net_edge_bps) >= EG.MIN_NET_EDGE_BPS


# ---------------------------------------------------------------- reading the venue (fake exchange)
@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _ex(w):
    import ms_testnet_transport as TT
    import ms_testnet_executor as TE
    client, _t = TT.build_client(TT.TestnetCredentials.from_mapping(w.env), w.fake)
    return TE.TestnetExecutor(client, symbols=["BTCUSDT"], spot_symbols=["BTCUSDT"], clock_ms=w.clock.now, kill_reader=lambda: (False, "t"))


def test_collect_reads_instruments_books_and_the_account_fee_tier(w):
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    inp = EG.collect(_ex(w), "BTCUSDT", D("375"), 720, D("0.0001"), w.clock.now() + 600_000, w.clock.now())
    assert inp.perp_fee == (D("0.0002"), D("0.00055")) and inp.fee_observed_ms == w.clock.now()
    v = EG.evaluate_edge(inp)
    assert v.session_class == EG.REJECTED and v.assumed_inputs == () and D(v.costs_bps["fees"]) == D("31")


def test_collect_falls_back_to_an_assumed_tier_when_the_account_fee_read_is_refused(w):
    w.fake.fee_rates = None
    inp = EG.collect(_ex(w), "BTCUSDT", D("375"), 720, D("0.0001"), w.clock.now() + 600_000, w.clock.now())
    assert inp.perp_fee is None and inp.fee_observed_ms is None
    v = EG.evaluate_edge(inp)
    assert "fees" in v.assumed_inputs and v.session_class != EG.PROFIT_CANDIDATE


def test_a_settlement_that_already_happened_or_is_exactly_now_is_not_inside_the_hold():
    for nxt in (NOW - 1, NOW, NOW - 600_000):
        v = EG.evaluate_edge(inputs("0.0100", next_funding_ms=nxt))
        assert v.session_class == EG.REJECTED and v.reasons == ("NO_SETTLEMENT_INSIDE_THE_HOLD",), nxt
    assert EG.evaluate_edge(inputs("0.0100", next_funding_ms=NOW + 720_000)).settlements_in_hold == 1       # exactly at the end of the hold: inside


# ---------------------------------------------------------------- the verdict's "now" must not precede the data it prices (a real clock sampled BEFORE the reads made every book look like it came from the future)
def _stale_perp_spot(v):
    return [r for r in v.reasons if "STALE_BOOK" in r]


def test_a_book_stamped_slightly_ahead_of_the_callers_earlier_clock_reading_is_not_stale(w):
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    earlier = w.clock.now() - 500                                                              # the caller read its clock 500 ms before the venue read: the books are stamped AFTER it
    inp = EG.collect(_ex(w), "BTCUSDT", D("375"), 720, D("0.0100"), w.clock.now() + 600_000, earlier, cash_alternative=STATED)
    assert inp.now_ms >= max(inp.perp_book.ts_ms, inp.spot_book.ts_ms) and inp.fee_observed_ms == inp.now_ms
    v = EG.evaluate_edge(inp)
    assert not _stale_perp_spot(v) and v.session_class != EG.INCONCLUSIVE, v.reasons


def test_a_clock_callable_supplies_the_reading_after_the_last_read(w):
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    stale_arg = w.clock.now() - 100
    inp = EG.collect(_ex(w), "BTCUSDT", D("375"), 720, D("0.0100"), w.clock.now() + 600_000, stale_arg, clock=w.clock.now, cash_alternative=STATED)
    assert inp.now_ms == w.clock.now() and not _stale_perp_spot(EG.evaluate_edge(inp))


def test_a_book_from_beyond_the_skew_bound_is_still_refused(w):
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    far_earlier = w.clock.now() - (EG.FUTURE_BOOK_TOLERANCE_MS + 3000)                          # a clock this far from the venue is not trusted
    inp = EG.collect(_ex(w), "BTCUSDT", D("375"), 720, D("0.0100"), w.clock.now() + 600_000, far_earlier, cash_alternative=STATED)
    assert inp.now_ms == far_earlier
    v = EG.evaluate_edge(inp)
    assert _stale_perp_spot(v) and v.session_class == EG.INCONCLUSIVE


def test_an_old_book_is_still_stale(w):
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    inp = EG.collect(_ex(w), "BTCUSDT", D("375"), 720, D("0.0100"), w.clock.now() + 600_000, w.clock.now() + EG.MAX_BOOK_AGE_MS + 60_000, cash_alternative=STATED)
    v = EG.evaluate_edge(inp)
    assert _stale_perp_spot(v) and v.session_class == EG.INCONCLUSIVE


# ---------------------------------------------------------------- what the EXECUTOR's own gate asks of the funding, against the BTC history (the policy research evaluates is not the policy the executor runs)
def test_no_settlement_in_the_committed_btc_history_would_open_the_executors_profit_gate():
    """The autopilot prices ONE settlement held at most ~1 h with the account's taker tier on BOTH legs, so it needs ~53 bps of funding in a single print. The committed Bybit BTC corpus (4,500
    settlements, 2022-2026) has a maximum print of ~11.3 bps: PROFIT_SEEKING sessions are unreachable on BTC, and the +20 bps research figure belongs to a 17-day hold the executor never runs. This is
    a tripwire: if a legitimate change to the gate makes it pass, MONEY_MISSION.md's statement must be revisited, not this test silently edited."""
    import opportunity_map as OM
    base = EG.evaluate_edge(inputs("0.0001"))
    assert base.required_funding_bps is not None
    required = float(D(str(base.required_funding_bps)))
    rows = OM.load_bybit(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    best = max(r.rate_bps for r in rows)
    assert len(rows) > 4000 and best < 12.0                                    # the corpus facts the claim rests on
    assert 40.0 < required < 70.0, required                                    # ~31 bps taker fees both legs + impact + basis + hurdle, scaled by the funding haircut
    assert sum(1 for r in rows if r.rate_bps >= required) == 0


# ---------------------------------------------------------------- the COST OF CAPITAL: unstated is UNKNOWN (never free), stated is charged on the capital actually committed
HURDLE = EG.MIN_NET_EDGE_BPS


def _oc(v):
    return None if v.costs_bps["opportunity_cost"] is None else D(v.costs_bps["opportunity_cost"])


@pytest.mark.parametrize("why,over", [
    ("UNSTATED", {"cash_alternative": None}),
    ("STALE", {"cash_alternative": EG.CashAlternative(apr=D("0.04"), source="OWNER_STATED", stated_ms=NOW - EG.MAX_CASH_ALTERNATIVE_AGE_MS - 1)}),
    ("FUTURE_DATED", {"cash_alternative": EG.CashAlternative(apr=D("0.04"), source="OWNER_STATED", stated_ms=NOW + EG.CASH_ALTERNATIVE_FUTURE_TOLERANCE_MS + 1)}),
])
def test_an_unusable_cost_of_capital_is_unknown_and_can_never_be_a_profit_candidate(why, over):
    """THE LOOPHOLE: the gate used to build opportunity_cost_apr = Quantity(0, 'modelled'), so an unset owner cost of capital became FREE capital and a 100 bps funding print was a PROFIT_CANDIDATE."""
    v = EG.evaluate_edge(inputs("0.0100", **over))                                   # 100 bps: every other cost is cleared by a wide margin
    assert v.session_class == EG.INCONCLUSIVE, (why, v.reasons)
    assert v.net_edge_bps is None and v.expected_net_usd is None and _oc(v) is None  # not a number: never a zero standing in for it
    assert any(r.startswith("COST_OF_CAPITAL_UNKNOWN:") for r in v.reasons) and any(r.startswith("UNKNOWN_INPUTS:") and "opportunity_cost" in r for r in v.reasons)
    assert v.capital["state"] == why and v.capital["cash_alternative_apr"] in (None, "0.04") and v.capital["opportunity_cost_bps"] is None


def test_the_freshness_window_of_a_stated_cost_of_capital_is_exact():
    at_limit = EG.CashAlternative(apr=D("0.04"), source="OWNER_STATED", stated_ms=NOW - EG.MAX_CASH_ALTERNATIVE_AGE_MS)
    just_over = EG.CashAlternative(apr=D("0.04"), source="OWNER_STATED", stated_ms=NOW - EG.MAX_CASH_ALTERNATIVE_AGE_MS - 1)
    edge_future = EG.CashAlternative(apr=D("0.04"), source="OWNER_STATED", stated_ms=NOW + EG.CASH_ALTERNATIVE_FUTURE_TOLERANCE_MS)
    assert EG.evaluate_edge(inputs("0.0100", cash_alternative=at_limit)).session_class == EG.PROFIT_CANDIDATE
    assert EG.evaluate_edge(inputs("0.0100", cash_alternative=just_over)).session_class == EG.INCONCLUSIVE
    assert EG.evaluate_edge(inputs("0.0100", cash_alternative=edge_future)).session_class == EG.PROFIT_CANDIDATE


def test_a_stated_cost_of_capital_is_charged_on_the_capital_committed_not_on_the_notional():
    """4%/yr on $375 notional at leverage 1 commits spot $375 + perp margin $375 = $750, held 720 s: the charge is 750 x 4% x 720/86400/365 dollars, expressed in bps of NOTIONAL."""
    v = EG.evaluate_edge(inputs("0.0100"))
    hold_years = D(720) / D(86_400) / D(365)
    dollars = D(750) * D("0.04") * hold_years
    assert abs(_oc(v) - dollars / D(375) * D(10_000)) < D("1e-12")                   # the independent derivation in DOLLARS then bps
    assert abs(_oc(v) - D("0.04") * 2 * D(720) / D(86_400) / D(365) * D(10_000)) < D("1e-12")
    assert v.capital["committed_usd"] == "750" and v.capital["multiple_of_notional"] == "2" and v.capital["state"] == "STATED"
    assert (v.capital["cash_alternative_apr"], v.capital["cash_alternative_source"], v.capital["cash_alternative_provenance"]) == ("0.04", "OWNER_STATED", "modelled")


def test_a_leveraged_perp_commits_less_capital_and_the_charge_follows_the_committed_amount():
    one = _oc(EG.evaluate_edge(inputs("0.0100", leverage=1)))
    five = _oc(EG.evaluate_edge(inputs("0.0100", leverage=5)))                       # capital multiple 1 + 1/5 = 1.2 instead of 2
    assert abs(five / one - D("1.2") / D(2)) < D("1e-12")
    assert EG.evaluate_edge(inputs("0.0100", leverage=5)).capital["multiple_of_notional"] == "1.2"


def test_the_charge_is_linear_in_the_stated_rate_and_in_the_hold():
    base = _oc(EG.evaluate_edge(inputs("0.0100")))
    dbl = _oc(EG.evaluate_edge(inputs("0.0100", cash_alternative=EG.CashAlternative(apr=D("0.08"), source="OWNER_STATED", stated_ms=NOW - DAY_MS))))
    longer = _oc(EG.evaluate_edge(inputs("0.0100", hold_s=1440, next_funding_ms=NOW + 600_000)))
    assert abs(dbl - 2 * base) < D("1e-12") and abs(longer - 2 * base) < D("1e-12")


def test_the_net_edge_falls_by_exactly_the_cost_of_capital_when_it_is_stated():
    free = EG.evaluate_edge(inputs("0.0100", cash_alternative=EG.CashAlternative(apr=D("0"), source="OWNER_STATED", stated_ms=NOW - DAY_MS)))
    dear = EG.evaluate_edge(inputs("0.0100", cash_alternative=EG.CashAlternative(apr=D("0.30"), source="OWNER_STATED", stated_ms=NOW - DAY_MS)))
    assert _oc(free) == 0 and free.capital["state"] == "STATED"
    assert abs((D(free.net_edge_bps) - D(dear.net_edge_bps)) - _oc(dear)) < D("1e-12")


def test_the_required_funding_includes_the_stated_cost_of_capital():
    zero = EG.evaluate_edge(inputs("0.0100", cash_alternative=EG.CashAlternative(apr=D("0"), source="OWNER_STATED", stated_ms=NOW - DAY_MS)))
    dear = EG.evaluate_edge(inputs("0.0100", cash_alternative=EG.CashAlternative(apr=D("0.30"), source="OWNER_STATED", stated_ms=NOW - DAY_MS)))
    assert abs((D(dear.required_funding_bps) - D(zero.required_funding_bps)) - _oc(dear) / ((1 - EG.FUNDING_HAIRCUT) * 1)) < D("1e-9")


def test_an_explicit_owner_zero_is_a_stated_decision_and_stays_visible():
    free = EG.CashAlternative(apr=D("0"), source="OWNER_STATED", stated_ms=NOW - DAY_MS)
    v = EG.evaluate_edge(inputs("0.0100", cash_alternative=free))
    assert v.session_class == EG.PROFIT_CANDIDATE and _oc(v) == 0
    assert "COST_OF_CAPITAL_ZERO_BY_OWNER_STATEMENT" in v.reasons and v.capital["cash_alternative_apr"] == "0"      # priced as free BY DECISION, said out loud


def test_a_hopeless_edge_is_rejected_even_when_the_cost_of_capital_is_unknown():
    """A cost of capital can only LOWER the net edge: if the edge misses the hurdle with the cost floored at zero, no capital cost could rescue it, so the honest answer is REJECTED, not 'need more input'."""
    v = EG.evaluate_edge(inputs("0.0001", cash_alternative=None))
    assert v.session_class == EG.REJECTED and any("BEFORE_ANY_COST_OF_CAPITAL" in r for r in v.reasons) and any(r.startswith("COST_OF_CAPITAL_UNKNOWN:") for r in v.reasons)
    assert v.net_edge_bps is None and v.expected_net_usd is None and v.required_funding_bps is None       # the bound is a rejection reason, not a reported net


def test_with_an_unknown_cost_of_capital_no_funding_level_can_make_a_profit_candidate():
    """Property: across a wide funding range the answer is REJECTED or INCONCLUSIVE, the net is never reported, and the boundary between them is the zero-capital-cost required funding."""
    boundary = D(EG.evaluate_edge(inputs("0.0001", cash_alternative=EG.CashAlternative(apr=D("0"), source="OWNER_STATED", stated_ms=NOW - DAY_MS))).required_funding_bps) / EG.BPS
    for bps in range(-20, 241, 5):
        v = EG.evaluate_edge(inputs(str(D(bps) / EG.BPS), cash_alternative=None))
        assert v.session_class in (EG.REJECTED, EG.INCONCLUSIVE) and v.net_edge_bps is None, (bps, v.session_class)
        assert (v.session_class == EG.REJECTED) == (D(bps) / EG.BPS < boundary), (bps, v.session_class)


def test_the_rejection_bound_is_never_used_when_anything_else_is_also_unusable():
    """With an ASSUMED fee tier the verdict stays INCONCLUSIVE (the existing rule), unknown cost of capital or not: the bound only ever REJECTS a clean-but-hopeless edge."""
    v = EG.evaluate_edge(inputs("0.0001", measured=False, cash_alternative=None))
    assert v.session_class == EG.INCONCLUSIVE and not any("BEFORE_ANY_COST_OF_CAPITAL" in r for r in v.reasons)


def test_an_unknown_cost_of_capital_does_not_hide_a_book_or_instrument_problem():
    v = EG.evaluate_edge(inputs("0.0100", cash_alternative=None, perp_book=book(depth="0.0001")))
    assert v.session_class == EG.INCONCLUSIVE and any("BOOK_TOO_THIN" in r for r in v.reasons) and any(r.startswith("COST_OF_CAPITAL_UNKNOWN:") for r in v.reasons)


def test_the_cost_of_capital_is_bound_into_the_input_digest_the_summary_and_the_verdict_digest():
    a = inputs("0.0100")
    b = inputs("0.0100", cash_alternative=EG.CashAlternative(apr=D("0.05"), source="OWNER_STATED", stated_ms=NOW - DAY_MS))
    c = inputs("0.0100", cash_alternative=EG.CashAlternative(apr=D("0.04"), source="ANOTHER_SOURCE", stated_ms=NOW - DAY_MS))
    d = inputs("0.0100", cash_alternative=EG.CashAlternative(apr=D("0.04"), source="OWNER_STATED", stated_ms=NOW - 2 * DAY_MS))
    e = inputs("0.0100", cash_alternative=None)
    assert len({x.digest() for x in (a, b, c, d, e)}) == 5
    assert len({EG.evaluate_edge(x).digest for x in (a, b, c, d, e)}) == 5
    s = a.summary()
    assert (s["cost_of_capital_state"], s["cash_alternative_apr"], s["cash_alternative_source"], s["cash_alternative_stated_ms"], s["cash_alternative_provenance"]) == ("STATED", "0.04", "OWNER_STATED", NOW - DAY_MS, "modelled")
    s = e.summary()
    assert s["cost_of_capital_state"] == "UNSTATED" and s["cash_alternative_apr"] is None and s["cash_alternative_source"] is None


@pytest.mark.parametrize("bad", [
    dict(apr=D("-0.01")), dict(apr=D("1.0001")), dict(apr=D("NaN")), dict(apr=D("Infinity")), dict(apr=0.04), dict(apr="0.04"), dict(apr=True), dict(apr=None),
    dict(source=""), dict(source="   "), dict(source="x" * 121), dict(source="line\nbreak"), dict(source=None), dict(source=7),
    dict(stated_ms=0), dict(stated_ms=-5), dict(stated_ms=True), dict(stated_ms=1.5), dict(stated_ms=None),
    dict(provenance="assumed"), dict(provenance="unknown"), dict(provenance="bogus"), dict(provenance=None)])
def test_a_cash_alternative_that_is_not_a_clean_statement_cannot_be_constructed(bad):
    kw = dict(apr=D("0.04"), source="OWNER_STATED", stated_ms=NOW - DAY_MS)
    kw.update(bad)
    with pytest.raises(ValueError):
        EG.CashAlternative(**kw)


def test_a_plain_number_is_not_a_cash_alternative():
    v = EG.evaluate_edge(inputs("0.0100", cash_alternative=D("0.04")))               # a bare number carries no source and no time: it is not a statement
    assert v.session_class == EG.INCONCLUSIVE and v.capital["state"] == "INVALID"


def test_every_modelled_estimate_is_not_blocked_only_the_missing_cost_of_capital():
    """The gate must not turn every modelled input into a refusal: impact, basis stress, the funding haircut and the adverse move stay modelled and the verdict passes with them."""
    v = EG.evaluate_edge(inputs("0.0100"))
    assert v.session_class == EG.PROFIT_CANDIDATE and v.assumed_inputs == () and all(v.costs_bps[k] is not None for k in v.costs_bps)


# ---------------------------------------------------------------- the owner states it in the environment: parsed strictly, never defaulted
ENV_OK = {EG.ENV_CASH_APR: "0.04", EG.ENV_CASH_SOURCE: "owner: OKX USDT lending, 4%", EG.ENV_CASH_STATED_UTC: "2026-11-09"}


def _ms(utc):
    import calendar
    import time
    fmt = "%Y-%m-%dT%H:%M:%SZ" if "T" in utc else "%Y-%m-%d"
    return calendar.timegm(time.strptime(utc, fmt)) * 1000


def test_the_environment_statement_is_parsed_into_a_cash_alternative():
    ca, state, why = EG.cash_alternative_from_env(ENV_OK)
    assert state == "STATED" and why == [] and ca == EG.CashAlternative(apr=D("0.04"), source="owner: OKX USDT lending, 4%", stated_ms=_ms("2026-11-09"), provenance="modelled")
    ca2, state2, _ = EG.cash_alternative_from_env({**ENV_OK, EG.ENV_CASH_STATED_UTC: "2026-11-09T08:30:00Z", EG.ENV_CASH_APR: "0"})
    assert state2 == "STATED" and ca2.stated_ms == _ms("2026-11-09T08:30:00Z") and ca2.apr == 0


def test_an_absent_environment_statement_is_unstated_and_never_a_default():
    assert EG.cash_alternative_from_env({}) == (None, "UNSTATED", ["NO_CASH_ALTERNATIVE_STATED"])
    assert EG.cash_alternative_from_env({"UNRELATED": "1"})[1] == "UNSTATED"


@pytest.mark.parametrize("env", [
    {EG.ENV_CASH_APR: "0.04"},                                                                   # a rate with no source and no date
    {EG.ENV_CASH_APR: "0.04", EG.ENV_CASH_SOURCE: "owner"},                                      # no date
    {EG.ENV_CASH_SOURCE: "owner", EG.ENV_CASH_STATED_UTC: "2026-11-09"},                         # no rate
    {**ENV_OK, EG.ENV_CASH_APR: "4%"}, {**ENV_OK, EG.ENV_CASH_APR: "abc"}, {**ENV_OK, EG.ENV_CASH_APR: ""}, {**ENV_OK, EG.ENV_CASH_APR: " 0.04"}, {**ENV_OK, EG.ENV_CASH_APR: "0.04 "},
    {**ENV_OK, EG.ENV_CASH_APR: "-0.01"}, {**ENV_OK, EG.ENV_CASH_APR: "4"}, {**ENV_OK, EG.ENV_CASH_APR: "NaN"}, {**ENV_OK, EG.ENV_CASH_APR: "1_0"}, {**ENV_OK, EG.ENV_CASH_APR: "1e-1"},
    {**ENV_OK, EG.ENV_CASH_STATED_UTC: "yesterday"}, {**ENV_OK, EG.ENV_CASH_STATED_UTC: "2026-13-45"}, {**ENV_OK, EG.ENV_CASH_STATED_UTC: "2026-11-09 "}, {**ENV_OK, EG.ENV_CASH_STATED_UTC: ""},
    {**ENV_OK, EG.ENV_CASH_SOURCE: ""}, {**ENV_OK, EG.ENV_CASH_SOURCE: "x" * 121},
])
def test_a_partial_or_malformed_environment_statement_is_invalid_and_stays_unknown(env):
    ca, state, why = EG.cash_alternative_from_env(env)
    assert ca is None and state == "INVALID" and why and all(isinstance(x, str) for x in why)


def test_the_environment_names_are_not_credential_shaped():
    for name in (EG.ENV_CASH_APR, EG.ENV_CASH_SOURCE, EG.ENV_CASH_STATED_UTC):
        assert not any(w in name.upper() for w in ("KEY", "SECRET", "TOKEN", "PASSWORD", "ACK")), name
