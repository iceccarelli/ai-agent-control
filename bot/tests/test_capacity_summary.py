import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import capacity_summary as CS  # noqa: E402
import md_capacity as MC  # noqa: E402
from test_md_integration import complete_state  # noqa: E402


def synth(at_ms, funding, cost_small=11.0, cost_big=13.0, big_ok=True):
    return {"at_ms": at_ms, "environment": "mainnet", "fee_source": "published",
            "funding_forecast_bps_per_settlement": funding,
            "max_displayed_fillable_notional_usd": 1_000_000.0 if big_ok else 10_000.0,
            "curve": [
                {"notional_usd": 10_000.0, "status": "MEASURED", "total_round_trip_bps": cost_small, "fee_round_trip_bps": 11.0},
                {"notional_usd": 1_000_000.0, "status": "MEASURED" if big_ok else "UNAVAILABLE",
                 **({"total_round_trip_bps": cost_big, "fee_round_trip_bps": 11.0} if big_ok else {"reasons": ["DEPTH_INSUFFICIENT"]})}]}


def write(tmp_path, rows, name="capacity-20261105.jsonl", extra=""):
    (tmp_path / name).write_text("".join(json.dumps(r) + "\n" for r in rows) + extra)


def test_the_numbers_are_the_arithmetic_of_the_rows(tmp_path):
    write(tmp_path, [synth(0, 1.0), synth(3_600_000, 0.5), synth(7_200_000, -0.5), synth(10_800_000, 1.5)])
    s = CS.summarise(CS.load_rows(str(tmp_path)), hold_settlements=21)
    assert s["samples"] == 4 and s["window_hours"] == 3.0
    f = s["funding_forecast_bps_per_settlement"]
    assert f["mean"] == pytest.approx(0.625) and f["share_positive"] == 0.75 and f["min"] == -0.5
    small = next(r for r in s["by_size"] if r["notional_usd"] == 10_000.0)
    assert small["mean_total_round_trip_bps"] == 11.0
    assert small["breakeven_settlements_at_mean_forecast"] == pytest.approx(11.0 / 0.625)
    assert small["net_bps_after_hold_at_mean_forecast"] == pytest.approx(21 * 0.625 - 11.0)
    # forecast x 21 >= 11 needs forecast >= 0.5238: rows with 1.0, 1.5 qualify, 0.5 and -0.5 do not
    assert small["share_of_snapshots_where_forecast_projection_covers_estimated_cost"] == 0.5
    assert s["max_displayed_fillable_notional_usd"] == 1_000_000.0


def test_a_size_the_book_cannot_fill_is_reported_as_not_fillable_not_priced(tmp_path):
    write(tmp_path, [synth(0, 1.0, big_ok=False), synth(60_000, 1.0, big_ok=False)])
    s = CS.summarise(CS.load_rows(str(tmp_path)))
    big = next(r for r in s["by_size"] if r["notional_usd"] == 1_000_000.0)
    assert big["book_fillable_snapshot_share"] == 0.0 and "mean_total_round_trip_bps" not in big
    assert "not fillable" in CS.render(s)


def test_negative_mean_funding_gives_no_breakeven_and_no_invented_net(tmp_path):
    write(tmp_path, [synth(0, -1.0), synth(60_000, -2.0)])
    small = next(r for r in CS.summarise(CS.load_rows(str(tmp_path)))["by_size"] if r["notional_usd"] == 10_000.0)
    assert "breakeven_settlements_at_mean_forecast" not in small and "net_bps_after_hold_at_mean_forecast" not in small
    assert small["share_of_snapshots_where_forecast_projection_covers_estimated_cost"] == 0.0


def test_a_torn_last_line_and_foreign_files_do_not_hide_the_good_rows(tmp_path):
    write(tmp_path, [synth(0, 1.0), synth(60_000, 1.0)], extra='{"at_ms": 5, "cur')
    (tmp_path / "capacity-20261104.jsonl").write_text('{"not": "a capacity row"}\n')
    assert len(CS.load_rows(str(tmp_path))) == 2


def test_no_rows_is_exit_2_and_a_clear_message(tmp_path, capsys):
    assert CS.main(["--dir", str(tmp_path)]) == 2
    assert "no capacity rows" in capsys.readouterr().out
    assert CS.main(["--dir", str(tmp_path), "--hold-settlements", "0"]) == 3


def test_it_summarises_a_row_produced_by_the_real_capacity_module(tmp_path):
    st, now, wall = complete_state()
    row = MC.capacity_row(st.view(now, wall), now_ms=wall)
    write(tmp_path, [row, dict(row, at_ms=row["at_ms"] + 300_000)])
    s = CS.summarise(CS.load_rows(str(tmp_path)))
    assert s["samples"] == 2 and s["by_size"] and "descriptive" in " ".join(s["caveats"])
    text = CS.render(s)
    assert "size USD" in text and "in-sample" in text
    assert CS.main(["--dir", str(tmp_path), "--json"]) == 0


# -- the regime verdict ----------------------------------------------------------------------------------

def _days(n_hours, funding, cost=11.0):
    return [synth(h * 3_600_000, funding, cost_small=cost) for h in range(n_hours + 1)]


def test_regime_arithmetic_is_gross_minus_amortised_cost(tmp_path):
    rows = _days(100, 0.65)
    r = CS.regime(rows, hurdle_apr_pct=2.0, hold_settlements=21, notional_usd=10_000.0)
    gross = 0.65 * 3 * 365 / 100.0
    cost = 11.0 * (365.0 / 7.0) / 100.0
    assert r["gross_apr_pct"] == pytest.approx(gross) and r["cost_apr_pct"] == pytest.approx(cost)
    assert r["net_apr_pct"] == pytest.approx(gross - cost)


def test_open_when_net_clears_the_hurdle_closed_when_it_does_not():
    rows = _days(100, 0.65)                  # gross 7.12 %/yr; cost 1.34 %/yr at a 30 d hold, 5.74 %/yr at 7 d
    assert CS.regime(rows, hurdle_apr_pct=2.0, hold_settlements=90, notional_usd=10_000.0)["verdict"] == "OPEN"
    assert CS.regime(rows, hurdle_apr_pct=6.9, hold_settlements=90, notional_usd=10_000.0)["verdict"] == "CLOSED"
    # a short hold pays the cost many times a year: the same funding no longer clears the same hurdle
    assert CS.regime(rows, hurdle_apr_pct=2.0, hold_settlements=21, notional_usd=10_000.0)["verdict"] == "CLOSED"


def test_a_short_window_or_no_hurdle_is_insufficient_never_open():
    assert CS.regime(_days(10, 5.0), hurdle_apr_pct=1.0, notional_usd=10_000.0)["verdict"] == "INSUFFICIENT"
    assert CS.regime(_days(100, 5.0), hurdle_apr_pct=None, notional_usd=10_000.0)["verdict"] == "INSUFFICIENT"
    assert CS.regime([], hurdle_apr_pct=1.0)["verdict"] == "INSUFFICIENT"
    assert CS.regime(_days(100, 5.0), hurdle_apr_pct=1.0, notional_usd=123.0)["verdict"] == "INSUFFICIENT"


def test_negative_funding_is_closed_and_the_cli_prints_the_verdict(tmp_path, capsys):
    rows = _days(100, -0.5)
    write(tmp_path, rows)
    assert CS.main(["--dir", str(tmp_path), "--hurdle-apr", "1", "--regime-notional", "10000"]) == 0
    out = capsys.readouterr().out
    assert "REGIME at $10,000" in out and "CLOSED" in out and "in-sample" in out
