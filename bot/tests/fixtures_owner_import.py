"""SYNTHETIC builders for tests/test_owner_import.py: Bybit V5 API-shaped rows (documented field names, string values, execFee > 0 paid, transaction-log funding > 0 received,
``change = cashFlow + funding - fee``). No real account data. Dates are after the committed public history (2026-09-09) so the public-market checks do not run.

Every number a test expects is written in the test as literal arithmetic; this module only lays rows out."""
from __future__ import annotations

import json
from datetime import datetime, timezone

HOUR = 3_600_000
T0 = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()) * 1000        # a multiple of 8 h: on the settlement grid
OPEN_TS = T0 + 30 * 60_000                 # 00:30 open the short
CLOSE_TS = T0 + 25 * HOUR                  # next day 01:00 close it
W0_TS = T0                                  # opening wallet snapshot
W1_TS = T0 + 26 * HOUR                      # closing wallet snapshot
SETTLE_TS = (T0 + 8 * HOUR, T0 + 16 * HOUR, T0 + 24 * HOUR)


def wallet(ts, usdt, btc, **extra):
    coins = [{"coin": "USDT", "walletBalance": repr(usdt), "equity": repr(usdt), "unrealisedPnl": "0", "borrowAmount": "0"},
             {"coin": "BTC", "walletBalance": repr(btc), "equity": repr(btc), "unrealisedPnl": "0", "borrowAmount": "0"}]
    env = {"retCode": 0, "retMsg": "OK", "result": {"list": [{"accountType": "UNIFIED", "totalEquity": "0", "coin": coins}]}, "retExtInfo": {}, "time": ts}
    env.update(extra)
    return env


def marks(spot_open=60_000.0, spot_close=59_000.0, ts_open=W0_TS, ts_close=W1_TS):
    return [{"instrument": "BTC_SPOT", "ts_ms": ts_open, "price": spot_open, "source": "owner-supplied test mark"},
            {"instrument": "BTC_SPOT", "ts_ms": ts_close, "price": spot_close, "source": "owner-supplied test mark"}]


def exec_row(i, side, qty, price, fee, ts, *, symbol="BTCUSDT", fee_rate="0.00055", maker=False, fee_currency="USDT", **over):
    r = {"symbol": symbol, "orderId": f"o{i}", "side": side, "execFee": repr(fee), "execId": f"x{i}", "execPrice": repr(price), "execQty": repr(qty),
         "execType": "Trade", "execValue": repr(qty * price), "execTime": str(ts), "feeCurrency": fee_currency, "isMaker": maker, "feeRate": fee_rate}
    r.update(over)
    return r


def settlement_row(n, ts, size, rate, pay, bal):
    side = "Sell" if size < 0 else "Buy"
    return {"id": f"{n}_BTCUSDT_{ts}", "tradeId": f"s{n}", "transactionTime": str(ts), "type": "SETTLEMENT", "category": "linear", "symbol": "BTCUSDT", "side": side,
            "size": repr(size), "currency": "USDT", "feeRate": repr(rate if side == "Buy" else -rate), "funding": repr(pay), "fee": "0", "cashFlow": "0",
            "change": repr(pay), "cashBalance": repr(bal)}


def trade_row(n, ts, tid, side, size_after, fee, cash_flow, bal, fee_rate="0.00055"):
    return {"id": f"{n}_BTCUSDT_{ts}", "tradeId": tid, "transactionTime": str(ts), "type": "TRADE", "category": "linear", "symbol": "BTCUSDT", "side": side,
            "size": repr(size_after), "currency": "USDT", "feeRate": fee_rate, "funding": "0", "fee": repr(fee), "cashFlow": repr(cash_flow),
            "change": repr(cash_flow - fee), "cashBalance": repr(bal)}


