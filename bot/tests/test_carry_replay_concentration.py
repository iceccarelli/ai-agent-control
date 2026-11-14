"""A headline return says nothing about HOW it was earned. The replay now also
reports per-trade net, how much of the profit one trade supplied, and the worst
peak-to-trough fall, and checks that the per-trade numbers add back to the
headline. Hand-computed cases; offline."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, os.path.dirname(HERE))

import carry_replay as rp  # noqa: E402
from test_carry_replay import rows  # noqa: E402


def trade(opened, closed, funding, perp=(100.0, 100.0), spot=(100.0, 100.0), qty=1.0):
    return {"opened_ms": opened, "closed_ms": closed, "funding": funding,
            "qty": qty, "spot_qty": qty, "entry_perp": perp[0], "exit_perp": perp[1],
            "entry_spot": spot[0], "exit_spot": spot[1], "prints_held": 3,
            "entry_index": 0, "reason": "X"}


def test_per_trade_net_is_funding_plus_both_legs_minus_fees_and_borrow():
    # short perp loses 5 (100->105), long spot gains 5 (100->105): basis 0
    t = trade(1, 9, funding=10.0, perp=(100.0, 105.0), spot=(100.0, 105.0))
    out = rp.trade_concentration([t], notional=1000.0,
                                 fee_events=[(1, 0.3), (9, 0.2)],
                                 borrow_events=[(5, 1.0)])
    assert out["trade_nets_usd"] == [pytest.approx(10.0 - 0.5 - 1.0)]


def test_best_trade_and_top_five_share_of_net():
    nets = [100.0, 10.0, 10.0, 10.0, 10.0, 10.0, -10.0]       # total 140
    trades = [trade(i * 10, i * 10 + 5, n) for i, n in enumerate(nets)]
    out = rp.trade_concentration(trades, notional=1000.0)
    assert out["net_usd"] == pytest.approx(140.0)
    assert out["best_trade_usd"] == pytest.approx(100.0)
    assert out["best_trade_share_of_net"] == pytest.approx(100.0 / 140.0)
    assert out["top5_share_of_net"] == pytest.approx(140.0 / 140.0)   # 100+10*4
    assert out["losing_trade_pct"] == pytest.approx(100.0 / 7)


def test_max_drawdown_is_the_worst_peak_to_trough_of_cumulative_closed_net():
    # cumulative: 10, -20, -15, -25  -> peak 10, trough -25 -> drawdown 35
    trades = [trade(0, 1, 10.0), trade(2, 3, -30.0), trade(4, 5, 5.0),
              trade(6, 7, -10.0)]
    out = rp.trade_concentration(trades, notional=1000.0)
    assert out["max_drawdown_usd"] == pytest.approx(35.0)
    assert out["max_drawdown_pct_of_notional"] == pytest.approx(3.5)


def test_drawdown_is_ordered_by_close_time_not_list_order():
    a, b = trade(0, 9, -30.0), trade(0, 1, 10.0)       # b closes first
    out = rp.trade_concentration([a, b], notional=1000.0)
    assert out["max_drawdown_usd"] == pytest.approx(30.0)       # +10 then -30 = -20: dd 30


def test_a_net_that_is_not_positive_has_no_share_to_report():
    out = rp.trade_concentration([trade(0, 1, -5.0), trade(2, 3, 2.0)], notional=100.0)
    assert out["best_trade_share_of_net"] is None and out["top5_share_of_net"] is None


def test_no_trades_is_reported_as_none_not_zero_edge():
    out = rp.trade_concentration([], notional=100.0)
    assert out["trades"] == 0 and out["best_trade_share_of_net"] is None
    assert out["max_drawdown_usd"] == 0.0


def test_the_per_trade_sum_is_checked_against_the_headline():
    t = [trade(0, 1, 10.0)]
    assert rp.trade_concentration(t, notional=100.0, net_usd=10.0)["reconciles_to_net"] is True
    assert rp.trade_concentration(t, notional=100.0, net_usd=99.0)["reconciles_to_net"] is False


class TestOnARealReplay:
    def test_the_replay_reports_it_and_it_adds_back_to_the_headline(self):
        data = rows(150, cycle=12)
        report = rp.replay(data, notional=100_000.0, borrow_apr=0.05)
        c = report["concentration"]
        assert c["trades"] == report["trades"] > 1
        assert c["reconciles_to_net"] is True
        assert sum(c["trade_nets_usd"]) == pytest.approx(report["net_usd"], abs=0.01)
        assert 0.0 <= c["losing_trade_pct"] <= 100.0
        assert c["max_drawdown_usd"] >= 0.0


class TestThePrintedReportShowsIt:
    def test_the_lines_state_the_concentration_and_the_without_best_figure(self):
        trades = [trade(i * 10, i * 10 + 5, n) for i, n in enumerate([100.0, 10.0, -10.0])]
        c = rp.trade_concentration(trades, notional=1000.0)
        text = "\n".join(rp.concentration_lines(c, years=1.0, notional=1000.0))
        assert "best trade" in text and "100.0% of net" in text
        assert "without the best trade" in text

    def test_nothing_is_printed_for_a_replay_with_no_trades(self):
        c = rp.trade_concentration([], notional=1000.0)
        assert rp.concentration_lines(c, years=1.0, notional=1000.0) == []


class TestTheCaptionSaysWhatWasCharged:
    """The 'without the best trade' figure comes from per-trade nets that already
    include whatever borrow the run charged. The caption used to say 'before
    financing' on every run, which is false the moment a borrow rate is set."""

    def _lines(self, borrow_apr):
        trades = [trade(0, 5, 100.0), trade(10, 15, 10.0)]
        c = rp.trade_concentration(trades, notional=1000.0)
        return "\n".join(rp.concentration_lines(
            c, years=1.0, notional=1000.0, borrow_apr=borrow_apr))

    def test_with_no_borrow_it_says_none_was_charged(self):
        assert "0% borrow" in self._lines(0.0)
        assert "before financing" not in self._lines(0.0)

    def test_with_borrow_it_says_the_rate_that_was_charged(self):
        assert "after 5% borrow" in self._lines(0.05)
        assert "before financing" not in self._lines(0.05)


