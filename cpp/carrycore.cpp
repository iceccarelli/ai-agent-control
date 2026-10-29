/* carrycore — see carrycore.h. Arithmetic only.
 *
 * Every operation below mirrors tools/carry_backtest.py in the same ORDER, so
 * the two agree to the cent on the same corpus. Where the Python reads
 * `carry_costs.evaluate_entry`, this reimplements that gate exactly: the same
 * EWMA over the same window, the same four refusals in the same sequence.
 * Changing either without the other breaks the differential test, which is
 * the point of having one.
 */
#include "carrycore.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <system_error>
#include <thread>
#include <vector>

namespace {

constexpr double kFundingPeriodsPerDay = 3.0;
constexpr int kMaxHistory = 64;

struct Trade {
    double qty = 0.0;
    double entry_spot = 0.0, entry_perp = 0.0;
    double exit_spot = 0.0, exit_perp = 0.0;
    double funding = 0.0, fees = 0.0, borrow = 0.0, impact = 0.0;
    int64_t opened_ms = 0, closed_ms = 0;
    double max_adverse_pct = 0.0;
    bool open_at_end = false;

    double basis() const {
        return qty * ((entry_perp - exit_perp) - (entry_spot - exit_spot));
    }
    double net() const { return funding + basis() - fees - borrow - impact; }
};

/* carry_costs.ewma_funding_bps: newest last, None below the minimum. */
bool ewma(const double *prints, int count, double alpha, int min_prints,
          double *out) {
    /* Non-finite prints are dropped BEFORE the minimum is applied, as
       carry_costs.ewma_funding_bps does. Without this one NaN poisons the
       average, every comparison against it is false, and the gate opens. */
    int used = 0;
    double smoothed = 0.0;
    for (int i = 0; i < count; ++i) {
        if (!std::isfinite(prints[i])) continue;
        smoothed = (used == 0) ? prints[i]
                               : alpha * prints[i] + (1.0 - alpha) * smoothed;
        ++used;
    }
    if (used < min_prints || used == 0) return false;
    *out = smoothed;
    return true;
}

/* Python's round(x, ndigits): the correctly rounded decimal, ties to even on
   the exact binary value. snprintf/strtod do the same, which is what keeps the
   break-even arithmetic below identical to carry_costs.min_settlements_to_clear
   at the boundary (std::round(x * 1e12) / 1e12 does not). */
double round_dp(double x, int digits) {
    char buf[512];
    std::snprintf(buf, sizeof buf, "%.*f", digits, x);
    return std::strtod(buf, nullptr);
}

/* carry_costs._invalid_cost_inputs. */
bool cost_inputs_valid(double round_trip_bps, double hold_days,
                       double borrow_apr) {
    return std::isfinite(round_trip_bps) && round_trip_bps > 0.0 &&
           std::isfinite(hold_days) && hold_days > 0.0 &&
           std::isfinite(borrow_apr) && borrow_apr >= 0.0;
}

/* carry_costs.hold_clears_round_trip, after evaluate_entry has allowed: does
   the smoothed funding earn back the round trip within the demonstrated hold?
   `smoothed` is finite and positive here. */
bool hold_clears(double smoothed, const CarryParams &p) {
    const double borrow_per_day = (p.borrow_apr / 365.0) * 1e4;
    const double budget = std::floor(round_dp(
        p.demonstrated_hold_days * kFundingPeriodsPerDay, 9));
    const double net =
        round_dp(smoothed - borrow_per_day / kFundingPeriodsPerDay, 12);
    if (net <= 0.0) return false;           /* never clears                  */
    const double needed =
        std::max(1.0, std::ceil(round_dp(p.round_trip_bps / net, 9)));
    return needed <= budget;
}

/* carry_costs.entry_gate: evaluate_entry gate for gate, in its order, then the
   hold check. Returns a CARRY_GATE_* code (0 = allowed). */
int gate_reason(const double *history, int count, double perp, double spot,
                const CarryParams &p) {
    /* carry_costs._invalid_cost_inputs: a NaN, a missing or non-positive
       round trip / hold, or a negative borrow is refused, not priced. */
    if (!cost_inputs_valid(p.round_trip_bps, p.hold_days, p.borrow_apr)) {
        return CARRY_GATE_COST_INPUT_INVALID;
    }
    double smoothed = 0.0;
    if (!ewma(history, count, p.ewma_alpha, p.ewma_min_prints, &smoothed)) {
        return CARRY_GATE_INSUFFICIENT_FUNDING_HISTORY;
    }
    if (smoothed <= 0.0) return CARRY_GATE_FUNDING_NOT_POSITIVE;

    const double carry_per_day = smoothed * kFundingPeriodsPerDay;
    const double borrow_per_day = (p.borrow_apr / 365.0) * 1e4;
    const double round_trip_per_day =
        p.round_trip_bps / std::max(p.hold_days, 1e-9);
    const double net_edge = carry_per_day - borrow_per_day - round_trip_per_day;

    /* carry_costs.basis_bps raises for a non-finite or non-positive price. */
    if (!(std::isfinite(perp) && std::isfinite(spot)) || spot <= 0.0 ||
        perp <= 0.0) {
        return CARRY_GATE_BASIS_UNREADABLE;
    }
    const double entry_basis = (perp / spot - 1.0) * 1e4;

    if (net_edge <= 0.0) return CARRY_GATE_CARRY_BELOW_COST_OF_CAPITAL;
    const double basis_budget = net_edge * p.hold_days;
    /* carry_costs.evaluate_entry: a budget that overflowed (inf - inf is NaN,
       inf carry is an unbounded budget) is refused, not compared. */
    if (!std::isfinite(basis_budget)) return CARRY_GATE_COST_INPUT_INVALID;
    if (entry_basis > basis_budget) return CARRY_GATE_BASIS_EXCEEDS_BUDGET;
    if (entry_basis < -basis_budget) return CARRY_GATE_BASIS_DISCOUNT_EXCEEDS_BUDGET;

    /* The hold check. A demonstrated hold that is not a positive number
       refuses (hold_clears_round_trip answers COST_INPUT_INVALID). */
    if (!cost_inputs_valid(p.round_trip_bps, p.demonstrated_hold_days,
                           p.borrow_apr) ||
        !std::isfinite(p.demonstrated_hold_days * kFundingPeriodsPerDay)) {
        return CARRY_GATE_COST_INPUT_INVALID;
    }
    if (!hold_clears(smoothed, p)) {
        return CARRY_GATE_BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED;
    }
    return CARRY_GATE_ALLOWED;
}

}  // namespace

