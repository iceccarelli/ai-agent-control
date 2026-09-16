#!/usr/bin/env python3
"""The financing cost as a SERIES, and the three ways it refuses.

WHY
===
`borrow_apr` is a constant at every call site and the matrices sweep it at
0/3/5/8 %/yr. Over the exact window PHASE1_DECISION was computed on the real
USDT rate has a mean of 4.98%/yr and a median of 3.00%, and it ranges from
1.00% to 104.00%. A flat number is not that.

WHAT IT REFUSES, AND WHY EACH REFUSAL EXISTS
============================================
1. OUTSIDE COVERAGE. The venue floor is 2021-12-14 and the carry corpus
   reaches 2019-09, so 32.2% of the seven-year corpus has NO financing data at
   any price. Forward-filling backwards would price 2019 at 2021's rate and
   report it as a measurement. `rate_at` raises; it does not extrapolate.

2. PAST A STALENESS BOUND. The series has one 10-hour hole (2022-12-18 08:00
   -> 18:00, 9 observations missing) inside the frozen window. Carrying the
   last rate forward indefinitely would silently price a month from a single
   stale print, so the carry is BOUNDED and a lookup beyond it raises.

3. A RATE THAT IS NOT A RATE. Non-finite or negative values are refused at
   load, not at use.

The failure this guards against is D46's inverted: that one silently DROPPED
real settlements; this one would silently INVENT financing. Both make a number
look cleaner than the data behind it.

WHAT THE NUMBER IS
==================
OKX's public savings LENDING rate — what a lender earns. A borrower pays more,
so every value here is a FLOOR on the true cost of financing. A book that
fails to clear its costs against these rates fails harder against real ones.
OKX is not the venue this book trades on; it is used because it is the only
deep public series of its kind.
"""
from __future__ import annotations

import bisect
import csv
import gzip
import os
from typing import List, Optional, Sequence, Tuple

DEFAULT_PATH = "data/real_borrow/OKX_LENDING_RATE_USDT_1H.csv.gz"

#: How long one observation may be carried forward.
#:
#: THE FIRST VALUE HERE WAS 12h AND IT WAS WRONG — wrong in the direction that
#: makes a guard useless. The series is hourly and its single hole is 10h
#: (2022-12-18 08:00 -> 18:00), so a 12h bound ADMITTED the only defect the
#: data actually has: a lookup inside that hole returned a rate instead of
#: refusing. A bound chosen to cover the gaps present is not a bound, it is a
#: description.
#:
#: 4h is three hourly observations of slack — enough that an ordinary late
#: print does not trip it, tight enough that the 10h hole REFUSES. The cost of
#: that refusal is visible and small: it is one window, and a simulator that
#: cannot price 9 hours should say so rather than charge a rate nobody quoted.
MAX_STALENESS_MS = 4 * 3_600_000


class BorrowCurveError(RuntimeError):
    """No honest financing rate is available for this instant."""


class BorrowCurve:
    """Hourly financing rates, with coverage you cannot accidentally leave."""

    def __init__(self, stamps: Sequence[int], rates: Sequence[float],
                 *, source: str = DEFAULT_PATH,
                 max_staleness_ms: int = MAX_STALENESS_MS) -> None:
        if len(stamps) != len(rates) or not stamps:
            raise BorrowCurveError("a curve needs matching, non-empty series")
        self._ms: List[int] = list(stamps)
        self._rate: List[float] = list(rates)
        self.source = source
        self.max_staleness_ms = int(max_staleness_ms)
        if self._ms != sorted(self._ms):
            raise BorrowCurveError(f"{source}: timestamps are not increasing")

    # -- coverage ---------------------------------------------------------

    @property
    def first_ms(self) -> int:
        return self._ms[0]

    @property
    def last_ms(self) -> int:
        return self._ms[-1]

    def covers(self, ms: int) -> bool:
        """True when `rate_at` would answer rather than raise."""
        try:
            self.rate_at(ms)
        except BorrowCurveError:
            return False
        return True

    # -- the lookup -------------------------------------------------------

    def rate_at(self, ms: int) -> float:
        """The last observation at or before `ms`, as an annual fraction.

        Raises rather than guessing. There is no forward-fill past the
        staleness bound and no extrapolation before the first observation:
        a financing cost invented for a period the venue has no data for is
        not a measurement, however plausible the number looks.
        """
        ms = int(ms)
        if ms < self._ms[0]:
            raise BorrowCurveError(
                f"{ms} is before this curve begins ({self._ms[0]}); the venue "
                "has no financing data there and inventing one would price a "
                "period from a rate that did not exist")
        index = bisect.bisect_right(self._ms, ms) - 1
        age = ms - self._ms[index]
        if age > self.max_staleness_ms:
            raise BorrowCurveError(
                f"the last observation before {ms} is {age / 3.6e6:.1f}h old "
                f"(bound {self.max_staleness_ms / 3.6e6:.0f}h); carrying a rate "
                "that far forward prices a period from a single stale print")
        return self._rate[index]

    # -- what a simulator charges -----------------------------------------

    def per_day(self, ms: int) -> float:
        """Financing for ONE held day, as a fraction of notional.

        Mirrors `(borrow_apr / 365.0)` exactly, so a flat curve reproduces the
        constant path bit for bit.
        """
        return self.rate_at(ms) / 365.0

    def per_print(self, ms: int, periods_per_day: int = 3) -> float:
        """Financing for ONE held funding print.

        Mirrors `borrow_apr / (365.0 * FUNDING_PERIODS_PER_DAY)` exactly.
        """
        return self.rate_at(ms) / (365.0 * periods_per_day)

    # -- construction -----------------------------------------------------

    @classmethod
    def flat(cls, apr: float) -> "BorrowCurve":
        """A constant rate, as a curve. The equivalence harness: handed this,
        every consumer must produce what the scalar produced."""
        return cls([0], [float(apr)], source=f"flat:{apr}",
                   max_staleness_ms=1 << 62)

    @classmethod
    def load(cls, repo: str = ".", path: str = DEFAULT_PATH,
             **kwargs) -> "BorrowCurve":
        full = os.path.join(repo, path)
        if not os.path.exists(full):
            raise BorrowCurveError(
                f"no borrow series at {path}. Fetch it:\n"
                "  python3 tools/fetch_borrow_rates.py --write")
        opener = gzip.open if full.endswith(".gz") else open
        stamps: List[int] = []
        rates: List[float] = []
        with opener(full, "rt") as handle:
            for row in csv.DictReader(handle):
                rate = float(row["rate_annual"])
                if rate != rate or rate < 0:
                    raise BorrowCurveError(
                        f"{path}: unusable rate {row['rate_annual']!r} at "
                        f"{row.get('utc')}")
                stamps.append(int(row["ts"]))
                rates.append(rate)
        return cls(stamps, rates, source=path, **kwargs)

    # -- reporting --------------------------------------------------------

    def gaps(self, expect_ms: int = 3_600_000) -> List[Tuple[int, int]]:
        """Holes wider than one observation interval. Reported, never filled."""
        return [(a, b) for a, b in zip(self._ms, self._ms[1:])
                if b - a > expect_ms]

    def summary(self) -> dict:
        return {"source": self.source, "observations": len(self._ms),
                "first_ms": self.first_ms, "last_ms": self.last_ms,
                "gaps": len(self.gaps()),
                "max_staleness_hours": self.max_staleness_ms / 3.6e6,
                "what_this_is": (
                    "OKX public LENDING rate: what a lender earns. A borrower "
                    "pays more, so this is a FLOOR on financing cost.")}
