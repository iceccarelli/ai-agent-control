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
    if (count < min_prints) return false;
    double smoothed = prints[0];
    for (int i = 1; i < count; ++i) {
        smoothed = alpha * prints[i] + (1.0 - alpha) * smoothed;
    }
    *out = smoothed;
    return true;
}

/* carry_costs.evaluate_entry, gate for gate, in order. */
bool may_open_gated(const double *history, int count, double perp, double spot,
                    const CarryParams &p) {
    double smoothed = 0.0;
    if (!ewma(history, count, p.ewma_alpha, p.ewma_min_prints, &smoothed)) {
        return false;                       /* INSUFFICIENT_FUNDING_HISTORY */
    }
    if (smoothed <= 0.0) return false;      /* FUNDING_NOT_POSITIVE          */

    const double carry_per_day = smoothed * kFundingPeriodsPerDay;
    const double borrow_per_day = (p.borrow_apr / 365.0) * 1e4;
    const double round_trip_per_day =
        p.round_trip_bps / std::max(p.hold_days, 1e-9);
    const double net_edge = carry_per_day - borrow_per_day - round_trip_per_day;

    if (!(std::isfinite(perp) && std::isfinite(spot)) || spot <= 0.0) {
        return false;                       /* BASIS_UNREADABLE              */
    }
    const double entry_basis = (perp / spot - 1.0) * 1e4;

    if (net_edge <= 0.0) return false;      /* CARRY_BELOW_COST_OF_CAPITAL   */
    if (entry_basis > net_edge * p.hold_days) return false; /* BASIS_BUDGET  */
    return true;
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

    for (int32_t i = 0; i < n; ++i) {
        const CarrySettlement &st = rows[i];
        const double r_bps = st.rate * 1e4;

        if (history_len == window) {
            for (int k = 1; k < window; ++k) history[k - 1] = history[k];
            history[window - 1] = r_bps;
        } else {
            history[history_len++] = r_bps;
        }

        if (has_live) {
            live.funding += st.rate * live.qty * st.perp;
            live.borrow += per_print_borrow * live.qty * st.spot;
            live.max_adverse_pct = std::max(
                live.max_adverse_pct, 100.0 * (st.perp_high / live.entry_perp - 1.0));
            streak = (r_bps < 0.0) ? streak + 1 : 0;
            if (streak >= p.negative_exit_prints) {
                live.closed_ms = st.ms;
                live.exit_spot = st.spot;
                live.exit_perp = st.perp;
                charge(live, st.spot, st.perp);
                trades.push_back(live);
                has_live = false;
                streak = 0;
            }
            continue;
        }

        bool may_open;
        if (p.gated) {
            may_open = may_open_gated(history, history_len, st.perp, st.spot, p);
            if (!may_open) ++refusals;
        } else {
            may_open = r_bps >= p.entry_bps;
        }
        if (may_open) {
            live = Trade();
            live.qty = p.notional / st.perp;
            live.entry_spot = st.spot;
            live.entry_perp = st.perp;
            live.opened_ms = st.ms;
            charge(live, st.spot, st.perp);
            has_live = true;
            streak = 0;
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
    *out = r;
    return 0;
}

extern "C" int32_t carry_sweep(const CarrySettlement *rows, int32_t n,
                               const CarryParams *base,
                               const double *entry_grid, int32_t n_entry,
                               const double *hold_grid, int32_t n_hold,
                               const int32_t *exit_grid, int32_t n_exit,
                               CarryResult *out, int32_t threads) {
    if (!rows || !base || !out || n <= 0) return 0;
    if (n_entry <= 0 || n_hold <= 0 || n_exit <= 0) return 0;
    const int32_t total = n_entry * n_hold * n_exit;

    int32_t workers = threads;
    if (workers <= 0) {
        workers = static_cast<int32_t>(std::thread::hardware_concurrency());
        if (workers <= 0) workers = 1;
    }
    workers = std::min(workers, total);

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
        pool.emplace_back(run_range, from, to);
    }
    for (std::thread &t : pool) t.join();
    return total;
}

extern "C" const char *carrycore_version(void) { return "carrycore/1 (0039)"; }