class TestReturnOnTotalCapital:
    """The headline is a return on NOTIONAL. A hedged book also posts margin for
    the short perp, so the money tied up is the spot leg plus that margin. The
    margin fraction is a stated assumption, never a venue figure."""

    def test_total_capital_is_spot_notional_plus_assumed_margin(self):
        out = rp.return_on_total_capital(net_annualised_pct=8.0, margin_fractions=(0.10, 0.25))
        assert out[0.10] == pytest.approx(8.0 / 1.10)
        assert out[0.25] == pytest.approx(8.0 / 1.25)

    def test_a_negative_return_stays_negative(self):
        assert rp.return_on_total_capital(-2.0, (0.2,))[0.2] == pytest.approx(-2.0 / 1.2)

    def test_the_lines_name_the_assumption_and_say_it_is_not_the_venues(self):
        text = "\n".join(rp.capital_lines(8.05))
        assert "10%" in text and "20%" in text and "33%" in text
        assert "assumed" in text and "not the venue" in text.lower()

    def test_an_unusable_fraction_is_refused(self):
        with pytest.raises(ValueError):
            rp.return_on_total_capital(8.0, (-0.1,))


# ---------------------------------------------------------------------------------------------------------------- return denominators
from types import SimpleNamespace as _NS  # noqa: E402


def _row(perp, rate):
    return _NS(perp=perp, rate=rate)


