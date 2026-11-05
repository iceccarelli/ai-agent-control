"""The proof that an unanswered order never landed is derived from the transport's
own worst case. Changing the transport's retries must change the window, never
silently outrun it (the window used to hard-code `attempts=4`)."""
from __future__ import annotations

import ast
import inspect
import os

import bybit_connection as BC
import order_recovery as OR

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_transports_default_retries_is_the_single_named_constant():
    default = inspect.signature(BC.BybitClient._request).parameters["retries"].default
    assert default == BC.DEFAULT_REQUEST_RETRIES == 3
    assert BC.BybitClient.REQUEST_ATTEMPTS == BC.DEFAULT_REQUEST_RETRIES + 1


def test_main_derives_its_attempts_from_the_transport_not_a_literal():
    tree = ast.parse(open(os.path.join(BOT, "main.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "absence_window_s":
            kw = {k.arg: k.value for k in node.keywords}
            assert isinstance(kw["attempts"], ast.Attribute) and kw["attempts"].attr == "REQUEST_ATTEMPTS", \
                "main.py must not hard-code the transport's attempt count"


def test_a_longer_transport_widens_the_window():
    base = OR.absence_window_s(request_timeout_s=10, recv_window_ms=5000,
                               attempts=BC.BybitClient.REQUEST_ATTEMPTS, max_wait_s=5)
    more = OR.absence_window_s(request_timeout_s=10, recv_window_ms=5000,
                               attempts=BC.BybitClient.REQUEST_ATTEMPTS + 2, max_wait_s=5)
    assert more > base


def test_the_transport_really_makes_retries_plus_one_attempts():
    """The loop the window is derived from: one try plus `retries` more."""
    assert "while attempt <= retries" in inspect.getsource(BC.BybitClient._request)
