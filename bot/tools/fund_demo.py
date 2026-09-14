#!/usr/bin/env python3
"""Fund a Bybit DEMO account. Refuses to run anywhere else.

WHY THIS EXISTS
===============
The two things standing between this repository and venue truth were an API
key and a wallet with BTC in it. Testnet coins come from a web faucet — 1 BTC,
once per 24 hours, PC browser only — so that wallet is a person's job and a
person's wait.

Bybit's demo trading service is a different module: real mainnet market data,
simulated matching, and a wallet funded by one signed request. Real marks, real
basis, real funding prints, no real money. `CARRY_EXECUTION_MODE=overlay`
hedges BTC the client already owns, so a wallet with BTC in it is the whole
precondition, and this is how it gets there without waiting a day.

WHAT IT REFUSES
===============
Anything that is not the demo base URL. Not by reading a config flag — by
looking at the URL the client is actually pointed at, because that is the thing
that decides where the request lands. A funding call is a WRITE, and a write
aimed at the wrong exchange is the failure this repository is organised around.

    python3 tools/fund_demo.py --btc 1 --usdt 10000
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

ENDPOINT = "/v5/account/demo-apply-money"

#: What the venue will grant, per the demo trading documentation. Asking for
#: more is refused HERE rather than by the venue, so a typo is a message
#: instead of a rejected signed request nobody reads.
MAX = {"BTC": 15.0, "ETH": 200.0, "USDT": 100_000.0, "USDC": 100_000.0}


class NotDemo(RuntimeError):
    """The client is not pointed at the demo venue. Nothing was sent."""


def apply(client: Any, amounts: Sequence[Tuple[str, str]],
          *, reduce: bool = False) -> Any:
    """One funding request. `amounts` is `(coin, amount_as_string)`."""
    from bybit_connection import DEMO_REST

    base = str(getattr(client, "base_url", ""))
    if base.rstrip("/") != DEMO_REST:
        raise NotDemo(
            f"the client is pointed at {base!r}, not {DEMO_REST}. This is a "
            "WRITE, and a write aimed at the wrong exchange is the failure "
            "this repository is organised around. Nothing was sent.")
    if not amounts:
        raise ValueError("nothing to apply for")
    rows = []
    for coin, amount in amounts:
        coin = str(coin).upper()
        if coin not in MAX:
            raise ValueError(
                f"{coin!r} is not fundable on demo; the venue grants "
                f"{sorted(MAX)}")
        try:
            value = float(amount)
        except (TypeError, ValueError):
            raise ValueError(f"{amount!r} is not a number")
        if not (0 < value <= MAX[coin]):
            raise ValueError(
                f"{coin} {value} is outside (0, {MAX[coin]}] — the venue's "
                "own maximum for one request")
        rows.append({"coin": coin, "amountStr": str(amount)})
    return client._request(
        "POST", ENDPOINT, signed=True,
        body={"adjustType": 1 if reduce else 0, "utaDemoApplyMoney": rows})


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--btc", default="")
    ap.add_argument("--usdt", default="")
    ap.add_argument("--reduce", action="store_true")
    args = ap.parse_args(argv)

    wanted: List[Tuple[str, str]] = []
    if args.btc:
        wanted.append(("BTC", args.btc))
    if args.usdt:
        wanted.append(("USDT", args.usdt))
    if not wanted:
        print("nothing asked for; --btc and/or --usdt", file=sys.stderr)
        return 2

    try:
        import config as _config
        from bybit_connection import BybitClient
    except Exception as exc:                                   # noqa: BLE001
        print(f"cannot import the stack ({exc}).\n"
              "Run it with the virtualenv the suite uses:\n"
              "    . .venv/bin/activate && python3 tools/fund_demo.py ...",
              file=sys.stderr)
        return 2

    cfg = _config.get_config_object()
    if getattr(cfg, "BYBIT_VENUE", "") != "demo":
        print(f"BYBIT_VENUE is {getattr(cfg, 'BYBIT_VENUE', None)!r}, not "
              "'demo'. Nothing was sent.", file=sys.stderr)
        return 1
    try:
        result = apply(BybitClient(config=cfg), wanted, reduce=args.reduce)
    except NotDemo as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"{'reduced' if args.reduce else 'applied'}: "
          + ", ".join(f"{c} {a}" for c, a in wanted))
    print(f"venue answered: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