extern "C" int carry_simulate(const CarrySettlement *rows, int32_t n,
                              const CarryParams *params, CarryResult *out) {
    if (!rows || !params || !out || n <= 0) return 1;
    const CarryParams &p = *params;

    std::vector<Trade> trades;
    Trade live;
    bool has_live = false;
    int streak = 0;
    int refusals = 0;
    int32_t by_reason[CARRY_GATE_REASONS] = {0};
    double history[kMaxHistory];
    int history_len = 0;

    const double per_print_borrow = p.borrow_apr / (365.0 * kFundingPeriodsPerDay);
    const double spot_legs = p.overlay ? 0.0 : 1.0;
    const int window = std::min(p.funding_history > 0 ? p.funding_history : 8,
                                kMaxHistory);

    auto charge = [&](Trade &t, double s, double q) {
        t.fees += t.qty * (spot_legs * s * p.taker_spot_bps
                           + q * p.taker_perp_bps) / 1e4;
        t.impact += t.qty * (spot_legs * s + q) * p.impact_bps / 1e4;
    };

    const int latency = p.latency_rows < 0 ? 0 : p.latency_rows;
    int64_t entry_fill_at = -1;           /* row a decided entry fills on */
    int64_t exit_fill_at = -1;            /* row a decided exit fills on  */
    bool exit_signalled = false;
    int unresolved_entries = 0, unresolved_exits = 0;

    auto close_live = [&](const CarrySettlement &st) {
        live.closed_ms = st.ms;
        live.exit_spot = st.spot;
        live.exit_perp = st.perp;
        charge(live, st.spot, st.perp);
        trades.push_back(live);
        has_live = false;
        streak = 0;
        exit_fill_at = -1;
        exit_signalled = false;
    };
    /* False (and no position) when either leg's price on the fill row is
       unusable: the decision row was gated, the fill row was not. */
    auto open_live = [&](const CarrySettlement &st) -> bool {
        if (!(std::isfinite(st.perp) && st.perp > 0.0 &&
              std::isfinite(st.spot) && st.spot > 0.0)) {
            streak = 0;
            return false;
        }
        live = Trade();
        live.qty = p.notional / st.perp;
        live.entry_spot = st.spot;
        live.entry_perp = st.perp;
        live.opened_ms = st.ms;
        charge(live, st.spot, st.perp);
        has_live = true;
        streak = 0;
        return true;
    };

    for (int32_t i = 0; i < n; ++i) {
        const CarrySettlement &st = rows[i];
        const double r_bps = st.rate * 1e4;

        if (history_len == window) {
            for (int k = 1; k < window; ++k) history[k - 1] = history[k];
            history[window - 1] = r_bps;
        } else {
            history[history_len++] = r_bps;
        }

        if (entry_fill_at >= 0) {
            if (i < entry_fill_at) continue;       /* decided, not yet filled */
            if (!open_live(st)) ++unresolved_entries;  /* this row's print is not ours */
            entry_fill_at = -1;
            continue;
        }

        if (has_live) {
            double funding = st.rate * live.qty * st.perp;
            /* Held until the fill: a payment is owed, a receipt not assumed. */
            if (exit_fill_at == i) funding = std::min(funding, 0.0);
            live.funding += funding;
            live.borrow += per_print_borrow * live.qty * st.spot;
            live.max_adverse_pct = std::max(
                live.max_adverse_pct, 100.0 * (st.perp_high / live.entry_perp - 1.0));
            if (exit_fill_at == i) {
                close_live(st);
                continue;
            }
            if (exit_signalled) continue;
            streak = (r_bps < 0.0) ? streak + 1 : 0;
            if (streak >= p.negative_exit_prints) {
                if (latency == 0) {
                    close_live(st);
                } else if (static_cast<int64_t>(i) + latency <= n - 1) {
                    exit_fill_at = static_cast<int64_t>(i) + latency;
                    exit_signalled = true;
                } else {
                    ++unresolved_exits;            /* stays open; marked at the end */
                    exit_signalled = true;
                }
            }
            continue;
        }

        bool may_open;
        if (p.gated) {
            const int reason =
                gate_reason(history, history_len, st.perp, st.spot, p);
            may_open = (reason == CARRY_GATE_ALLOWED);
            if (!may_open) {
                ++refusals;
                ++by_reason[reason];
            }
        } else {
            may_open = r_bps >= p.entry_bps;
        }
        if (!may_open) continue;
        /* A fill on the last row or past it has no settlement left to hold
           through: abstained and counted, never filled at the last price. */
        const int64_t fill_row = static_cast<int64_t>(i) + latency;
        if (fill_row >= n - 1) {
            ++unresolved_entries;
        } else if (latency == 0) {
            if (!open_live(st)) ++unresolved_entries;
        } else {
            entry_fill_at = fill_row;
        }
    }

    if (has_live) {
        const CarrySettlement &last = rows[n - 1];
        live.closed_ms = last.ms;
        live.exit_spot = last.spot;
        live.exit_perp = last.perp;
        charge(live, last.spot, last.perp);
        live.open_at_end = true;
        trades.push_back(live);
    }

    CarryResult r{};
    for (const Trade &t : trades) {
        r.funding += t.funding;
        r.basis += t.basis();
        r.fees += t.fees;
        r.borrow += t.borrow;
        r.impact += t.impact;
        r.net += t.net();
        r.days_in_market +=
            static_cast<double>(t.closed_ms - t.opened_ms) / 86400000.0;
        r.max_adverse_short_pct =
            std::max(r.max_adverse_short_pct, t.max_adverse_pct);
        if (t.net() < 0.0) ++r.losing_trades;
        if (!t.open_at_end) ++r.closed_trades;
    }
    r.trades = static_cast<int32_t>(trades.size());
    r.refusals = refusals;
    for (int k = 0; k < CARRY_GATE_REASONS; ++k) r.refusals_by_reason[k] = by_reason[k];
    r.unresolved_entries = unresolved_entries;
    r.unresolved_exits = unresolved_exits;
    *out = r;
    return 0;
}