def test_the_return_bases_by_hand_for_one_trade_whose_price_rises():
    # entered at index 0 at 100, three prints held (indices 1..3) while the perp marks 110, 120, 130; rate 1 bp per print; qty 1
    rows_ = [_row(100.0, 0.0), _row(110.0, 1e-4), _row(120.0, 1e-4), _row(130.0, 1e-4)]
    t = trade(0, 3, funding=0.0, perp=(100.0, 130.0), qty=1.0)
    funding_at_mark = 1e-4 * (110.0 + 120.0 + 130.0)                     # 0.036: what the engine books (rate x qty x MARK)
    funding_at_entry = 1e-4 * 100.0 * 3                                   # 0.030: the same prints priced at the entry
    net = funding_at_mark - 0.01                                          # 0.01 of fees
    out = rp.denominator_report([dict(t, funding=funding_at_mark)], rows_, net_usd=net, funding_usd=funding_at_mark, years=1.0, notional=100.0)
    assert out["reconciles"] is True and out["held_prints"] == 3
    assert out["funding_recomputed_at_mark_usd"] == pytest.approx(0.036) and out["funding_at_entry_notional_usd"] == pytest.approx(0.030)
    assert out["avg_marked_hedged_notional_usd"] == pytest.approx(120.0) and out["marked_over_entry_ratio"] == pytest.approx(1.2)
    assert out["on_entry_notional_pct"] == pytest.approx(100.0 * 0.026 / 100.0)                       # the headline, unchanged
    assert out["drift_neutral_pct"] == pytest.approx(100.0 * (0.026 - 0.036 + 0.030) / 100.0)        # 0.020
    assert out["on_avg_marked_notional_pct"] == pytest.approx(100.0 * 0.026 / 120.0)
    assert out["by_margin"][1.0] == pytest.approx(100.0 * 0.026 / 240.0) and out["by_margin"][0.0] == pytest.approx(out["on_avg_marked_notional_pct"])


def test_at_a_constant_price_every_basis_agrees():
    rows_ = [_row(100.0, 0.0)] + [_row(100.0, 2e-4)] * 5
    t = trade(0, 5, funding=0.0, perp=(100.0, 100.0), qty=2.0)
    f = 2e-4 * 2.0 * 100.0 * 5
    out = rp.denominator_report([dict(t, funding=f, prints_held=5)], rows_, net_usd=f, funding_usd=f, years=1.0, notional=200.0)
    assert out["marked_over_entry_ratio"] == pytest.approx(1.0)
    assert out["drift_neutral_pct"] == pytest.approx(out["on_entry_notional_pct"]) == pytest.approx(out["on_avg_marked_notional_pct"])


def test_a_falling_price_makes_the_headline_understate_and_the_drift_neutral_figure_exceed_it():
    rows_ = [_row(100.0, 0.0), _row(90.0, 1e-4), _row(80.0, 1e-4)]
    t = trade(0, 2, funding=0.0, perp=(100.0, 80.0), qty=1.0)
    f = 1e-4 * (90.0 + 80.0)
    out = rp.denominator_report([dict(t, funding=f, prints_held=2)], rows_, net_usd=f, funding_usd=f, years=1.0, notional=100.0)
    assert out["marked_over_entry_ratio"] < 1.0 and out["drift_neutral_pct"] > out["on_entry_notional_pct"]


def test_when_the_funding_cannot_be_reproduced_from_the_held_prints_nothing_is_claimed():
    rows_ = [_row(100.0, 0.0), _row(110.0, 1e-4), _row(120.0, 1e-4)]
    t = trade(0, 2, funding=0.0, perp=(100.0, 120.0), qty=1.0)
    out = rp.denominator_report([dict(t, prints_held=2)], rows_, net_usd=1.0, funding_usd=0.5, years=1.0, notional=100.0)        # the engine's funding is not rate x qty x mark
    assert out["reconciles"] is False and out["drift_neutral_pct"] is None and out["on_avg_marked_notional_pct"] is None
    assert "not claimed" in rp.denominator_lines(out)[0]
    empty = rp.denominator_report([], rows_, net_usd=0.0, funding_usd=0.0, years=1.0, notional=100.0)
    assert empty["held_prints"] == 0 and empty["drift_neutral_pct"] is None