class Trip:
    """One short round trip: sell (``opens``: [(qty, price)], taker) at OPEN_TS, three settlements at ``rate`` while short, buy back (``closes``) at CLOSE_TS.
    ``spot``: [(minutes after T0, side, qty, price, fee, fee_coin)] spot executions before the short is opened. Opening wallet ``usdt0`` / ``btc0``.

    Per-row ``cashFlow`` on a closing fill is the venue's realised P&L for that fill (what the log would print); tests state every expected figure as literal
    arithmetic and never read these helpers' sums."""

    def __init__(self, opens=((0.5, 60_000.0),), closes=((0.5, 59_000.0),), rate=0.0001, fee_rate=0.00055, usdt0=10_000.0, btc0=1.0, settle_mark=60_000.0, spot=()):
        self.opens, self.closes, self.rate, self.fee_rate = list(opens), list(closes), rate, fee_rate
        self.usdt0, self.btc0, self.settle_mark, self.spot = usdt0, btc0, settle_mark, list(spot)
        self.qty = sum(q for q, _ in self.opens)
        self.avg = sum(q * p for q, p in self.opens) / self.qty
        self.pay = self.qty * settle_mark * rate

    def executions(self):
        rows, i = [], 1
        for q, p in self.opens:
            rows.append(exec_row(i, "Sell", q, p, q * p * self.fee_rate, OPEN_TS + i * 1000))
            i += 1
        for q, p in self.closes:
            rows.append(exec_row(i, "Buy", q, p, q * p * self.fee_rate, CLOSE_TS + i * 1000))
            i += 1
        return rows

    def spot_executions(self):
        out = []
        for k, (m, side, q, p, fee, coin) in enumerate(self.spot, 1):
            out.append(exec_row(f"s{k}", side, q, p, fee, T0 + m * 60_000, fee_rate=("0.001"), fee_currency=coin))
        return out

    def txlog(self, funding_sign=1, funding_scale=1.0, drop_settlement=None, newest_first=False):
        usdt, btc, rows, n = self.usdt0, self.btc0, [], 0
        for k, (m, side, q, p, fee, coin) in enumerate(self.spot, 1):
            ts = T0 + m * 60_000
            d_btc = (q if side == "Buy" else -q) - (fee if coin == "BTC" else 0.0)
            d_usdt = (-q * p if side == "Buy" else q * p) - (fee if coin == "USDT" else 0.0)
            for cur, d in (("BTC", d_btc), ("USDT", d_usdt)):
                n += 1
                if cur == "BTC":
                    btc += d
                    bal = btc
                else:
                    usdt += d
                    bal = usdt
                rows.append({"id": f"{n}_BTCUSDT_{ts}", "tradeId": f"xs{k}", "transactionTime": str(ts), "type": "TRADE", "category": "spot", "symbol": "BTCUSDT",
                             "side": side, "size": "0", "currency": cur, "feeRate": "0.001", "funding": "0", "fee": "0", "cashFlow": repr(d), "change": repr(d),
                             "cashBalance": repr(bal)})
        size = 0.0
        for i, (q, p) in enumerate(self.opens, 1):
            fee = q * p * self.fee_rate
            size -= q
            usdt -= fee
            n += 1
            rows.append(trade_row(n, OPEN_TS + i * 1000, f"x{i}", "Sell", size, fee, 0.0, usdt))
        for k, ts in enumerate(SETTLE_TS):
            if drop_settlement == k:
                continue
            pay = funding_sign * funding_scale * self.pay
            usdt += pay
            n += 1
            rows.append(settlement_row(n, ts, -self.qty, self.rate, pay, usdt))
        for j, (q, p) in enumerate(self.closes, len(self.opens) + 1):
            fee = q * p * self.fee_rate
            cash = (self.avg - p) * q
            size += q
            usdt += cash - fee
            n += 1
            rows.append(trade_row(n, CLOSE_TS + j * 1000, f"x{j}", "Buy", 0.0 if abs(size) < 1e-12 else size, fee, cash, usdt))
        return list(reversed(rows)) if newest_first else rows

    def closing(self):
        """(usdt, btc) the closing wallet shows: the last running balance of the log."""
        usdt, btc = self.usdt0, self.btc0
        for r in self.txlog():
            if r["currency"] == "USDT":
                usdt = float(r["cashBalance"])
            else:
                btc = float(r["cashBalance"])
        return usdt, btc


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return str(path)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    return str(path)


def lay_out(tmp, trip, *, execs=None, txlog=None, w0=None, w1=None, with_wallets=True, spot_exec=True):
    """Write the standard four files into ``tmp`` and return the role/path pairs."""
    pairs = [("linear_execution", write_jsonl(tmp / "exec_linear.jsonl", execs if execs is not None else trip.executions())),
             ("transaction_log", write_jsonl(tmp / "txlog.jsonl", txlog if txlog is not None else trip.txlog()))]
    if with_wallets:
        pairs.append(("wallet_opening", write_json(tmp / "wallet_open.json", w0 or wallet(W0_TS, trip.usdt0, trip.btc0))))
        cu, cb = trip.closing()
        pairs.append(("wallet_closing", write_json(tmp / "wallet_close.json", w1 or wallet(W1_TS, cu, cb))))
    if trip.spot and spot_exec:
        pairs.append(("spot_execution", write_jsonl(tmp / "exec_spot.jsonl", trip.spot_executions())))
    return pairs
