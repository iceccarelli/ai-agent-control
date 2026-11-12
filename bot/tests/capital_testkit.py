"""Test helpers for the ANCHORED capital book (production never opens a bare book; see ms_capital_anchor). A test that drives the autopilot or the ``capital`` CLI against a root must have
created that root's lineage the way the owner does (GENESIS), in the per-test anchor directory the suite fixture ``_isolated_capital_anchor`` provides (never the real ~/.ai-agent-control)."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_capital as CC  # noqa: E402
import ms_capital_anchor as AN  # noqa: E402

USER_ID = 100000001                                   # what the fake Testnet venue states as the account (ms_fake_bybit_testnet.FakeBybitTestnet.user_id)
ACCOUNT = AN.account_ref_of("testnet", USER_ID)
ACCOUNT2 = AN.account_ref_of("testnet", USER_ID + 1)      # a second account (a second root of the SAME account would share the first one's lineage: that is the point)


def anchor_dir() -> str:
    return AN.anchor_dir_for(os.environ)


def anchored(root: str, account: str = ACCOUNT, *, create: bool = True) -> "CC.CapitalBook":
    """The production book for ``root``; its lineage is created (the owner's GENESIS, here by the library) when ``create`` and none exists."""
    adir = anchor_dir()
    try:
        return CC.CapitalBook.anchored(root, account_ref=account, venue="testnet", anchor_dir=adir)
    except CC.BookRefused as exc:
        if not create or exc.code not in ("ACCOUNT_NOT_INITIALISED", "BOOK_UNANCHORED"):
            raise
    CC.CapitalBook(root, AN.Anchor(root, account, "testnet", adir)).initialise({"test": True})
    return CC.CapitalBook.anchored(root, account_ref=account, venue="testnet", anchor_dir=adir)
