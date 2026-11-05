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
    assert small["share_of_samples_where_forecast_x_hold_covers_cost"] == 0.5
    assert s["max_displayed_fillable_notional_usd"] == 1_000_000.0


def test_a_size_the_book_cannot_fill_is_reported_as_not_fillable_not_priced(tmp_path):
    write(tmp_path, [synth(0, 1.0, big_ok=False), synth(60_000, 1.0, big_ok=False)])
    s = CS.summarise(CS.load_rows(str(tmp_path)))
    big = next(r for r in s["by_size"] if r["notional_usd"] == 1_000_000.0)
    assert big["fillable_share"] == 0.0 and "mean_total_round_trip_bps" not in big
    assert "not fillable" in CS.render(s)


def test_negative_mean_funding_gives_no_breakeven_and_no_invented_net(tmp_path):
    write(tmp_path, [synth(0, -1.0), synth(60_000, -2.0)])
    small = next(r for r in CS.summarise(CS.load_rows(str(tmp_path)))["by_size"] if r["notional_usd"] == 10_000.0)
    assert "breakeven_settlements_at_mean_forecast" not in small and "net_bps_after_hold_at_mean_forecast" not in small
    assert small["share_of_samples_where_forecast_x_hold_covers_cost"] == 0.0


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