def test_the_real_replay_reports_the_bases_and_the_headline_is_untouched():
    data = rows(120, rate=0.0006)
    report = rp.replay(data, notional=100_000.0, borrow_apr=0.0)
    d = report["denominators"]
    assert report["net_annualised_pct"] == pytest.approx(d["on_entry_notional_pct"])
    assert d["reconciles"] is True, d                        # the engine's own funding IS rate x qty x mark over the held prints
    assert d["funding_recomputed_at_mark_usd"] == pytest.approx(report["funding_usd"], abs=1e-6)
    assert d["marked_over_entry_ratio"] > 1.0                # the synthetic corpus rises 0.02 % a print
    assert d["drift_neutral_pct"] < d["on_entry_notional_pct"]


# ------------------------------------------------------------------------------------------------ Round 20: the console report states what each figure is and is not

import ast  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402

_PRESENTATION = {"main", "capital_lines", "denominator_lines", "usdt_financing_lines", "owner_capital_lines", "concentration_lines"}
#: sha256 of the exact source text of every OTHER top-level definition of tools/carry_replay.py, taken from the 56f9da6 / 413175f file (byte-identical between them) that the registered forward
#: kill test (E1, docs/MONEY_MISSION.md) names as its engine. The console section may change; the computation may not.
_COMPUTATION_DIGEST = "e239af91e29c70fdd4809615a01a36b468565aafb6d06d1c70a7cbfea372f545"
_MAIN_HEAD_DIGEST = "e02229601f338f8bc0d135363504fbd79c75422e6a29940661fb19ea03c04c7e"       # source text of main()'s statements up to and including the replay(...) assignment (argparse defaults, corpus, haircut, cost-of-capital fallback)
_REPLAY_CALL_DIGEST = "fe7f673b8efccf746468e4186ddc9560c38a721a138f487cbb1a213f07cc0103"       # ast.unparse of the replay(...) call in main(): the arguments the registered command reaches it with


def _computation_digest(src):
    tree = ast.parse(src)
    lines = src.splitlines()
    parts = []
    for n in tree.body:
        name = getattr(n, "name", None) or ""
        if name in _PRESENTATION or name.startswith("present_"):
            continue
        start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        parts.append("\n".join(lines[start - 1:n.end_lineno]))
    return hashlib.sha256("\n\x00\n".join(parts).encode()).hexdigest(), len(parts)


def _replay_call_digest(src):
    for n in ast.parse(src).body:
        if getattr(n, "name", None) == "main":
            for sub in ast.walk(n):
                if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call) and getattr(sub.value.func, "id", None) == "replay":
                    return hashlib.sha256(ast.unparse(sub.value).encode()).hexdigest()
    return None


def _main_head_digest(src):
    lines = src.splitlines()
    for n in ast.parse(src).body:
        if getattr(n, "name", None) == "main":
            parts = []
            for st in n.body:
                parts.append("\n".join(lines[st.lineno - 1:st.end_lineno]))
                if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) and getattr(st.value.func, "id", None) == "replay":
                    return hashlib.sha256("\n\x00\n".join(parts).encode()).hexdigest()
    return None


