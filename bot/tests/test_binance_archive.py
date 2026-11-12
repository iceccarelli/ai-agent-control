"""binance_archive: the RESEARCH-ONLY reader of Binance's public data archive. Offline only: every fetch is injected, a socket guard fails any real connection, every directory is a tmp dir.

What these tests pin: a URL is built only from validated parts on the two archive hosts; a download is kept only if its bytes equal the sha256 in its own .CHECKSUM; a bad zip, a zip bomb, a path-traversal member, a foreign
checksum name and a malformed row are counted rejects and are never stored under the final name or repaired; a geo or policy refusal stops the run and is never retried; the store is private (0700/0600), outside git, and
re-verified on every read; and nothing in the module can place an order, read a credential or touch the environment.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import socket
import stat
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import binance_archive as BA  # noqa: E402

TOOLS = os.path.join(os.path.dirname(__file__), "..", "tools")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a test reached the network")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate"
KLINE_HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"
DAY = 86_400_000


def make_zip(member: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, text)
    return buf.getvalue()


def checksum_text(body: bytes, name: str) -> bytes:
    return f"{hashlib.sha256(body).hexdigest()}  {name}\n".encode()


def funding_csv(rows, header=True) -> str:
    lines = ([FUNDING_HEADER] if header else []) + [f"{t},{i},{r}" for t, i, r in rows]
    return "\n".join(lines) + "\n"


def kline_csv(rows, header=True) -> str:
    lines = [KLINE_HEADER] if header else []
    for t, o, h, l, c, v, qv in rows:
        lines.append(f"{t},{o},{h},{l},{c},{v},{t + DAY - 1},{qv},10,0,0,0")
    return "\n".join(lines) + "\n"


class FakeNet:
    """url -> (status, body); records every url asked for."""

    def __init__(self):
        self.files = {}
        self.calls = []

    def add(self, url, body=b"", status=200):
        self.files[url] = (status, body)

    def add_month(self, dataset, symbol, interval, month, csv_text, *, tamper_checksum=False, checksum_name=None):
        name = BA.data_path(dataset, symbol, interval, month).rsplit("/", 1)[-1]
        member = name[:-4] + ".csv"
        body = make_zip(member, csv_text)
        self.add(BA.data_url(dataset, symbol, interval, month), body)
        ck = checksum_text(body + (b"x" if tamper_checksum else b""), checksum_name or name)
        self.add(BA.data_url(dataset, symbol, interval, month, ext="zip.CHECKSUM"), ck)
        return body

    def __call__(self, url):
        self.calls.append(url)
        return self.files.get(url, (404, b""))


def archive(tmp_path, net, **kw):
    root = str(tmp_path / "store")
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("rps", 1000.0)
    return BA.Archive(root, fetch=net, free_bytes_fn=lambda p: 10 ** 12, **kw)


GOOD_FUNDING = funding_csv([(1704067200000, 8, "0.00037409"), (1704096000000, 8, "0.00027213"), (1704124800000, 8, "0.00010000")])
GOOD_KLINES = kline_csv([(1704067200000, "42000.0", "43000.0", "41000.0", "42500.0", "100.5", "4250000.0"),
                         (1704153600000, "42500.0", "43500.0", "41500.0", "43000.0", "90.1", "3900000.0")])


# ------------------------------------------------------------------------------------------------ URLs are built from validated parts only
def test_a_data_url_is_the_archive_host_plus_a_fixed_path():
    assert BA.data_path("fundingRate", "BTCUSDT", None, "2024-01") == "data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    assert BA.data_path("klines", "BTCUSDT", "1d", "2024-01") == "data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2024-01.zip"
    assert BA.data_path("spot_klines", "BTCUSDT", "1d", "2024-01") == "data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2024-01.zip"
    assert BA.data_url("markPriceKlines", "ETHUSDT", "1d", "2023-12") == "https://data.binance.vision/data/futures/um/monthly/markPriceKlines/ETHUSDT/1d/ETHUSDT-1d-2023-12.zip"
    assert BA.data_url("fundingRate", "BTCUSDT", None, "2024-01", ext="zip.CHECKSUM").endswith("BTCUSDT-fundingRate-2024-01.zip.CHECKSUM")


@pytest.mark.parametrize("args", [
    ("fundingRate", "btcusdt", None, "2024-01"),          # lower case
    ("fundingRate", "BTC/USDT", None, "2024-01"),         # a path separator
    ("fundingRate", "..", None, "2024-01"),
    ("fundingRate", "BTCUSDT", None, "2024-13"),          # month 13
    ("fundingRate", "BTCUSDT", None, "2024-1"),
    ("fundingRate", "BTCUSDT", None, "2024-01/../x"),
    ("fundingRate", "BTCUSDT", "1d", "2024-01"),          # funding has no interval
    ("klines", "BTCUSDT", None, "2024-01"),               # klines need one
    ("klines", "BTCUSDT", "1w", "2024-01"),               # not an allowed interval
    ("trades", "BTCUSDT", "1d", "2024-01"),               # not a registered dataset
    ("fundingRate", "A" * 30, None, "2024-01"),
])
def test_a_bad_part_is_refused_before_any_url_exists(args):
    with pytest.raises(BA.ArchiveError) as e:
        BA.data_path(*args)
    assert e.value.code == "BAD_ARGUMENT"


def test_the_listing_url_is_the_s3_endpoint_with_a_prefix_inside_the_archive():
    u = BA.listing_url("data/futures/um/monthly/fundingRate/", marker="data/futures/um/monthly/fundingRate/1000BONKUSDT/")
    assert u.startswith("https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?")
    assert "delimiter=%2F" in u and "prefix=data%2Ffutures%2Fum%2Fmonthly%2FfundingRate%2F" in u
    for bad in ("etc/passwd", "../data/", "data/../x/", "data/futures/um/monthly/fundingRate/ A", ""):
        with pytest.raises(BA.ArchiveError):
            BA.listing_url(bad)


# ------------------------------------------------------------------------------------------------ the transport only reaches the two archive hosts
@pytest.mark.parametrize("url", [
    "http://data.binance.vision/data/x.zip",                      # not https
    "https://evil.example/data/x.zip",
    "https://data.binance.vision.evil.example/data/x.zip",
    "https://data.binance.vision@evil.example/data/x.zip",
    "https://api.bybit.com/v5/market/time",                       # never a venue API
    "https://fapi.binance.com/fapi/v1/time",
    "https://data.binance.vision/etc/passwd",                     # a path outside data/
    "https://data.binance.vision/data/../secret",
    "https://data.binance.vision:8443/data/x.zip",
    "file:///etc/passwd",
    "https://evil.example/data.binance.vision?prefix=data%2Ffutures%2Fum%2Fmonthly%2FfundingRate%2F",      # a foreign host that copies the listing path
    "https://api.bybit.com/data.binance.vision?prefix=data%2Ffutures%2Fum%2Fmonthly%2FfundingRate%2F",
    "https://s3-ap-northeast-1.amazonaws.com/data/x.zip",                                                    # the listing host serves listings, not files
    "https://data.binance.vision/data.binance.vision?prefix=data%2F",                                        # the file host serves files, not listings
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data%2F&callback=x",                # a query key outside the four listing parameters
])
def test_the_transport_refuses_anything_but_the_two_archive_hosts(url):
    with pytest.raises(BA.ArchiveError) as e:
        BA.http_fetch(url)                                          # raises before any socket exists (the autouse guard would also fail the test)
    assert e.value.code == "HOST_NOT_ALLOWED"


def test_a_redirect_is_never_followed():
    assert BA._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/") is None


# ------------------------------------------------------------------------------------------------ the S3 listing
def _listing(prefixes=(), keys=(), truncated=False, next_marker=""):
    body = ['<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>data.binance.vision</Name>']
    body.append(f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>")
    if next_marker:
        body.append(f"<NextMarker>{next_marker}</NextMarker>")
    body += [f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes]
    body += [f"<Contents><Key>{k}</Key><Size>10</Size></Contents>" for k in keys]
    body.append("</ListBucketResult>")
    return "".join(body).encode()


def test_symbols_are_listed_across_pages(tmp_path):
    net = FakeNet()
    base = "data/futures/um/monthly/fundingRate/"
    net.add(BA.listing_url(base), _listing([base + "BTCUSDT/", base + "ETHUSDT/"], truncated=True, next_marker=base + "ETHUSDT/"))
    net.add(BA.listing_url(base, marker=base + "ETHUSDT/"), _listing([base + "SOLUSDT/", base + "1000PEPEUSDT/"]))
    a = archive(tmp_path, net)
    assert a.list_symbols("fundingRate") == ["1000PEPEUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_months_come_from_zip_keys_only_and_never_from_checksum_or_foreign_keys(tmp_path):
    net = FakeNet()
    pre = "data/futures/um/monthly/klines/BTCUSDT/1d/"
    net.add(BA.listing_url(pre, delimiter=False), _listing(keys=[pre + "BTCUSDT-1d-2024-02.zip", pre + "BTCUSDT-1d-2024-02.zip.CHECKSUM", pre + "BTCUSDT-1d-2024-01.zip",
                                                              pre + "BTCUSDT-1d-2024-01.zip.CHECKSUM", pre + "notes.txt", pre + "OTHERUSDT-1d-2024-03.zip"]))
    a = archive(tmp_path, net)
    assert a.list_months("klines", "BTCUSDT", "1d") == ["2024-01", "2024-02"]


def test_a_listing_that_never_ends_is_an_error_not_a_loop(tmp_path):
    net = FakeNet()
    base = "data/futures/um/monthly/fundingRate/"
    net.add(BA.listing_url(base), _listing([base + "A1USDT/"], truncated=True, next_marker=base + "A1USDT/"))
    net.add(BA.listing_url(base, marker=base + "A1USDT/"), _listing([base + "A1USDT/"], truncated=True, next_marker=base + "A1USDT/"))    # the marker does not advance
    with pytest.raises(BA.ArchiveError) as e:
        archive(tmp_path, net).list_symbols("fundingRate")
    assert e.value.code == "LISTING_DID_NOT_ADVANCE"


# ------------------------------------------------------------------------------------------------ one month: verified, stored privately, resumable
def test_a_verified_month_is_stored_with_its_checksum_a_manifest_row_and_private_modes(tmp_path):
    net = FakeNet()
    body = net.add_month("fundingRate", "BTCUSDT", None, "2024-01", GOOD_FUNDING)
    a = archive(tmp_path, net)
    r = a.download_month("fundingRate", "BTCUSDT", None, "2024-01")
    assert r["status"] == "OK" and r["sha256"] == hashlib.sha256(body).hexdigest() and r["bytes"] == len(body)
    p = a.file_path("fundingRate", "BTCUSDT", None, "2024-01")
    assert open(p, "rb").read() == body and open(p + ".CHECKSUM", "rb").read().startswith(hashlib.sha256(body).hexdigest().encode())
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode) == 0o700
    rows = [json.loads(l) for l in open(os.path.join(a.root, "MANIFEST.jsonl"), encoding="utf-8")]
    assert len(rows) == 1 and rows[0]["sha256"] == r["sha256"] and rows[0]["dataset"] == "fundingRate" and rows[0]["month"] == "2024-01"
    assert rows[0]["url"] == BA.data_url("fundingRate", "BTCUSDT", None, "2024-01") and "fetched_ms" in rows[0]


def test_a_month_already_on_disk_and_still_verifying_is_not_fetched_again(tmp_path):
    net = FakeNet()
    net.add_month("klines", "BTCUSDT", "1d", "2024-01", GOOD_KLINES)
    a = archive(tmp_path, net)
    assert a.download_month("klines", "BTCUSDT", "1d", "2024-01")["status"] == "OK"
    before = len(net.calls)
    assert a.download_month("klines", "BTCUSDT", "1d", "2024-01")["status"] == "EXISTS"
    assert len(net.calls) == before


def test_a_stored_month_that_no_longer_verifies_is_refetched_not_trusted(tmp_path):
    net = FakeNet()
    net.add_month("klines", "BTCUSDT", "1d", "2024-01", GOOD_KLINES)
    a = archive(tmp_path, net)
    a.download_month("klines", "BTCUSDT", "1d", "2024-01")
    p = a.file_path("klines", "BTCUSDT", "1d", "2024-01")
    os.chmod(p, 0o600)
    with open(p, "ab") as fh:
        fh.write(b"corrupt")
    r = a.download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert r["status"] == "OK"                                       # fetched again and replaced atomically
    assert BA.read_rows(p, "klines")                                   # and it verifies again


def test_a_download_whose_bytes_do_not_match_its_checksum_is_a_counted_reject_and_never_stored(tmp_path):
    net = FakeNet()
    net.add_month("fundingRate", "BTCUSDT", None, "2024-01", GOOD_FUNDING, tamper_checksum=True)
    a = archive(tmp_path, net)
    r = a.download_month("fundingRate", "BTCUSDT", None, "2024-01")
    assert r["status"] == "REJECT" and r["code"] == "CHECKSUM_MISMATCH"
    assert not os.path.exists(a.file_path("fundingRate", "BTCUSDT", None, "2024-01"))
    rej = [json.loads(l) for l in open(os.path.join(a.root, "REJECTS.jsonl"), encoding="utf-8")]
    assert rej[0]["code"] == "CHECKSUM_MISMATCH" and rej[0]["month"] == "2024-01"


def test_a_checksum_that_names_another_file_is_rejected(tmp_path):
    net = FakeNet()
    net.add_month("fundingRate", "BTCUSDT", None, "2024-01", GOOD_FUNDING, checksum_name="ETHUSDT-fundingRate-2024-01.zip")
    r = archive(tmp_path, net).download_month("fundingRate", "BTCUSDT", None, "2024-01")
    assert r["status"] == "REJECT" and r["code"] == "CHECKSUM_NAME_MISMATCH"


@pytest.mark.parametrize("body", [b"", b"\n", b"not a checksum\n", b"zz" * 32 + b"  BTCUSDT-fundingRate-2024-01.zip\n", b"a" * 64 + b"\n"])
def test_a_malformed_checksum_file_is_rejected(tmp_path, body):
    net = FakeNet()
    net.add_month("fundingRate", "BTCUSDT", None, "2024-01", GOOD_FUNDING)
    net.add(BA.data_url("fundingRate", "BTCUSDT", None, "2024-01", ext="zip.CHECKSUM"), body)
    r = archive(tmp_path, net).download_month("fundingRate", "BTCUSDT", None, "2024-01")
    assert r["status"] == "REJECT" and r["code"] in ("CHECKSUM_MALFORMED", "CHECKSUM_NAME_MISMATCH")


def test_a_zip_with_no_checksum_published_is_rejected(tmp_path):
    net = FakeNet()
    net.add(BA.data_url("fundingRate", "BTCUSDT", None, "2024-01"), make_zip("BTCUSDT-fundingRate-2024-01.csv", GOOD_FUNDING))
    r = archive(tmp_path, net).download_month("fundingRate", "BTCUSDT", None, "2024-01")
    assert r["status"] == "REJECT" and r["code"] == "CHECKSUM_UNAVAILABLE"


def test_a_month_the_archive_does_not_have_is_absent_and_remembered(tmp_path):
    net = FakeNet()
    a = archive(tmp_path, net)
    r = a.download_month("klines", "BTCUSDT", "1d", "2019-01")
    assert r["status"] == "ABSENT"
    n = len(net.calls)
    assert a.download_month("klines", "BTCUSDT", "1d", "2019-01")["status"] == "ABSENT" and len(net.calls) == n        # remembered: no second probe
    assert not os.path.exists(a.file_path("klines", "BTCUSDT", "1d", "2019-01"))


@pytest.mark.parametrize("status", [403, 451])
def test_a_geo_or_policy_refusal_stops_the_run_and_is_never_retried(tmp_path, status):
    net = FakeNet()
    net.add(BA.data_url("klines", "BTCUSDT", "1d", "2024-01"), b"", status=status)
    a = archive(tmp_path, net)
    with pytest.raises(BA.ArchiveError) as e:
        a.download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert e.value.code == "REFUSED_BY_ARCHIVE" and str(status) in str(e.value)
    assert len(net.calls) == 1                                       # not retried; and not 'fixed' by another host


def test_a_rate_limit_is_retried_with_backoff_then_succeeds(tmp_path):
    slept = []
    state = {"n": 0}
    good = FakeNet()
    good.add_month("klines", "BTCUSDT", "1d", "2024-01", GOOD_KLINES)

    def flaky(url):
        if url.endswith(".zip") and state["n"] < 2:
            state["n"] += 1
            return 429, b""
        return good(url)
    a = archive(tmp_path, flaky, sleep=slept.append)
    assert a.download_month("klines", "BTCUSDT", "1d", "2024-01")["status"] == "OK"
    assert state["n"] == 2 and len(slept) >= 2 and slept[1] > slept[0] > 0     # exponential


def test_a_server_error_that_never_clears_is_an_error_after_the_bounded_retries(tmp_path):
    net = FakeNet()
    net.add(BA.data_url("klines", "BTCUSDT", "1d", "2024-01"), b"", status=503)
    a = archive(tmp_path, net, retries=3)
    with pytest.raises(BA.ArchiveError) as e:
        a.download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert e.value.code == "HTTP_ERROR" and len(net.calls) == 4       # one try + three retries


# ------------------------------------------------------------------------------------------------ hostile or broken zips
def _put_raw_zip(net, name, body, dataset="klines", interval="1d"):
    net.add(BA.data_url(dataset, "BTCUSDT", interval, "2024-01"), body)
    net.add(BA.data_url(dataset, "BTCUSDT", interval, "2024-01", ext="zip.CHECKSUM"), checksum_text(body, name))


def test_not_a_zip_is_rejected(tmp_path):
    net = FakeNet()
    _put_raw_zip(net, "BTCUSDT-1d-2024-01.zip", b"this is not a zip file at all")
    r = archive(tmp_path, net).download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert r["status"] == "REJECT" and r["code"] == "ZIP_INVALID"


def test_a_zip_with_the_wrong_member_name_or_several_members_is_rejected(tmp_path):
    for members in ({"other.csv": GOOD_KLINES}, {"BTCUSDT-1d-2024-01.csv": GOOD_KLINES, "extra.csv": "x"}, {"../escape.csv": GOOD_KLINES}, {"/abs.csv": GOOD_KLINES}):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for k, v in members.items():
                z.writestr(k, v)
        net = FakeNet()
        _put_raw_zip(net, "BTCUSDT-1d-2024-01.zip", buf.getvalue())
        r = archive(tmp_path / str(len(members)) / next(iter(members)).replace("/", "_"), net).download_month("klines", "BTCUSDT", "1d", "2024-01")
        assert r["status"] == "REJECT" and r["code"] == "ZIP_MEMBER_INVALID", members


def test_a_zip_bomb_is_rejected_before_it_is_held_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(BA, "MAX_CSV_BYTES", 10_000)
    net = FakeNet()
    _put_raw_zip(net, "BTCUSDT-1d-2024-01.zip", make_zip("BTCUSDT-1d-2024-01.csv", "0" * 2_000_000))
    r = archive(tmp_path, net).download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert r["status"] == "REJECT" and r["code"] == "ZIP_TOO_LARGE"


def test_an_oversized_download_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(BA, "MAX_ZIP_BYTES", 100)
    net = FakeNet()
    net.add_month("klines", "BTCUSDT", "1d", "2024-01", GOOD_KLINES)
    r = archive(tmp_path, net).download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert r["status"] == "REJECT" and r["code"] == "DOWNLOAD_TOO_LARGE"


def test_rows_that_do_not_parse_make_the_month_a_reject_not_a_repair(tmp_path):
    net = FakeNet()
    bad = funding_csv([(1704067200000, 8, "0.0001"), (1704067200000, 8, "0.0002")])           # a duplicate time
    net.add_month("fundingRate", "BTCUSDT", None, "2024-01", bad)
    r = archive(tmp_path, net).download_month("fundingRate", "BTCUSDT", None, "2024-01")
    assert r["status"] == "REJECT" and r["code"] == "ROWS_INVALID"
    assert not os.path.exists(archive(tmp_path, net).file_path("fundingRate", "BTCUSDT", None, "2024-01"))


# ------------------------------------------------------------------------------------------------ row parsing
def test_funding_rows_parse_with_or_without_a_header():
    for hdr in (True, False):
        rows = BA.parse_rows("fundingRate", funding_csv([(1704067200000, 8, "0.00037409"), (1704096000000, 4, "-0.0001")], header=hdr))
        assert rows == [(1704067200000, 8, 0.00037409), (1704096000000, 4, -0.0001)]


def test_kline_rows_parse_with_or_without_a_header_and_keep_volume():
    for hdr in (True, False):
        rows = BA.parse_rows("klines", kline_csv([(1704067200000, "1.5", "2.0", "1.0", "1.8", "10", "17.5")], header=hdr))
        t, o, h, l, c, v, ct, qv, n = rows[0]
        assert (t, o, h, l, c, v, qv, n) == (1704067200000, 1.5, 2.0, 1.0, 1.8, 10.0, 17.5, 10) and ct == t + DAY - 1


@pytest.mark.parametrize("text", [
    funding_csv([(1704067200000, 8, "nan")]),
    funding_csv([(1704067200000, 8, "inf")]),
    funding_csv([(1704067200000, 0, "0.0001")]),                       # interval 0
    funding_csv([(1704067200000, 7, "0.0001")]),                       # an interval the archive never uses
    funding_csv([(1704067200000, 8, "0.0001"), (1704067100000, 8, "0.0001")]),      # time goes backwards
    funding_csv([(-5, 8, "0.0001")]),
    funding_csv([(1704067200000, 8, "5.0")]),                          # a rate of 500%: not a funding rate
    "calc_time,funding_interval_hours\n1704067200000,8\n",             # missing column
    "",
])
def test_bad_funding_rows_are_refused(text):
    with pytest.raises(BA.ArchiveError) as e:
        BA.parse_rows("fundingRate", text)
    assert e.value.code == "ROWS_INVALID"


@pytest.mark.parametrize("row", [
    (1704067200000, "nan", "2.0", "1.0", "1.8", "10", "17.5"),
    (1704067200000, "1.5", "2.0", "1.0", "-1.8", "10", "17.5"),         # a negative price
    (1704067200000, "1.5", "2.0", "1.0", "0", "10", "17.5"),            # a zero price
    (1704067200000, "1.5", "1.0", "2.0", "1.8", "10", "17.5"),          # high < low
    (1704067200000, "1.5", "2.0", "1.0", "1.8", "-10", "17.5"),         # negative volume
    (1704067200000, "1.5", "2.0", "1.0", "1.8", "10", "inf"),
])
def test_bad_kline_rows_are_refused(row):
    with pytest.raises(BA.ArchiveError) as e:
        BA.parse_rows("klines", kline_csv([row]))
    assert e.value.code == "ROWS_INVALID"


def test_kline_times_must_strictly_increase():
    rows = [(1704153600000, "1", "2", "1", "1.5", "1", "1"), (1704067200000, "1", "2", "1", "1.5", "1", "1")]
    with pytest.raises(BA.ArchiveError):
        BA.parse_rows("klines", kline_csv(rows))


def test_the_row_error_names_the_line(tmp_path):
    with pytest.raises(BA.ArchiveError) as e:
        BA.parse_rows("fundingRate", funding_csv([(1704067200000, 8, "0.0001"), (1704096000000, 8, "nan")]))
    assert "line 3" in str(e.value)                                     # the header is line 1


# ------------------------------------------------------------------------------------------------ reading what is stored
def test_a_stored_month_is_reverified_against_its_checksum_on_every_read(tmp_path):
    net = FakeNet()
    net.add_month("fundingRate", "BTCUSDT", None, "2024-01", GOOD_FUNDING)
    a = archive(tmp_path, net)
    a.download_month("fundingRate", "BTCUSDT", None, "2024-01")
    p = a.file_path("fundingRate", "BTCUSDT", None, "2024-01")
    assert len(BA.read_rows(p, "fundingRate")) == 3
    with open(p, "r+b") as fh:                                          # a byte flips on disk
        fh.seek(40)
        fh.write(b"\x00")
    with pytest.raises(BA.ArchiveError) as e:
        BA.read_rows(p, "fundingRate")
    assert e.value.code == "STORE_CHECKSUM_MISMATCH"


def test_a_stored_month_without_its_checksum_file_is_not_read(tmp_path):
    net = FakeNet()
    net.add_month("fundingRate", "BTCUSDT", None, "2024-01", GOOD_FUNDING)
    a = archive(tmp_path, net)
    a.download_month("fundingRate", "BTCUSDT", None, "2024-01")
    p = a.file_path("fundingRate", "BTCUSDT", None, "2024-01")
    os.remove(p + ".CHECKSUM")
    with pytest.raises(BA.ArchiveError) as e:
        BA.read_rows(p, "fundingRate")
    assert e.value.code == "STORE_CHECKSUM_MISSING"


# ------------------------------------------------------------------------------------------------ a window that stops where the protocol stops
def _month_klines(month):
    start, _end = BA.month_bounds_ms(month)
    return kline_csv([(start, "42000.0", "43000.0", "41000.0", "42500.0", "100.5", "4250000.0"), (start + DAY, "42500.0", "43500.0", "41500.0", "43000.0", "90.1", "3900000.0")])


def test_months_after_the_until_month_are_never_requested(tmp_path):
    net = FakeNet()
    for m in ("2024-08", "2024-09", "2024-10", "2024-11"):
        net.add_month("klines", "BTCUSDT", "1d", m, _month_klines(m))
    a = archive(tmp_path, net)
    out = a.download_range("klines", "BTCUSDT", "1d", ["2024-08", "2024-09", "2024-10", "2024-11"], until_month="2024-09")
    assert [o["month"] for o in out] == ["2024-08", "2024-09"] and all(o["status"] == "OK" for o in out)
    assert not any("2024-10" in c or "2024-11" in c for c in net.calls)


def test_a_range_without_an_until_month_is_refused(tmp_path):
    with pytest.raises(BA.ArchiveError) as e:
        archive(tmp_path, FakeNet()).download_range("klines", "BTCUSDT", "1d", ["2024-08"], until_month=None)
    assert e.value.code == "BAD_ARGUMENT"


# ------------------------------------------------------------------------------------------------ the store itself
def test_the_store_is_refused_inside_a_git_work_tree(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(BA.ArchiveError) as e:
        BA.Archive(str(tmp_path / "repo" / "data"), fetch=FakeNet(), free_bytes_fn=lambda p: 10 ** 12)
    assert e.value.code == "ROOT_NOT_ALLOWED"


def test_the_store_is_refused_when_it_is_a_symlink_or_group_accessible_or_nearly_full(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(BA.ArchiveError):
        BA.Archive(str(link), fetch=FakeNet(), free_bytes_fn=lambda p: 10 ** 12)
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    os.chmod(open_dir, 0o755)
    with pytest.raises(BA.ArchiveError):
        BA.Archive(str(open_dir), fetch=FakeNet(), free_bytes_fn=lambda p: 10 ** 12)
    with pytest.raises(BA.ArchiveError) as e:
        BA.Archive(str(tmp_path / "small"), fetch=FakeNet(), free_bytes_fn=lambda p: 1000)
    assert e.value.code == "ROOT_NOT_ALLOWED" and "free space" in str(e.value)


def test_requests_are_spaced_by_the_rate_limit():
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s
    bucket = BA.Throttle(rate=2.0, burst=1, monotonic=lambda: now[0], sleep=sleep)
    for _ in range(4):
        bucket.acquire()
    assert sum(sleeps) == pytest.approx(1.5)                            # 3 waits of 0.5 s after the first free token


# ------------------------------------------------------------------------------------------------ structure: no order path, no credentials, no environment, no shell
def _imports(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module.split(".")[0])
    return out


def test_the_module_has_no_order_credential_environment_or_shell_machinery():
    src = open(os.path.join(TOOLS, "binance_archive.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    imps = _imports(tree)
    assert not imps & {"hmac", "bybit_connection", "carry_broker", "entry_authority", "requests", "socket", "subprocess", "secrets", "venues", "runtime_capability", "config"}, imps
    assert "os.environ" not in src and "getenv" not in src and "subprocess" not in src and "eval(" not in src and "exec(" not in src and "__import__" not in src
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"post", "put", "delete", "patch"}, calls
    for n in ast.walk(tree):                                              # one method, GET, and only through the transport
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            assert n.value not in ("POST", "PUT", "DELETE", "PATCH")
    assert "api.bybit.com" not in src and "fapi.binance.com" not in src and "api.binance.com" not in src


def test_the_module_names_itself_research_only_and_not_a_venue():
    src = open(os.path.join(TOOLS, "binance_archive.py"), encoding="utf-8").read()
    assert "RESEARCH ONLY" in src and "never a venue" in src
    import venues
    assert "binance" not in " ".join(str(h) for h in getattr(venues, "rest_hosts")()).lower()

def test_a_row_outside_the_month_its_file_is_named_for_is_a_reject_and_is_never_read(tmp_path):
    net = FakeNet()
    net.add_month("klines", "BTCUSDT", "1d", "2024-02", GOOD_KLINES)               # January rows in a file named for February
    a = archive(tmp_path, net)
    r = a.download_month("klines", "BTCUSDT", "1d", "2024-02")
    assert r["status"] == "REJECT" and r["code"] == "ROWS_INVALID"
    assert not os.path.exists(a.file_path("klines", "BTCUSDT", "1d", "2024-02"))
    # a stored file whose rows do not belong to its month is refused on read too (a file moved or renamed by hand)
    net2 = FakeNet()
    net2.add_month("klines", "BTCUSDT", "1d", "2024-01", GOOD_KLINES)
    b = archive(tmp_path / "b", net2)
    b.download_month("klines", "BTCUSDT", "1d", "2024-01")
    p = b.file_path("klines", "BTCUSDT", "1d", "2024-01")
    q = b.file_path("klines", "BTCUSDT", "1d", "2024-03")
    os.makedirs(os.path.dirname(q), exist_ok=True)
    import shutil
    shutil.copy(p, q)
    shutil.copy(p + ".CHECKSUM", q + ".CHECKSUM")
    with pytest.raises(BA.ArchiveError):
        BA.read_rows(q, "klines")                                                   # the checksum names January: refused before any row is read


def test_month_bounds_are_utc_calendar_months():
    assert BA.month_bounds_ms("2024-01") == (1704067200000, 1706745600000)
    assert BA.month_bounds_ms("2023-12") == (1701388800000, 1704067200000)
    assert BA.month_bounds_ms("2024-02")[1] - BA.month_bounds_ms("2024-02")[0] == 29 * DAY
    with pytest.raises(BA.ArchiveError):
        BA.month_bounds_ms("2024-13")


def test_parallel_workers_creating_the_same_new_directories_do_not_collide(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    net = FakeNet()
    months = [f"2023-{m:02d}" for m in range(1, 13)] + [f"2024-{m:02d}" for m in range(1, 5)]
    for m in months:
        net.add_month("klines", "BTCUSDT", "1d", m, _month_klines(m))
    a = archive(tmp_path, net)
    with ThreadPoolExecutor(max_workers=16) as pool:
        out = list(pool.map(lambda m: a.download_month("klines", "BTCUSDT", "1d", m), months))
    assert [o["status"] for o in out] == ["OK"] * 16
    leaf = os.path.dirname(a.file_path("klines", "BTCUSDT", "1d", "2023-01"))
    assert stat.S_IMODE(os.stat(leaf).st_mode) == 0o700 and stat.S_IMODE(os.stat(os.path.join(a.root, "data")).st_mode) == 0o700
    assert len(open(os.path.join(a.root, "MANIFEST.jsonl"), encoding="utf-8").read().splitlines()) == 16


def test_a_symlink_where_a_directory_belongs_is_refused(tmp_path):
    net = FakeNet()
    net.add_month("klines", "BTCUSDT", "1d", "2024-01", GOOD_KLINES)
    a = archive(tmp_path, net)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), os.path.join(a.root, "data"))
    with pytest.raises(BA.ArchiveError) as e:
        a.download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert e.value.code == "ROOT_NOT_ALLOWED"


def test_a_transient_network_error_is_retried_with_growing_pauses_and_a_lasting_one_is_reported(tmp_path):
    good = FakeNet()
    good.add_month("klines", "BTCUSDT", "1d", "2024-01", GOOD_KLINES)
    state = {"n": 0}
    slept = []

    def flaky(url):
        if state["n"] < 2:
            state["n"] += 1
            raise BA.ArchiveError("NETWORK_ERROR", "request failed (URLError: timeout)")
        return good(url)
    a = archive(tmp_path, flaky, sleep=slept.append)
    assert a.download_month("klines", "BTCUSDT", "1d", "2024-01")["status"] == "OK"
    assert state["n"] == 2 and slept[1] > slept[0] > 0

    calls = []

    def dead(url):
        calls.append(url)
        raise BA.ArchiveError("NETWORK_ERROR", "request failed (URLError: refused)")
    b = archive(tmp_path / "b", dead, retries=2)
    with pytest.raises(BA.ArchiveError) as e:
        b.download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert e.value.code == "NETWORK_ERROR" and len(calls) == 3                            # one try and two retries


def test_a_blocked_host_is_never_retried_as_if_it_were_a_network_blip(tmp_path):
    calls = []

    def blocked(url):
        calls.append(url)
        raise BA.ArchiveError("HOST_NOT_ALLOWED", "no")
    with pytest.raises(BA.ArchiveError) as e:
        archive(tmp_path, blocked).download_month("klines", "BTCUSDT", "1d", "2024-01")
    assert e.value.code == "HOST_NOT_ALLOWED" and len(calls) == 1
