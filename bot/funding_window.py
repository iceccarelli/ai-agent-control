"""The funding a hold measured on bars is exposed to, with the boundaries stated.

WHY THIS IS A NEW MODULE
------------------------
`signals.funding_carry_fade_v1.funding_paid_over_hold` is part of a FROZEN
(ABSENT) measurement: its numbers are pinned in static artefacts, and its
shared functions are required to be the same objects in the BTC variant. It is
left byte-for-byte as it is. Its window is ``(close_time(signal bar),
close_time(exit bar)]`` which, on the daily corpora, begins 1 s (1 ms in
hand-built fixtures) BEFORE the entry bar's open, so a print stamped exactly at
the entry open -- one per daily trade, since prints land at 00:00 UTC -- is
credited to a position that opens at that very instant; and it ends at the exit
bar's close even when a stop or target ended the trade earlier in that bar.
Both errors credit RECEIPTS to a strategy that is short exactly when funding
is rich and positive. Correcting the frozen function in place would change the
frozen evidence, so the corrected convention lives here, for any NEW
measurement, and the frozen one is only guarded by a parity test.

THE CONVENTION
--------------
* The position opens at ``open[signal + 1]`` (``T``). A print stamped at or
  before ``T`` belongs to whoever held the position before: the window is
  exclusive at ``T``.
* The window ends at the exit bar's close (inclusive: a print stamped exactly
  there is paid by the position still held).
* A trade ended INTRABAR (a stop or target touched inside the exit bar) cannot
  be placed against the prints inside that bar, so the unfavourable order is
  assumed: prints after the exit bar's open are counted only when they are a
  PAYMENT by the holder, never a receipt.
* A missing print costs nothing here (as in the frozen function); completeness
  of the funding history is checked upstream, in the data contract.
"""
from __future__ import annotations

from typing import Sequence

from signals.funding_carry_fade_v1 import (  # noqa: F401  (shared, unmodified)
    LONG_SETUP, SHORT_SETUP, FundingSeries, close_time_ms)


def funding_paid_over_window(funding: FundingSeries, bars: Sequence,
                             signal_index: int, bars_held: int, direction: str,
                             *, intrabar_exit: bool = False) -> float:
    """Funding paid (positive) or received (negative) over one hold, in rate
    units, on the convention in this module's docstring."""
    entry_index = signal_index + 1
    if entry_index >= len(bars):
        raise ValueError("a signal on the last bar has no entry bar to open on")
    if direction == LONG_SETUP:
        sign = 1.0
    elif direction == SHORT_SETUP:
        sign = -1.0
    else:
        raise ValueError(f"unknown direction {direction!r}")
    exit_index = min(entry_index + bars_held, len(bars) - 1)
    entry_open = int(bars[entry_index].start_ms)
    exit_open = int(bars[exit_index].start_ms)
    exit_close = close_time_ms(bars[exit_index])
    if not intrabar_exit:
        return sign * funding.sum_between(entry_open, exit_close)
    settled = funding.sum_between(entry_open, exit_open)
    tail = [rate for stamp, rate in zip(funding.times_ms, funding.rates)
            if exit_open < stamp <= exit_close]
    # inside the exit bar: only what the holder would PAY
    return sign * settled + sum(max(sign * rate, 0.0) for rate in tail)
