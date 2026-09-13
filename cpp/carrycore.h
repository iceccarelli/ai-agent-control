/* carrycore — the settlement-clock simulator, as a C ABI.
 *
 * WHY THIS IS IN C++ AND WHAT IT IS NOT FOR
 * =========================================
 * Measured on this tree before a line of this was written:
 *
 *   one engine decision            1.8 us       (the book decides 3x a day)
 *   one 4-year settlement sim      5 ms         (4,500 prints)
 *
 * So C++ buys NOTHING on the decision path. An 8-hour funding clock does not
 * care about microseconds, and anyone who tells you otherwise is selling
 * latency to a book that holds for five days.
 *
 * What Python IS too slow for is SEARCH. Every question this book still has to
 * answer is a sweep over thousands of configurations against millions of
 * events: what exit rule survives out of sample, what a maker fill does to the
 * round trip, where impact eats the edge at size, what the wick does to the
 * short leg at tick resolution. Those are 10^4 to 10^7 simulations, and at 5 ms
 * each Python turns a morning's question into a week.
 *
 * This library is therefore a THROUGHPUT engine, not a latency one. It places
 * no orders, reads no venue, holds no state between calls, and has no path to
 * anything that can lose money. It is arithmetic that must agree with
 * tools/carry_backtest.py to the cent — a differential test asserts it.
 */
#ifndef CARRYCORE_H
#define CARRYCORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* One funding settlement: the prices at the stamp, the rate that printed, and
 * the worst perp trade in the window that ended there. */
typedef struct {
    int64_t ms;
    double perp;
    double spot;
    double rate;       /* fraction, not bps */
    double perp_high;
} CarrySettlement;

typedef struct {
    double notional;
    double entry_bps;            /* ungated rule: open at or above this print */
    double borrow_apr;
    double impact_bps;           /* per leg */
    double round_trip_bps;       /* what the gate is told the exit costs */
    double hold_days;            /* the gate's amortisation window */
    double taker_spot_bps;
    double taker_perp_bps;
    double ewma_alpha;
    int gated;                   /* 1 = the rule CarryEngine runs */
    int overlay;                 /* 1 = never trade the client's spot */
    int ewma_min_prints;
    int negative_exit_prints;
    int funding_history;         /* prints the EWMA may see */
} CarryParams;

typedef struct {
    double funding;
    double basis;
    double fees;                 /* positive = a cost */
    double borrow;
    double impact;
    double net;
    double days_in_market;
    double max_adverse_short_pct;
    int32_t trades;
    int32_t closed_trades;       /* excludes the one open at the window end */
    int32_t losing_trades;
    int32_t refusals;            /* gated entries the cost gate refused */
} CarryResult;

/* One simulation. Returns 0 on success, non-zero on bad input. */
int carry_simulate(const CarrySettlement *rows, int32_t n,
                   const CarryParams *params, CarryResult *out);

/* The same simulation over a grid of (entry_bps x hold_days x exit_prints).
 * `out` must hold n_entry * n_hold * n_exit results, indexed in that order.
 * `threads` <= 0 means "one per core". Returns the number of runs. */
int32_t carry_sweep(const CarrySettlement *rows, int32_t n,
                    const CarryParams *base,
                    const double *entry_grid, int32_t n_entry,
                    const double *hold_grid, int32_t n_hold,
                    const int32_t *exit_grid, int32_t n_exit,
                    CarryResult *out, int32_t threads);

const char *carrycore_version(void);

#ifdef __cplusplus
}
#endif
#endif /* CARRYCORE_H */
