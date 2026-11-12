"""proxy_carry_study: the pre-registered Binance-archive proxy study of spot-hedged funding carry (RESEARCH ONLY). Offline: a synthetic archive store is written into a tmp directory.

The centre of this file is an INDEPENDENT ORACLE: a plain-loop re-implementation, written from the protocol and not from the production code, of the point-in-time universe, the formation score, the top-k pick, the
funding of the hold, the two bounds' cost / basis / capital / cash arithmetic and the aligned weekly rows. The production series must equal the oracle's for every variant and both bounds, on a market that contains
the traps the reviews named: a delisted contract whose marks stay frozen, a hole in the funding prints, an illiquid symbol, a spot pair that lists late, one base under two symbols, a stablecoin base and no-spot symbols.
Then the individual rules (snapping, holes, segments, the cut, bounded cash, the decision) and the structure (no environment, no order path, no real registry) are pinned on their own.
"""
from __future__ import annotations

import ast
import copy
import datetime as dt
import io
import json
import math
import os
import random
import statistics
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import binance_archive as BA  # noqa: E402
import cash_basis as CB  # noqa: E402
import proxy_carry_study as P  # noqa: E402

TOOLS = os.path.join(os.path.dirname(__file__), "..", "tools")
DAY = 86_400_000
H = 3_600_000
START = P.day_ms("2019-12-01")                 # the first day of generated data
WINDOW_START = "2020-01-01"
CUT = "2021-06-01"
LAST_MONTH = "2021-05"
N_DAYS = (P.day_ms(CUT) - START) // DAY       # days of data before the cut


