import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import btc_hold_benchmark as B

def cyc(net=10.0, cap=1000.0, p0=100.0, p1=100.0, ok=True):
    return {"net_usd": net, "capital_usd": cap, "price_start": p0, "price_end": p1, "confirmed": ok}

GOOD = [cyc(net=10 + (i % 3)) for i in range(20)]

def test_unset_owner_inputs_can_never_give_go():
    r = B.evaluate(GOOD, evidence_class=B.ACTUAL)
    assert r["verdict"] == B.INSUFFICIENT and "min_cycles" in r["why"] and "z" in r["why"]

@pytest.mark.parametrize("cls", ["PAPER_EXECUTION", "HISTORICAL_SIMULATION", "FORWARD_SIMULATION", "UNAVAILABLE"])
def test_nothing_but_actual_reconciled_counts(cls):
    assert B.evaluate(GOOD, evidence_class=cls, min_cycles=5, z=2.0)["verdict"] == B.INSUFFICIENT

def test_reconciled_cycles_that_beat_the_hold_with_margin_are_go():
    assert B.evaluate(GOOD, evidence_class=B.ACTUAL, min_cycles=10, z=2.0)["verdict"] == B.GO

def test_too_few_cycles_is_insufficient():
    assert B.evaluate(GOOD[:3], evidence_class=B.ACTUAL, min_cycles=10, z=2.0)["verdict"] == B.INSUFFICIENT

def test_a_rising_btc_that_outruns_the_carry_is_no_go():
    rising = [cyc(net=10, p0=100, p1=110 + (i % 2)) for i in range(20)]
    assert B.evaluate(rising, evidence_class=B.ACTUAL, min_cycles=10, z=2.0)["verdict"] == B.NO_GO

def test_negative_net_is_no_go_even_if_btc_fell_more():
    falling = [cyc(net=-1 - (i % 2), p0=100, p1=60) for i in range(20)]
    assert B.evaluate(falling, evidence_class=B.ACTUAL, min_cycles=10, z=2.0)["verdict"] == B.NO_GO

def test_unconfirmed_and_unusable_cycles_are_excluded_not_counted():
    mixed = GOOD + [cyc(ok=False)] * 5 + [cyc(cap=0.0), cyc(net=float("nan"))]
    r = B.evaluate(mixed, evidence_class=B.ACTUAL, min_cycles=10, z=2.0)
    assert r["n_cycles"] == 20 and r["excluded"] == {"unconfirmed": 5, "unusable": 2}

def test_noisy_results_straddling_zero_are_insufficient():
    noisy = [cyc(net=(30 if i % 2 else -28)) for i in range(20)]
    assert B.evaluate(noisy, evidence_class=B.ACTUAL, min_cycles=10, z=2.0)["verdict"] == B.INSUFFICIENT

@pytest.mark.parametrize("mc,z", [(1, 2.0), (True, 2.0), (10, 0.0), (10, -1.0), (10, float("nan"))])
def test_bad_owner_inputs_never_give_go(mc, z):
    assert B.evaluate(GOOD, evidence_class=B.ACTUAL, min_cycles=mc, z=z)["verdict"] == B.INSUFFICIENT