def test_the_part_of_main_that_decides_what_the_registered_command_runs_is_pinned_too():
    """REPRODUCED (independent review, Round 20): excluding ALL of main() let `impact = (0.0 if True else ...)` and `stated_cost = 0.05` pass the pin while changing what the registered command runs."""
    src = open(rp.__file__, encoding="utf-8").read()
    assert _main_head_digest(src) == _MAIN_HEAD_DIGEST
    for before, after in (("impact = (0.0 if args.no_impact", "impact = (0.0 if True"), ("stated_cost = (0.0 if args.opportunity_cost_apr is None", "stated_cost = (0.05 if args.opportunity_cost_apr is None")):
        assert before in src, before
        assert _main_head_digest(src.replace(before, after, 1)) != _MAIN_HEAD_DIGEST, after
    console = src.replace('print(f"  driver     {r[\'driver\']}")', 'print(f"  driver     {r[\'driver\']} (console edit)")', 1)
    assert console != src and _main_head_digest(console) == _MAIN_HEAD_DIGEST, "the printing section stays free to change"


def test_the_replay_computation_is_byte_identical_to_the_engine_the_forward_test_registers():
    """E1 registers the engine at 56f9da6. Round 20 changed only the console section of this file: every other definition, and the arguments main() hands to replay(), must still hash to the pinned
    values. If this fails the registered experiment's engine moved: revert the computation change or re-register the experiment with the owner."""
    src = open(rp.__file__, encoding="utf-8").read()
    digest, n = _computation_digest(src)
    assert n >= 40 and digest == _COMPUTATION_DIGEST, "a computation definition of carry_replay.py changed"
    assert _replay_call_digest(src) == _REPLAY_CALL_DIGEST, "main() now reaches replay() with different arguments"


def _acquire_r():
    return {"execution_mode": "acquire", "ledger": {"realised_usd": 428.34}}


def test_acquire_mode_never_prints_a_hedge_failure_for_a_leg_the_journal_books_elsewhere():
    """REPRODUCED (Round 20): 'residual -$93,425 = -100.000% of a leg' was printed for an ACQUIRE run whose hedge is fine: the revaluation entries it sums exist only for a held coin."""
    eff = {"perp_realised_usd": -93425.0, "inventory_change_usd": 0.0, "residual_usd": -93425.0, "residual_fraction_of_leg": -1.0}
    text = "\n".join(rp.present_hedge_lines(_acquire_r(), eff))
    assert "% of a leg" not in text and "-100" not in text and "NOT a hedge failure" in text and "not measured in ACQUIRE mode" in text
    over = dict(_acquire_r(), execution_mode="overlay")
    assert "residual" in "\n".join(rp.present_hedge_lines(over, dict(eff, inventory_change_usd=93017.0, residual_usd=-408.0, residual_fraction_of_leg=-0.00437)))


def test_both_break_even_bases_are_printed_with_the_assumption_that_makes_each_one_right():
    """REPRODUCED (Round 20): only the whole-window figure (3.42% overlay / 1.46% acquire) was printed as 'the break-even'. Against a flat 4.4% alternative that says acquire LOSES while on the
    held-capital basis (9.50%) it beats it. Both bases are printed, named, with the capital-time each assumes."""
    v = {"available": True, "break_even_cost_of_capital_pct": 1.46}
    text = "\n".join(rp.present_break_even_lines(v, 15.3))
    assert "whole-window" in text and "1.46%/yr" in text and "idle between trades earns NOTHING" in text
    assert "held-capital" in text and f"{1.46 / 0.153:.2f}%/yr" in text and "earns the alternative while idle" in text and "REDEPLOYED WHEN FLAT" in text
    assert rp.present_break_even_lines({"available": False}, 15.3) == [] and rp.present_break_even_lines(v, 0.0) == []
    # independent review (Round 20): in an OVERLAY half the committed capital is the owner's held coin, which is not redeployed when flat; and both figures are upper bounds while the USDT interest is unstated
    over = "\n".join(rp.present_break_even_lines(dict(v, break_even_status="UPPER_BOUND_USDT_FINANCING_UNSTATED"), 79.3, "overlay"))
    assert "HELD COIN" in over and "margin-cash component only" in over and "BOTH figures are UPPER BOUNDS" in over and "REDEPLOYED WHEN FLAT" not in over