# ------------------------------------------------------------------------------------------------ a synthetic Binance archive
def ymd(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m")


def make_zip(member, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, text)
    return buf.getvalue()


def put(root, dataset, symbol, interval, month, csv_text):
    path = os.path.join(root, *BA.data_path(dataset, symbol, interval, month).split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = make_zip(os.path.basename(path)[:-4] + ".csv", csv_text)
    import hashlib
    BA._write_atomic(path, body)
    BA._write_atomic(path + ".CHECKSUM", f"{hashlib.sha256(body).hexdigest()}  {os.path.basename(path)}\n".encode())


def index_px(d, phase):
    return 100.0 * math.exp(0.002 * d + 0.05 * math.sin(d / 7.0 + phase))


def mark_px(d, phase):
    return index_px(d, phase) * (1.0 + 0.001 * math.sin(d / 3.0 + phase * 2.0))


class Sym:
    def __init__(self, perp, spot, base_rate, vol, perp_start, spot_start, end_day=None, hole=None, frozen_marks_days=0, seed=1, jitter=False, last_day=N_DAYS, mark_gap=None):
        self.perp, self.spot, self.vol, self.perp_start, self.spot_start, self.end_day, self.hole = perp, spot, vol, perp_start, spot_start, end_day, hole
        self.phase = seed * 0.7
        rng = random.Random(seed)
        jrng = random.Random(seed + 1000)                              # the jitter has its own generator: it must not change a single rate
        stop = last_day if end_day is None else end_day
        self.funding = []                                              # (ts, interval_h, rate as a fraction)
        k = 0
        t = START + perp_start * DAY
        while t < START + stop * DAY or (end_day is not None and t == START + end_day * DAY):
            skip = hole is not None and START + hole[0] * DAY <= t < START + hole[1] * DAY
            if not skip:
                rate = base_rate * (1.0 + 0.6 * math.sin(k / 25.0 + self.phase)) + (rng.random() - 0.5) * 1e-4
                self.funding.append((t + (jrng.randrange(0, 31) if jitter else 0), 8, rate))
            t += 8 * H
            k += 1
        stop_days = stop if end_day is None else end_day
        self.kl = [(START + d * DAY, d) for d in range(perp_start, stop_days)]                      # trade klines stop at delivery
        self.mk = [(START + d * DAY, d) for d in range(perp_start, stop_days + frozen_marks_days) if not (mark_gap and mark_gap[0] <= d < mark_gap[1])]      # a gap in the mark and index files
        self.frozen_from = stop_days if frozen_marks_days else None
        self.spot_days = list(range(spot_start, last_day))
        self.last_day = last_day

    def mark_close(self, d):
        return mark_px(d, self.phase) if self.frozen_from is None or d < self.frozen_from else 1e-4        # a halted contract's mark stays frozen at a tiny price


def kline_rows(sym, days, price, qv):
    out = []
    for d in days:
        c = price(d)
        out.append((START + d * DAY, c * 0.999, c * 1.01, c * 0.99, c, qv(d)))
    return out


def kline_csv(rows):
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"]
    for t, o, h, l, c, qv in rows:
        lines.append(f"{t},{o!r},{h!r},{l!r},{c!r},{qv / 100.0!r},{t + DAY - 1},{qv!r},10,0,0,0")
    return "\n".join(lines) + "\n"


def funding_csv(rows):
    return "\n".join(["calc_time,funding_interval_hours,last_funding_rate"] + [f"{t},{iv},{r!r}" for t, iv, r in rows]) + "\n"


def write_store(root, syms, extra_future=False):
    by_month = lambda ms: ymd(ms)
    for s in syms:
        groups = {}
        for t, iv, r in s.funding:
            groups.setdefault(by_month(t), []).append((t, iv, r))
        for m, rows in groups.items():
            if m <= LAST_MONTH or extra_future:
                put(root, "fundingRate", s.perp, None, m, funding_csv(rows))
        qv = lambda d: s.vol * (1.0 + 0.05 * math.sin(d))
        for dataset, rows in (("klines", kline_rows(s, [d for _, d in s.kl], lambda d: index_px(d, s.phase), qv)),
                              ("markPriceKlines", kline_rows(s, [d for _, d in s.mk], s.mark_close, lambda d: 0.0)),
                              ("indexPriceKlines", kline_rows(s, [d for _, d in s.mk], lambda d: index_px(d, s.phase), lambda d: 0.0))):
            g = {}
            for r in rows:
                g.setdefault(by_month(r[0]), []).append(r)
            for m, rr in g.items():
                if m <= LAST_MONTH or extra_future:
                    put(root, dataset, s.perp, "1d", m, kline_csv(rr))
        g = {}
        for r in kline_rows(s, s.spot_days, lambda d: index_px(d, s.phase) * 1.0001, lambda d: 1e6):
            g.setdefault(by_month(r[0]), []).append(r)
        for m, rr in g.items():
            if m <= LAST_MONTH or extra_future:
                put(root, "spot_klines", s.spot, "1d", m, kline_csv(rr))


def market(jitter=False):
    return [
        Sym("AAAUSDT", "AAAUSDT", 0.0007, 3.0e8, 0, 0, seed=1, jitter=jitter, mark_gap=(330, 336)),                 # six days without mark or index files
        Sym("BBBUSDT", "BBBUSDT", 0.0004, 8.0e7, 0, 0, seed=2, jitter=jitter),
        Sym("CCCUSDT", "CCCUSDT", 0.0001, 4.0e7, 0, 0, seed=3, jitter=jitter),
        Sym("DDDUSDT", "DDDUSDT", 0.0010, 9.0e7, 0, 0, end_day=300, frozen_marks_days=60, seed=4, jitter=jitter),
        Sym("EEEUSDT", "EEEUSDT", 0.0012, 1.0e6, 0, 0, seed=5, jitter=jitter),                          # illiquid: below the floor
        Sym("FFFUSDT", "FFFUSDT", 0.0009, 6.0e7, 0, 200, seed=6, jitter=jitter),                         # the spot pair lists late
        Sym("1000PEPEUSDT", "PEPEUSDT", 0.0006, 7.0e7, 0, 0, seed=7, jitter=jitter),                    # one base, two symbols
        Sym("PEPEUSDT", "PEPEUSDT", 0.0005, 5.0e7, 0, 0, seed=8, jitter=jitter),
        Sym("GGGUSDT", "GGGUSDT", 0.0008, 2.5e8, 0, 0, hole=(150, 153), seed=9, jitter=jitter),          # a hole in the funding prints
        Sym("USDCUSDT", "USDCUSDT", 0.0001, 9.0e8, 0, 0, seed=10, jitter=jitter),                       # a stablecoin base
        Sym("NOSPOTUSDT", "NOSPOTUSDT", 0.0020, 9.0e8, 0, 0, seed=11, jitter=jitter),                   # no spot pair at all (not written below)
    ]


def protocol():
    proto, digest = P.load_protocol()
    proto = copy.deepcopy(proto)
    proto["parameters"].update({"window_start_utc": WINDOW_START, "cut_utc": CUT, "last_explore_month": LAST_MONTH, "top_n_by_median_volume": 5})
    proto["parameters"]["walk_forward"] = {**proto["parameters"]["walk_forward"], "oos_start_utc": "2020-07-01"}
    proto["parameters"]["decision"] = {**proto["parameters"]["decision"], "survivor_years": [2020, 2021], "survivor_min_positive_years": 1}
    return proto, P.protocol_digest(proto)                                       # the edited protocol is a different file: its digest is not the pinned one


class FakeCurve:
    """A piecewise-constant annual rate; None before ``first_ms`` (like the real series)."""

    def __init__(self, steps):
        self.steps = sorted(steps)

    def first_ms(self):
        return self.steps[0][0]

    def mean_apr(self, a, b):
        if a < self.steps[0][0]:
            return None
        total, cur = 0.0, a
        for i, (s0, apr) in enumerate(self.steps):
            s1 = self.steps[i + 1][0] if i + 1 < len(self.steps) else 10 ** 18
            lo, hi = max(cur, s0), min(b, s1)
            if hi > lo:
                total += apr * (hi - lo)
        return total / (b - a)


CASH_FIRST = START + 380 * DAY
CASH_STEPS = [(CASH_FIRST, 0.05), (CASH_FIRST + 90 * DAY, 0.10)]


def cash_basis():
    return CB.CashBasis(CB.HISTORICAL, "test cash", curve=FakeCurve(CASH_STEPS), meta={})


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("arch"))
    mk = [s for s in market() if s.perp != "NOSPOTUSDT"] + [market()[-1]]
    write_store(root, [s for s in mk if s.perp != "NOSPOTUSDT"])
    s = market()[-1]                                                                       # NOSPOT: perp data but no spot_klines directory
    for dataset in ("fundingRate",):
        g = {}
        for t, iv, r in s.funding:
            g.setdefault(ymd(t), []).append((t, iv, r))
        for m, rows in g.items():
            if m <= LAST_MONTH:
                put(root, dataset, s.perp, None, m, funding_csv(rows))
    rows = kline_rows(s, [d for _, d in s.kl], lambda d: index_px(d, s.phase), lambda d: s.vol)
    g = {}
    for r in rows:
        g.setdefault(ymd(r[0]), []).append(r)
    for m, rr in g.items():
        if m <= LAST_MONTH:
            put(root, "klines", s.perp, "1d", m, kline_csv(rr))
    return root


def instruments(store_root, proto=None):
    proto = proto or protocol()[0]
    params = proto["parameters"]
    st = P.Store(store_root, params["last_explore_month"])
    pairs = P.plan_pairs(st.symbols("klines", "1d"), st.symbols("spot_klines", "1d"), params)
    cut = P.day_ms(params["cut_utc"])
    insts = [P.build_instrument(st, a, b, params, cut) for a, b in pairs]
    return st, pairs, [i for i in insts if i is not None]


# ------------------------------------------------------------------------------------------------ the independent oracle (plain loops, written from the protocol)
def ceil_day(ms):
    return -(-ms // DAY) * DAY


def oracle_rows(mkt, params, F, K):
    prefixes = params["multiplier_prefixes"]
    hold, cut = 7 * DAY, P.day_ms(params["cut_utc"])
    pairs = {}
    for s in mkt:
        base = s.perp[:-4]
        if base in params["stable_bases"]:
            continue
        if s.perp == "NOSPOTUSDT":
            continue
        pairs[s.perp] = s
    first_fund = min(s.funding[0][0] for s in pairs.values())
    first = max(P.day_ms(params["window_start_utc"]), first_fund)
    slots, t = [], ceil_day(first + 30 * DAY)
    while t + hold <= cut - DAY:
        slots.append(t)
        t += hold
    out = {}
    medv = {}
    for t in slots:
        cand = []
        for s in pairs.values():
            launch = max(s.kl[0][0], START + s.spot_start * DAY)
            if t - launch < 30 * DAY:
                continue
            spot_month = ymd(START + s.spot_start * DAY)
            if spot_month >= ymd(t):
                continue
            last_print = s.funding[-1][0]
            delivered = last_print < cut - 3 * DAY
            delivery = last_print if delivered else None
            if delivery is not None and t >= delivery:
                continue
            end = t + hold if delivery is None else min(t + hold, delivery)
            prints = [p[0] for p in s.funding]
            holed = any(b - a > 1.5 * 8 * H and a < t and b > t - 30 * DAY for a, b in zip(prints, prints[1:]))               # the trailing window only
            if holed:
                continue
            days = {o: d for o, d in s.kl}
            if any((t - n * DAY) not in days or s.vol * (1.0 + 0.05 * math.sin(days[t - n * DAY])) <= 0 for n in (1, 2, 3)):
                continue
            comp = [o for o in days if o + DAY <= t]
            vols = [s.vol * (1.0 + 0.05 * math.sin(days[o])) for o in comp if t - 30 * DAY <= o]
            if len(vols) < 28:
                continue
            mv = statistics.median(vols)
            if mv < params["min_median_quote_volume_usdt"]:
                continue
            key = s.perp[:-4]
            for p in sorted(prefixes, key=len, reverse=True):
                if key.startswith(p) and len(key) - len(p) >= 2 and key[len(p)].isalpha():
                    key = key[len(p):]
                    break
            cand.append((mv, s, key, delivery))
        best = {}
        for mv, s, key, delivery in cand:
            if key not in best or (mv, s.perp) > (best[key][0], best[key][1].perp):
                best[key] = (mv, s, delivery)
        ranked = sorted(best.values(), key=lambda x: (-x[0], x[1].perp))[:params["top_n_by_median_volume"]]
        uni = [(s, delivery, mv) for mv, s, delivery in ranked]
        medv[t] = {s.perp: mv for s, _, mv in uni}
        for (f, k) in ((F, K),):
            scored = []
            for s, delivery, mv in uni:
                ps = [(ts, r * 1e4) for ts, iv, r in s.funding if t - f * DAY < ts <= t]
                if not ps:
                    continue
                score = statistics.fmean(r * 1440.0 / 480.0 for _, r in ps)
                m0 = last_obs(s, t, 2 * DAY)
                if m0 is None:
                    continue
                scored.append((score, s, delivery))
            if len(scored) < k:
                continue
            scored.sort(key=lambda x: (-x[0], x[1].perp))
            legs = []
            for score, s, delivery in scored[:k]:
                end = t + hold if delivery is None else min(t + hold, delivery)
                terminal = delivery is not None and delivery <= t + hold
                if terminal:
                    m1 = last_obs(s, delivery, DAY)
                else:
                    m1 = last_obs(s, end, 2 * DAY)
                if m1 is None:
                    continue
                fund = sum(r * 1e4 for ts, iv, r in s.funding if t < ts <= end)
                legs.append({"symbol": s.perp, "funding": fund, "terminal": terminal, "delivery": delivery})
            if legs:
                out.setdefault((F, K), {})[t] = {"legs": legs, "funding": statistics.fmean(l["funding"] for l in legs)}
    return slots, out.get((F, K), {}), medv


def last_obs(s, t, stale):
    best = None
    for o, d in s.mk:
        if o + DAY <= t and (best is None or o + DAY > best[0]):
            best = (o + DAY, s.mark_close(d))
    if best is None or t - best[0] > stale:
        return None
    return best[1]


def basis_at(s, t, stale):
    mk = idx = None
    for o, d in s.mk:
        if o + DAY <= t:
            mk, idx = ((o + DAY, s.mark_close(d)), (o + DAY, index_px(d, s.phase)))
    if mk is None or t - mk[0] > stale:
        return None
    return (mk[1] - idx[1]) / idx[1]


def cash_apr(a, b, before):
    """The annual rate over [a, b]: the series where it exists, ``before`` ahead of it (a window across the start is weighted by time)."""
    if a >= CASH_FIRST:
        steps = CASH_STEPS
    else:
        steps = [(a, before)] + [s for s in CASH_STEPS]
    total = 0.0
    for i, (s0, apr) in enumerate(steps):
        s1 = steps[i + 1][0] if i + 1 < len(steps) else 10 ** 18
        lo, hi = max(a, s0), min(b, s1)
        if hi > lo:
            total += apr * (hi - lo)
    return total / (b - a)


def oracle_basis_floor(mkt, all_rows, ts):
    """The worst measured weekly basis P&L of any selected leg of any variant over the aligned weeks (plain loops)."""
    by = {s.perp: s for s in mkt}
    worst = None
    for rr in all_rows.values():
        for t in ts:
            for l in rr[t]["legs"]:
                if l["delivery"] is not None and l["delivery"] <= t + 7 * DAY:
                    continue
                s = by[l["symbol"]]
                b0, b1 = basis_at(s, t, 2 * DAY), basis_at(s, t + 7 * DAY, 2 * DAY)
                if b0 is not None and b1 is not None:
                    v = -(b1 - b0) * 1e4
                    worst = v if worst is None or v < worst else worst
    return worst


def oracle_bound(mkt, rows, medv, bound, params, stress=0.0):
    by = {s.perp: s for s in mkt}
    ts = sorted(rows)
    tset = set(ts)
    hold = 7 * DAY
    capital = 2.04 if bound == "ADVERSE" else 1.224
    before = 0.05 if bound == "ADVERSE" else 0.0                      # the mean of the series' first 90 days is 0.05
    out = {}
    for t in ts:
        legs = rows[t]["legs"]
        prev = {l["symbol"] for l in rows[t - hold]["legs"]} if (t - hold) in tset else set()
        nxt = {l["symbol"] for l in rows[t + hold]["legs"]} if (t + hold) in tset else set()
        nets = []
        for l in legs:
            s = by[l["symbol"]]
            b = stress                                                  # an unmeasured basis (stale marks, or delivered inside the hold) is charged the stress
            if not (l["delivery"] is not None and l["delivery"] <= t + hold):
                b0, b1 = basis_at(s, t, 2 * DAY), basis_at(s, t + hold, 2 * DAY)
                if b0 is not None and b1 is not None:
                    b = -(b1 - b0) * 1e4
            if bound == "ADVERSE":
                mv = medv[t][l["symbol"]]
                slip = 2.0 if mv >= 200e6 else (6.0 if mv >= 50e6 else 15.0)
                cost = 31.4 + slip
            else:
                cost = 0.5 * 31.0 * ((0 if l["symbol"] in prev else 1) + (0 if l["symbol"] in nxt else 1))
            nets.append(l["funding"] - cost + b)
        net = statistics.fmean(nets)
        cash_bps = cash_apr(t, t + hold, before) * 7.0 / 365.0 * 1e4
        out[t] = (net, net / capital - cash_bps)
    return out


def production_series(store_root, proto):
    st, pairs, insts = instruments(store_root, proto)
    params = proto["parameters"]
    cut = P.day_ms(params["cut_utc"])
    first = max(P.day_ms(params["window_start_utc"]), min(i.series.funding[0][0] for i in insts))
    slots = P.slot_grid(first, cut, params)
    rows, medvol, _why, stats = P.build_variant_rows(insts, slots, params)
    common = set(slots)
    for v in rows.values():
        common &= set(v)
    ts = sorted(common)
    by = {i.symbol: i for i in insts}
    costs = P.CR.family_costs(P.FAMILY)["taker"]
    floor = P.measured_basis_floor(rows, ts, by, params)
    stress = {"ADVERSE": min(0.0, floor) if floor is not None else 0.0, "FAVOURABLE": 0.0}
    out = {}
    for bound in ("ADVERSE", "FAVOURABLE"):
        cash = P.BoundedCash(cash_basis(), params[bound.lower()]["cash_before_series"])
        for (f, k) in rows:
            out[(bound, f, k)] = P.bound_series(rows[(f, k)], ts, bound, by, medvol, params, cash, costs, stress[bound])
    production_series.last_stats = stats
    production_series.last_floor = floor
    return ts, rows, medvol, out


# ------------------------------------------------------------------------------------------------ the oracle against the production series
def test_the_production_series_equal_an_independent_oracle_for_every_variant_and_both_bounds(store):
    proto, _ = protocol()
    params = proto["parameters"]
    mkt = market()
    ts, rows, medvol, series = production_series(store, proto)
    assert len(ts) >= 60
    o_rows = {}
    o_medv = {}
    slots = None
    for f in params["formation_days"]:
        for k in params["top_k"]:
            slots, r, mv = oracle_rows(mkt, params, f, k)
            o_rows[(f, k)] = r
            o_medv = mv
    o_common = sorted(set(slots).intersection(*[set(v) for v in o_rows.values()]))
    assert ts == o_common, "the aligned decision times differ"
    for (f, k), rr in o_rows.items():
        for t in ts:
            prod_legs = [l["symbol"] for l in rows[(f, k)][t]["legs"]]
            assert prod_legs == [l["symbol"] for l in rr[t]["legs"]], (f, k, t)
            assert rows[(f, k)][t]["funding_bps"] == pytest.approx(rr[t]["funding"], abs=1e-9)
    o_floor = oracle_basis_floor(mkt, o_rows, ts)
    assert o_floor == pytest.approx(production_series.last_floor, abs=1e-9)
    for bound in ("ADVERSE", "FAVOURABLE"):
        stress = min(0.0, o_floor) if bound == "ADVERSE" and o_floor is not None else 0.0
        for (f, k), rr in o_rows.items():
            ob = oracle_bound(mkt, {t: rr[t] for t in ts}, o_medv, bound, params, stress)
            got = series[(bound, f, k)]
            assert [g["t"] for g in got] == ts
            for g in got:
                net, exc = ob[g["t"]]
                assert g["net_bps_of_gross"] == pytest.approx(net, abs=1e-8), (bound, f, k, g["t"])
                assert g["excess_bps_of_capital"] == pytest.approx(exc, abs=1e-8), (bound, f, k, g["t"])


def test_the_market_really_contains_the_traps_the_oracle_is_meant_to_catch(store):
    proto, _ = protocol()
    ts, rows, medvol, series = production_series(store, proto)
    seen = {l["symbol"] for rr in rows.values() for r in rr.values() for l in r["legs"]}
    assert {"AAAUSDT", "GGGUSDT", "DDDUSDT"} <= seen                              # the liquid, high-funding names trade
    assert "EEEUSDT" not in seen and "USDCUSDT" not in seen and "NOSPOTUSDT" not in seen
    assert "PEPEUSDT" not in seen                                                 # the lower-volume duplicate of one base never trades
    terminal = [r for rr in rows.values() for r in rr.values() if any(l.get("closed_during_hold") for l in r["legs"])]
    assert terminal, "no week held the delisted contract through its delivery"
    delivery = START + 300 * DAY
    assert all(t < delivery for rr in rows.values() for t, r in rr.items() if any(l["symbol"] == "DDDUSDT" for l in r["legs"]))       # nothing is held after delivery: a frozen mark creates no ghost leg


# ------------------------------------------------------------------------------------------------ jitter, the cut, holes, intervals
def test_funding_time_jitter_of_up_to_30_ms_changes_nothing(store, tmp_path):
    root = str(tmp_path / "j")
    mk = [s for s in market(jitter=True) if s.perp != "NOSPOTUSDT"]
    write_store(root, mk)
    proto, _ = protocol()
    ts0, rows0, _m0, series0 = production_series(store, proto)
    ts1, rows1, _m1, series1 = production_series(root, proto)
    # the jittered market draws its own random rates only through the same seeds, so the series must be bit-identical after snapping
    assert ts0 == ts1
    for key in series0:
        assert [r["excess_bps_of_capital"] for r in series0[key]] == [r["excess_bps_of_capital"] for r in series1[key]]


def test_a_month_after_the_cut_is_never_read_and_cannot_change_a_result(store, tmp_path):
    root = str(tmp_path / "f")
    mk = [s for s in market() if s.perp != "NOSPOTUSDT"]
    write_store(root, mk)
    extra = Sym("AAAUSDT", "AAAUSDT", 0.0007, 3.0e8, 0, 0, seed=1, last_day=N_DAYS + 31)
    g = {}
    for t, iv, r in extra.funding:
        if ymd(t) == "2021-06":
            g.setdefault("2021-06", []).append((t, iv, r * 1000))                    # an extreme June
    put(root, "fundingRate", "AAAUSDT", None, "2021-06", funding_csv(g["2021-06"]))
    proto, _ = protocol()
    st, _pairs, _insts = instruments(root, proto)                                   # the store a run actually reads
    assert "2021-06" not in st.months("fundingRate", "AAAUSDT", None)
    assert st.read_log and not any("2021-06" in k for k in st.read_log)
    ts0, _r0, _m0, series0 = production_series(store, proto)
    ts1, _r1, _m1, series1 = production_series(root, proto)
    assert ts0 == ts1 and all([r["excess_bps_of_capital"] for r in series0[k]] == [r["excess_bps_of_capital"] for r in series1[k]] for k in series0)


def test_a_store_row_at_or_after_the_cut_inside_an_allowed_month_is_ignored(store):
    proto, _ = protocol()
    st, pairs, insts = instruments(store, proto)
    cut = P.day_ms(CUT)
    assert all(p[0] < cut for i in insts for p in i.series.funding)
    assert all(t <= cut for i in insts for t in i.mark_ts) and all(d + DAY <= cut for i in insts for d in i.day_open)


def test_the_explore_run_refuses_a_store_opened_with_another_ceiling(store):
    proto, digest = protocol()
    st = P.Store(store, "2021-07")
    with pytest.raises(P.StudyError) as e:
        P.run_explore(st, [], proto, digest, os.path.join(store, "x.jsonl"), cash_basis(), {}, allow_unpinned=True)
    assert e.value.code == "STORE_CEILING"


def test_the_store_only_reads_months_it_may_and_reverifies_every_one(store):
    st = P.Store(store, "2020-03")
    assert st.months("fundingRate", "AAAUSDT", None)[-1] == "2020-03"
    rows = st.rows("fundingRate", "AAAUSDT", None)
    assert rows and max(r[0] for r in rows) < P.month_start_ms("2020-04")
    assert len(st.digest()) == 64 and all(not k.endswith("2020-04.zip") for k in st.read_log)
    p = os.path.join(store, *BA.data_path("fundingRate", "AAAUSDT", None, "2020-03").split("/"))
    raw = open(p, "rb").read()
    try:
        with open(p, "r+b") as fh:
            fh.seek(30)
            fh.write(b"\x00")
        with pytest.raises(BA.ArchiveError) as e:
            P.Store(store, "2020-03").rows("fundingRate", "AAAUSDT", None)
        assert e.value.code == "STORE_CHECKSUM_MISMATCH"
    finally:
        open(p, "wb").write(raw)


def test_a_symbol_with_a_hole_is_excluded_for_the_weeks_around_it_and_only_those(store):
    proto, _ = protocol()
    params = proto["parameters"]
    st, pairs, insts = instruments(store, proto)
    g = next(i for i in insts if i.symbol == "GGGUSDT")
    assert len(g.holes) == 1
    # the prints stop at day 149 16:00 and resume at day 153: only the TRAILING window chooses the universe, so a decision t is affected when t > 149.67 d and t - 30 d < 153 d, that is for t in (149.67, 183)
    for d in (150, 160, 182):
        uni, _mv, why = P.universe_at(insts, START + d * DAY, params)
        assert "GGGUSDT" not in [i.symbol for i in uni] and why["funding_hole"] >= 1, d
    for d in (140, 145, 149, 190, 200):                                                  # up to the hole (the hold may contain it: that is not used) and more than 30 days after it
        uni, _mv, why = P.universe_at(insts, START + d * DAY, params)
        assert "GGGUSDT" in [i.symbol for i in uni], d


def test_one_base_under_two_symbols_keeps_the_higher_volume_one_and_a_stablecoin_never_enters(store):
    proto, _ = protocol()
    params = proto["parameters"]
    st, pairs, insts = instruments(store, proto)
    names = {a for a, _ in pairs}
    assert "USDCUSDT" not in names and "NOSPOTUSDT" not in names and {"1000PEPEUSDT", "PEPEUSDT"} <= names
    uni, _mv, why = P.universe_at(insts, ceil_day(START + 250 * DAY), params)
    syms = [i.symbol for i in uni]
    assert ("1000PEPEUSDT" in syms) != ("PEPEUSDT" in syms) and "1000PEPEUSDT" in syms
    assert why["duplicate_base_dropped"] >= 1


def test_a_late_spot_listing_keeps_the_symbol_out_until_it_is_30_days_old_and_the_month_rule_is_its_own(store):
    proto, _ = protocol()
    params = proto["parameters"]
    st, pairs, insts = instruments(store, proto)
    f = next(i for i in insts if i.symbol == "FFFUSDT")
    for d in (205, 220, 229):                                                      # the spot pair listed on day 200: younger than 30 days
        uni, _mv, why = P.universe_at(insts, START + d * DAY, params)
        assert "FFFUSDT" not in [i.symbol for i in uni] and why["listing_age"] >= 1, d
    # the month rule on its own: an instrument 40 days old whose spot pair listed in the month of the decision is not eligible, one whose spot listed the month before is
    import dataclasses
    for spot_first, expect_in in ((P.day_ms("2020-08-01"), False), (P.day_ms("2020-07-31"), True)):
        ser = dataclasses.replace(f.series, launch_ms=spot_first)
        inst = P.Instrument(symbol=f.symbol, spot_symbol=f.spot_symbol, base_key="ZZZ", perp_first_ms=f.perp_first_ms, spot_first_ms=spot_first, series=ser, day_open=f.day_open, day_qv=f.day_qv,
                            holes=[], delivered=False)
        t = P.day_ms("2020-08-31")
        uni, _mv, why = P.universe_at([inst], t, params)
        assert (inst in uni) == expect_in, spot_first


def test_an_illiquid_symbol_is_excluded_and_the_reason_is_counted(store):
    proto, _ = protocol()
    st, pairs, insts = instruments(store, proto)
    uni, _mv, why = P.universe_at(insts, ceil_day(START + 250 * DAY), proto["parameters"])
    assert "EEEUSDT" not in [i.symbol for i in uni] and why["volume_below_floor_or_too_few_days"] >= 1


def test_the_delivered_contract_is_delivered_at_its_last_funding_print_not_at_its_last_mark(store):
    proto, _ = protocol()
    st, pairs, insts = instruments(store, proto)
    d = next(i for i in insts if i.symbol == "DDDUSDT")
    assert d.delivered and d.series.delivery_ms == d.series.funding[-1][0] == START + 300 * DAY
    assert d.mark_ts[-1] > d.series.delivery_ms                                    # the mark file goes on, frozen: it must not extend the contract's life
    assert [p for t, p in zip(d.mark_ts, d.mark_px) if t > d.series.delivery_ms][0] == pytest.approx(1e-4)
    uni, _mv, why = P.universe_at(insts, START + 310 * DAY, proto["parameters"])
    assert "DDDUSDT" not in [i.symbol for i in uni] and why["delivered"] >= 1


# ------------------------------------------------------------------------------------------------ the loader's own cut and delivery rules (a fake store feeds it rows the real one never would)
class FakeStore:
    def __init__(self, funding_last_ms, extra_prints_after_cut=0, days=60):
        cut = P.day_ms(CUT)
        self.cut = cut
        first = cut - 100 * DAY
        prints, t = [], first
        while t <= funding_last_ms + extra_prints_after_cut * 8 * H:
            prints.append((t, 8, 0.0001))
            t += 8 * H
        self.funding = prints
        self.klines = [(first + n * DAY, 10.0, 11.0, 9.0, 10.0, 1.0, first + (n + 1) * DAY - 1, 5e7, 1) for n in range(100)]
        self.px = [(first + n * DAY, 10.0, 11.0, 9.0, 10.0, 0.0, first + (n + 1) * DAY - 1, 0.0, 1) for n in range(100)]

    def rows(self, dataset, symbol, interval):
        return {"fundingRate": self.funding, "klines": self.klines, "spot_klines": self.klines, "markPriceKlines": self.px, "indexPriceKlines": self.px}[dataset]


def test_the_loader_ignores_anything_at_or_after_the_cut_even_inside_an_allowed_month():
    params = protocol()[0]["parameters"]
    cut = P.day_ms(CUT)
    fs = FakeStore(cut - 8 * H, extra_prints_after_cut=6)                      # prints at the cut and after it, as if a month file were mislabelled
    inst = P.build_instrument(fs, "ZZZUSDT", "ZZZUSDT", params, cut)
    assert inst.series.funding[-1][0] == cut - 8 * H and all(t < cut for t, _ in inst.series.funding)
    assert all(t <= cut for t in inst.mark_ts) and all(o + DAY <= cut for o in inst.day_open) and all(t <= cut for t in inst.index_ts)


def test_a_contract_is_delivered_only_if_its_funding_ended_more_than_three_days_before_the_cut():
    params = protocol()[0]["parameters"]
    cut = P.day_ms(CUT)
    alive = P.build_instrument(FakeStore(cut - 2 * DAY), "ZZZUSDT", "ZZZUSDT", params, cut)                 # its last print is two days before the cut: still trading as far as the data goes
    assert alive.delivered is False and alive.series.delivery_ms is None
    gone = P.build_instrument(FakeStore(cut - 5 * DAY), "ZZZUSDT", "ZZZUSDT", params, cut)
    assert gone.delivered is True and gone.series.delivery_ms == gone.series.funding[-1][0] == cut - 5 * DAY
    edge = P.build_instrument(FakeStore(cut - 3 * DAY), "ZZZUSDT", "ZZZUSDT", params, cut)                  # exactly three days: not "more than"
    assert edge.delivered is False


# ------------------------------------------------------------------------------------------------ snapping, holes, segments, names
def test_funding_times_snap_to_their_stated_grid_and_a_print_off_the_grid_is_dropped_and_counted():
    h8 = 8 * H
    rows = [(0 * h8 + 12, 8, 0.0001), (1 * h8 - 20, 8, 0.0002), (2 * h8 + 5_000, 8, 0.0003), (3 * h8, 8, 0.0004), (3 * h8 + 7, 8, 0.0005), (4 * h8 + 4 * H + 9, 4, 0.0006)]
    prints, q = P.snap_funding(rows, 1000)
    assert [p[0] for p in prints] == [0, h8, 3 * h8, 4 * h8 + 4 * H]
    assert q == {"funding_off_grid_dropped": 1, "funding_duplicate_after_snap_dropped": 1}
    assert prints[0][2] == pytest.approx(1.0)                                         # 0.0001 -> 1 bp


def test_holes_are_gaps_longer_than_one_and_a_half_intervals_and_an_interval_change_is_not_one():
    h = H
    prints = [(0, 8, 1.0), (8 * h, 8, 1.0), (16 * h, 8, 1.0), (40 * h, 8, 1.0), (44 * h, 4, 1.0), (48 * h, 4, 1.0), (60 * h, 4, 1.0)]
    assert P.funding_holes(prints, 1.5) == [(16 * h, 40 * h), (48 * h, 60 * h)]
    assert P.interval_segments(prints) == [(0, 40 * h, 480), (44 * h, 60 * h, 240)]
    assert P.funding_holes([(0, 8, 1.0), (8 * h, 4, 1.0)], 1.5) == []


def test_two_character_bases_are_perps_too_and_one_character_is_not():
    assert P.perp_base("OPUSDT") == "OP" and P.perp_base("ARUSDT") == "AR" and P.perp_base("BTCUSDT") == "BTC" and P.perp_base("AUSDT") is None and P.perp_base("USDT") is None
    assert P.perp_base("BTCUSDC") is None and P.perp_base("BTCBUSD") is None
    assert P.plan_pairs(["OPUSDT", "AUSDT"], ["OPUSDT", "AUSDT"], protocol()[0]["parameters"]) == [("OPUSDT", "OPUSDT")]


def test_multipliers_and_pairs_follow_the_protocol_rules():
    pre = ["1000000", "10000", "1000", "1M"]
    assert P.strip_multiplier("1000PEPE", pre) == "PEPE" and P.strip_multiplier("1000000MOG", pre) == "MOG" and P.strip_multiplier("1MBABYDOGE", pre) == "BABYDOGE"
    assert P.strip_multiplier("10000LADYS", pre) == "LADYS" and P.strip_multiplier("100000X", pre) == "100000X"                      # the longest matching prefix wins; nothing is stripped to a stub
    assert P.strip_multiplier("1INCH", pre) == "1INCH" and P.strip_multiplier("1000", pre) == "1000" and P.strip_multiplier("1000A", pre) == "1000A"
    assert P.spot_candidates("1000PEPEUSDT", pre) == ["1000PEPEUSDT", "PEPEUSDT"] and P.spot_candidates("BTCUSDC", pre) == []
    params = protocol()[0]["parameters"]
    pairs = P.plan_pairs(["1000PEPEUSDT", "BTCUSDT", "USDCUSDT", "TUSDUSDT", "NOSPOTUSDT", "1INCHUSDT", "BTCBUSD"], ["PEPEUSDT", "BTCUSDT", "USDCUSDT", "TUSDUSDT", "1INCHUSDT"], params)
    assert pairs == [("1000PEPEUSDT", "PEPEUSDT"), ("1INCHUSDT", "1INCHUSDT"), ("BTCUSDT", "BTCUSDT")]


# ------------------------------------------------------------------------------------------------ the thresholds on their own (the synthetic market never lands exactly on them)
def test_slippage_tiers_switch_exactly_at_the_registered_volumes():
    tiers = protocol()[0]["parameters"]["adverse"]["extra_slippage_bps_by_median_volume"]
    assert [P.slippage_bps(v, tiers) for v in (500e6, 200e6, 199_999_999, 50e6, 49_999_999, 20e6, 19_999_999, 1.0)] == [2.0, 2.0, 6.0, 6.0, 15.0, 15.0, 15.0, 15.0]


def _stub(day_open, day_qv, symbol="ZZZUSDT"):
    import dataclasses
    del dataclasses
    return P.Instrument(symbol=symbol, spot_symbol=symbol, base_key=symbol[:-4], perp_first_ms=day_open[0], spot_first_ms=0, series=None, day_open=list(day_open), day_qv=list(day_qv))


def test_the_median_volume_uses_the_30_complete_days_before_the_decision_and_nothing_from_the_decision_day():
    t = P.day_ms("2021-03-01")
    opens = [t - n * DAY for n in range(40, -1, -1)]                       # t-40 d ... t (the decision day itself is incomplete)
    qv = [float(n) for n in range(41)]                                       # t-40 d -> 0, ..., t -> 40
    inst = _stub(opens, qv)
    # the complete days in [t-30 d, t): opens t-30 d ... t-1 d -> values 10 ... 39 -> median 24.5 ; the decision day's own 40 and the older 0..9 must not move it
    assert P.median_volume(inst, t, 30, 28) == pytest.approx(24.5)
    inst2 = _stub(opens, qv[:-1] + [1e12])                                  # an absurd volume TODAY changes nothing
    assert P.median_volume(inst2, t, 30, 28) == pytest.approx(24.5)
    assert P.median_volume(_stub(opens[-20:], qv[-20:]), t, 30, 28) is None  # too few days present


def test_a_symbol_is_alive_only_if_each_of_the_three_days_right_before_the_decision_exists_and_traded():
    proto, _ = protocol()
    params = proto["parameters"]
    t = P.day_ms("2021-03-01")
    import dataclasses
    from candidate_readiness import SymbolSeries

    def inst(days_back, vols):
        opens = [t - n * DAY for n in days_back]
        ser = SymbolSeries(symbol="ZZZUSDT", interval_min=480, funding=[(t - 40 * DAY, 1.0), (t + 7 * DAY, 1.0)], mark=[], launch_ms=t - 100 * DAY, delivery_ms=None, spot_listed=True,
                           funding_intervals=[(t - 40 * DAY, t + 7 * DAY, 480)], mark_interval_ms=DAY)
        i = _stub(opens, vols)
        i.series, i.spot_first_ms, i.perp_first_ms = ser, t - 100 * DAY, t - 100 * DAY
        return i
    ok = inst(range(60, 0, -1), [5e7] * 60)
    assert ok in P.universe_at([ok], t, params)[0]
    stale = inst(range(60, 2, -1), [5e7] * 58)                              # the file stops two days before the decision: not a live market
    assert stale not in P.universe_at([stale], t, params)[0]
    gap = inst([n for n in range(60, 0, -1) if n != 2], [5e7] * 59)         # yesterday and the day before exist but the day before that does not... here day t-2 is missing
    assert gap not in P.universe_at([gap], t, params)[0]
    quiet = inst(range(60, 0, -1), [5e7] * 59 + [0.0])                      # yesterday did not trade
    assert quiet not in P.universe_at([quiet], t, params)[0]


# ------------------------------------------------------------------------------------------------ one week by hand
def test_one_week_by_hand_adverse_and_favourable():
    class I:                                                                       # a minimal instrument: marks and index only
        pass

    def inst(sym, delivery=None):
        i = I()
        i.symbol = sym
        i.series = type("S", (), {"delivery_ms": delivery})()
        i.holes = []
        i.mark_ts, i.mark_px = [0, 7 * DAY, 14 * DAY], [100.2, 100.1, 100.05]     # the mark at the three times
        i.index_ts, i.index_px = [0, 7 * DAY, 14 * DAY], [100.0, 100.0, 100.0]     # the index is flat
        return i
    a, b = inst("AAAUSDT"), inst("BBBUSDT")
    t0, t1 = 0, 7 * DAY
    rows = {t0: {"legs": [{"symbol": "AAAUSDT", "funding_bps": 40.0}, {"symbol": "BBBUSDT", "funding_bps": 20.0}], "funding_bps": 30.0},
            t1: {"legs": [{"symbol": "AAAUSDT", "funding_bps": 10.0}], "funding_bps": 10.0}}
    params = copy.deepcopy(protocol()[0]["parameters"])
    cash = type("C", (), {"apr_over": lambda self, s, e: 0.073, "label": "X"})()
    costs = {"bps": 31.4, "fee_bps": 31.0}
    medvol = {t0: {"AAAUSDT": 300e6, "BBBUSDT": 30e6}, t1: {"AAAUSDT": 300e6}}
    adv = P.bound_series(rows, [t0, t1], "ADVERSE", {"AAAUSDT": a, "BBBUSDT": b}, medvol, params, cash, costs)
    # basis: short perp, long spot: -(basis_exit - basis_entry) with basis = (mark - index) / index = +0.002 then +0.001 -> +10 bps
    # week 0: legs net = (40 - (31.4 + 2) + 10, 20 - (31.4 + 15) + 10) = (16.6, -16.4) -> mean 0.1 ; capital 2.04 ; cash 0.073 * 7/365 * 1e4 = 14.0
    assert adv[0]["net_bps_of_gross"] == pytest.approx(0.1) and adv[0]["capital"] == 2.04
    assert adv[0]["excess_bps_of_capital"] == pytest.approx(0.1 / 2.04 - 0.073 * 7 / 365 * 1e4)
    fav = P.bound_series(rows, [t0, t1], "FAVOURABLE", {"AAAUSDT": a, "BBBUSDT": b}, medvol, params, cash, costs)
    # week 0: A enters (half the 31.0 fee) and is held on (no exit); B enters and exits (a full 31.0): legs (40 - 15.5 + 10, 20 - 31.0 + 10) = (34.5, -1.0) -> 16.75 ; capital 1.224
    assert fav[0]["net_bps_of_gross"] == pytest.approx(16.75) and fav[0]["capital"] == 1.224
    # week 1: the premium goes from +0.10% to +0.05%: +5 bps; A is already held (no entry) and exits (half the fee): 10 - 15.5 + 5 = -0.5
    assert fav[1]["net_bps_of_gross"] == pytest.approx(-0.5)


def test_a_leg_delivered_inside_the_hold_has_an_unmeasured_basis_that_is_counted():
    class I:
        pass
    i = I()
    i.series = type("S", (), {"delivery_ms": 3 * DAY})()
    i.mark_ts, i.mark_px, i.index_ts, i.index_px = [0], [101.0], [0], [100.0]
    assert P.leg_basis_pnl_bps(i, 0, 7 * DAY, 2 * DAY) is None
    i.series = type("S", (), {"delivery_ms": None})()
    assert P.leg_basis_pnl_bps(i, 0, 7 * DAY, 2 * DAY) is None                     # the exit marks are older than two days: stale
    i.mark_ts, i.mark_px, i.index_ts, i.index_px = [0, 7 * DAY], [101.0, 100.5], [0, 7 * DAY], [100.0, 100.0]
    assert P.leg_basis_pnl_bps(i, 0, 7 * DAY, 2 * DAY) == pytest.approx(50.0)      # the premium fell from 1.0% to 0.5%: +50 bps to a short perp / long spot pair


def test_bounded_cash_extends_the_series_backwards_as_the_bound_says_and_weights_a_straddling_window():
    base = CB.CashBasis(CB.HISTORICAL, "t", curve=FakeCurve([(100 * DAY, 0.05), (190 * DAY, 0.10)]), meta={})
    z, m = P.BoundedCash(base, "ZERO"), P.BoundedCash(base, "MEAN_OF_FIRST_90_DAYS")
    assert z.apr_over(10 * DAY, 17 * DAY) == 0.0 and m.apr_over(10 * DAY, 17 * DAY) == pytest.approx(0.05)
    assert m.apr_over(200 * DAY, 207 * DAY) == pytest.approx(0.10) and z.apr_over(100 * DAY, 107 * DAY) == pytest.approx(0.05)
    assert z.apr_over(96 * DAY, 103 * DAY) == pytest.approx((0.0 * 4 + 0.05 * 3) / 7) and m.apr_over(96 * DAY, 103 * DAY) == pytest.approx(0.05)
    with pytest.raises(P.StudyError):
        P.BoundedCash(base, "SOMETHING")


# ------------------------------------------------------------------------------------------------ the decision
def real_params():
    return P.load_protocol()[0]["parameters"]


def _bound(mean, lo, hi, adj=0.01, years=None, drop=1.0, share=0.2):
    return {"stats": {"mean": mean, "ci": {"status": "OK", "lo": lo, "hi": hi}}, "p_adjusted": adj, "by_year": years or {"2022": 1.0, "2023": 1.0, "2024": -1.0}, "mean_without_best_weeks": drop, "max_symbol_share": share}


def test_the_decision_rules_are_the_registered_ones():
    params = real_params()
    good, bad_fav = _bound(5.0, 1.0, 9.0), _bound(-2.0, -4.0, -0.5)
    assert P.decide(good, bad_fav, params)["verdict"] == "INCONCLUSIVE"                       # contradiction: nothing is claimed
    assert P.decide(good, _bound(8.0, 2.0, 12.0), params)["verdict"] == "SURVIVOR_PENDING_FINAL_READ"
    assert P.decide(_bound(-1.0, -3.0, 1.0), bad_fav, params)["verdict"] == "NO_WINNER"
    assert P.decide(_bound(-1.0, -3.0, 1.0), _bound(3.0, -1.0, 7.0), params)["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("change", [
    {"stats": {"mean": 5.0, "ci": {"status": "OK", "lo": -0.1, "hi": 9.0}}},                  # the interval's lower end is not above zero
    {"p_adjusted": 0.2},                                                                       # not significant after Holm
    {"by_year": {"2022": 1.0, "2023": -1.0, "2024": -1.0}},                                    # positive in fewer than two of the three years
    {"mean_without_best_weeks": -0.1},                                                         # the best weeks carry the result
    {"max_symbol_share": 0.26},                                                                # one symbol above 25% of the P&L
    {"max_symbol_share": None},
    {"stats": {"mean": None, "ci": None}},
])
def test_each_survivor_criterion_blocks_a_survivor_on_its_own(change):
    params = real_params()
    adv = {**_bound(5.0, 1.0, 9.0), **change}
    assert P.decide(adv, _bound(8.0, 2.0, 12.0), params)["verdict"] == "INCONCLUSIVE"


def test_the_favourable_bound_must_have_a_usable_interval_to_close_the_direction():
    params = real_params()
    weak = {"stats": {"mean": -5.0, "ci": None}, "p_adjusted": 1.0, "by_year": {}, "mean_without_best_weeks": None, "max_symbol_share": None}
    assert P.decide(_bound(-1.0, -3.0, 1.0), weak, params)["verdict"] == "INCONCLUSIVE"
    assert P.decide(_bound(-1.0, -3.0, 1.0), _bound(-1.0, -3.0, 0.0), params)["verdict"] == "NO_WINNER"       # at zero counts: not above it


# ------------------------------------------------------------------------------------------------ fetching in two stages, against a local mirror of the synthetic archive
class Mirror:
    """A fetch that serves the synthetic store as if it were the public archive: S3 listings from its directories, files and checksums from its bytes."""

    def __init__(self, root):
        self.root, self.calls = root, []

    def __call__(self, url):
        import urllib.parse
        self.calls.append(url)
        u = urllib.parse.urlsplit(url)
        if u.hostname == BA.HOST_LISTING:
            q = urllib.parse.parse_qs(u.query)
            prefix, delim = q["prefix"][0], "delimiter" in q
            base = os.path.join(self.root, *prefix.rstrip("/").split("/"))
            names = sorted(os.listdir(base)) if os.path.isdir(base) else []
            if delim:
                body = "".join(f"<CommonPrefixes><Prefix>{prefix}{n}/</Prefix></CommonPrefixes>" for n in names if os.path.isdir(os.path.join(base, n)))
            else:
                body = "".join(f"<Contents><Key>{prefix}{n}</Key><Size>1</Size></Contents>" for n in names if os.path.isfile(os.path.join(base, n)))
            return 200, ('<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>' + body + "</ListBucketResult>").encode()
        path = os.path.join(self.root, *u.path.lstrip("/").split("/"))
        return (200, open(path, "rb").read()) if os.path.isfile(path) else (404, b"")


def mirror_archive(store_root, dest):
    mirror = Mirror(store_root)
    return mirror, BA.Archive(str(dest), fetch=mirror, rps=1000.0, sleep=lambda s: None, free_bytes_fn=lambda p: 10 ** 12)


def test_the_candidate_stage_fetches_volume_klines_for_hedgeable_perps_inside_the_explore_months_only(store, tmp_path):
    proto, _ = protocol()
    params = proto["parameters"]
    mirror, arc = mirror_archive(store, tmp_path / "dest")
    res = P.fetch_candidates(arc, params, workers=2)
    assert res["pairs"] == 9 and {p[0] for p in res["pair_list"]} == {"AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT", "FFFUSDT", "1000PEPEUSDT", "PEPEUSDT", "GGGUSDT"}
    src = P.Store(store, LAST_MONTH)
    expect = sum(len([m for m in src.months("klines", p, "1d") if m >= WINDOW_START[:7]]) for p, _ in res["pair_list"])     # December 2019 is before the window and is not fetched
    assert res["by_status"] == {"OK": expect}
    assert not any("USDCUSDT" in c or "NOSPOTUSDT" in c for c in mirror.calls if "klines" in c and c.endswith(".zip"))
    assert not any(f"-{m}.zip" in c for c in mirror.calls for m in ("2021-06", "2021-07"))                  # no month after the last explore month is ever requested
    dest = P.Store(str(tmp_path / "dest"), LAST_MONTH)
    assert dest.symbols("klines", "1d") == sorted(p for p, _ in res["pair_list"])


def test_the_detail_stage_fetches_only_the_symbols_that_ever_rank_and_one_spot_month_each(store, tmp_path):
    proto, _ = protocol()
    params = proto["parameters"]
    mirror, arc = mirror_archive(store, tmp_path / "dest")
    pairs = [tuple(p) for p in P.fetch_candidates(arc, params, workers=2)["pair_list"]]
    dest = P.Store(str(tmp_path / "dest"), LAST_MONTH)
    # by volume the top 3 are AAA (3e8) and GGG (2.5e8) throughout, then DDD (9e7) until its klines end, after which BBB (8e7) enters
    assert P.ever_ranked(dest, pairs, params, 3) == ["AAAUSDT", "BBBUSDT", "DDDUSDT", "GGGUSDT"]
    assert P.ever_ranked(dest, pairs, params, 1) == ["AAAUSDT"]
    res = P.fetch_details(arc, dest, pairs, params, workers=2, n_ranked=3)
    assert res["ranked_symbols"] == 4 and res["pairs_fetched"] == 4
    d2 = P.Store(str(tmp_path / "dest"), LAST_MONTH)
    assert d2.symbols("fundingRate", None) == d2.symbols("markPriceKlines", "1d") == d2.symbols("indexPriceKlines", "1d") == ["AAAUSDT", "BBBUSDT", "DDDUSDT", "GGGUSDT"]
    assert d2.symbols("spot_klines", "1d") == ["AAAUSDT", "BBBUSDT", "DDDUSDT", "GGGUSDT"]
    for sym in d2.symbols("spot_klines", "1d"):
        assert len(d2.months("spot_klines", sym, "1d")) == 1                                                 # the first month only: the listing evidence, not the whole history
    assert d2.months("spot_klines", "AAAUSDT", "1d") == [P.Store(store, LAST_MONTH).months("spot_klines", "AAAUSDT", "1d")[0]]
    assert not any("EEEUSDT" in c or "CCCUSDT" in c for c in mirror.calls if "fundingRate" in c)


def test_a_volume_floor_and_listing_age_keep_a_symbol_out_of_the_ranking_superset(store, tmp_path):
    proto, _ = protocol()
    params = proto["parameters"]
    mirror, arc = mirror_archive(store, tmp_path / "dest")
    pairs = [tuple(p) for p in P.fetch_candidates(arc, params, workers=2)["pair_list"]]
    dest = P.Store(str(tmp_path / "dest"), LAST_MONTH)
    everyone = P.ever_ranked(dest, pairs, params, 100)
    assert "EEEUSDT" not in everyone and {"AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "FFFUSDT", "GGGUSDT", "1000PEPEUSDT", "PEPEUSDT"} <= set(everyone)


def test_the_ranking_superset_applies_the_listing_age_rule_on_the_perps_own_history():
    params = protocol()[0]["parameters"]
    cut = P.day_ms(CUT)
    slots = P.CR.holding_grid(P.day_ms(WINDOW_START), cut - DAY, formation_days=30, hold_days=7)
    launch = slots[20] - 28 * DAY                                            # at slot 20 the newcomer is 28 days old: enough days of data for a median, too young for the 30-day age rule

    class KlineStore:
        def __init__(self, spec):
            self.spec = spec

        def rows(self, dataset, symbol, interval):
            return [(o, 1.0, 1.0, 1.0, 1.0, 1.0, o + DAY - 1, qv, 1) for o, qv in self.spec[symbol]]
    old = [(P.day_ms("2019-12-01") + n * DAY, 1e8) for n in range((cut - P.day_ms("2019-12-01")) // DAY)]
    new = [(launch + n * DAY, 5e8) for n in range(29)]                       # a huge volume for 29 days, then nothing: it can only rank if the age rule is not applied
    st = KlineStore({"AAAUSDT": old, "BBBUSDT": new})
    assert P.ever_ranked(st, [("AAAUSDT", "AAAUSDT"), ("BBBUSDT", "BBBUSDT")], params, 1) == ["AAAUSDT"]


# ------------------------------------------------------------------------------------------------ the whole run
def test_the_explore_run_end_to_end_counts_twelve_series_writes_only_its_ledger_and_is_repeatable(store, tmp_path):
    proto, digest = protocol()
    st = P.Store(store, LAST_MONTH)
    pairs = P.plan_pairs(st.symbols("klines", "1d"), st.symbols("spot_klines", "1d"), proto["parameters"])
    ledger = str(tmp_path / "ledger" / "proxy.jsonl")
    os.makedirs(os.path.dirname(ledger))
    before = sorted(os.listdir(store))
    rep = P.run_explore(st, pairs, proto, digest, ledger, cash_basis(), {}, allow_unpinned=True)
    assert rep["label"] == "PROXY_RESEARCH_ONLY" and rep["protocol_digest"] == digest and rep["data_digest"] == st.digest() and rep["pre_registered"] is False
    assert rep["bounds"]["ADVERSE"]["n_variants_counted"] == 12 and rep["bounds"]["FAVOURABLE"]["n_variants_counted"] == 12
    assert rep["decision"]["verdict"] in ("NO_WINNER", "SURVIVOR_PENDING_FINAL_READ", "INCONCLUSIVE")
    adv, fav = rep["bounds"]["ADVERSE"], rep["bounds"]["FAVOURABLE"]
    assert adv["cash_covered_only"] is True and adv["oos_rows"] < adv["oos_rows_all"]                  # the survivor statistic uses only rows the cash series itself covers
    assert fav["cash_covered_only"] is False and fav["oos_rows"] == fav["oos_rows_all"]
    assert adv["cash"]["before_series"] == "MEAN_OF_FIRST_90_DAYS" and fav["cash"]["before_series"] == "ZERO"
    assert adv["quarters"][0] == "2020Q3" and adv["quarters"] == fav["quarters"] and adv["folds"] == len(adv["picks"]) == len(adv["quarters"])
    assert adv["ledger_anchor"]["n"] == 12 and len(adv["ledger_anchor"]["head"]) == 64
    assert adv["minimum_detectable_mean_excess_bps_80pct_power"] > 0 and adv["descriptives"]["overall"]["weeks"] == adv["oos_rows"]
    assert adv["unmeasured_basis_charge_bps"] <= 0.0 and fav["unmeasured_basis_charge_bps"] == 0.0
    sf = rep["selection_facts"]
    assert sf["terminal_exit_unpriced_count"] == 0 and sf["slots_with_a_universe"] > 0 and rep["rejects_recorded_by_the_downloader"] == {}
    assert "not Bybit" in rep["claim_limits"] and rep["forbidden"]
    assert set(rep["code_sha256"]) == {"proxy_carry_study.py", "binance_archive.py"} and all(len(v) == 64 for v in rep["code_sha256"].values())
    assert sorted(os.listdir(store)) == before                                      # the store is only read
    assert os.path.exists(ledger)
    rep2 = P.run_explore(P.Store(store, LAST_MONTH), pairs, proto, digest, ledger, cash_basis(), {}, allow_unpinned=True)
    assert rep2["bounds"]["ADVERSE"]["n_variants_counted"] == 12                  # the same series are the same trials
    assert rep2["bounds"]["ADVERSE"]["stats"]["mean"] == rep["bounds"]["ADVERSE"]["stats"]["mean"]
    json.dumps(P._clean(rep), allow_nan=False)                                       # a report is JSON with no NaN


def test_too_few_aligned_rows_is_insufficient_data_not_a_verdict(store, tmp_path):
    proto, digest = protocol()
    proto["parameters"]["min_aligned_rows"] = 10 ** 6
    st = P.Store(store, LAST_MONTH)
    pairs = P.plan_pairs(st.symbols("klines", "1d"), st.symbols("spot_klines", "1d"), proto["parameters"])
    rep = P.run_explore(st, pairs, proto, digest, str(tmp_path / "l.jsonl"), cash_basis(), {}, allow_unpinned=True)
    assert rep["verdict"] == "INSUFFICIENT_DATA" and "bounds" not in rep and rep["reason"].startswith("COMPLETENESS")


def test_the_protocol_digest_moves_with_any_change_and_the_file_is_the_registered_one():
    proto, digest = P.load_protocol()
    assert digest == P.protocol_digest(proto) and proto["id"] == "binance_archive_carry_v1" and proto["label"] == "PROXY_RESEARCH_ONLY" and proto["registered"]["state"] == "BEFORE_ANY_STRATEGY_RETURN_WAS_COMPUTED"
    assert len(digest) == 64
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p2 = os.path.join(d, "p.json")
        proto2 = copy.deepcopy(proto)
        proto2["parameters"]["top_k"] = [1, 3, 5]
        with open(p2, "w") as fh:
            json.dump(proto2, fh)
        assert P.load_protocol(p2)[1] != digest
        proto2["schema"] = "other"
        with open(p2, "w") as fh:
            json.dump(proto2, fh)
        with pytest.raises(P.StudyError):
            P.load_protocol(p2)


def test_the_registered_numbers_in_the_parameter_block_are_the_ones_the_prose_states():
    proto, _ = P.load_protocol()
    p = proto["parameters"]
    assert (p["cut_utc"], p["last_explore_month"], p["hold_days"], p["formation_days"], p["top_k"]) == ("2024-10-01", "2024-09", 7, [7, 14, 30], [1, 3])
    assert p["min_median_quote_volume_usdt"] == 20_000_000 and p["top_n_by_median_volume"] == 60 and p["min_listing_age_days"] == 30
    assert p["adverse"]["leverage"] == 1 and p["favourable"]["leverage"] == 5 and p["adverse"]["extra_slippage_bps_by_median_volume"] == [[200000000, 2], [50000000, 6], [20000000, 15]]
    assert "2.04" in proto["bounds"]["ADVERSE"]["capital"] and "1.224" in proto["bounds"]["FAVOURABLE"]["capital"]
    assert P.CB.capital_per_gross_notional(True, 1) == 2.04 and P.CB.capital_per_gross_notional(True, 5) == 1.224
    assert proto["multiple_testing"]["family"] == "proxy.binance_archive_carry_v1" and len(p["formation_days"]) * len(p["top_k"]) * 2 == 12


# ------------------------------------------------------------------------------------------------ structure
def test_the_runner_reads_no_environment_touches_no_real_registry_and_has_no_order_path():
    src = open(os.path.join(TOOLS, "proxy_carry_study.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    imps = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imps |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imps.add(n.module.split(".")[0])
    assert not imps & {"hmac", "bybit_connection", "carry_broker", "entry_authority", "requests", "socket", "subprocess", "urllib", "history_holdout", "venues", "config", "runtime_capability"}, imps
    for forbidden in ("os.environ", "getenv", "subprocess", ".ai-agent-control", "HISTORY_HOLDOUT_LEDGER", "CANDIDATE_LEDGER_DIR", "unreserved", "prior_reads", "_KEY_BASIS_OBSERVED", "LIVE_TRADING_ACK", "api.bybit.com"):
        assert forbidden not in src, forbidden
    assert "RESEARCH ONLY" in src and "HYPOTHESIS" in src

# ------------------------------------------------------------------------------------------------ the verdict layer on hand-built rows (the oracle covers rows and series, not this)
def weekly(start="2021-05-03", n=87):
    t0 = P.day_ms(start)
    return [t0 + k * 7 * DAY for k in range(n)]


WF = {"scheme": "CALENDAR_QUARTERS", "oos_start_utc": "2022-01-01", "label_horizon_rows": 1, "embargo_rows": 1, "train_min_rows": 20}


def test_calendar_folds_are_the_quarters_from_the_start_and_one_missing_slot_moves_one_block_by_one_row():
    ts = weekly()                                                                # 2021-05-03 ... 2022-12-26
    folds = P.calendar_folds(ts, WF)
    assert [f"{k[0]}Q{k[1] + 1}" for _a, _b, k in folds] == ["2022Q1", "2022Q2", "2022Q3", "2022Q4"]
    for train, test, key in folds:
        assert all(P.quarter_key(ts[i]) == key for i in test) and test == list(range(test[0], test[0] + len(test)))
        assert train == list(range(0, test[0] - 2)) and len(train) >= 20             # every row at least label_horizon + embargo before the block
    assert sum(len(f[1]) for f in folds) == len([t for t in ts if t >= P.day_ms("2022-01-01")])
    dropped = ts[:60] + ts[61:]                                                  # one slot of 2022Q2 is missing
    f2 = P.calendar_folds(dropped, WF)
    members = lambda ff, tt: [[tt[i] for i in b] for _a, b, _k in ff]
    for a, b in zip(members(folds, ts), members(f2, dropped)):
        assert set(b) <= set(a) and len(a) - len(b) in (0, 1)                    # the blocks keep their calendar membership
    assert sum(len(f[1]) for f in f2) == sum(len(f[1]) for f in folds) - 1
    early = P.calendar_folds(ts, {**WF, "oos_start_utc": "2021-05-03"})          # an out-of-sample start so early that the first quarters have too little training data
    assert all(len(tr) >= 20 for tr, _te, _k in early) and early[0][2] > (2021, 1)
    for _tr, te, key in early:                                                   # a block with too little training data is skipped, never shortened or merged
        assert len(te) == len([t for t in ts if P.quarter_key(t) == key])


def test_a_calendar_fold_that_leaked_would_be_refused(monkeypatch):
    monkeypatch.setattr(P.SP, "audit_split", lambda *a, **k: {"clean": False, "reading": "leaks"})
    with pytest.raises(P.StudyError) as e:
        P.calendar_folds(weekly(), WF)
    assert e.value.code == "FOLD_LEAKS"


def test_the_walk_forward_picks_on_train_rows_only_and_pools_the_blocks_it_applied_the_pick_to():
    ts = weekly()
    n = len(ts)
    folds = P.calendar_folds(ts, WF)
    a = [2.0] * n
    b = [0.0] * n
    c = [0.5] * n
    for i in range(folds[1][1][0], folds[2][1][0] - 2):                          # B is very good on rows that only the third and fourth blocks train on
        b[i] = 30.0
    series = {"A": a, "B": b, "C": c}
    wf = P.walk_forward(series, ts, WF)
    expected = [max(series, key=lambda k, tr=tr: (sum(series[k][i] for i in tr) / len(tr), k)) for tr, _te, _k in folds]       # the best TRAIN mean, plain arithmetic
    assert wf["picks"] == expected == ["A", "A", "B", "B"]
    assert wf["oos"] == [(p, i) for p, (_tr, te, _k) in zip(expected, folds) for i in te]
    assert wf["quarters"] == ["2022Q1", "2022Q2", "2022Q3", "2022Q4"] and wf["folds"] == 4
    for k, (_tr, te, _key) in enumerate(folds):                                  # a variant that is enormous ONLY in a block's own test rows cannot be picked for that block: only train rows choose
        p2 = {name: list(v) for name, v in series.items()}
        for i in te:
            p2["C"][i] = 1e9
        assert P.walk_forward(p2, ts, WF)["picks"][k] == wf["picks"][k]
    late = {name: list(v) for name, v in series.items()}                         # ... while the same values, once they are in the past of a later block, do count: the window expands
    for i in folds[0][1]:
        late["C"][i] = 1e9
    assert P.walk_forward(late, ts, WF)["picks"][1] == "C"


class StubLedger:
    def __init__(self, n):
        self.n_variants = n

    def anchor(self):
        return P.SP.LedgerAnchor(self.n_variants, "ab" * 32)


def hand_rows(ts, values, legs=("XXXUSDT", "YYYUSDT"), leg_nets=None, capital=2.04, cash=7.0):
    out = []
    for t, v in zip(ts, values):
        ln = leg_nets(t, v) if leg_nets else [v * 2.04 + 7.0, v * 2.04 + 7.0]
        net = sum(ln) / len(ln)
        out.append({"t": t, "net_bps_of_gross": net, "capital": capital, "cash_bps_of_capital": cash, "excess_bps_of_capital": v, "legs": list(legs), "leg_net_bps": ln, "unmeasured_basis_legs": 0, "hold_hole_legs": 0,
                    "gross_funding_bps": net + 31.4, "cost_bps_mean": 31.4, "basis_bps_mean": 0.0, "entered_legs": 2, "exited_legs": 2, "leg_funding_bps": [30.0, 20.0], "leg_formation_bps_per_day": [4.0, 3.0]})
    return out


def test_bound_report_pools_the_picked_blocks_and_derives_every_figure_by_hand():
    params = copy.deepcopy(real_params())
    params["walk_forward"] = WF
    ts = weekly()
    n = len(ts)
    vals = [3.0 + (i % 5) - 2.0 for i in range(n)]
    vals[70], vals[74], vals[78], vals[82] = 40.0, 35.0, 30.0, 25.0              # four big weeks inside the out-of-sample blocks: the three LARGEST are dropped
    better = hand_rows(ts, vals)
    worse = hand_rows(ts, [-5.0] * n)
    rep = P.bound_report("ADVERSE", {"A": better, "B": worse}, ts, StubLedger(12), params, cash_first_ms=P.day_ms("2022-03-01"), covered_only=False)
    folds = P.calendar_folds(ts, WF)
    oos_idx = [i for _tr, te, _k in folds for i in te]
    oos = [vals[i] for i in oos_idx]
    assert rep["picks"] == ["A"] * 4 and rep["oos_rows"] == rep["oos_rows_all"] == len(oos) and rep["quarters"] == ["2022Q1", "2022Q2", "2022Q3", "2022Q4"]
    st = P.RH.significance(oos, ci=True)
    assert rep["stats"]["mean"] == pytest.approx(statistics.fmean(oos)) and rep["stats"]["p_one_sided"] == st["p_one_sided"]
    assert rep["n_variants_counted"] == 12 and rep["p_adjusted"] == pytest.approx(min(1.0, 12 * st["p_one_sided"]))                 # Holm over the whole family of 12
    assert rep["mean_without_best_weeks"] == pytest.approx(statistics.fmean(sorted(oos)[:-3])) and rep["dropped_best_weeks"] == 3    # the three LARGEST are dropped, not the smallest
    assert rep["mean_without_best_weeks"] < statistics.fmean(oos) < statistics.fmean(sorted(oos)[3:])
    by_year = {}
    for i in oos_idx:
        by_year.setdefault(str(P.year_of(ts[i])), []).append(vals[i])
    assert rep["by_year"] == {y: pytest.approx(statistics.fmean(v)) for y, v in by_year.items()} and set(rep["by_year"]) == {"2022"}
    assert rep["ledger_anchor"] == {"n": 12, "head": "ab" * 32}
    assert rep["minimum_detectable_mean_excess_bps_80pct_power"] == pytest.approx(P.mde_bps(st["sd"], st["ess"], 12, 0.05, 0.8))
    cov = P.bound_report("ADVERSE", {"A": better, "B": worse}, ts, StubLedger(12), params, cash_first_ms=P.day_ms("2022-07-01"), covered_only=True)       # only rows the cash series covers
    assert cov["oos_rows"] < cov["oos_rows_all"] and cov["oos_rows"] == len([i for i in oos_idx if ts[i] >= P.day_ms("2022-07-01")])


def test_the_symbol_share_is_the_largest_symbols_part_of_the_summed_net_and_is_none_without_a_gain():
    params = copy.deepcopy(real_params())
    params["walk_forward"] = WF
    ts = weekly()
    rows = hand_rows(ts, [2.0] * len(ts), leg_nets=lambda t, v: [30.0, 10.0])      # X earns 30, Y earns 10 per leg-week: per row the book nets 20, X is 15 of it
    rep = P.bound_report("ADVERSE", {"A": rows}, ts, StubLedger(12), params, cash_first_ms=0, covered_only=False)
    assert rep["max_symbol_share"] == pytest.approx(15.0 / 20.0) and rep["symbol_share_top"][0][0] == "XXXUSDT"
    loss = hand_rows(ts, [2.0] * len(ts), leg_nets=lambda t, v: [-30.0, -10.0])
    assert P.bound_report("ADVERSE", {"A": loss}, ts, StubLedger(12), params, cash_first_ms=0, covered_only=False)["max_symbol_share"] is None


def test_the_detectable_effect_is_the_holm_scaled_normal_margin_and_grows_with_the_family():
    assert P.mde_bps(60.0, 40.0, 12, 0.05, 0.8) == pytest.approx(33.013, abs=1e-3)
    assert P.mde_bps(60.0, 40.0, 1, 0.05, 0.8) == pytest.approx(23.589, abs=1e-3)
    assert P.mde_bps(60.0, 40.0, 12, 0.05, 0.8) > P.mde_bps(60.0, 40.0, 6, 0.05, 0.8) > P.mde_bps(60.0, 40.0, 1, 0.05, 0.8)
    assert P.mde_bps(60.0, 160.0, 12, 0.05, 0.8) == pytest.approx(P.mde_bps(60.0, 40.0, 12, 0.05, 0.8) / 2)
    assert P.mde_bps(None, 40.0, 12, 0.05, 0.8) is None and P.mde_bps(60.0, 0.0, 12, 0.05, 0.8) is None


def test_the_descriptive_block_and_persistence_by_hand():
    ts = weekly(n=4)
    rows = hand_rows(ts, [10.0, -4.0, 6.0, 0.0])
    rows[1]["entered_legs"], rows[1]["exited_legs"] = 0, 2
    rows[2]["hold_hole_legs"], rows[3]["unmeasured_basis_legs"] = 1, 2
    d = P.descriptive_block(rows)
    assert d["weeks"] == 4 and d["mean_excess_bps_of_capital"] == pytest.approx(3.0) and d["share_of_weeks_with_positive_excess"] == 0.5
    assert d["turnover_share_of_legs_traded_per_week"] == pytest.approx((4 + 2 + 4 + 4) / (2.0 * 8))                       # (entries + exits) over twice the legs
    assert d["break_even_gross_funding_bps_per_week"] == pytest.approx(31.4 - 0.0 + 7.0 * 2.04)
    assert d["legs_with_a_funding_hole_in_the_hold"] == 1 and d["legs_with_unmeasured_basis"] == 2 and P.descriptive_block([]) is None
    pr = P.persistence(rows)
    assert pr["pairs"] == 8 and pr["mean_forward_over_mean_trailing"] == pytest.approx(25.0 / (3.5 * 7)) and pr["correlation"] is not None


# ------------------------------------------------------------------------------------------------ what the build and the run now do with data they cannot trust or price
def test_a_hole_inside_the_hold_does_not_choose_the_universe_but_the_leg_is_counted(store):
    proto, _ = protocol()
    ts, rows, medvol, series = production_series(store, proto)
    assert START + 145 * DAY in ts                                              # a grid slot whose hold (day 145 to 152) contains the prints that are missing from day 149 16:00 to day 153
    held = [r for key, rr in series.items() for r in rr if r["t"] == START + 145 * DAY and "GGGUSDT" in r["legs"]]
    assert held and all(r["hold_hole_legs"] >= 1 for r in held)
    assert all(r["hold_hole_legs"] == 0 for key, rr in series.items() for r in rr if r["t"] < START + 138 * DAY)


def test_an_unpriced_terminal_exit_is_listed_and_caps_the_verdict(tmp_path):
    root = str(tmp_path / "s")
    mk = [s for s in market() if s.perp not in ("NOSPOTUSDT", "AAAUSDT")]
    mk.append(Sym("HHHUSDT", "HHHUSDT", 0.0030, 5.0e8, 0, 0, end_day=400, mark_gap=(395, 400), seed=12))      # the highest funding, delivered on day 400, no mark in its last five days
    write_store(root, [s for s in mk])
    proto, digest = protocol()
    st = P.Store(root, LAST_MONTH)
    pairs = P.plan_pairs(st.symbols("klines", "1d"), st.symbols("spot_klines", "1d"), proto["parameters"])
    rep = P.run_explore(st, pairs, proto, digest, str(tmp_path / "l.jsonl"), cash_basis(), {}, allow_unpinned=True)
    sf = rep["selection_facts"]
    assert sf["terminal_exit_unpriced_count"] > 0 and {u["symbol"] for u in sf["terminal_exit_unpriced_first_20"]} == {"HHHUSDT"}
    assert rep["decision"]["verdict"] == "INSUFFICIENT_DATA" and rep["decision"]["note"].startswith("TERMINAL_EXIT_UNPRICED") and "verdict_before_the_cap" in rep["decision"]
    assert "bounds" in rep                                                      # everything is still computed and shown; only the claim is withheld


def test_an_incomplete_store_is_insufficient_data_and_a_stricter_gate_is_honoured(store, tmp_path):
    import shutil
    proto, digest = protocol()
    short = str(tmp_path / "short")
    shutil.copytree(store, short)
    for root, _d, files in os.walk(short):
        for f in files:
            if "2021-05" in f:
                os.remove(os.path.join(root, f))                                # the store ends with April while the protocol's last explore month is May
    pairs = P.plan_pairs(P.Store(short, LAST_MONTH).symbols("klines", "1d"), P.Store(short, LAST_MONTH).symbols("spot_klines", "1d"), proto["parameters"])
    rep = P.run_explore(P.Store(short, LAST_MONTH), pairs, proto, digest, str(tmp_path / "a.jsonl"), cash_basis(), {}, allow_unpinned=True)
    assert rep["verdict"] == "INSUFFICIENT_DATA" and rep["reason"].startswith("COMPLETENESS") and rep["last_row_ms"] < rep["last_slot_expected_ms"]
    strict = copy.deepcopy(proto)
    strict["parameters"]["completeness"] = {"min_fraction_of_grid": 1.01}
    full_pairs = P.plan_pairs(P.Store(store, LAST_MONTH).symbols("klines", "1d"), P.Store(store, LAST_MONTH).symbols("spot_klines", "1d"), strict["parameters"])
    rep2 = P.run_explore(P.Store(store, LAST_MONTH), full_pairs, strict, digest, str(tmp_path / "b.jsonl"), cash_basis(), {}, allow_unpinned=True)
    assert rep2["verdict"] == "INSUFFICIENT_DATA" and rep2["aligned_rows"] < rep2["grid_slots_required"]


def test_only_the_pinned_protocol_is_pre_registered_and_any_other_file_is_refused_or_marked(store, tmp_path):
    proto, digest = P.load_protocol()
    assert P.EXPECTED_PROTOCOL_DIGEST == digest and len(digest) == 64            # the registered file, and the digest the runner pins
    st = P.Store(store, "2021-07")                                              # a wrong ceiling: whatever is judged first decides the error
    with pytest.raises(P.StudyError) as e:
        P.run_explore(st, [], proto, "0" * 64, str(tmp_path / "x.jsonl"), cash_basis(), {})
    assert e.value.code == "PROTOCOL_NOT_PINNED"                                 # not the pinned digest: refused before anything is read
    with pytest.raises(P.StudyError) as e2:
        P.run_explore(st, [], proto, digest, str(tmp_path / "x.jsonl"), cash_basis(), {})
    assert e2.value.code == "STORE_CEILING"                                      # the pinned digest passes the pin and meets the next check


def test_a_stored_month_that_fails_its_checksum_stops_the_run_and_no_symbol_is_dropped_silently(store, tmp_path):
    import shutil
    proto, digest = protocol()
    root = str(tmp_path / "t")
    shutil.copytree(store, root)
    victim = os.path.join(root, *BA.data_path("fundingRate", "BBBUSDT", None, "2020-05").split("/"))
    with open(victim, "r+b") as fh:
        fh.seek(30)
        fh.write(b"\x00")
    st = P.Store(root, LAST_MONTH)
    pairs = P.plan_pairs(st.symbols("klines", "1d"), st.symbols("spot_klines", "1d"), proto["parameters"])
    with pytest.raises(P.StudyError) as e:
        P.run_explore(st, pairs, proto, digest, str(tmp_path / "l.jsonl"), cash_basis(), {}, allow_unpinned=True)
    assert e.value.code == "STORE_ERROR" and "BBBUSDT" in str(e.value)


def test_a_symbol_that_cannot_be_built_is_counted_with_its_reason(store, tmp_path):
    proto, digest = protocol()
    st = P.Store(store, LAST_MONTH)
    pairs = P.plan_pairs(st.symbols("klines", "1d"), st.symbols("spot_klines", "1d"), proto["parameters"]) + [("ZZZUSDT", "ZZZUSDT")]
    rep = P.run_explore(st, pairs, proto, digest, str(tmp_path / "l.jsonl"), cash_basis(), {}, allow_unpinned=True)
    assert rep["quality"]["instrument_not_buildable:no_funding_months"] == 1
    assert P.not_buildable_reason(st, "AAAUSDT", "ZZZUSDT") == "no_spot_month"


def test_a_delisted_spot_pair_ends_the_hedge_after_its_last_listed_month(store, tmp_path):
    proto, digest = protocol()
    params = proto["parameters"]
    st = P.Store(store, LAST_MONTH)
    pairs = P.plan_pairs(st.symbols("klines", "1d"), st.symbols("spot_klines", "1d"), params)
    ctr = {"CCCUSDT": {"first": "2019-12", "last": "2020-08"}, "BBBUSDT": {"first": "2019-12", "last": "2030-01"}}
    insts = {p: P.build_instrument(st, p, s, params, P.day_ms(CUT), P.BA.month_bounds_ms(ctr[s]["last"])[1] if s in ctr and ctr[s]["last"] <= LAST_MONTH else None) for p, s in pairs if p in ("CCCUSDT", "BBBUSDT")}
    assert insts["CCCUSDT"].spot_last_ms == P.BA.month_bounds_ms("2020-08")[1] and insts["BBBUSDT"].spot_last_ms is None
    before, _mv, w1 = P.universe_at([insts["CCCUSDT"]], P.day_ms("2020-08-31"), params)
    after, _mv, w2 = P.universe_at([insts["CCCUSDT"]], P.day_ms("2020-09-01"), params)
    assert w1["spot_delisted"] == 0 and w2["spot_delisted"] == 1 and after == []
    rep = P.run_explore(st, pairs, proto, digest, str(tmp_path / "l.jsonl"), cash_basis(), ctr, allow_unpinned=True)
    assert rep["exclusions"]["spot_delisted"] > 0 and rep["quality"]["pairs_with_a_spot_listing"] == 2


def test_a_print_off_the_grid_of_its_stated_interval_makes_that_calendar_month_a_hole():
    params = protocol()[0]["parameters"]
    cut = P.day_ms(CUT)
    fs = FakeStore(cut - 8 * H)
    fs.funding = [(t + (7_000 if (cut - 40 * DAY) <= t < (cut - 39 * DAY) else 0), iv, r) for t, iv, r in fs.funding]          # one print 7 s off its grid
    inst = P.build_instrument(fs, "ZZZUSDT", "ZZZUSDT", params, cut)
    assert inst.quality["funding_off_grid_dropped"] >= 1 and inst.quality["interval_disagreement_months"] == 1
    month = P.ym_of(cut - 40 * DAY)
    lo, hi = P.BA.month_bounds_ms(month)
    assert P.has_hole(inst, lo + DAY, lo + 2 * DAY) and not P.has_hole(inst, hi + DAY, hi + 2 * DAY) and not P.has_hole(inst, lo - 5 * DAY, lo - 3 * DAY)


def test_the_downloaders_counted_rejects_are_read_and_a_garbage_line_is_skipped(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "REJECTS.jsonl").write_text('{"code": "CHECKSUM_MISMATCH"}\n{"code": "ROWS_INVALID"}\nnot json\n{"code": "CHECKSUM_MISMATCH"}\n')
    assert P.Store(str(root), LAST_MONTH).rejects() == {"CHECKSUM_MISMATCH": 2, "ROWS_INVALID": 1}
    assert P.Store(str(tmp_path / "none"), LAST_MONTH).rejects() == {}


def test_the_spot_listing_file_round_trips_merges_and_refuses_garbage(tmp_path):
    root = str(tmp_path)
    assert P.load_spot_listing(root) == {}
    P.write_spot_listing(root, {"AAAUSDT": {"first": "2019-12", "last": "2026-10"}})
    P.write_spot_listing(root, {"BBBUSDT": {"first": "2020-02", "last": "2022-11"}})
    got = P.load_spot_listing(root)
    assert got == {"AAAUSDT": {"first": "2019-12", "last": "2026-10"}, "BBBUSDT": {"first": "2020-02", "last": "2022-11"}}
    open(os.path.join(root, P.SPOT_LISTING_NAME), "w").write('{"spots": {"bad/symbol": {"first": "x"}, "CCCUSDT": 5, "DDDUSDT": {"first": "2020-01", "last": "2020-02"}}}')
    assert P.load_spot_listing(root) == {"DDDUSDT": {"first": "2020-01", "last": "2020-02"}}
    open(os.path.join(root, P.SPOT_LISTING_NAME), "w").write("not json")
    assert P.load_spot_listing(root) == {}


def test_an_unmeasured_basis_is_charged_the_stress_under_the_adverse_bound_and_zero_under_the_favourable_one():
    class I:
        pass
    i = I()
    i.symbol = "AAAUSDT"
    i.holes = []
    i.series = type("S", (), {"delivery_ms": None})()
    i.mark_ts, i.mark_px, i.index_ts, i.index_px = [0], [100.0], [0], [100.0]               # no exit marks: the basis cannot be measured
    rows = {0: {"legs": [{"symbol": "AAAUSDT", "funding_bps": 40.0}], "funding_bps": 40.0}}
    params = copy.deepcopy(real_params())
    cash = type("C", (), {"apr_over": lambda self, s, e: 0.0, "label": "X"})()
    costs = {"bps": 31.4, "fee_bps": 31.0}
    medvol = {0: {"AAAUSDT": 300e6}}
    adv = P.bound_series(rows, [0], "ADVERSE", {"AAAUSDT": i}, medvol, params, cash, costs, -25.0)
    fav = P.bound_series(rows, [0], "FAVOURABLE", {"AAAUSDT": i}, medvol, params, cash, costs, 0.0)
    assert adv[0]["net_bps_of_gross"] == pytest.approx(40.0 - 33.4 - 25.0) and adv[0]["unmeasured_basis_legs"] == 1
    assert fav[0]["net_bps_of_gross"] == pytest.approx(40.0 - 31.0) and fav[0]["unmeasured_basis_legs"] == 1


def test_the_basis_floor_is_the_worst_measured_leg_week_and_none_without_a_measurement():
    class I:
        pass

    def inst(entry_mark, exit_mark):
        i = I()
        i.series = type("S", (), {"delivery_ms": None})()
        i.mark_ts, i.mark_px, i.index_ts, i.index_px = [0, 7 * DAY], [entry_mark, exit_mark], [0, 7 * DAY], [100.0, 100.0]
        return i
    insts = {"A": inst(100.5, 100.2), "B": inst(100.0, 101.0), "C": I()}
    insts["C"].series = type("S", (), {"delivery_ms": 3 * DAY})()
    insts["C"].mark_ts, insts["C"].mark_px, insts["C"].index_ts, insts["C"].index_px = [0], [100.0], [0], [100.0]
    rows = {(7, 1): {0: {"legs": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]}}, (7, 3): {0: {"legs": [{"symbol": "A"}]}}}
    params = real_params()
    assert P.measured_basis_floor(rows, [0], insts, params) == pytest.approx(-100.0)       # B: the premium goes from 0 to +1.00%: -100 bps to a short perp / long spot pair; A gains 30 bps; C is unmeasured
    assert P.measured_basis_floor(rows, [], insts, params) is None
    assert P.measured_basis_floor({(7, 1): {0: {"legs": [{"symbol": "C"}]}}}, [0], insts, params) is None


def test_a_lower_confidence_bound_of_exactly_zero_is_not_a_survivor():
    params = real_params()
    zero = {**_bound(5.0, 0.0, 9.0)}
    assert P.decide(zero, _bound(8.0, 2.0, 12.0), params)["survivor_checks"]["a_adverse_mean_positive_holm_and_ci_lo_positive"] is False
    assert P.decide(_bound(5.0, 0.01, 9.0), _bound(8.0, 2.0, 12.0), params)["survivor_checks"]["a_adverse_mean_positive_holm_and_ci_lo_positive"] is True


def test_the_run_charges_the_adverse_bound_the_worst_measured_basis_for_a_leg_it_could_not_measure(store, tmp_path):
    proto, digest = protocol()
    st = P.Store(store, LAST_MONTH)
    pairs = P.plan_pairs(st.symbols("klines", "1d"), st.symbols("spot_klines", "1d"), proto["parameters"])
    rep = P.run_explore(st, pairs, proto, digest, str(tmp_path / "l.jsonl"), cash_basis(), {}, allow_unpinned=True)
    _ts, _rows, _mv, _series = production_series(store, proto)
    floor = production_series.last_floor
    assert floor is not None and floor < 0
    assert rep["bounds"]["ADVERSE"]["unmeasured_basis_charge_bps"] == pytest.approx(min(0.0, floor)) and rep["bounds"]["FAVOURABLE"]["unmeasured_basis_charge_bps"] == 0.0
    assert rep["quality"]["aligned_rows_before_the_cash_series_(cash_extended)"] > 0                  # the synthetic cash series starts late: those rows used the extension


def test_the_builder_s_dropped_legs_and_missing_rows_are_counted_not_lost(store):
    proto, _ = protocol()
    production_series(store, proto)
    stats = production_series.last_stats
    assert stats["slots_with_universe"] > 0
    # AAA has no mark or index file for days 330-336: a top-3 week that would hold it cannot price it, so the builder drops the leg: a count, not silence
    assert sum(stats["legs_dropped"].values()) > 0
    k1_rows_missing = stats["slots_without_row"]
    assert all(isinstance(v, int) and v >= 0 for v in k1_rows_missing.values())
    st, pairs, insts = instruments(store, proto)
    params = proto["parameters"]
    cut = P.day_ms(CUT)
    first = max(P.day_ms(params["window_start_utc"]), min(i.series.funding[0][0] for i in insts))
    slots = P.slot_grid(first, cut, params)
    only = [i for i in insts if i.symbol in ("AAAUSDT",)]
    rows, medvol, why, st2 = P.build_variant_rows(only, slots, params)                      # a one-symbol universe: top-3 weeks cannot even form, top-1 weeks lose the leg when its marks are stale
    assert st2["slots_with_universe"] > 0 and sum(st2["slots_without_row"].values()) > 0