extern "C" int carry_entry_gate(const double *prints_bps, int32_t n,
                                double perp, double spot,
                                const CarryParams *params, int32_t *reason) {
    if (!params || !reason || (n > 0 && !prints_bps)) return 0;
    /* Exactly the prints given, like the Python function: the simulators and
       the engine pass the newest `funding_history` themselves. */
    const int code = gate_reason(prints_bps, static_cast<int>(std::max<int32_t>(n, 0)),
                                 perp, spot, *params);
    *reason = code;
    return code == CARRY_GATE_ALLOWED ? 1 : 0;
}

extern "C" int32_t carry_sweep(const CarrySettlement *rows, int32_t n,
                               const CarryParams *base,
                               const double *entry_grid, int32_t n_entry,
                               const double *hold_grid, int32_t n_hold,
                               const int32_t *exit_grid, int32_t n_exit,
                               CarryResult *out, int32_t threads) {
    if (!rows || !base || !out || n <= 0) return 0;
    if (n_entry <= 0 || n_hold <= 0 || n_exit <= 0) return 0;
    /* The product is taken in 64 bits: in int32 a grid of 2**32 wraps to 0 (a
       division by zero below) and one of 2**31 wraps negative. A grid the
       return type cannot count is refused, not wrapped. */
    const int64_t total64 = static_cast<int64_t>(n_entry) * n_hold * n_exit;
    if (total64 > INT32_MAX) return 0;
    const int32_t total = static_cast<int32_t>(total64);

    int32_t workers = threads;
    if (workers <= 0) {
        workers = static_cast<int32_t>(std::thread::hardware_concurrency());
        if (workers <= 0) workers = 1;
    }
    /* More threads than this buys nothing on any machine, and asking the OS
       for 100,000 of them ends in std::system_error, which nothing catches
       across extern "C": the process aborts and every result is lost. */
    constexpr int32_t kMaxWorkers = 256;
    workers = std::min({workers, total, kMaxWorkers});

    auto run_range = [&](int32_t from, int32_t to) {
        for (int32_t index = from; index < to; ++index) {
            const int32_t e = index / (n_hold * n_exit);
            const int32_t rest = index % (n_hold * n_exit);
            const int32_t h = rest / n_exit;
            const int32_t x = rest % n_exit;
            CarryParams p = *base;
            p.entry_bps = entry_grid[e];
            p.hold_days = hold_grid[h];
            p.negative_exit_prints = exit_grid[x];
            carry_simulate(rows, n, &p, &out[index]);
        }
    };

    if (workers == 1) {
        run_range(0, total);
        return total;
    }
    std::vector<std::thread> pool;
    const int32_t chunk = (total + workers - 1) / workers;
    for (int32_t w = 0; w < workers; ++w) {
        const int32_t from = w * chunk;
        const int32_t to = std::min(total, from + chunk);
        if (from >= to) break;
        try {
            pool.emplace_back(run_range, from, to);
        } catch (const std::system_error &) {
            run_range(from, to);        /* no thread available: do it here */
        }
    }
    for (std::thread &t : pool) t.join();
    return total;
}

extern "C" const char *carrycore_version(void) { return "carrycore/4 (latency, cost-input gate, hold gate)"; }