def test_the_overlay_ledger_block_does_not_sit_a_cash_net_under_a_headline_it_does_not_equal():
    """REPRODUCED (Round 20): 'NET realised -$135,485.61 ... agrees with the run above: True' under a +$33,049.51 headline. The cash legs, the inventory mark and the pair net are now one traceable sum."""
    st = {"funding_usd": 38885.91, "fees_usd": 5425.82, "borrow_usd": 0.0, "realised_usd": -168945.71, "net_usd": -135485.61, "inventory_mark_usd": 168537.45, "pair_net_usd": 33051.84, "fees_unknown": 0}
    r = {"execution_mode": "overlay", "ledger": st, "ledger_entries": 194, "unrealised_usd": -2.33, "net_usd": 33049.51, "ledger_agrees": True}
    text = "\n".join(rp.present_ledger_lines(r))
    assert "NOT the headline NET" in text and "inventory mark" in text and "= PAIR net" in text and "33,051.84" in text and "agrees True" in text
    withheld = dict(r, ledger=dict(st, pair_net_usd=None), ledger_agrees=None)
    wtext = "\n".join(rp.present_ledger_lines(withheld))
    assert "WITHHELD" in wtext and "UNKNOWN" in wtext and "agrees True" not in wtext


def test_an_unstated_cost_of_capital_is_never_printed_as_a_priced_zero():
    """REPRODUCED (Round 20): 'opportunity cost -$0.00 -> net after it $33,049.51' read as a priced result when no rate was stated."""
    r = {"owner_capital": {"cost_of_capital": {"state": "UNSTATED"}}, "opportunity_cost_usd": -0.0, "net_after_opportunity_cost_usd": 33049.51}
    text = "\n".join(rp.present_opportunity_cost_lines(r))
    assert "NOT PRICED" in text and "-0.00" not in text and "33,049.51" not in text
    stated = dict(r, owner_capital={"cost_of_capital": {"state": "STATED"}}, opportunity_cost_usd=1000.0, net_after_opportunity_cost_usd=32049.51)
    assert "32,049.51" in "\n".join(rp.present_opportunity_cost_lines(stated))


def test_the_real_report_states_its_structure_capital_costs_and_that_it_is_partial(capsys, tmp_path):
    """End to end on the committed corpus: the P&L is untouched (the pinned headline), and the report says what it is."""
    out = tmp_path / "overlay.json"
    assert rp.main(["--mode", "overlay", "--style", "taker", "--exit-policy", "funding_streak", "--notional", "100000", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    r = json.load(open(out))
    assert r["net_usd"] == pytest.approx(33049.51, abs=0.01), "the underlying P&L must not move"
    for needle in ("WHAT THIS REPORT IS", "OWNED_INVENTORY_OVERLAY", "BREAK-EVEN BASES", "whole-window", "held-capital", "NOT PRICED", "PAIR net", "NOT the headline NET", "PARTIAL"):
        assert needle in text, needle
    eb = r["evidence_basis"]
    assert eb["cost_complete"] is False and eb["execution"].startswith("MODELLED") and eb["financing"]["cost_of_capital"] == "UNSTATED"
    assert eb["impact_provenance"]["floor"].startswith("ASSUMED") and eb["impact_provenance"]["latency"].startswith("NOT_MODELLED") and eb["costs_not_included"]
    assert "% of a leg" not in text.split("DID THE HEDGE HEDGE")[0]


def test_the_real_acquire_report_prints_no_false_hedge_failure_and_the_held_capital_break_even(capsys):
    assert rp.main(["--mode", "acquire", "--style", "taker", "--exit-policy", "funding_streak", "--notional", "100000"]) == 0
    text = capsys.readouterr().out
    assert "-100.000% of a leg" not in text and "NOT a hedge failure" in text
    assert "9.50%/yr" in text and "1.46%/yr" in text, "both break-even bases of the committed acquire replay"
    assert "16,398.79" in text, "the underlying P&L must not move"
