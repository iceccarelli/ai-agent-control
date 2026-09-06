#!/usr/bin/env python3
"""Run the full evaluation: backtest, walk-forward, Monte Carlo.

Usage::

    python3 tools/run_evaluation.py                  # the data/ corpus (default)
    python3 tools/run_evaluation.py data/BTC.csv     # explicit CSVs
    python3 tools/run_evaluation.py --synthetic      # in-line generated series
    python3 tools/run_evaluation.py --category linear

With the shipped corpus this loads OHLCV **and** the L2 order book, so the
liquidity gate is actually evaluated rather than skipped. Whatever the source,
the provenance is printed first and the verdict is printed last, and neither is
optional.

The corpus in ``data/`` is synthetic. It is not a market. It exists so the
machinery can be exercised and the plumbing verified offline; every number
produced from it measures the harness. Replace those files with real CoinAPI
flat files or Bybit klines — same filenames, same columns — and this same
command produces numbers that mean something.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import backtest as bt
import market_data as md

# DATA-READ LEDGER (slice 78). Installed BEFORE the first load_corpus call so
# that every corpus this tool reads is recorded, and a future holdout can be
# certified untouched by tools/reserved_holdout.py. Reading is reading: a
# negative result steers the next hypothesis exactly as a positive one does,
# which is how slice 57 ended up "out of sample" on bytes slice 55 had read.
try:
    import reserved_holdout as _read_ledger
    _read_ledger.install()
except Exception as _ledger_exc:  # noqa: BLE001 - never blocks a measurement
    # NOT a silent pass: the reason is bound and the flag is legible. An
    # unrecorded read is a real loss (a future holdout cannot be certified),
    # but it must not take the measurement down with it.
    _read_ledger = None
    _READ_LEDGER_UNAVAILABLE = repr(_ledger_exc)




def synthetic_series(n: int = 2000, seed: int = 2024):
    """A series with a 2020-03-style crash and a 2022-style grinding bear leg.

    Explicitly synthetic. It is here so the harness can be exercised without
    network access or the data corpus; it is not a market and must never be
    reported as one.
    """
    rng = np.random.default_rng(seed)
    price = 100.0
    bars = []
    for i in range(n):
        phase = i / max(1, n)
        if 0.22 <= phase < 0.25:
            mu, sigma = -0.06, 0.05          # 2020-03-style crash
        elif 0.25 <= phase < 0.30:
            mu, sigma = 0.012, 0.03          # violent recovery
        elif 0.55 <= phase < 0.78:
            mu, sigma = -0.0025, 0.012       # 2022-style grinding bear
        else:
            mu, sigma = 0.0006, 0.008
        ret = rng.normal(mu, sigma)
        opened = price
        price = max(0.5, price * (1 + ret))
        bars.append(bt.Bar(
            1_600_000_000_000 + i * 3_600_000, opened,
            max(opened, price) * (1 + abs(rng.normal(0, sigma / 3))),
            min(opened, price) * (1 - abs(rng.normal(0, sigma / 3))),
            price, 1000.0,
        ))
    return bars


def _fold_span(bars, fold, total_folds):
    """The calendar dates a fold covers, and what the asset did in them.

    Printed next to every fold because "fold 3 lost money" is a much weaker
    statement than "the 2022 bear market lost money". A losing fold that maps
    onto the one down-trending period in the sample is not noise, it is the
    diagnosis.
    """
    import datetime as _dt

    size = len(bars) // max(1, total_folds)
    start = (fold["fold"] - 1) * size
    end = min(len(bars) - 1, start + size)

    def day(index):
        ms = bars[max(0, min(index, len(bars) - 1))].start_ms
        return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%Y-%m")

    move = 0.0
    if bars[start].close > 0:
        move = (bars[end].close - bars[start].close) / bars[start].close
    return f"{day(start)}..{day(end)}  asset {move:>+8.1%}"


def buy_and_hold(bars_by_symbol):
    """The return of simply holding each symbol over the sample.

    The benchmark this harness lacked, and its absence is why a drift-capturing
    long-only result could have read as an edge. A strategy that is long an
    appreciating asset must be compared against being long that asset; anything
    else measures the asset, not the strategy.

    Deliberately gross of fees. One entry and one exit is a rounding error
    against a multi-year hold, and subtracting them would only flatter the
    strategy being compared.
    """
    out = {}
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 2 or bars[0].close <= 0:
            continue
        out[symbol] = (bars[-1].close - bars[0].close) / bars[0].close
    return out


def _to_backtest_bars(bars):
    """``market_data.Bar`` -> ``backtest.Bar``.

    They are separate types on purpose: ``market_data.Bar`` carries microsecond
    exchange timestamps and a trade count, and the backtester wants neither.
    Converting explicitly here — rather than making one an alias of the other —
    keeps the loader free to describe the data more richly than the simulator
    needs.
    """
    return [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in bars]


def load_inputs(args):
    """Return ``(bars_by_symbol, books_by_symbol, provenance_lines)``."""
    if args.synthetic:
        return (
            {"BTCUSDT": synthetic_series()}, {},
            ["SYNTHETIC in-line series — a generated path, not a market.",
             "No order book: the liquidity gate abstains for every bar."],
        )

    if args.paths:
        bars_by_symbol = {}
        for path in args.paths:
            symbol = os.path.splitext(os.path.basename(path))[0].upper()
            bars_by_symbol[symbol] = bt.load_bars_csv(path)
        return (
            bars_by_symbol, {},
            [f"kline CSVs: {', '.join(args.paths)}",
             "No order book supplied: the liquidity gate abstains."],
        )

    bars, books, notes = md.load_corpus(
        args.data_dir, symbols=args.symbols or None, levels=args.levels
    )
    lines = [f"corpus: {os.path.abspath(args.data_dir)}"] + notes
    for symbol in sorted(bars):
        lines.append(
            f"  {symbol}: {len(bars[symbol])} bars, "
            f"{'with' if symbol in books else 'WITHOUT'} an order book"
        )
    return ({s: _to_backtest_bars(b) for s, b in bars.items()}, books, lines)


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="explicit kline CSV files")
    parser.add_argument("--data-dir", default=md.DATA_ROOT)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--category", default="spot", choices=("spot", "linear"))
    parser.add_argument("--levels", type=int, default=25)
    # The bar size the corpus is in. It matters more than any other single
    # setting: the round-trip cost is fixed in bps while the ATR risk unit
    # scales with the bar, so the SAME geometry that loses 0.60R per trade on
    # hourly bars loses 0.08R on daily ones. See GEOMETRY.md.
    parser.add_argument("--interval", default="60",
                        help="Bybit interval code of the corpus (60, 240, D)")
    parser.add_argument("--stop-atr", type=float, default=None)
    parser.add_argument("--min-rr", type=float, default=None)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--min-confidence", type=float, default=0.12)
    parser.add_argument("--min-agreement", type=float, default=0.40)
    args = parser.parse_args(argv)

    bars_by_symbol, books_by_symbol, provenance = load_inputs(args)

    cfg = bt.BacktestConfig(
        starting_cash=10_000.0,
        warmup_bars=200,
        category=args.category,
        # The shipped defaults (0.60/0.60) produce ZERO trades on this
        # strategy. The looser operating point below exists so the harness is
        # exercised and the block reasons are visible; it is printed with the
        # results rather than applied quietly, because choosing an operating
        # point is a calibration decision that belongs to the operator.
        min_confidence=args.min_confidence,
        min_component_agreement=args.min_agreement,
        interval=args.interval,
        **({"stop_atr_multiple": args.stop_atr} if args.stop_atr else {}),
        **({"min_risk_reward": args.min_rr} if args.min_rr else {}),
    )

    print("=" * 74)
    print("DATA PROVENANCE")
    print("=" * 74)
    for line in provenance:
        print(line)
    print()
    print(f"category          : {cfg.category}"
          f"  (shorts {'ENABLED' if cfg.category == 'linear' else 'unavailable'})")
    print(f"operating point   : min_confidence={cfg.min_confidence}, "
          f"min_agreement={cfg.min_component_agreement}  "
          "(shipped defaults are 0.60/0.60 and produce zero trades)")
    print(f"geometry          : interval={cfg.interval}, "
          f"stop={cfg.stop_atr_multiple}xATR, reward={cfg.min_risk_reward}xrisk")

    print()
    print("=" * 74)
    print("FULL-PERIOD BACKTEST")
    print("=" * 74)
    full = bt.Backtester(bars_by_symbol, cfg, books_by_symbol=books_by_symbol).run()
    print(full.summary())

    print()
    print("=" * 74)
    print("BENCHMARK — WHAT DOING NOTHING WOULD HAVE PAID")
    print("=" * 74)
    benchmark = buy_and_hold(bars_by_symbol)
    for symbol, ret in sorted(benchmark.items()):
        print(f"  buy and hold {symbol:<10} {ret:>+12.2%}")
    best_bench = max(benchmark.values()) if benchmark else 0.0
    print()
    print("A long-only strategy on an asset that appreciated over the sample will")
    print("show positive expectancy in a barrier framework WITHOUT any edge: the")
    print("drift resolves the timeouts in its favour. The comparison below is the")
    print("cheapest defence against mistaking that for skill.")
    print(f"  strategy                  {full.total_return:>+12.2%}")
    print(f"  best buy-and-hold         {best_bench:>+12.2%}")
    if best_bench > 0 and full.total_return < best_bench:
        print(f"  -> the strategy captured {full.total_return / best_bench:.1%} of "
              "the move it was long into.")

    print()
    print("=" * 74)
    print("WALK-FORWARD — OUT-OF-SAMPLE ONLY")
    print("=" * 74)
    folds = bt.walk_forward(bars_by_symbol, cfg, folds=args.folds,
                            books_by_symbol=books_by_symbol)
    header = (f"{'fold':>5}{'train':>8}{'test':>7}{'trades':>8}"
              f"{'return':>10}{'maxDD':>8}{'winrate':>9}")
    print(header)
    primary = sorted(bars_by_symbol)[0]
    series = bars_by_symbol[primary]
    for fold in folds:
        wr = f"{fold['win_rate']:.1%}" if fold["win_rate"] is not None else "n/a"
        print(f"{fold['fold']:>5}{fold['train_bars']:>8}{fold['test_bars']:>7}"
              f"{fold['trades']:>8}{fold['return']:>+10.2%}"
              f"{fold['max_drawdown']:>8.2%}{wr:>9}"
              f"   {_fold_span(series, fold, len(folds))}")
    returns = [f["return"] for f in folds]
    losing = [f["fold"] for f in folds if f["return"] < 0]
    silent = [f["fold"] for f in folds if f["trades"] == 0]
    print(f"\nfolds profitable  : {len(folds) - len(losing)}/{len(folds)}")
    print(f"mean fold return  : {sum(returns) / len(returns):+.2%}")
    print(f"worst fold        : {min(returns):+.2%}")
    print(f"LOSING FOLDS      : {losing or 'none'}")
    if silent:
        # A fold that took no trades returns exactly 0.00% and would otherwise
        # be counted as "profitable". Saying so is the difference between a
        # report and a sales pitch.
        print(f"FOLDS THAT TOOK NO TRADES : {silent}  "
              "(counted above as 'profitable' only because they did nothing)")

    print()
    print("=" * 74)
    print("MONTE CARLO — BOOTSTRAP RUIN ESTIMATE")
    print("=" * 74)
    mc = bt.monte_carlo_ruin(
        full.trade_returns, starting_equity=cfg.starting_cash,
        position_fraction=cfg.max_position_pct,
        runs=5000, ruin_threshold=0.5,
    )
    for key, value in mc.items():
        shown = round(value, 6) if isinstance(value, float) else value
        print(f"  {key:26} {shown}")

    print()
    print("=" * 74)
    print("VERDICT")
    print("=" * 74)
    oos = sum(returns) / len(returns)
    ready = (
        oos > 0
        and not losing
        and not silent
        and not mc.get("insufficient_data", True)
        and mc.get("probability_of_ruin", 1.0) < 0.05
        and full.reconciliation_breaks == 0
    )
    print("NOT READY for live capital." if not ready else
          "Backtest criteria met — still requires paper and testnet stages.")
    if oos <= 0:
        print(f"  - mean out-of-sample return is {oos:+.2%} (must be > 0)")
    if losing:
        print(f"  - folds {losing} lost money out of sample")
    if silent:
        print(f"  - folds {silent} took no trades, so they are not evidence")
    if mc.get("probability_of_ruin", 1.0) >= 0.05:
        print(f"  - probability of ruin {mc.get('probability_of_ruin')} "
              "is above the 5% ceiling")
    if full.reconciliation_breaks:
        print(f"  - {full.reconciliation_breaks} book/exchange reconciliation "
              "breaks — results are not trustworthy until this is zero")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
