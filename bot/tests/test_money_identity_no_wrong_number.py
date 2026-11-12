"""'NO WRONG NUMBER' (round 10, priority three): for every fault scenario the whole session runs on this repository's fake Testnet and what the session RECORD publishes (price - fees + funding) is compared with an
ORACLE written from first principles from the VENUE's wallet (wallet conservation: what the account actually gained, leftover coin valued at the final spot mark). Independent of every ledger, engine and pricer here.

Allowed outcomes: the published net is None (UNKNOWN) or equals the oracle. A published number that differs from the oracle is a defect: the stack's own rule is "unknown is fine, wrong is not".

Reproduced first by the round-10 execution specialist (scratch ``spec_probe_nowrong.py``): a spot fee with no currency stated and a rebate each published a WRONG net; a perp entry execution that the venue's list did not
show published a ZERO funding for a held settlement (+0.48 USD in truth) into the record, the evidence event and the research feedback row.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ms_testnet_world import World  # noqa: E402


def oracle(w, usdt0, btc0):
    """Wallet conservation: what the account actually gained, valuing leftover coin at the final spot mark. Independent of every ledger, engine and pricer in the repository."""
    mark = D(w.fake.book["BTCUSDT"]["spot_mark"])
    return (w.fake.wallet["USDT"] - usdt0) + (w.fake.wallet["BTC"] - btc0) * mark


SCENARIOS = {
    "baseline": lambda w: None,
    "spot_fee_in_base": lambda w: setattr(w.fake, "spot_fee_in_base", True),
    "third_currency_fee_MNT_spot_buy": "mnt",
    "blank_fee_currency_spot_buy_base_fee": "blank",
    "rebate_negative_taker_fee": lambda w: setattr(w.fake, "taker_rate", D("-0.0001")),
    "duplicate_execution_rows": lambda w: setattr(w.fake, "dup_executions", True),
    "perp_entry_execution_hidden_from_the_list": "hide_perp_entry",
    "coarse_spot_lot_dust": "coarse_lot",
    "async_fills_2s_with_base_fee": "async",
    "lowercase_base_fee_currency": "lower",
    "wallet_funding_row_H2_convention": "h2",
    "partial_perp_entry_thin_liquidity": "thin",
}


def arm(w, name):
    spec = SCENARIOS[name]
    if callable(spec):
        spec(w)
        return
    orig = w.fake._fill
    if spec == "mnt":
        def fill(o, qty, px, *, taker, dup=False):
            orig(o, qty, px, taker=taker, dup=dup)
            if o["category"] == "spot" and o["side"] == "Buy":
                w.fake.execs[-1]["feeCurrency"], w.fake.execs[-1]["execFee"] = "MNT", "0.18"
        w.fake._fill = fill
    elif spec == "blank":
        w.fake.spot_fee_in_base = True

        def fill(o, qty, px, *, taker, dup=False):
            orig(o, qty, px, taker=taker, dup=dup)
            if o["category"] == "spot" and o["side"] == "Buy":
                w.fake.execs[-1]["feeCurrency"] = ""
        w.fake._fill = fill
    elif spec == "hide_perp_entry":
        def fill(o, qty, px, *, taker, dup=False):
            orig(o, qty, px, taker=taker, dup=dup)
            if o["orderLinkId"].endswith("-p"):
                w.fake.hidden_execs.add(w.fake.execs[-1]["execId"])
        w.fake._fill = fill
    elif spec == "async":
        w.fake.spot_fee_in_base = True
        w.fake.fill_delay_ms = 2000
    elif spec == "lower":
        w.fake.spot_fee_in_base = True

        def fill(o, qty, px, *, taker, dup=False):
            orig(o, qty, px, taker=taker, dup=dup)
            if o["category"] == "spot" and o["side"] == "Buy":
                w.fake.execs[-1]["feeCurrency"] = w.fake.execs[-1]["feeCurrency"].lower()
        w.fake._fill = fill
    elif spec == "h2":
        pass
    elif spec == "thin":
        w.fake.liquidity["BTCUSDT"] = D("0.0015")
    elif spec == "coarse_lot":
        w.fake.spot_fee_in_base = True
        w.fake.spot_instruments["BTCUSDT"]["base_precision"] = "0.0001"


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_published_net_is_unknown_or_equals_the_wallet_oracle(name, tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    arm(w, name)
    w.fake.funding_rate = "0.0080"
    assert w.run("preflight") == 0, w.text()
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--hold-seconds", "150", "--exit-wait-s", "120") == 0, w.text()
    d = w.plan_doc()["plan_digest"]
    usdt0, btc0 = w.fake.wallet["USDT"], w.fake.wallet["BTC"]
    start, st, real = w.clock.now(), {"d": False}, w.clock.sleep

    def sleep(s):
        real(s)
        if not st["d"] and w.clock.now() >= start + 75_000 and any(p["size"] != 0 for p in w.fake.positions.values()):
            w.fake.settle_funding("BTCUSDT", "0.0080", ts_ms=w.clock.now(), convention=("H2" if name.endswith("H2_convention") else "H1"))
            st["d"] = True
    w.clock.sleep = sleep
    rc = w.run("execute", "--confirm-plan-digest", d, typed=d)
    rec = w.record()
    n = rec["net"]
    comps = (n["price_pnl_usd"], n["fees_usd"], n["funding_usd"])
    published = None if any(c is None for c in comps) else D(str(n["price_pnl_usd"])) - D(str(n["fees_usd"])) + D(str(n["funding_usd"]))
    ref = oracle(w, usdt0, btc0)
    verdict = "UNKNOWN (ok)" if published is None else ("EQUAL" if abs(published - ref) < D("1e-6") else f"WRONG by {published - ref}")
    assert published is None or abs(published - ref) < D("1e-6"), f"{name}: the record publishes {published}, the wallet oracle says {ref}"


def _run_session(name, tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    arm(w, name)
    w.fake.funding_rate = "0.0080"
    assert w.run("preflight") == 0, w.text()
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--hold-seconds", "150", "--exit-wait-s", "120") == 0, w.text()
    d = w.plan_doc()["plan_digest"]
    start, st, real = w.clock.now(), {"d": False}, w.clock.sleep

    def sleep(s):
        real(s)
        if not st["d"] and w.clock.now() >= start + 75_000 and any(p["size"] != 0 for p in w.fake.positions.values()):
            w.fake.settle_funding("BTCUSDT", "0.0080", ts_ms=w.clock.now())
            st["d"] = True
    w.clock.sleep = sleep
    rc = w.run("execute", "--confirm-plan-digest", d, typed=d)
    return w, rc


def test_a_perp_entry_execution_the_list_does_not_show_never_publishes_a_zero_funding(tmp_path, monkeypatch):
    """H1: the funding window starts at the first VISIBLE perp fill, so a settlement the pair was really held through falls BEFORE it and 'no print while held' was declared (a DERIVED zero). The net's fees and price
    were already unknown (fills incomplete); funding must be too, in the record, in the evidence event and in the research feedback row (which fed a measured-vs-modelled comparison with a 0)."""
    import json
    import exec_lifecycle as EL
    import exec_quality as EQ
    w, rc = _run_session("perp_entry_execution_hidden_from_the_list", tmp_path, monkeypatch)
    rec = w.record()
    assert rec["net"]["fees_usd"] is None and rec["net"]["price_pnl_usd"] is None
    assert rec["net"]["funding_usd"] is None, "an unknown held window cannot be a zero funding"
    fs = json.load(open(os.path.join(w.state_dir, "funding_settlement.json")))
    assert fs.get("suppressed") == "FILLS_INCOMPLETE" and all(r.get("all_prints_resolved") is False and r.get("booked_usd") is None for r in fs["recognitions"]), fs
    events = EL.funding_events(fs)
    rec_events = [e for e in events if e[1] == "FUNDING_RECOGNITION"]
    assert rec_events and all(e[2]["all_resolved"] is False and e[2]["booked_usd"] is None for e in rec_events), rec_events
    q = json.load(open(os.path.join(w.state_dir, "execution_quality.json")))
    row = EQ.feedback_row(session="s", at_ms=1, decision_id=None, quality=q, funding=fs, edge={"expected_funding_bps": "5.6"}, flat_verified=True)
    assert row["realised_funding_bps"] is None and row["funding_booked_usd"] is None, row
