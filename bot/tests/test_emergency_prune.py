"""The emergency prune (a full volume frees its oldest closed day) must never delete evidence that has no verified off-volume copy. Default-deny; one day per interval; a verified
backup receipt is the only key; the sealed log still checks afterwards."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import md_seal as SL  # noqa: E402
import md_store as ST  # noqa: E402
from test_md_seal import DAY3, NOW3, make_store  # noqa: E402


@pytest.fixture
def world(tmp_path):
    md = make_store(tmp_path / "md")                           # days 1 and 2
    st = ST.MarketStore(md, commit_every=1, environment="mainnet")
    for i in range(3):
        st.append("frame", wall_ms=DAY3 + i * 1000, mono_s=float(i), category="linear", conn_id="c1", topic="tickers.BTCUSDT", payload=f'{{"i":{i},"d":3}}')
    st.close()
    return {"md": md, "tmp": tmp_path, "manifest": str(tmp_path / "off" / "m.jsonl"), "receipts": str(tmp_path / "off" / "r.jsonl"), "bk": str(tmp_path / "bk")}


def store(world):
    return ST.MarketStore(world["md"], commit_every=1, environment="mainnet")


def backup(world):
    os.makedirs(os.path.dirname(world["manifest"]), exist_ok=True)
    SL.seal(world["md"], world["manifest"], now_ms=NOW3)
    SL.backup(world["md"], world["bk"], world["receipts"], manifest=world["manifest"], now_ms=NOW3)


def test_default_is_deny_nothing_is_deleted_and_the_refusal_is_counted(world):
    st = store(world)
    before = st.segments()
    assert st.emergency_prune() == []
    assert st.segments() == before and st.emergency_prunes_denied == 1 and st.emergency_prunes == 0


def test_a_predicate_that_raises_or_says_no_is_a_no(world):
    st = store(world)
    assert st.emergency_prune(lambda n: False) == []

    def boom(n):
        raise RuntimeError("x")
    assert st.emergency_prune(boom) == [] and st.emergency_prunes_denied == 2 and len(st.segments()) == 3


def test_an_unbacked_day_is_refused_with_the_real_predicate(world):
    st = store(world)
    assert not SL.backed_up(world["md"], st.segments()[0], world["receipts"], world["bk"])
    assert st.emergency_prune(lambda n: SL.backed_up(world["md"], n, world["receipts"], world["bk"])) == []
    assert len(st.segments()) == 3


def test_a_backed_up_day_is_pruned_once_per_interval_and_the_seal_still_checks(world):
    backup(world)
    st = store(world)
    pred = lambda n: SL.backed_up(world["md"], n, world["receipts"], world["bk"])      # noqa: E731
    oldest = st.segments()[0]
    assert pred(oldest)
    assert st.emergency_prune(pred, min_interval_s=3600) == [oldest]
    assert st.emergency_prune(pred, min_interval_s=3600) == []                          # rate limited: the second closed day waits
    assert st.emergency_prunes == 1
    chk = SL.check(world["md"], world["manifest"], receipts=world["receipts"], backup=world["bk"], now_ms=NOW3)
    assert chk["ok"], chk.get("problems")


def test_a_tampered_backup_copy_is_not_a_key(world):
    backup(world)
    name = ST.MarketStore(world["md"], commit_every=1, environment="mainnet").segments()[0]
    with open(os.path.join(world["bk"], name), "ab") as fh:
        fh.write(b"x")
    assert not SL.backed_up(world["md"], name, world["receipts"], world["bk"])


def test_feed_never_emergency_prunes_when_retention_is_zero_or_no_predicate(world):
    import md_feed as F

    class Eng:
        lock = __import__("threading").RLock()
        def __init__(self, st): self.store = st
    st = store(world)
    st.min_free_bytes = 1 << 62                                                       # always "low"
    feed = F.Feed.__new__(F.Feed)
    feed.engine, feed.retention_errors, feed.emergency_may_prune = Eng(st), 0, None
    feed.cfg = F.FeedConfig(symbol="BTCUSDT", environment="mainnet", retention_days=0)
    assert feed.emergency_retention() == [] and st.emergency_prunes_denied == 0       # retention 0 = keep everything: not even asked
    feed.cfg = F.FeedConfig(symbol="BTCUSDT", environment="mainnet", retention_days=2)
    assert feed.emergency_retention() == [] and st.emergency_prunes_denied == 1       # no predicate: denied and counted
