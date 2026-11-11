#!/usr/bin/env python3
"""TESTNET-ONLY experiment harness: how does Bybit V5 linear-perpetual ORDER
IDENTITY (orderLinkId) behave, recorded as evidence.

It exists so the owner can decide, from recorded testnet observations rather
than from the documentation, whether re-sending an orderLinkId is safe. It
RECORDS; it does not interpret. The only text it prints beyond the raw record
is a short table of observed facts, and every such table carries the sentence
"testnet behaviour is not mainnet proof".

WHAT IT NEVER DOES
  * talk to any host but https://api-testnet.bybit.com. The host is checked
    on the client object it will use AND on the URL of every single request,
    so a base_url changed after the start-up check still cannot carry a
    request anywhere else. There is no fallback host.
  * start without --i-understand-this-places-testnet-orders, a testnet
    configuration, no live authorisation of any kind (LIVE_TRADING_ACK present
    at all => refusal; its value is never read into any output), credentials
    present (names only are ever shown), a readable one-way position mode, a
    readable instrument and a CLEAN start (no open orders, flat position).
  * send a request to any path outside a fixed allowlist (order create,
    cancel, realtime, history; execution list; position list; instruments-info;
    tickers; account info). /v5/position/switch-mode, cancel-all, leverage and
    every other endpoint are refused locally before anything is sent.
  * send a Buy that is not reduce-only, an order above the notional limit, or
    an order beyond the order ceiling.

HARD LIMITS (constants below; the CLI can only LOWER them, never raise)
  max notional per order, max total order-create requests (ceiling, with a
  reserve kept for the final flat step), max wall-clock duration, max
  experiments run. Each order is sized from the instrument's min qty and lot
  step read from the venue, then sanity-checked against the limits.

EVERY RUN ENDS WITH A MANDATORY FLAT-POSITION AND OPEN-ORDER CHECK. If the
account is not flat the tool makes ONE bounded reduce-only close attempt,
re-checks, and if it is still not flat exits 5 with a loud NOT FLAT report.

EXIT CODES
  0  finished; flat and clean verified
  2  cannot start (bad arguments, no usable stack)
  3  refused before any order (pre-conditions not met); nothing was placed
  4  AMBIGUITY HALT: an ambiguous venue state was detected; venue state was
     captured, the final flat step ran and the account is flat
  5  NOT FLAT: the account is not verified flat/clean at the end. LOUD.
  6  LIMIT/GUARD HALT: a hard limit or a local safety guard stopped the run;
     the final flat step ran and the account is flat

PHASES  --phase full (default) | submit | recover
  submit  places ONE far-from-market post-only limit order (experiment E8) and
          persists a manifest, then exits leaving that single resting order.
          Kill/leave the process as you like, then run --phase recover in a
          NEW process: it reads the manifest, finds the order purely by
          orderLinkId, cancels it, and ends with the flat step.
  recover also reconciles anything the manifest says this tool placed.

--dry-run makes ZERO network calls and prints the plan, limits and commands.
--self-check-offline runs the whole harness against a FAKE venue (no network).

NOT PROOF: testnet behaviour is not mainnet proof.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import bybit_connection as bc  # noqa: E402

SCHEMA = "close_testnet_experiment/1"
NOT_PROOF = "testnet behaviour is not mainnet proof"

# ---------------------------------------------------------------------------
# the one host, and the hard limits
# ---------------------------------------------------------------------------

#: Spelled out here AND taken from bybit_connection; they must agree or this
#: module does not import. The harness has no other host in it.
TESTNET_BASE = "https://api-testnet.bybit.com"
TESTNET_HOSTNAME = "api-testnet.bybit.com"
assert bc.TESTNET_REST == TESTNET_BASE
assert bc.VENUE_HOSTS["testnet"] == TESTNET_HOSTNAME

#: ceilings: constants. The CLI lowers, never raises.
HARD_MAX_NOTIONAL_USD = 20.0
HARD_MAX_ORDERS = 30            # order-create requests, all experiments
EMERGENCY_RESERVE = 2           # of those, kept for the final flat step
HARD_MAX_DURATION_S = 1800
HARD_MAX_EXPERIMENTS = 11
HARD_MAX_REQUESTS = 1500
FINAL_EXTRA_REQUESTS = 300      # extra reads/cancels allowed in the final step
HARD_MAX_CANCELS = 40
DEFAULT_MAX_DURATION_S = 1200
FINAL_STEP_BUDGET_S = 180
EMERGENCY_QTY_MULT = 3          # emergency close: at most this many x qmax
MAX_CLOSE_CHUNKS = 3
NOTIONAL_PRICE_BUFFER = Decimal("1.10")

POLL_INTERVAL_S = 1.0
VISIBLE_POLLS = 15
EXEC_POLLS = 10
POS_POLLS = 10
E7_MAX_POLLS = 40
E8_DELAY_S = 5.0

EXIT_OK, EXIT_CANNOT_START, EXIT_REFUSED = 0, 2, 3
EXIT_AMBIGUITY, EXIT_NOT_FLAT, EXIT_LIMIT = 4, 5, 6

LINK_PREFIX = "XP"

#: The ONLY paths this tool will ever send a request to, and the method each
#: takes. Anything else is refused locally before the client is touched.
ALLOWED_ENDPOINTS: Dict[str, str] = {
    "/v5/order/create": "POST",
    "/v5/order/cancel": "POST",
    "/v5/order/realtime": "GET",
    "/v5/order/history": "GET",
    "/v5/execution/list": "GET",
    "/v5/position/list": "GET",
    "/v5/market/instruments-info": "GET",
    "/v5/market/tickers": "GET",
    "/v5/account/info": "GET",
}
PUBLIC_ENDPOINTS = frozenset({"/v5/market/instruments-info",
                              "/v5/market/tickers"})
REALTIME, HISTORY = "/v5/order/realtime", "/v5/order/history"
CREATE, CANCEL = "/v5/order/create", "/v5/order/cancel"
EXECUTIONS, POSITIONS = "/v5/execution/list", "/v5/position/list"
INSTRUMENTS, TICKERS = "/v5/market/instruments-info", "/v5/market/tickers"
ACCOUNT = "/v5/account/info"

OPEN_STATUSES = frozenset(bc.OPEN_ORDER_STATUS) | {"Created"}
TERMINAL_STATUSES = frozenset(bc.TERMINAL_ORDER_STATUS)
#: Statuses this tool has a defined expectation for. Anything else is ambiguity.
EXPECTED_STATUSES = frozenset({"Created", "New", "PartiallyFilled", "Filled",
                               "Cancelled", "Rejected",
                               "PartiallyFilledCanceled"})

#: Venue behaviours this tool had to ASSUME. Not verified; printed in the
#: report and in --dry-run so the owner sees them before running.
ASSUMPTIONS = (
    "UNVERIFIED: a one-way account lists exactly positionIdx 0 rows on "
    "/v5/position/list and a hedge-mode account lists positionIdx 1/2 rows "
    "(mode is inferred from the position list; /v5/account/info does not "
    "state it).",
    "UNVERIFIED: GET /v5/order/realtime without openOnly returns open orders "
    "(and possibly recent closed ones; closed rows are ignored for the "
    "clean-start check).",
    "UNVERIFIED: GET /v5/order/realtime and /v5/order/history accept an "
    "orderLinkId filter together with category+symbol and return a list.",
    "UNVERIFIED: a reduce-only market Buy with no position is rejected "
    "synchronously with a non-zero retCode (E6); if instead it is accepted, "
    "the status is recorded as observed.",
    "UNVERIFIED: a post-only limit Sell about 5-6 percent above the last "
    "price is accepted and rests (inside the venue's price band) on the "
    "testnet BTC/USDT-style linear symbols; if it is rejected the run halts.",
    "UNVERIFIED: a minimum-size market order fills fully at once; a partial "
    "fill is never expected and is treated as ambiguity.",
    "UNVERIFIED: /v5/execution/list filtered by orderLinkId lists every "
    "fill of the order (execType Trade) and its rows can lag the order row; "
    "the tool polls up to a bound before declaring a mismatch.",
    "UNVERIFIED: the real BybitClient called with retries=0 performs exactly "
    "one HTTP attempt per call and sends nothing else. (With retries>0 it can "
    "internally call /v5/market/time on retCode 10002; this tool never "
    "asks for retries.)",
    "UNVERIFIED: a duplicate orderLinkId is answered by a non-zero retCode "
    "(the repo's DUPLICATE_LINK_ID_RET_CODES lists 110072, 170130, 170141). "
    "The harness records whatever comes back and does not rely on it.",
)


class Refused(Exception):
    """A pre-condition is not met. Nothing was placed."""


class Halt(Exception):
    """Stop everything, reconcile, report."""
    exit_code = EXIT_AMBIGUITY

    def __init__(self, code: str, detail: str, **captured: Any) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.captured = captured


class Ambiguity(Halt):
    exit_code = EXIT_AMBIGUITY


class LimitHalt(Halt):
    exit_code = EXIT_LIMIT


class UnsafeRequest(LimitHalt):
    """A request this tool refuses to send (local guard). Nothing was sent."""


# ---------------------------------------------------------------------------
# limits (can only be lowered)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    max_notional_usd: float = HARD_MAX_NOTIONAL_USD
    max_orders: int = HARD_MAX_ORDERS
    max_duration_s: int = DEFAULT_MAX_DURATION_S
    max_experiments: int = HARD_MAX_EXPERIMENTS

    @staticmethod
    def resolve(notional: Optional[float] = None, orders: Optional[int] = None,
                duration: Optional[int] = None,
                experiments: Optional[int] = None) -> "Limits":
        """Validate requested limits. A value above its ceiling is REFUSED
        (not clamped, so the operator sees that it did not take effect); so is
        anything not positive and finite."""
        def pick(name, value, ceiling, default, integer):
            if value is None:
                return default
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name}={value!r} is not a number")
            if not math.isfinite(num) or num <= 0:
                raise ValueError(f"{name}={value!r} must be positive and finite")
            if integer and num != int(num):
                raise ValueError(f"{name}={value!r} must be a whole number")
            if num > ceiling:
                raise ValueError(
                    f"{name}={value!r} is above the hard ceiling {ceiling}; "
                    "limits can only be lowered")
            return int(num) if integer else num
        return Limits(
            pick("--max-notional-usd", notional, HARD_MAX_NOTIONAL_USD,
                 HARD_MAX_NOTIONAL_USD, False),
            pick("--max-orders", orders, HARD_MAX_ORDERS, HARD_MAX_ORDERS, True),
            pick("--max-duration-s", duration, HARD_MAX_DURATION_S,
                 DEFAULT_MAX_DURATION_S, True),
            pick("--max-experiments", experiments, HARD_MAX_EXPERIMENTS,
                 HARD_MAX_EXPERIMENTS, True))

    def as_dict(self) -> Dict[str, Any]:
        return {"max_notional_usd": self.max_notional_usd,
                "max_orders": self.max_orders,
                "max_duration_s": self.max_duration_s,
                "max_experiments": self.max_experiments,
                "ceilings": {"notional_usd": HARD_MAX_NOTIONAL_USD,
                             "orders": HARD_MAX_ORDERS,
                             "duration_s": HARD_MAX_DURATION_S,
                             "experiments": HARD_MAX_EXPERIMENTS,
                             "requests": HARD_MAX_REQUESTS,
                             "cancels": HARD_MAX_CANCELS,
                             "emergency_reserve_orders": EMERGENCY_RESERVE}}


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = frozenset({
    "apikey", "apisecret", "secret", "xbapiapikey", "xbapisign",
    "xbapitimestamp", "xbapirecvwindow", "xbapisigntype", "sign",
    "signature", "authorization", "cookie", "setcookie", "token",
    "accesstoken", "refreshtoken", "password", "bybitapikey",
    "bybitapisecret", "livetradingack"})
REDACTED = "***REDACTED***"


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


class Redactor:
    """Removes credential material from anything before it is written or
    printed: values under credential/signature/header keys at any depth, and
    any occurrence of a configured secret VALUE inside any string (raw and
    JSON-escaped), including inside dict keys."""

    def __init__(self, secrets: Sequence[Any] = ()) -> None:
        self._needles: List[str] = []
        for secret in secrets:
            self.add_secret(secret)

    def add_secret(self, secret: Any) -> None:
        text = "" if secret is None else str(secret)
        if not text:
            return
        for variant in (text, json.dumps(text)[1:-1]):
            if variant and variant not in self._needles:
                self._needles.append(variant)
        self._needles.sort(key=len, reverse=True)

    def scrub_text(self, text: str) -> str:
        for needle in self._needles:
            if needle in text:
                text = text.replace(needle, REDACTED)
        return text

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, Mapping):
            return {self.scrub_text(str(k)):
                    (REDACTED if _norm_key(k) in _SENSITIVE_KEYS
                     else self.scrub(v)) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set, frozenset)):
            return [self.scrub(v) for v in obj]
        if isinstance(obj, str):
            return self.scrub_text(obj)
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        return self.scrub_text(str(obj))

    def dumps(self, obj: Any) -> str:
        return self.scrub_text(json.dumps(self.scrub(obj), sort_keys=True,
                                          default=str))


class Console:
    """Every line shown to the operator goes through the redactor."""

    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor

    def out(self, text: str = "") -> None:
        print(self.redactor.scrub_text(str(text)), file=sys.stdout)

    def err(self, text: str = "") -> None:
        print(self.redactor.scrub_text(str(text)), file=sys.stderr)


# ---------------------------------------------------------------------------
# records and the persisted manifest
# ---------------------------------------------------------------------------


class Recorder:
    """Append-only JSON lines under the output directory, redacted, flushed
    and fsynced per line."""

    def __init__(self, out_dir: str, redactor: Redactor, clock: Callable[[], float],
                 now_ms: Callable[[], int], *, append: bool = False) -> None:
        self.redactor, self.clock, self.now_ms = redactor, clock, now_ms
        self.path = os.path.join(out_dir, "records.jsonl")
        self.t0 = clock()
        self.seq = 0
        if append and os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.seq = sum(1 for _ in fh)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if not append:
            flags |= os.O_EXCL
        self._fd = os.open(self.path, flags, 0o600)

    def event(self, kind: str, **data: Any) -> None:
        self.seq += 1
        line = self.redactor.dumps({"seq": self.seq, "ts_ms": self.now_ms(),
                                    "t_s": round(self.clock() - self.t0, 3),
                                    "kind": kind, **data}) + "\n"
        os.write(self._fd, line.encode("utf-8"))
        os.fsync(self._fd)

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


class Manifest:
    """What a later process needs: the links this tool used, the cumulative
    create count and open quantity, and a pending experiment (E8)."""

    def __init__(self, path: str, redactor: Redactor,
                 data: Optional[Dict[str, Any]] = None) -> None:
        self.path, self.redactor = path, redactor
        self.data: Dict[str, Any] = data or {
            "schema": SCHEMA, "links": [], "pending": {}, "experiments": {},
            "not_run": [], "creates_total": 0, "opens_attempted": "0",
            "phase_history": []}

    @classmethod
    def load(cls, path: str, redactor: Redactor) -> "Manifest":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise Refused(f"{path} is not a {SCHEMA} manifest")
        return cls(path, redactor, data)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        text = self.redactor.dumps(self.data)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def add_link(self, link: str, exp: str, step: str, side: str, qty: str,
                 intent: str) -> None:
        self.data["links"].append({"link": link, "experiment": exp,
                                   "step": step, "side": side, "qty": qty,
                                   "intent": intent})
        if side == "Sell" and intent == "open":
            self.data["opens_attempted"] = str(
                Decimal(self.data["opens_attempted"]) + Decimal(qty))
        self.save()

    def known_links(self) -> List[str]:
        out: List[str] = []
        for item in self.data["links"]:
            if item["link"] not in out:
                out.append(item["link"])
        return out


# ---------------------------------------------------------------------------
# order identity
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"[A-Za-z0-9_-]{1,36}")


def validate_link_id(link: str) -> str:
    if not _LINK_RE.fullmatch(link or "") or not link.startswith(LINK_PREFIX + "-"):
        raise UnsafeRequest("BAD_LINK_ID",
                            f"orderLinkId {link!r} is not <=36 chars of "
                            f"[A-Za-z0-9_-] starting with {LINK_PREFIX}-")
    return link


def experiment_link_id(run_id: str, experiment: str, step: int,
                       symbol: str) -> str:
    """One deterministic id per (run, experiment, step), built with the
    repo's own `build_order_link_id` (restart-safe: same inputs, same id).
    side/qty/price are deliberately NOT part of the seed: experiments E2/E3
    re-send the SAME id with a different qty/price on purpose."""
    return validate_link_id(bc.build_order_link_id(
        seq=int(step), symbol=symbol, side="", qty="", price="",
        purpose=experiment.lower(), session_token=run_id,
        prefix=LINK_PREFIX))


# ---------------------------------------------------------------------------
# endpoint check (the actual client object and the actual URL)
# ---------------------------------------------------------------------------


def endpoint_refusal(base_url: Any) -> Optional[str]:
    """Why `base_url` is not exactly the testnet REST origin, or None."""
    text = "" if base_url is None else str(base_url)
    if not text:
        return "the client has no base_url (empty host)"
    try:
        parts = urlsplit(text)
        host, port = parts.hostname, parts.port
    except ValueError:
        return f"base_url {text!r} cannot be parsed"
    if parts.scheme != "https":
        return f"base_url scheme {parts.scheme!r} is not https"
    if not host:
        return "base_url has an empty host"
    if host != TESTNET_HOSTNAME:
        return f"base_url host {host!r} is not exactly {TESTNET_HOSTNAME}"
    if parts.username is not None or parts.password is not None:
        return "base_url carries credentials in the URL"
    if port not in (None, 443):
        return f"base_url port {port!r} is not 443"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return "base_url has a path/query/fragment"
    if text.rstrip("/") != TESTNET_BASE:
        return f"base_url {text!r} is not exactly {TESTNET_BASE}"
    return None


def client_refusal(client: Any) -> Optional[str]:
    why = endpoint_refusal(getattr(client, "base_url", None))
    if why:
        return why
    if getattr(client, "venue", None) != "testnet":
        return f"client.venue is {getattr(client, 'venue', None)!r}, not 'testnet'"
    if getattr(client, "testnet", None) is not True:
        return "client.testnet is not True"
    if not callable(getattr(client, "_request", None)):
        return "the client has no _request"
    if not (getattr(client, "api_key", "") and getattr(client, "api_secret", "")):
        return "the client has no credentials (BYBIT_API_KEY / BYBIT_API_SECRET)"
    return None


# ---------------------------------------------------------------------------
# instrument, sizing, order guard
# ---------------------------------------------------------------------------


def dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def fmt(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_price: Optional[Decimal]

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "tick": fmt(self.tick),
                "step": fmt(self.step), "min_qty": fmt(self.min_qty),
                "min_notional": fmt(self.min_notional),
                "max_price": None if self.max_price is None
                else fmt(self.max_price)}


@dataclass(frozen=True)
class Sizing:
    ref_price: Decimal
    q0: Decimal            # smallest valid order
    qmax: Decimal          # q0 + one lot step: the largest order ever sent
    limit_price_1: Decimal
    limit_price_2: Decimal
    cap_usd: Decimal

    def as_dict(self) -> Dict[str, str]:
        return {k: fmt(v) for k, v in self.__dict__.items()}


def compute_sizing(inst: Instrument, ref_price: Decimal,
                   cap_usd: float) -> Sizing:
    """Sizing from the VENUE's lot rules, then sanity-checked. Raises Refused
    when no order that respects the venue's minimums fits under the cap."""
    for name in ("tick", "step", "min_qty", "min_notional"):
        value = getattr(inst, name)
        if not (value.is_finite() and value > 0):
            raise Refused(f"instrument {name}={value} is not positive")
    if not (ref_price.is_finite() and ref_price > 0):
        raise Refused("reference price is not positive")
    need = max(inst.min_qty, inst.min_notional * Decimal("1.1") / ref_price)
    q0 = (need / inst.step).to_integral_value(rounding=ROUND_CEILING) * inst.step
    qmax = q0 + inst.step
    if q0 < inst.min_qty or q0 / inst.step != (q0 / inst.step).to_integral_value():
        raise Refused("computed quantity is not on the lot grid")
    if q0 > inst.min_qty * 1000:
        raise Refused(f"computed quantity {q0} is >1000x the venue minimum "
                      f"{inst.min_qty}; the price read is not credible")
    cap = Decimal(str(cap_usd))
    worst = qmax * ref_price * NOTIONAL_PRICE_BUFFER
    if worst > cap:
        raise Refused(
            f"the smallest valid order set does not fit the notional limit: "
            f"largest order {fmt(qmax)} x price {fmt(ref_price)} x "
            f"{NOTIONAL_PRICE_BUFFER} = {fmt(worst.quantize(Decimal('0.01')))} "
            f"USD > limit {fmt(cap)} USD. Choose a cheaper linear symbol with "
            "--symbol; the limit cannot be raised.")

    def up(mult: str) -> Decimal:
        return (ref_price * Decimal(mult) / inst.tick).to_integral_value(
            rounding=ROUND_CEILING) * inst.tick
    p1 = up("1.05")
    p2 = max(up("1.06"), p1 + inst.tick)
    if inst.max_price is not None and p2 > inst.max_price:
        raise Refused("far-from-market limit price exceeds the venue maxPrice")
    if q0 * p2 > cap or qmax * p2 > cap * NOTIONAL_PRICE_BUFFER:
        raise Refused("far-from-market limit notional exceeds the limit")
    return Sizing(ref_price, q0, qmax, p1, p2, cap)


class OrderGuard:
    """Every create body is checked here before the client sees it."""

    ALLOWED_KEYS = frozenset({"category", "symbol", "side", "orderType", "qty",
                              "price", "timeInForce", "orderLinkId",
                              "positionIdx", "reduceOnly"})

    def __init__(self, symbol: str, inst: Instrument, sizing: Sizing) -> None:
        self.symbol, self.inst, self.sizing = symbol, inst, sizing

    def check_create(self, body: Mapping[str, Any], kind: str) -> None:
        def bad(why: str) -> "UnsafeRequest":
            return UnsafeRequest("UNSAFE_ORDER", why)
        extra = set(body) - self.ALLOWED_KEYS
        if extra:
            raise bad(f"unexpected order fields {sorted(extra)}")
        if body.get("category") != "linear" or body.get("symbol") != self.symbol:
            raise bad("category/symbol is not the configured linear symbol")
        side = body.get("side")
        if side not in ("Buy", "Sell"):
            raise bad(f"side {side!r}")
        reduce_only = body.get("reduceOnly")
        if not isinstance(reduce_only, bool):
            raise bad("reduceOnly must be stated explicitly as a boolean")
        if side == "Buy" and reduce_only is not True:
            raise bad("a Buy must be reduce-only (reduceOnly=true); this "
                      "tool never opens or enlarges a long")
        if kind == "open" and (side != "Sell" or reduce_only):
            raise bad("an opening order is a non-reduce-only Sell")
        if kind in ("close", "probe", "emergency") and not reduce_only:
            raise bad(f"a {kind} order must be reduce-only")
        if kind not in ("open", "close", "probe", "emergency"):
            raise bad(f"unknown order kind {kind!r}")
        if str(body.get("positionIdx", "0")) != "0":
            raise bad("positionIdx must be 0 (one-way)")
        validate_link_id(str(body.get("orderLinkId", "")))
        qty = dec(body.get("qty"))
        if qty is None or qty <= 0:
            raise bad("qty is not positive")
        if qty / self.inst.step != (qty / self.inst.step).to_integral_value():
            raise bad("qty is not on the lot-step grid")
        ceiling = self.sizing.qmax * (EMERGENCY_QTY_MULT if kind == "emergency"
                                      else 1)
        if qty > ceiling:
            raise bad(f"qty {fmt(qty)} exceeds the sized maximum {fmt(ceiling)}")
        if kind in ("open",) and qty < self.inst.min_qty:
            raise bad("opening qty is below the venue minimum")
        otype = body.get("orderType")
        if otype == "Market":
            if "price" in body or "timeInForce" in body:
                raise bad("market order with a price/timeInForce")
            if kind != "emergency" and (qty * self.sizing.ref_price
                                        * NOTIONAL_PRICE_BUFFER
                                        > self.sizing.cap_usd):
                raise bad("market order notional exceeds the limit")
        elif otype == "Limit":
            price = dec(body.get("price"))
            if price is None or price <= 0:
                raise bad("limit price is not positive")
            if body.get("timeInForce") != "PostOnly":
                raise bad("a limit order must be PostOnly")
            if side != "Sell" or reduce_only:
                raise bad("limit orders are only the far-from-market "
                          "opening Sell")
            if price / self.inst.tick != (price / self.inst.tick).to_integral_value():
                raise bad("limit price is not on the tick grid")
            if price < self.sizing.ref_price * Decimal("1.04"):
                raise bad("limit price is not far above the reference price")
            if qty * price > self.sizing.cap_usd * NOTIONAL_PRICE_BUFFER:
                raise bad("limit notional exceeds the limit")
        else:
            raise bad(f"orderType {otype!r}")


# ---------------------------------------------------------------------------
# the gateway: the only thing that touches the client
# ---------------------------------------------------------------------------


@dataclass
class Resp:
    ok: bool
    ret_code: Optional[int]
    ret_msg: str
    body: Any                  # parsed envelope (ok) or the error payload
    error_class: str
    elapsed_s: float
    started_ms: int
    discarded: bool = False

    def result(self) -> Dict[str, Any]:
        if isinstance(self.body, Mapping):
            res = self.body.get("result")
            if isinstance(res, Mapping):
                return dict(res)
        return {}

    def public(self) -> Dict[str, Any]:
        if self.discarded:
            return {"discarded": True, "elapsed_s": round(self.elapsed_s, 3),
                    "error_class": self.error_class}
        return {"ok": self.ok, "ret_code": self.ret_code,
                "ret_msg": self.ret_msg, "body": self.body,
                "error_class": self.error_class,
                "elapsed_s": round(self.elapsed_s, 3),
                "started_ms": self.started_ms}


class Gateway:
    def __init__(self, client: Any, *, symbol: str, limits: Limits,
                 rec: Recorder, manifest: Manifest, clock: Callable[[], float],
                 now_ms: Callable[[], int]) -> None:
        self.client, self.symbol, self.limits = client, symbol, limits
        self.rec, self.manifest, self.clock, self.now_ms = rec, manifest, clock, now_ms
        self.t0 = clock()
        self.requests = 0
        self.cancels = 0
        self.guard: Optional[OrderGuard] = None
        self.final_mode = False
        self.final_t0 = 0.0
        self.known_order_ids: set = set()
        self.request_log: List[Tuple[str, str]] = []

    @property
    def creates(self) -> int:
        return int(self.manifest["creates_total"])

    def elapsed(self) -> float:
        return self.clock() - self.t0

    def enter_final_mode(self) -> None:
        self.final_mode = True
        self.final_t0 = self.clock()

    def request(self, method: str, path: str, *, params: Optional[dict] = None,
                body: Optional[dict] = None, kind: str = "read",
                discard: bool = False, tag: str = "") -> Resp:
        method = method.upper()
        if ALLOWED_ENDPOINTS.get(path) != method:
            raise UnsafeRequest("PATH_NOT_ALLOWED",
                                f"{method} {path} is not on the allowlist; "
                                "nothing was sent")
        why = endpoint_refusal(getattr(self.client, "base_url", None))
        if why:
            raise UnsafeRequest("ENDPOINT_NOT_TESTNET",
                                f"{why}; nothing was sent")
        if method == "GET":
            if path not in PUBLIC_ENDPOINTS and path != ACCOUNT:
                if (params or {}).get("category") != "linear" \
                        or (params or {}).get("symbol") != self.symbol:
                    raise UnsafeRequest("BAD_PARAMS",
                                        "category/symbol must be linear/"
                                        f"{self.symbol}")
        # limits
        cap = HARD_MAX_REQUESTS + (FINAL_EXTRA_REQUESTS if self.final_mode else 0)
        if self.requests >= cap:
            raise LimitHalt("REQUEST_CEILING", f"{self.requests} requests")
        if self.final_mode:
            if self.clock() - self.final_t0 > FINAL_STEP_BUDGET_S:
                raise LimitHalt("FINAL_STEP_TIME", "final step time budget")
        elif self.elapsed() > self.limits.max_duration_s:
            raise LimitHalt("DURATION", f"{self.elapsed():.0f}s > "
                            f"{self.limits.max_duration_s}s")
        if path == CREATE:
            if self.guard is None:
                raise UnsafeRequest("GUARD_NOT_ARMED", "no instrument guard")
            self.guard.check_create(body or {}, kind)
            if self.creates >= self.limits.max_orders:
                raise LimitHalt("ORDER_CEILING",
                                f"{self.creates} orders already sent "
                                f"(max {self.limits.max_orders})")
            if kind != "emergency" and not self.final_mode and \
                    self.creates >= self.limits.max_orders - EMERGENCY_RESERVE:
                raise LimitHalt("ORDER_RESERVE",
                                "the remaining orders are reserved for the "
                                "final flat step")
        elif path == CANCEL:
            self._check_cancel(body or {})
            if self.cancels >= HARD_MAX_CANCELS:
                raise LimitHalt("CANCEL_CEILING", f"{self.cancels} cancels")
        # counted BEFORE the attempt, like the client does: a lost response
        # still left the process
        self.requests += 1
        self.request_log.append((method, path))
        if path == CREATE:
            self.manifest["creates_total"] = self.creates + 1
            self.manifest.save()
        elif path == CANCEL:
            self.cancels += 1
        self.rec.event("request", method=method, path=path, params=params,
                       body=body, order_kind=kind, tag=tag,
                       order_link_id=(body or params or {}).get("orderLinkId"))
        started, started_ms = self.clock(), self.now_ms()
        ok, ret_code, ret_msg, payload, ecls = True, 0, "OK", None, ""
        try:
            payload = self.client._request(
                method, path, params=params if method == "GET" else None,
                body=body if method == "POST" else None,
                signed=path not in PUBLIC_ENDPOINTS, retries=0)
            if isinstance(payload, Mapping):
                try:
                    ret_code = int(payload.get("retCode", 0))
                except (TypeError, ValueError):
                    ret_code = None
                ret_msg = str(payload.get("retMsg", ""))
        except bc.BybitAPIError as exc:
            ok, ret_code, ret_msg = False, exc.ret_code, str(exc)
            payload, ecls = exc.payload, type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - network/transport layer
            ok, ret_code, ret_msg = False, None, f"{type(exc).__name__}: {exc}"
            payload, ecls = None, type(exc).__name__
        resp = Resp(ok, ret_code, ret_msg, payload, ecls,
                    self.clock() - started, started_ms, discarded=discard)
        if discard:
            payload = None
            resp.body = None
            resp.ret_msg = ""
        self.rec.event("response", path=path, tag=tag,
                       order_link_id=(body or params or {}).get("orderLinkId"),
                       **resp.public())
        return resp

    def get(self, path: str, params: dict, *, tag: str = "") -> Resp:
        """A read, retried at the TOOL level (each attempt is its own
        recorded, allowlisted request) when the outcome is transient."""
        resp = self.request("GET", path, params=params, tag=tag)
        for _ in range(2):
            if resp.ok or (resp.ret_code is not None
                           and resp.ret_code not in bc.TRANSIENT_RET_CODES
                           and resp.ret_code != 0):
                break
            resp = self.request("GET", path, params=params, tag=tag + ":retry")
        return resp

    def _check_cancel(self, body: Mapping[str, Any]) -> None:
        extra = set(body) - {"category", "symbol", "orderLinkId", "orderId"}
        if extra or body.get("category") != "linear" \
                or body.get("symbol") != self.symbol:
            raise UnsafeRequest("UNSAFE_CANCEL", "cancel body is not "
                                "category/symbol plus one id")
        link, oid = body.get("orderLinkId"), body.get("orderId")
        if bool(link) == bool(oid):
            raise UnsafeRequest("UNSAFE_CANCEL", "exactly one of "
                                "orderLinkId/orderId")
        if link:
            validate_link_id(str(link))
        elif str(oid) not in self.known_order_ids:
            raise UnsafeRequest("UNSAFE_CANCEL", "orderId was not observed "
                                "as one of this tool's orders")


# ---------------------------------------------------------------------------
# the lab: venue reads, experiments
# ---------------------------------------------------------------------------


def classify_create(resp: Resp) -> str:
    if resp.ok:
        return "accepted"
    if resp.ret_code is None or resp.ret_code in bc.UNKNOWN_OUTCOME_RET_CODES \
            or resp.ret_code in bc.TRANSIENT_RET_CODES:
        return "unknown"
    return "rejected"


def summary(resp: Resp) -> Dict[str, Any]:
    res = resp.result()
    return {"outcome": classify_create(resp), "ok": resp.ok,
            "ret_code": resp.ret_code, "ret_msg": resp.ret_msg,
            "order_id": res.get("orderId"),
            "order_link_id_echo": res.get("orderLinkId"),
            "error_class": resp.error_class,
            "ret_code_in_repo_duplicate_table":
                resp.ret_code in bc.DUPLICATE_LINK_ID_RET_CODES,
            "elapsed_s": round(resp.elapsed_s, 3)}


class Exp:
    """The record of one experiment."""

    def __init__(self, lab: "Lab", eid: str, title: str, question: str) -> None:
        self.lab, self.id, self.title, self.question = lab, eid, title, question
        self.steps: List[Dict[str, Any]] = []
        self.facts: Dict[str, Any] = {}
        self.links: List[str] = []
        self.status = "RUNNING"
        self.pos_before: Optional[str] = None
        self.pos_after: Optional[str] = None
        self.t_start = lab.clock()
        self.creates_start = lab.gw.creates

    def step(self, name: str, **data: Any) -> None:
        entry = {"name": name,
                 "exp_t_s": round(self.lab.clock() - self.t_start, 3), **data}
        self.steps.append(entry)
        self.lab.rec.event("step", experiment=self.id, **entry)

    def result(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "question": self.question,
                "status": self.status, "position_before": self.pos_before,
                "position_after": self.pos_after, "facts": self.facts,
                "order_links": self.links, "steps": self.steps,
                "duration_s": round(self.lab.clock() - self.t_start, 3),
                "order_creates_sent": self.lab.gw.creates - self.creates_start}


@dataclass
class OrderRow:
    order_id: str
    link: str
    status: str
    side: str
    qty: Decimal
    cum: Decimal
    price: str
    reduce_only: Any
    sources: Tuple[str, ...]
    status_by_source: Dict[str, str]

    def signed_cum(self) -> Decimal:
        return -self.cum if self.side == "Sell" else self.cum

    def public(self) -> Dict[str, Any]:
        return {"order_id": self.order_id, "link": self.link,
                "status": self.status, "side": self.side, "qty": fmt(self.qty),
                "cum_exec_qty": fmt(self.cum), "price": self.price,
                "reduce_only": self.reduce_only,
                "seen_in": list(self.sources),
                "status_by_source": self.status_by_source}


class Lab:
    def __init__(self, gw: Gateway, rec: Recorder, manifest: Manifest,
                 console: Console, inst: Instrument, sizing: Sizing,
                 run_id: str, symbol: str, *, clock, now_ms, sleep) -> None:
        self.gw, self.rec, self.manifest, self.console = gw, rec, manifest, console
        self.inst, self.sizing, self.run_id, self.symbol = inst, sizing, run_id, symbol
        self.clock, self.now_ms, self.sleep = clock, now_ms, sleep

    # -- reading -----------------------------------------------------------

    def _rows(self, resp: Resp, what: str) -> List[Dict[str, Any]]:
        if not resp.ok:
            raise Ambiguity("AMBIG_UNREADABLE",
                            f"{what}: retCode={resp.ret_code} {resp.ret_msg}")
        result = resp.result()
        rows = result.get("list")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise Ambiguity("AMBIG_UNREADABLE", f"{what}: no list in result")
        if result.get("nextPageCursor"):
            raise Ambiguity("AMBIG_UNREADABLE", f"{what}: more pages than one "
                            "request returns; refusing to treat it as complete")
        return rows

    def read_position(self) -> Tuple[Decimal, List[Dict[str, Any]]]:
        resp = self.gw.get(POSITIONS, {"category": "linear",
                                       "symbol": self.symbol}, tag="position")
        rows = self._rows(resp, "position list")
        if not rows:
            raise Ambiguity("AMBIG_UNREADABLE", "position list is empty; "
                            "never read as flat")
        net = Decimal(0)
        for row in rows:
            idx = str(row.get("positionIdx", ""))
            if idx in ("1", "2"):
                raise Ambiguity("AMBIG_HEDGE_MODE",
                                f"positionIdx {idx} listed: hedge mode")
            if idx != "0":
                raise Ambiguity("AMBIG_UNREADABLE",
                                f"positionIdx {idx!r} is not 0")
            size = dec(row.get("size"))
            if size is None or size < 0:
                raise Ambiguity("AMBIG_UNREADABLE",
                                f"position size {row.get('size')!r}")
            if size > 0:
                side = row.get("side")
                if side == "Sell":
                    net -= size
                elif side == "Buy":
                    net += size
                else:
                    raise Ambiguity("AMBIG_UNREADABLE",
                                    f"position side {side!r} with size")
        return net, rows

    def read_open_orders(self) -> List[Dict[str, Any]]:
        resp = self.gw.get(REALTIME, {"category": "linear",
                                      "symbol": self.symbol, "limit": 50},
                           tag="open_orders")
        out = []
        for row in self._rows(resp, "open orders"):
            status = str(row.get("orderStatus") or "")
            if status not in EXPECTED_STATUSES and status not in TERMINAL_STATUSES:
                raise Ambiguity("AMBIG_UNEXPECTED_STATUS",
                                f"open-order list holds status {status!r}")
            if status in OPEN_STATUSES:
                out.append(row)
        return out

    def assert_clean(self, label: str) -> str:
        net, _rows = self.read_position()
        opens = self.read_open_orders()
        if net != 0 or opens:
            raise Ambiguity("AMBIG_NOT_CLEAN",
                            f"{label}: position={fmt(net)} open_orders="
                            f"{[o.get('orderLinkId') for o in opens]}")
        return fmt(net)

    def lookup(self, link: str, tag: str = "") -> Dict[str, Any]:
        """The venue's answer for one orderLinkId from BOTH lists."""
        merged: Dict[str, OrderRow] = {}
        raw = {}
        for path in (REALTIME, HISTORY):
            resp = self.gw.get(path, {"category": "linear",
                                      "symbol": self.symbol,
                                      "orderLinkId": link, "limit": 50},
                               tag=f"lookup{tag}")
            rows = self._rows(resp, f"{path} by link")
            raw[path] = rows
            for row in rows:
                status = str(row.get("orderStatus") or "")
                if status not in EXPECTED_STATUSES:
                    raise Ambiguity("AMBIG_UNEXPECTED_STATUS",
                                    f"{path} status {status!r} for {link}",
                                    row=row)
                qty, cum = dec(row.get("qty")), dec(row.get("cumExecQty"))
                if qty is None or cum is None or cum < 0:
                    raise Ambiguity("AMBIG_UNREADABLE",
                                    f"{path} qty/cumExecQty unreadable", row=row)
                oid = str(row.get("orderId") or "")
                if not oid:
                    raise Ambiguity("AMBIG_UNREADABLE", f"{path} row without "
                                    "orderId", row=row)
                if qty > self.sizing.qmax or cum > qty:
                    raise Ambiguity("AMBIG_QTY_BEYOND_SENT",
                                    f"order {oid} qty={qty} cum={cum} beyond "
                                    "what this tool sends", row=row)
                self.gw.known_order_ids.add(oid)
                prev = merged.get(oid)
                if prev is None:
                    merged[oid] = OrderRow(
                        oid, str(row.get("orderLinkId") or ""), status,
                        str(row.get("side") or ""), qty, cum,
                        str(row.get("price") or ""), row.get("reduceOnly"),
                        (path.rsplit("/", 1)[1],), {path.rsplit("/", 1)[1]: status})
                else:
                    src = path.rsplit("/", 1)[1]
                    prev.sources += (src,)
                    prev.status_by_source[src] = status
                    if path == HISTORY:      # the history row is the final word
                        prev.status, prev.cum = status, cum
        rows = list(merged.values())
        return {"link": link, "rows": rows,
                "in_realtime": len(raw[REALTIME]), "in_history": len(raw[HISTORY]),
                "order_ids": sorted(merged)}

    def settle(self, ex: Exp, link: str, *, allowed: Sequence[str] = ("Filled",),
               allow_empty: bool = False, polls: int = VISIBLE_POLLS,
               name: str = "settle", min_polls: int = 2) -> Dict[str, Any]:
        """Poll by orderLinkId until every order found is terminal and the set
        is unchanged on two consecutive polls. Records every poll."""
        prev = None
        log = []
        for i in range(1, polls + 1):
            view = self.lookup(link, tag=f":{name}")
            sig = tuple(sorted((r.order_id, r.status, str(r.cum))
                               for r in view["rows"]))
            terminal = bool(view["rows"]) and all(
                r.status in TERMINAL_STATUSES for r in view["rows"])
            log.append({"poll": i, "in_realtime": view["in_realtime"],
                        "in_history": view["in_history"],
                        "statuses": [r.status for r in view["rows"]]})
            for r in view["rows"]:
                if r.status in TERMINAL_STATUSES and r.status not in allowed:
                    ex.step(name, polls=log)
                    raise Ambiguity("AMBIG_UNEXPECTED_STATUS",
                                    f"{link}: terminal status {r.status!r} "
                                    f"not in {list(allowed)}",
                                    rows=[x.public() for x in view["rows"]])
            if terminal and sig == prev and i >= min_polls:
                ex.step(name, polls=log, rows=[r.public() for r in view["rows"]])
                return view
            if allow_empty and not view["rows"] and i >= 3:
                ex.step(name, polls=log, rows=[])
                return view
            prev = sig
            self.sleep(POLL_INTERVAL_S)
        ex.step(name, polls=log, exhausted=True)
        if not view["rows"]:
            raise Ambiguity("AMBIG_NOT_VISIBLE",
                            f"{link} not visible in realtime or history after "
                            f"{polls} polls")
        raise Ambiguity("AMBIG_UNEXPECTED_STATUS",
                        f"{link} not terminal/stable after {polls} polls",
                        rows=[x.public() for x in view["rows"]])

    def observe(self, ex: Exp, link: str, name: str, polls: int = 4
                ) -> Dict[str, Any]:
        """Look a few times, record what is there, never insist on a state.
        Used where either outcome of a re-POST is a legitimate observation."""
        log = []
        view: Dict[str, Any] = {}
        for i in range(1, polls + 1):
            view = self.lookup(link, tag=f":{name}")
            log.append({"poll": i, "in_realtime": view["in_realtime"],
                        "in_history": view["in_history"],
                        "statuses": [r.status for r in view["rows"]]})
            for r in view["rows"]:
                if r.cum > 0:
                    ex.step(name, polls=log)
                    raise Ambiguity("AMBIG_UNEXPECTED_FILL",
                                    f"{link}: a far-from-market post-only "
                                    "order shows an execution",
                                    rows=[x.public() for x in view["rows"]])
            if i < polls:
                self.sleep(POLL_INTERVAL_S)
        ex.step(name, polls=log, rows=[r.public() for r in view["rows"]])
        return view

    def wait_open(self, ex: Exp, link: str, name: str = "wait_open",
                  tolerate: Sequence[str] = ()) -> Dict[str, Any]:
        """A resting order: visible and open in the realtime list."""
        log = []
        for i in range(1, VISIBLE_POLLS + 1):
            view = self.lookup(link, tag=f":{name}")
            log.append({"poll": i, "in_realtime": view["in_realtime"],
                        "in_history": view["in_history"],
                        "statuses": [r.status for r in view["rows"]]})
            for r in view["rows"]:
                if r.cum > 0:
                    ex.step(name, polls=log)
                    raise Ambiguity("AMBIG_UNEXPECTED_FILL",
                                    f"{link}: a far-from-market post-only "
                                    "order shows an execution",
                                    rows=[x.public() for x in view["rows"]])
                if r.status in TERMINAL_STATUSES and r.status not in tolerate:
                    ex.step(name, polls=log)
                    raise Ambiguity("AMBIG_UNEXPECTED_STATUS",
                                    f"{link}: resting order is {r.status!r}",
                                    rows=[x.public() for x in view["rows"]])
            live = [r for r in view["rows"] if r.status not in tolerate]
            if live and all(r.status in OPEN_STATUSES for r in live):
                ex.step(name, polls=log, rows=[r.public() for r in view["rows"]])
                return view
            self.sleep(POLL_INTERVAL_S)
        ex.step(name, polls=log, exhausted=True)
        raise Ambiguity("AMBIG_NOT_VISIBLE",
                        f"{link} not visible as an open order after "
                        f"{VISIBLE_POLLS} polls")

    def executions(self, *, link: str = "", order_id: str = "") -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {"category": "linear", "symbol": self.symbol,
                                  "limit": 100}
        params["orderId" if order_id else "orderLinkId"] = order_id or link
        seen = set()
        for _page in range(5):
            resp = self.gw.get(EXECUTIONS, dict(params), tag="executions")
            if not resp.ok:
                raise Ambiguity("AMBIG_UNREADABLE",
                                f"execution list: retCode={resp.ret_code} "
                                f"{resp.ret_msg}")
            result = resp.result()
            page = result.get("list")
            if not isinstance(page, list) or not all(isinstance(r, dict)
                                                     for r in page):
                raise Ambiguity("AMBIG_UNREADABLE", "execution list has no list")
            rows.extend(page)
            cursor = result.get("nextPageCursor")
            if not cursor:
                return rows
            if cursor in seen:
                raise Ambiguity("AMBIG_UNREADABLE", "execution cursor repeated")
            seen.add(cursor)
            params["cursor"] = cursor
        raise Ambiguity("AMBIG_UNREADABLE", "execution list did not finish")

    def reconcile_execs(self, ex: Exp, link: str, view: Dict[str, Any], *,
                        name: str = "reconcile_executions") -> Dict[str, Any]:
        """Sum of execution qty and unique execIds vs each order's cumExecQty.
        Polls (the list can lag the order row); a persistent mismatch halts."""
        last: Dict[str, Any] = {}
        for i in range(1, EXEC_POLLS + 1):
            rows = self.executions(link=link)
            ids = [str(r.get("execId") or "") for r in rows]
            problems = []
            by_order: Dict[str, Decimal] = {}
            for r in rows:
                q = dec(r.get("execQty"))
                if q is None or q < 0 or not r.get("execId"):
                    raise Ambiguity("AMBIG_UNREADABLE",
                                    f"execution row unreadable: {r}")
                if str(r.get("execType") or "Trade") != "Trade":
                    continue
                oid = str(r.get("orderId") or "")
                by_order[oid] = by_order.get(oid, Decimal(0)) + q
            if len(set(ids)) != len(ids):
                problems.append("duplicate execIds in the list")
            expected = {r.order_id: r.cum for r in view["rows"] if r.cum > 0}
            for oid, cum in expected.items():
                if by_order.get(oid, Decimal(0)) != cum:
                    problems.append(f"order {oid}: sum(execQty)="
                                    f"{fmt(by_order.get(oid, Decimal(0)))} != "
                                    f"cumExecQty={fmt(cum)}")
            for oid in by_order:
                if oid not in expected:
                    problems.append(f"executions for order {oid} that has no "
                                    "cumExecQty in the order rows")
            last = {"poll": i, "exec_rows": len(rows), "unique_exec_ids": len(set(ids)),
                    "exec_sum": fmt(sum(by_order.values(), Decimal(0))),
                    "cum_exec_total": fmt(sum(expected.values(), Decimal(0))),
                    "exec_order_ids": sorted(by_order),
                    "exec_rows_raw": rows, "problems": problems}
            if not problems:
                ex.step(name, **last)
                return last
            self.sleep(POLL_INTERVAL_S)
            view = self.lookup(link, tag=":reconcile")
        ex.step(name, **last)
        raise Ambiguity("AMBIG_EXEC_SUM_MISMATCH", "; ".join(last["problems"]),
                        reconcile=last)

    @staticmethod
    def expected_net(rows: Sequence[OrderRow]) -> Decimal:
        return sum((r.signed_cum() for r in rows), Decimal(0))

    def expect_position(self, ex: Exp, expected: Decimal,
                        name: str = "position") -> Decimal:
        last = None
        for i in range(1, POS_POLLS + 1):
            net, rows = self.read_position()
            last = net
            if net == expected:
                ex.step(name, expected=fmt(expected), observed=fmt(net), polls=i)
                return net
            self.sleep(POLL_INTERVAL_S)
        ex.step(name, expected=fmt(expected), observed=fmt(last or Decimal(0)),
                exhausted=True)
        raise Ambiguity("AMBIG_POSITION_MISMATCH",
                        f"position {fmt(last or Decimal(0))} != expected "
                        f"{fmt(expected)} after {POS_POLLS} reads")

    # -- writing -----------------------------------------------------------

    def link(self, ex_id: str, step: int) -> str:
        return experiment_link_id(self.run_id, ex_id, step, self.symbol)

    def body(self, side: str, qty: Decimal, link: str, *, otype: str = "Market",
             price: Optional[Decimal] = None, reduce_only: bool = False
             ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"category": "linear", "symbol": self.symbol,
                                "side": side, "orderType": otype,
                                "qty": fmt(qty), "orderLinkId": link,
                                "positionIdx": 0, "reduceOnly": reduce_only}
        if otype == "Limit":
            body["price"] = fmt(price)  # type: ignore[arg-type]
            body["timeInForce"] = "PostOnly"
        return body

    def place(self, ex: Exp, name: str, body: Dict[str, Any], intent: str, *,
              discard: bool = False) -> Tuple[Resp, str]:
        link = body["orderLinkId"]
        if link not in ex.links:
            ex.links.append(link)
        self.manifest.add_link(link, ex.id, name, body["side"], body["qty"],
                               intent)
        resp = self.gw.request("POST", CREATE, body=body, kind=intent,
                               discard=discard, tag=f"{ex.id}:{name}")
        outcome = "discarded" if discard else classify_create(resp)
        ex.step(name, request=body, response=resp.public(), outcome=outcome,
                intent=intent)
        if not discard and outcome == "unknown":
            raise Ambiguity("AMBIG_UNKNOWN_CREATE_OUTCOME",
                            f"{ex.id}/{name}: retCode={resp.ret_code} "
                            f"{resp.ret_msg}; the order may or may not exist",
                            link=link)
        return resp, outcome

    def must_accept(self, ex: Exp, name: str, resp: Resp, outcome: str) -> None:
        if outcome != "accepted":
            raise Ambiguity("AMBIG_UNEXPECTED_CREATE_RESULT",
                            f"{ex.id}/{name}: expected the order to be "
                            f"accepted, got retCode={resp.ret_code} "
                            f"{resp.ret_msg}")

    def cancel(self, ex: Exp, name: str, *, link: str = "",
               order_id: str = "") -> Resp:
        body: Dict[str, Any] = {"category": "linear", "symbol": self.symbol}
        body["orderLinkId" if link else "orderId"] = link or order_id
        resp = self.gw.request("POST", CANCEL, body=body, kind="cancel",
                               tag=f"{ex.id}:{name}")
        ex.step(name, request=body, response=resp.public())
        if not resp.ok:
            raise Ambiguity("AMBIG_UNEXPECTED_CREATE_RESULT",
                            f"{ex.id}/{name}: cancel failed retCode="
                            f"{resp.ret_code} {resp.ret_msg}")
        return resp

    def close_flat(self, ex: Exp, *, detailed: bool = False) -> None:
        """Reduce-only market orders (each <= qmax) until the venue shows a
        position of exactly zero."""
        net, _ = self.read_position()
        chunks = 0
        while net != 0:
            if chunks >= MAX_CLOSE_CHUNKS:
                raise Ambiguity("AMBIG_POSITION_MISMATCH",
                                f"still {fmt(net)} after {chunks} close orders")
            side = "Buy" if net < 0 else "Sell"
            qty = min(abs(net), self.sizing.qmax)
            link = self.link(ex.id, 90 + chunks)
            resp, outcome = self.place(
                ex, f"close_{chunks + 1}",
                self.body(side, qty, link, reduce_only=True), "close")
            self.must_accept(ex, f"close_{chunks + 1}", resp, outcome)
            view = self.settle(ex, link, name=f"close_{chunks + 1}_settle")
            if detailed:
                self.reconcile_execs(ex, link, view,
                                     name=f"close_{chunks + 1}_executions")
            target = net + (qty if side == "Buy" else -qty)
            net = self.expect_position(ex, target, f"close_{chunks + 1}_position")
            chunks += 1
        # exactly flat, twice, a second apart
        self.sleep(POLL_INTERVAL_S)
        net2, rows = self.read_position()
        if net2 != 0:
            raise Ambiguity("AMBIG_POSITION_MISMATCH",
                            f"flat read once, then {fmt(net2)}")
        ex.step("flat_verified", position=fmt(net2),
                raw_sizes=[str(r.get("size")) for r in rows])

    def begin(self, eid: str, title: str, question: str) -> Exp:
        ex = Exp(self, eid, title, question)
        ex.pos_before = self.assert_clean(f"{eid} start")
        ex.step("start_state_clean", position=ex.pos_before, open_orders=0)
        return ex

    def finish(self, ex: Exp) -> Dict[str, Any]:
        ex.pos_after = self.assert_clean(f"{ex.id} end")
        ex.status = "COMPLETED"
        ex.step("end_state_clean", position=ex.pos_after, open_orders=0)
        return ex.result()

    # -- experiments -------------------------------------------------------

    def _dup_market(self, ex: Exp, *, first_qty: Decimal, repost_qty: Decimal,
                    after_fill: bool) -> None:
        L = self.link(ex.id, 1)
        r1, o1 = self.place(ex, "post_1", self.body("Sell", first_qty, L), "open")
        self.must_accept(ex, "post_1", r1, o1)
        if after_fill:
            v = self.settle(ex, L, name="settle_before_repost")
            ex.facts["status_before_repost"] = [r.status for r in v["rows"]]
        r2, o2 = self.place(ex, "repost_same_link",
                            self.body("Sell", repost_qty, L), "open")
        view = self.settle(ex, L, name="settle_after_repost", min_polls=4)
        execs = self.reconcile_execs(ex, L, view)
        net = self.expect_position(ex, self.expected_net(view["rows"]))
        ex.facts.update({
            "first": summary(r1), "repost": summary(r2),
            "first_qty": fmt(first_qty), "repost_qty": fmt(repost_qty),
            "distinct_order_ids_by_link": len(view["rows"]),
            "orders_by_link": [r.public() for r in view["rows"]],
            "second_order_created": len(view["rows"]) > 1,
            "distinct_orders_with_executions": len(execs["exec_order_ids"]),
            "second_execution_exists": len(execs["exec_order_ids"]) > 1,
            "exec_rows": execs["exec_rows"],
            "unique_exec_ids": execs["unique_exec_ids"],
            "position_after_repost": fmt(net)})
        self.close_flat(ex)

    def e1(self) -> Dict[str, Any]:
        ex = self.begin("E1", "Immediate identical re-POST of a market order",
                        "Same orderLinkId, identical payload, re-POSTed "
                        "immediately after the first POST")
        self._dup_market(ex, first_qty=self.sizing.q0, repost_qty=self.sizing.q0,
                         after_fill=False)
        return self.finish(ex)

    def e2(self) -> Dict[str, Any]:
        ex = self.begin("E2", "Re-POST with a different qty",
                        "Same orderLinkId, different qty (the re-POST is the "
                        "smaller one; size is never increased)")
        self._dup_market(ex, first_qty=self.sizing.qmax,
                         repost_qty=self.sizing.q0, after_fill=False)
        return self.finish(ex)

    def e3(self) -> Dict[str, Any]:
        ex = self.begin("E3", "Re-POST with a different price field",
                        "Same orderLinkId, different price on a resting "
                        "post-only limit order")
        ex.facts["why_limit_order"] = (
            "A market order has no price field, so a market order can only "
            "differ in qty (that is E2). Price is only meaningful on a "
            "limit order, so E3 uses a resting far-from-market post-only "
            "limit. A different SIDE on the same id is NOT run: a Buy would "
            "have to be reduce-only and there is no position to reduce, so "
            "it would test the reduce-only rule, not order identity (E6 "
            "covers a rejected reduce-only Buy).")
        L = self.link(ex.id, 1)
        r1, o1 = self.place(ex, "post_1", self.body(
            "Sell", self.sizing.q0, L, otype="Limit",
            price=self.sizing.limit_price_1), "open")
        self.must_accept(ex, "post_1", r1, o1)
        self.wait_open(ex, L, "wait_open_1")
        r2, o2 = self.place(ex, "repost_different_price", self.body(
            "Sell", self.sizing.q0, L, otype="Limit",
            price=self.sizing.limit_price_2), "open")
        view = self.wait_open(ex, L, "wait_open_after_repost") \
            if o2 == "accepted" else self.lookup(L)
        ex.facts.update({
            "first": summary(r1), "repost": summary(r2),
            "price_1": fmt(self.sizing.limit_price_1),
            "price_2": fmt(self.sizing.limit_price_2),
            "orders_by_link": [r.public() for r in view["rows"]],
            "distinct_order_ids_by_link": len(view["rows"]),
            "second_order_created": len(view["rows"]) > 1,
            "prices_on_venue_rows": [r.price for r in view["rows"]]})
        self._cancel_all_of(ex, L, view)
        return self.finish(ex)

    def _cancel_all_of(self, ex: Exp, link: str, view: Dict[str, Any]) -> None:
        open_rows = [r for r in view["rows"] if r.status in OPEN_STATUSES]
        if len(open_rows) == 1 and len(view["rows"]) == 1:
            self.cancel(ex, "cancel_by_link", link=link)
        else:
            for i, r in enumerate(open_rows, 1):
                self.cancel(ex, f"cancel_by_order_id_{i}", order_id=r.order_id)
        if open_rows:
            self.settle(ex, link, allowed=("Cancelled",), name="settle_cancelled")
        orders = self.read_open_orders()
        if orders:
            raise Ambiguity("AMBIG_OPEN_ORDERS_UNEXPECTED",
                            f"{len(orders)} open orders after cancel")

    def e4(self) -> Dict[str, Any]:
        ex = self.begin("E4", "Re-POST after the first order FILLED",
                        "Same orderLinkId re-POSTed after the first order is "
                        "confirmed Filled")
        self._dup_market(ex, first_qty=self.sizing.q0, repost_qty=self.sizing.q0,
                         after_fill=True)
        return self.finish(ex)

    def e5(self) -> Dict[str, Any]:
        ex = self.begin("E5", "Re-POST after the first order was CANCELLED",
                        "Same orderLinkId re-POSTed after a resting post-only "
                        "limit order was cancelled")
        L = self.link(ex.id, 1)
        body = self.body("Sell", self.sizing.q0, L, otype="Limit",
                         price=self.sizing.limit_price_1)
        r1, o1 = self.place(ex, "post_1", body, "open")
        self.must_accept(ex, "post_1", r1, o1)
        self.wait_open(ex, L)
        self.cancel(ex, "cancel_by_link", link=L)
        v1 = self.settle(ex, L, allowed=("Cancelled",), name="settle_cancelled")
        ex.facts["status_after_cancel"] = [r.status for r in v1["rows"]]
        r2, o2 = self.place(ex, "repost_same_link", dict(body), "open")
        view = self.observe(ex, L, "observe_after_repost")
        if any(r.status in OPEN_STATUSES for r in view["rows"]):
            view = self.wait_open(ex, L, "wait_open_after_repost",
                                  tolerate=("Cancelled",))
            self._cancel_all_of(ex, L, view)
        ex.facts.update({
            "first": summary(r1), "repost": summary(r2),
            "orders_by_link_after_repost": [r.public() for r in view["rows"]],
            "distinct_order_ids_by_link": len(view["rows"]),
            "second_order_created": len(view["rows"]) > 1})
        return self.finish(ex)

    def e6(self) -> Dict[str, Any]:
        ex = self.begin("E6", "Re-POST after the first submission was REJECTED",
                        "First submission is a reduce-only market Buy with no "
                        "position (expected to be rejected); the same id is "
                        "then re-sent identically and then with a valid "
                        "opening Sell")
        L = self.link(ex.id, 1)
        bad = self.body("Buy", self.sizing.q0, L, reduce_only=True)
        r1, o1 = self.place(ex, "post_1_reduce_only_no_position", bad, "probe")
        v1 = self.lookup(L, ":after_first")
        ex.step("rows_after_first", rows=[r.public() for r in v1["rows"]])
        r2, o2 = self.place(ex, "repost_identical", dict(bad), "probe")
        good = self.body("Sell", self.sizing.q0, L)
        r3, o3 = self.place(ex, "repost_valid_open_same_link", good, "open")
        view = self.settle(ex, L, allowed=("Filled", "Rejected", "Cancelled"),
                           allow_empty=(o3 != "accepted"))
        if o3 == "accepted":
            self.reconcile_execs(ex, L, view)
        net = self.expect_position(ex, self.expected_net(view["rows"]))
        ex.facts.update({
            "first_rejected_probe": summary(r1), "identical_repost": summary(r2),
            "valid_repost_same_link": summary(r3),
            "rows_after_first": [r.public() for r in v1["rows"]],
            "orders_by_link_final": [r.public() for r in view["rows"]],
            "position_after": fmt(net)})
        self.close_flat(ex)
        return self.finish(ex)

    def e7(self) -> Dict[str, Any]:
        ex = self.begin("E7", "Lost-acknowledgement simulation",
                        "Submit, DISCARD the response, recover purely by "
                        "querying by orderLinkId (realtime then history)")
        L = self.link(ex.id, 1)
        t_sent = self.clock()
        _resp, _o = self.place(ex, "post_response_discarded",
                               self.body("Sell", self.sizing.q0, L), "open",
                               discard=True)
        first_rt = first_hist = first_any = None
        log = []
        for i in range(1, E7_MAX_POLLS + 1):
            view = self.lookup(L, ":recover")
            secs = round(self.clock() - t_sent, 3)
            log.append({"poll": i, "seconds_since_send": secs,
                        "in_realtime": view["in_realtime"],
                        "in_history": view["in_history"],
                        "statuses": [r.status for r in view["rows"]]})
            if view["in_realtime"] and first_rt is None:
                first_rt = (i, secs)
            if view["in_history"] and first_hist is None:
                first_hist = (i, secs)
            if view["rows"] and first_any is None:
                first_any = (i, secs)
            if view["rows"] and all(r.status in TERMINAL_STATUSES
                                    for r in view["rows"]):
                break
            self.sleep(POLL_INTERVAL_S)
        ex.step("recover_by_link_polls", polls=log)
        if first_any is None:
            raise Ambiguity("AMBIG_NOT_VISIBLE",
                            f"{L} not visible by orderLinkId after {E7_MAX_POLLS}"
                            " polls; the submit outcome is unknown")
        view = self.settle(ex, L, name="settle")
        self.reconcile_execs(ex, L, view)
        net = self.expect_position(ex, self.expected_net(view["rows"]))
        ex.facts.update({
            "first_visible_realtime": first_rt, "first_visible_history": first_hist,
            "first_visible_any": first_any,
            "polls_until_visible": first_any[0],
            "seconds_until_visible": first_any[1],
            "orders_by_link": [r.public() for r in view["rows"]],
            "position_after": fmt(net)})
        self.close_flat(ex)
        return self.finish(ex)

    def e8_submit(self, ex: Exp) -> None:
        L = self.link("E8", 1)
        body = self.body("Sell", self.sizing.q0, L, otype="Limit",
                         price=self.sizing.limit_price_1)
        r1, o1 = self.place(ex, "post_1", body, "open")
        self.must_accept(ex, "post_1", r1, o1)
        submitted_ms = self.now_ms()
        t = self.clock()
        view = self.lookup(L, ":immediate")
        ex.step("query_immediate", seconds_since_submit=round(self.clock() - t, 3),
                in_realtime=view["in_realtime"], in_history=view["in_history"],
                rows=[r.public() for r in view["rows"]])
        ex.facts["submit"] = summary(r1)
        ex.facts["query_immediate"] = {"in_realtime": view["in_realtime"],
                                       "in_history": view["in_history"],
                                       "statuses": [r.status for r in view["rows"]]}
        self.manifest["pending"]["E8"] = {
            "state": "SUBMITTED", "link": L, "qty": fmt(self.sizing.q0),
            "price": fmt(self.sizing.limit_price_1), "side": "Sell",
            "submitted_ms": submitted_ms, "run_id": self.run_id}
        self.manifest.save()

    def e8_recover(self, ex: Exp, *, label: str, delay_s: Optional[float] = None
                   ) -> None:
        """Find the order using ONLY the persisted manifest, by orderLinkId."""
        pending = self.manifest["pending"].get("E8")
        if not pending or pending.get("state") != "SUBMITTED":
            raise Refused("the manifest holds no SUBMITTED E8 order")
        L = pending["link"]
        validate_link_id(L)
        if delay_s is None:
            delay_s = (self.now_ms() - int(pending["submitted_ms"])) / 1000.0
        view = self.lookup(L, f":{label}")
        ex.step(f"query_{label}", seconds_since_submit=round(delay_s, 3),
                in_realtime=view["in_realtime"], in_history=view["in_history"],
                rows=[r.public() for r in view["rows"]])
        ex.facts.setdefault("queries", []).append({
            "label": label, "seconds_since_submit": round(delay_s, 3),
            "in_realtime": view["in_realtime"], "in_history": view["in_history"],
            "statuses": [r.status for r in view["rows"]]})
        if not view["rows"]:
            raise Ambiguity("AMBIG_NOT_VISIBLE",
                            f"{L} not visible by orderLinkId {label}")

    def e8_finish(self, ex: Exp) -> Dict[str, Any]:
        pending = self.manifest["pending"]["E8"]
        L = pending["link"]
        view = self.wait_open(ex, L, "wait_open_before_cancel")
        self.cancel(ex, "cancel_by_link", link=L)
        self.settle(ex, L, allowed=("Cancelled",), name="settle_cancelled")
        pending["state"] = "RECOVERED"
        self.manifest.save()
        return self.finish(ex)

    def e8_full(self) -> Dict[str, Any]:
        ex = self.begin("E8", "Query immediately vs after delay vs after restart",
                        "Visibility of a resting order by orderLinkId "
                        "immediately, after a delay, and after the state is "
                        "reloaded from the persisted manifest")
        ex.facts["restart_note"] = (
            "In --phase full the 'restart' is SIMULATED in-process: the "
            "manifest is re-read from disk and only its contents are used. "
            "It is not a process restart. Use --phase submit then "
            "--phase recover for a real one.")
        self.e8_submit(ex)
        self.sleep(E8_DELAY_S)
        self.e8_recover(ex, label="after_delay", delay_s=E8_DELAY_S)
        reloaded = Manifest.load(self.manifest.path, self.manifest.redactor)
        self.manifest.data["pending"] = reloaded["pending"]
        self.e8_recover(ex, label="after_simulated_restart_manifest_reload")
        return self.e8_finish(ex)

    def _single_market(self, eid: str, title: str, question: str):
        ex = self.begin(eid, title, question)
        L = self.link(eid, 1)
        r1, o1 = self.place(ex, "post_1", self.body("Sell", self.sizing.q0, L),
                            "open")
        self.must_accept(ex, "post_1", r1, o1)
        ex.facts["first"] = summary(r1)
        return ex, L

    def e9(self) -> Dict[str, Any]:
        ex, L = self._single_market(
            "E9", "Order history vs realtime visibility of a filled order",
            "Where a Filled market order is visible by orderLinkId, poll by poll")
        log = []
        first_rt = first_hist = None
        view = None
        for i in range(1, VISIBLE_POLLS + 1):
            view = self.lookup(L, ":e9")
            log.append({"poll": i, "in_realtime": view["in_realtime"],
                        "in_history": view["in_history"],
                        "statuses": [r.status for r in view["rows"]]})
            if view["in_realtime"] and first_rt is None:
                first_rt = i
            if view["in_history"] and first_hist is None:
                first_hist = i
            if view["in_history"] and all(r.status in TERMINAL_STATUSES
                                          for r in view["rows"]):
                break
            self.sleep(POLL_INTERVAL_S)
        ex.step("visibility_polls", polls=log)
        if view is None or not view["rows"]:
            raise Ambiguity("AMBIG_NOT_VISIBLE", f"{L} not visible")
        view = self.settle(ex, L, name="settle")
        self.reconcile_execs(ex, L, view)
        net = self.expect_position(ex, self.expected_net(view["rows"]))
        ex.facts.update({"polls": log, "first_poll_in_realtime": first_rt,
                         "first_poll_in_history": first_hist,
                         "final_in_realtime": view["in_realtime"],
                         "final_in_history": view["in_history"],
                         "orders_by_link": [r.public() for r in view["rows"]],
                         "position_after": fmt(net)})
        self.close_flat(ex)
        return self.finish(ex)

    def e10(self) -> Dict[str, Any]:
        ex, L = self._single_market(
            "E10", "Execution list vs order cumExecQty",
            "sum(execQty) and unique execIds from /v5/execution/list against "
            "the order's cumExecQty")
        view = self.settle(ex, L, name="settle")
        by_link = self.reconcile_execs(ex, L, view, name="executions_by_link")
        by_id = []
        for row in view["rows"]:
            rows = self.executions(order_id=row.order_id)
            qty = sum((dec(r.get("execQty")) or Decimal(0) for r in rows
                       if str(r.get("execType") or "Trade") == "Trade"),
                      Decimal(0))
            by_id.append({"order_id": row.order_id, "exec_rows": len(rows),
                          "unique_exec_ids": len({r.get("execId") for r in rows}),
                          "exec_sum": fmt(qty), "cum_exec_qty": fmt(row.cum),
                          "matches": qty == row.cum})
            if qty != row.cum or len({r.get("execId") for r in rows}) != len(rows):
                ex.step("executions_by_order_id", detail=by_id)
                raise Ambiguity("AMBIG_EXEC_SUM_MISMATCH",
                                f"by orderId: sum {fmt(qty)} vs cumExecQty "
                                f"{fmt(row.cum)}", detail=by_id)
        ex.step("executions_by_order_id", detail=by_id)
        net = self.expect_position(ex, self.expected_net(view["rows"]))
        ex.facts.update({
            "by_link": {k: v for k, v in by_link.items() if k != "exec_rows_raw"},
            "by_order_id": by_id,
            "orders_by_link": [r.public() for r in view["rows"]],
            "mismatch_flagged": False, "position_after": fmt(net)})
        self.close_flat(ex)
        return self.finish(ex)

    def e11(self) -> Dict[str, Any]:
        ex, L = self._single_market(
            "E11", "Close the tiny short with a reduce-only Buy",
            "Open a minimum short, close it with a reduce-only Buy and verify "
            "the position is exactly flat")
        view = self.settle(ex, L, name="settle_open")
        net = self.expect_position(ex, self.expected_net(view["rows"]),
                                   "position_open")
        ex.facts["position_open"] = fmt(net)
        self.close_flat(ex, detailed=True)
        ex.facts["position_after_close"] = "0"
        return self.finish(ex)


EXPERIMENTS: Dict[str, Dict[str, Any]] = {
    "E1": {"fn": "e1", "title": "immediate identical re-POST (market)",
           "creates": 3, "worst": 4},
    "E2": {"fn": "e2", "title": "re-POST with different qty (market)",
           "creates": 3, "worst": 4},
    "E3": {"fn": "e3", "title": "re-POST with different price (resting limit)",
           "creates": 2, "worst": 2},
    "E4": {"fn": "e4", "title": "re-POST after the first order FILLED",
           "creates": 3, "worst": 4},
    "E5": {"fn": "e5", "title": "re-POST after the first order CANCELLED",
           "creates": 2, "worst": 2},
    "E6": {"fn": "e6", "title": "re-POST after the first submission REJECTED",
           "creates": 4, "worst": 4},
    "E7": {"fn": "e7", "title": "lost-acknowledgement recovery by orderLinkId",
           "creates": 2, "worst": 2},
    "E8": {"fn": "e8_full", "title": "query immediately / after delay / "
           "after restart (resting limit)", "creates": 1, "worst": 1},
    "E9": {"fn": "e9", "title": "realtime vs history visibility of a filled "
           "order", "creates": 2, "worst": 2},
    "E10": {"fn": "e10", "title": "execution list vs cumExecQty",
            "creates": 2, "worst": 2},
    "E11": {"fn": "e11", "title": "close with reduce-only Buy, verify exactly "
            "flat", "creates": 2, "worst": 2},
}
NOT_RUN_ALWAYS = [{
    "id": "PARTIAL_FILL", "status": "NOT_RUN",
    "why": ("A partial fill cannot be forced on the testnet with a market "
            "order, and a limit-order version needs a counterparty to take "
            "part of a resting order: with the order sized at the venue's "
            "minimum lot (one or two lot steps) it cannot be split, and a "
            "larger order would break the hard notional limit. No partial-"
            "fill behaviour was observed or is claimed. Any partial fill "
            "that does appear during a run is treated as ambiguity and halts "
            "the run."),
}, {
    "id": "E3_DIFFERENT_SIDE", "status": "NOT_RUN",
    "why": ("A different side on a reused id would be a Buy, which this tool "
            "only ever sends reduce-only; with no position to reduce it "
            "tests the reduce-only rule rather than order identity. Not "
            "run; no claim is made.")}]


# ---------------------------------------------------------------------------
# the final flat step (mandatory)
# ---------------------------------------------------------------------------


def capture_state(lab: Lab, why: str) -> Dict[str, Any]:
    """Read-only snapshot of everything this tool knows about. Never raises."""
    snap: Dict[str, Any] = {"why": why, "captured_ms": lab.now_ms()}

    def attempt(name: str, fn: Callable[[], Any]) -> None:
        try:
            snap[name] = fn()
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, KeyboardInterrupt):
                raise
            snap[name] = {"error": f"{type(exc).__name__}: {exc}"}
    attempt("position", lambda: (lambda n_r: {"net": fmt(n_r[0]),
                                              "rows": n_r[1]})(lab.read_position()))
    attempt("open_orders", lambda: [dict(o) for o in lab.read_open_orders()])
    links: Dict[str, Any] = {}
    for link in lab.manifest.known_links()[-30:]:
        try:
            view = lab.lookup(link, ":capture")
            execs = lab.executions(link=link)
            links[link] = {"orders": [r.public() for r in view["rows"]],
                           "executions": execs}
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, KeyboardInterrupt):
                raise
            links[link] = {"error": f"{type(exc).__name__}: {exc}"}
    snap["by_link"] = links
    lab.rec.event("captured_venue_state", **snap)
    return snap


def final_step(lab: Lab, *, allow_open_links: Sequence[str] = ()
               ) -> Dict[str, Any]:
    """Flat-position and open-order check, with ONE bounded reduce-only close
    attempt if needed. Returns a verdict; never raises."""
    gw = lab.gw
    gw.enter_final_mode()
    out: Dict[str, Any] = {"flat": False, "clean": False, "close_attempted": False,
                           "cancel_attempts": 0}
    allow = set(allow_open_links)

    def read() -> Tuple[Optional[Decimal], Optional[List[Dict[str, Any]]], str]:
        try:
            net, _rows = lab.read_position()
            opens = lab.read_open_orders()
            return net, opens, ""
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, KeyboardInterrupt):
                raise
            return None, None, f"{type(exc).__name__}: {exc}"

    def foreign(opens: List[Dict[str, Any]]) -> List[str]:
        return [str(o.get("orderLinkId")) for o in opens
                if str(o.get("orderLinkId") or "") not in allow]
    net, opens, err = read()
    out["first_read"] = {"position": None if net is None else fmt(net),
                         "open_orders": None if opens is None else
                         [o.get("orderLinkId") for o in opens], "error": err}
    if net is not None and opens is not None and net == 0 and not foreign(opens):
        out.update(flat=True, clean=True, position="0",
                   open_orders=[o.get("orderLinkId") for o in opens])
        lab.rec.event("final_step", **out)
        return out
    # one bounded clean-up: cancel THIS tool's open orders, one reduce-only close
    try:
        if opens:
            for o in opens:
                link = str(o.get("orderLinkId") or "")
                oid = str(o.get("orderId") or "")
                if link.startswith(LINK_PREFIX + "-") and link not in allow \
                        and oid and out["cancel_attempts"] < 10:
                    gw.known_order_ids.add(oid)
                    out["cancel_attempts"] += 1
                    gw.request("POST", CANCEL, body={
                        "category": "linear", "symbol": lab.symbol,
                        "orderId": oid}, kind="cancel", tag="final:cancel")
        if net is not None and net != 0:
            opened = Decimal(lab.manifest["opens_attempted"])
            if abs(net) > opened:
                out["close_refused"] = (
                    f"venue position {fmt(abs(net))} exceeds everything this "
                    f"tool ever tried to open ({fmt(opened)}); not touching it")
            elif abs(net) > lab.sizing.qmax * EMERGENCY_QTY_MULT:
                out["close_refused"] = "position exceeds the emergency close bound"
            else:
                side = "Buy" if net < 0 else "Sell"
                link = experiment_link_id(
                    lab.run_id, "fin", lab.gw.creates + 1, lab.symbol)
                body = lab.body(side, abs(net), link, reduce_only=True)
                lab.manifest.add_link(link, "FIN", "emergency_close", side,
                                      body["qty"], "emergency")
                out["close_attempted"] = True
                resp = gw.request("POST", CREATE, body=body, kind="emergency",
                                  tag="final:close")
                out["close_response"] = summary(resp)
                for _ in range(POS_POLLS):
                    lab.sleep(POLL_INTERVAL_S)
                    n2, o2, e2 = read()
                    if n2 is not None and n2 == 0:
                        break
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, KeyboardInterrupt):
            raise
        out["cleanup_error"] = f"{type(exc).__name__}: {exc}"
    net, opens, err = read()
    out["second_read"] = {"position": None if net is None else fmt(net),
                          "open_orders": None if opens is None else
                          [o.get("orderLinkId") for o in opens], "error": err}
    out["flat"] = net is not None and net == 0
    out["clean"] = opens is not None and not foreign(opens)
    out["position"] = None if net is None else fmt(net)
    out["open_orders"] = None if opens is None else [o.get("orderLinkId")
                                                     for o in opens]
    lab.rec.event("final_step", **out)
    return out


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def yesno(value: Any) -> str:
    return "not recorded" if value is None else ("yes" if value else "no")


def conclusion_rows(results: Sequence[Mapping[str, Any]]) -> List[Tuple[str, str]]:
    """Facts only, one line each, in the owner's question form. Nothing here
    generalises beyond the observation."""
    rows: List[Tuple[str, str]] = []
    for res in results:
        f = res.get("facts", {})
        eid = res["id"]
        if res.get("status") != "COMPLETED":
            rows.append((eid, f"{res['title']}: status {res.get('status')} "
                              "(see halt record)"))
            continue
        if eid in ("E1", "E2", "E4"):
            rp = f.get("repost", {})
            label = {"E1": "same link id re-POST, identical payload, "
                           "immediately",
                     "E2": "same link id re-POST, different qty "
                           f"({f.get('first_qty')} -> {f.get('repost_qty')})",
                     "E4": "same link id re-POST after FILL"}[eid]
            rows.append((eid, f"{label}: retCode {rp.get('ret_code')} "
                              f"retMsg {rp.get('ret_msg')!r}; second order "
                              f"created: {yesno(f.get('second_order_created'))}"
                              f" ({f.get('distinct_order_ids_by_link')} order "
                              "id(s) by link); second execution order: "
                              f"{yesno(f.get('second_execution_exists'))}; "
                              f"position after the re-POST: {f.get('position_after_repost')}"))
        elif eid == "E3":
            rp = f.get("repost", {})
            rows.append((eid, f"same link id re-POST, different price: retCode "
                              f"{rp.get('ret_code')} retMsg {rp.get('ret_msg')!r};"
                              f" second order created: "
                              f"{yesno(f.get('second_order_created'))}; prices "
                              f"on venue rows: {f.get('prices_on_venue_rows')} "
                              f"(sent {f.get('price_1')} then {f.get('price_2')})"))
        elif eid == "E5":
            rp = f.get("repost", {})
            rows.append((eid, "same link id re-POST after CANCEL: retCode "
                              f"{rp.get('ret_code')} retMsg {rp.get('ret_msg')!r};"
                              f" second order created: "
                              f"{yesno(f.get('second_order_created'))}; status "
                              f"after cancel: {f.get('status_after_cancel')}"))
        elif eid == "E6":
            rows.append((eid, "after a REJECTED first submission (reduce-only "
                              "Buy, flat): first retCode "
                              f"{f.get('first_rejected_probe', {}).get('ret_code')}"
                              f" retMsg {f.get('first_rejected_probe', {}).get('ret_msg')!r};"
                              " identical re-POST retCode "
                              f"{f.get('identical_repost', {}).get('ret_code')}"
                              f" retMsg {f.get('identical_repost', {}).get('ret_msg')!r};"
                              " valid opening Sell with the same id: retCode "
                              f"{f.get('valid_repost_same_link', {}).get('ret_code')}"
                              f" retMsg {f.get('valid_repost_same_link', {}).get('ret_msg')!r}"
                              f" outcome {f.get('valid_repost_same_link', {}).get('outcome')}"))
        elif eid == "E7":
            rows.append((eid, "response discarded, recovered by orderLinkId: "
                              f"first visible after {f.get('polls_until_visible')}"
                              f" poll(s) / {f.get('seconds_until_visible')}s "
                              f"(realtime {f.get('first_visible_realtime')}, "
                              f"history {f.get('first_visible_history')})"))
        elif eid == "E8":
            rows.append((eid, "resting order lookups by orderLinkId: " +
                         "; ".join(f"{q['label']} +{q['seconds_since_submit']}s "
                                   f"realtime={q['in_realtime']} "
                                   f"history={q['in_history']} "
                                   f"{q['statuses']}"
                                   for q in f.get("queries", []))))
        elif eid == "E9":
            rows.append((eid, "filled market order: first poll visible in "
                              f"realtime={f.get('first_poll_in_realtime')}, in "
                              f"history={f.get('first_poll_in_history')}; at "
                              f"settle realtime rows={f.get('final_in_realtime')},"
                              f" history rows={f.get('final_in_history')}"))
        elif eid == "E10":
            bl = f.get("by_link", {})
            rows.append((eid, f"executions: {bl.get('exec_rows')} row(s), "
                              f"{bl.get('unique_exec_ids')} unique execId(s), "
                              f"sum {bl.get('exec_sum')} vs cumExecQty "
                              f"{bl.get('cum_exec_total')}; mismatch flagged: "
                              f"{yesno(f.get('mismatch_flagged'))}"))
        elif eid == "E11":
            rows.append((eid, f"open short {f.get('position_open')}, reduce-only"
                              f" Buy close, position after: "
                              f"{f.get('position_after_close')}"))
    return rows


def render_table(rows: Sequence[Tuple[str, str]]) -> List[str]:
    lines = ["RECORDED FACTS (not conclusions; " + NOT_PROOF + ")",
             "-" * 72]
    for eid, text in rows:
        lines.append(f"{eid:>4}  {text}")
    lines.append("-" * 72)
    return lines


def plan_lines(limits: Limits, symbol: str, selected: Sequence[str],
               out_dir: str) -> List[str]:
    expected = sum(EXPERIMENTS[e]["creates"] for e in selected)
    worst = sum(EXPERIMENTS[e]["worst"] for e in selected)
    lines = ["PLAN (no network call has been made)",
             f"  venue: {TESTNET_BASE}   symbol: {symbol}  (testnet only)",
             f"  limits: notional/order <= {limits.max_notional_usd} USD, "
             f"order-create requests <= {limits.max_orders} "
             f"(last {EMERGENCY_RESERVE} reserved for the final flat step), "
             f"duration <= {limits.max_duration_s}s, "
             f"experiments <= {limits.max_experiments}",
             f"  ceilings (cannot be raised): notional {HARD_MAX_NOTIONAL_USD}"
             f" USD, orders {HARD_MAX_ORDERS}, duration {HARD_MAX_DURATION_S}s,"
             f" experiments {HARD_MAX_EXPERIMENTS}, requests {HARD_MAX_REQUESTS}",
             "  order size: the venue's min qty / lot step / min notional, read "
             "at run time (not known offline)",
             f"  order-create requests: {expected} expected, {worst} "
             "worst-case across the selected experiments"]
    for eid in selected:
        e = EXPERIMENTS[eid]
        lines.append(f"    {eid:<4} {e['title']}  (creates: {e['creates']}, "
                     f"worst {e['worst']})")
    lines.append("  NOT RUN, by design:")
    for item in NOT_RUN_ALWAYS:
        lines.append(f"    {item['id']}: {item['why']}")
    lines.append("  allowed endpoints: " + ", ".join(sorted(ALLOWED_ENDPOINTS)))
    lines.append("  ASSUMED venue behaviours (unverified):")
    lines.extend(f"    - {a}" for a in ASSUMPTIONS)
    lines += ["", "COMMANDS (env var NAMES only; set the values yourself)",
              "  export BYBIT_VENUE=testnet USE_TESTNET=1",
              "  export BYBIT_API_KEY=...   BYBIT_API_SECRET=...   "
              "# testnet credentials; LIVE_TRADING_ACK must stay unset",
              "  cd bot",
              "  # 1. dry run (zero network calls)",
              f"  python3 tools/close_testnet_experiment.py --dry-run "
              f"--symbol {symbol}",
              "  # 2. full run",
              f"  python3 tools/close_testnet_experiment.py "
              f"--i-understand-this-places-testnet-orders --symbol {symbol} "
              f"--out-dir {out_dir}",
              "  # 3. submit, kill the process as you like, then recover in a "
              "NEW process",
              f"  python3 tools/close_testnet_experiment.py "
              f"--i-understand-this-places-testnet-orders --phase submit "
              f"--symbol {symbol} --out-dir {out_dir}_restart",
              f"  python3 tools/close_testnet_experiment.py "
              f"--i-understand-this-places-testnet-orders --phase recover "
              f"--symbol {symbol} --out-dir {out_dir}_restart"]
    return lines


# ---------------------------------------------------------------------------
# pre-flight and the run
# ---------------------------------------------------------------------------


def static_refusal(args: argparse.Namespace, cfg: Any,
                   env: Mapping[str, str]) -> Optional[str]:
    """Pre-conditions that need no network. First reason wins."""
    if not args.i_understand_this_places_testnet_orders:
        return ("--i-understand-this-places-testnet-orders was not given; "
                "this tool places real TESTNET orders")
    if env is not None and "LIVE_TRADING_ACK" in env:
        return ("LIVE_TRADING_ACK is present in the environment (value not "
                "read); unset it. This tool refuses to run beside a live "
                "authorisation")
    if cfg is None:
        return "no configuration could be loaded"
    if getattr(cfg, "LIVE_TRADING_ACK", ""):
        return ("a live-trading acknowledgement is present in the "
                "configuration (value not read); unset it")
    import config as _config
    try:
        armed, _why = _config.is_live_authorized(cfg)
    except Exception as exc:  # noqa: BLE001
        return f"live authorisation could not be evaluated ({type(exc).__name__})"
    if armed is not False:
        return "live trading is authorised in this configuration"
    if getattr(cfg, "USE_TESTNET", None) is not True:
        return "USE_TESTNET is not true"
    if str(getattr(cfg, "BYBIT_VENUE", "")).lower() != "testnet":
        return "BYBIT_VENUE is not testnet"
    missing = [name for name in ("BYBIT_API_KEY", "BYBIT_API_SECRET")
               if not getattr(cfg, name, "")]
    if missing:
        return "credentials missing: " + ", ".join(missing) + " (names only)"
    return None


def preflight_venue(lab_factory: Callable[[Instrument, Sizing], Lab],
                    gw: Gateway, symbol: str, limits: Limits, console: Console,
                    clock: Callable[[], float], *, require_clean: bool,
                    persisted: Optional[Dict[str, str]] = None
                    ) -> Tuple[Lab, Dict[str, Any]]:
    """Reads only. Any problem is a refusal."""
    facts: Dict[str, Any] = {}
    scratch = Lab.__new__(Lab)                       # reader before sizing
    scratch.gw, scratch.symbol = gw, symbol
    scratch.rec, scratch.clock = gw.rec, clock
    scratch.sleep = lambda s: None
    scratch.sizing = Sizing(Decimal(1), Decimal(1), Decimal(1), Decimal(1),
                            Decimal(1), Decimal(1))
    try:
        resp = gw.get(INSTRUMENTS, {"category": "linear", "symbol": symbol},
                      tag="instrument")
        rows = scratch._rows(resp, "instruments-info")
        if len(rows) != 1:
            raise Refused(f"instruments-info returned {len(rows)} rows")
        row = rows[0]
        lot, pf = row.get("lotSizeFilter") or {}, row.get("priceFilter") or {}
        if str(row.get("status")) != "Trading":
            raise Refused(f"{symbol} status is {row.get('status')!r}")
        if row.get("contractType") not in (None, "LinearPerpetual"):
            raise Refused(f"{symbol} contractType {row.get('contractType')!r}")
        values = [dec(pf.get("tickSize")), dec(lot.get("qtyStep")),
                  dec(lot.get("minOrderQty")), dec(lot.get("minNotionalValue"))]
        if any(v is None for v in values):
            raise Refused("instrument tick/step/minQty/minNotional unreadable")
        inst = Instrument(symbol, values[0], values[1], values[2], values[3],
                          dec(pf.get("maxPrice")))
        facts["instrument"] = inst.as_dict()
        if persisted:
            # recover: size rules are the ones the run was started with, so a
            # price move cannot make the clean-up refuse or misjudge an order
            sizing = Sizing(**{k: Decimal(v) for k, v in persisted.items()})
        else:
            resp = gw.get(TICKERS, {"category": "linear", "symbol": symbol},
                          tag="ticker")
            trows = scratch._rows(resp, "tickers")
            last, mark = (dec(trows[0].get("lastPrice")) if trows else None,
                          dec(trows[0].get("markPrice")) if trows else None)
            if last is None or mark is None or last <= 0 or mark <= 0:
                raise Refused("last/mark price unreadable")
            if abs(last - mark) / mark > Decimal("0.05"):
                raise Refused(f"last {fmt(last)} and mark {fmt(mark)} disagree "
                              "by more than 5 percent; the price is not "
                              "credible")
            sizing = compute_sizing(inst, last, limits.max_notional_usd)
        facts["sizing"] = sizing.as_dict()
        resp = gw.get(ACCOUNT, {}, tag="account")
        if not resp.ok or not resp.result().get("marginMode"):
            raise Refused("account info is unreadable (no marginMode)")
        facts["account"] = {k: resp.result().get(k) for k in
                            ("marginMode", "unifiedMarginStatus")}
        net, prow = scratch.read_position()      # also proves one-way (idx 0)
        facts["position_before"] = fmt(net)
        opens = scratch.read_open_orders()
        if require_clean:
            if net != 0 or opens:
                raise Refused(
                    "start state is not clean: position "
                    f"{fmt(net)}, open orders "
                    f"{[(o.get('orderLinkId'), o.get('orderStatus')) for o in opens]}"
                    ". Close/cancel on the testnet first; nothing was placed.")
        elif net != 0 or opens:
            facts["recover_start_state"] = {
                "position": fmt(net),
                "open_orders": [o.get("orderLinkId") for o in opens]}
    except Halt as exc:
        raise Refused(f"{exc.code}: {exc.detail}") from exc
    gw.guard = OrderGuard(symbol, inst, sizing)
    lab = lab_factory(inst, sizing)
    return lab, facts


class Interrupted(Exception):
    pass


def _install_signal_handlers() -> None:
    def handler(signum, _frame):
        raise Interrupted(f"signal {signum}")
    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        pass


def execute_run(lab: Lab, selected: Sequence[str], limits: Limits,
                phase: str, manifest: Manifest) -> Tuple[List[Dict[str, Any]],
                                                          Optional[Halt],
                                                          Dict[str, Any]]:
    """Run the plan. Returns (experiment results, halt or None, extras)."""
    results: List[Dict[str, Any]] = []
    halt: Optional[Halt] = None
    extras: Dict[str, Any] = {"allow_open": []}
    started = 0
    try:
        if phase == "submit":
            ex = lab.begin("E8", "E8 submit phase (resting order, then exit)",
                           "Submit a resting order and persist the manifest")
            lab.e8_submit(ex)
            ex.status = "SUBMITTED_AWAITING_RECOVER"
            results.append(ex.result())
            extras["allow_open"] = [manifest["pending"]["E8"]["link"]]
            return results, None, extras
        if phase == "recover":
            ex = Exp(lab, "E8", "E8 recover phase (new process)",
                     "Find the persisted order by orderLinkId and cancel it")
            pending = manifest["pending"].get("E8")
            if not pending or pending.get("state") != "SUBMITTED":
                ex.status = "NOTHING_PENDING"
                ex.step("no_submitted_e8_in_manifest")
                results.append(ex.result())
                return results, None, extras
            try:
                ex.pos_before = fmt(lab.read_position()[0])
                lab.e8_recover(ex, label="after_process_restart")
                lab.e8_finish(ex)
            finally:
                results.append(ex.result())
            return results, None, extras
        for eid in selected:
            spec = EXPERIMENTS[eid]
            if started >= limits.max_experiments:
                manifest["not_run"].append({"id": eid, "why": "max experiments"})
                continue
            if lab.gw.creates + spec["worst"] + EMERGENCY_RESERVE > limits.max_orders:
                manifest["not_run"].append({
                    "id": eid, "why": f"order budget ({lab.gw.creates} sent, "
                    f"worst case {spec['worst']}, reserve {EMERGENCY_RESERVE}, "
                    f"max {limits.max_orders})"})
                continue
            if lab.gw.elapsed() + 90 > limits.max_duration_s:
                manifest["not_run"].append({"id": eid, "why": "duration limit"})
                continue
            started += 1
            manifest["experiments"][eid] = "RUNNING"
            manifest.save()
            lab.console.out(f"running {eid}: {spec['title']}")
            try:
                results.append(getattr(lab, spec["fn"])())
                manifest["experiments"][eid] = "COMPLETED"
            except BaseException:
                manifest["experiments"][eid] = "HALTED"
                raise
            finally:
                manifest.save()
    except Halt as exc:
        halt = exc
    except (KeyboardInterrupt, Interrupted) as exc:
        halt = Ambiguity("AMBIG_INTERRUPTED", f"{type(exc).__name__}: {exc}")
    except Refused as exc:
        halt = Ambiguity("AMBIG_REFUSED_MID_RUN", str(exc))
    except Exception as exc:  # noqa: BLE001
        halt = Ambiguity("AMBIG_INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
    return results, halt, extras


def run(args: argparse.Namespace, client: Any, cfg: Any, redactor: Redactor,
        console: Console, *, clock, now_ms, sleep) -> int:
    limits: Limits = args.limits
    symbol, phase = args.symbol, args.phase
    out_dir = args.out_dir
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    manifest_path = os.path.join(out_dir, "manifest.json")
    if phase == "recover":
        if not os.path.exists(manifest_path):
            console.err(f"REFUSED: no manifest at {manifest_path}")
            return EXIT_REFUSED
        manifest = Manifest.load(manifest_path, redactor)
        if manifest["symbol"] != symbol:
            console.err(f"REFUSED: manifest symbol {manifest['symbol']!r} != "
                        f"--symbol {symbol!r}")
            return EXIT_REFUSED
        run_id = manifest["run_id"]
    else:
        if os.path.exists(manifest_path) or os.path.exists(
                os.path.join(out_dir, "records.jsonl")):
            console.err("REFUSED: the output directory already holds a run; "
                        "use a new --out-dir. Nothing was placed.")
            return EXIT_REFUSED
        run_id = args.run_id or time.strftime("%y%m%d%H%M%S", time.gmtime())
        manifest = Manifest(manifest_path, redactor)
        manifest["run_id"], manifest["symbol"] = run_id, symbol
        manifest["created_ms"] = now_ms()
    rec = Recorder(out_dir, redactor, clock, now_ms, append=(phase == "recover"))
    manifest["limits"] = limits.as_dict()
    manifest["host"] = TESTNET_HOSTNAME
    manifest["phase_history"].append({"phase": phase, "started_ms": now_ms()})
    manifest.save()
    rec.event("run_start", phase=phase, run_id=run_id, symbol=symbol,
              limits=limits.as_dict(), host=TESTNET_HOSTNAME,
              assumptions=list(ASSUMPTIONS), note=NOT_PROOF)
    gw = Gateway(client, symbol=symbol, limits=limits, rec=rec,
                 manifest=manifest, clock=clock, now_ms=now_ms)

    def make_lab(inst: Instrument, sizing: Sizing) -> Lab:
        return Lab(gw, rec, manifest, console, inst, sizing, run_id, symbol,
                   clock=clock, now_ms=now_ms, sleep=sleep)
    try:
        lab, facts = preflight_venue(
            make_lab, gw, symbol, limits, console, clock,
            require_clean=(phase != "recover"),
            persisted=(manifest.data.get("sizing")
                       if phase == "recover" else None))
        if phase != "recover":
            manifest["sizing"] = facts["sizing"]
            manifest["instrument"] = facts["instrument"]
            manifest.save()
    except Refused as exc:
        rec.event("refused", reason=str(exc))
        console.err(f"REFUSED: {exc}")
        console.err(f"nothing was placed ({gw.creates} order requests sent).")
        rec.close()
        return EXIT_REFUSED
    except Halt as exc:
        rec.event("refused", reason=str(exc))
        console.err(f"REFUSED: {exc}")
        rec.close()
        return EXIT_REFUSED
    rec.event("preflight_ok", **facts)
    console.out(f"preflight OK: testnet, one-way, flat/clean start verified "
                f"(phase {phase}). {NOT_PROOF}.")
    selected = [e for e in args.experiments]
    if phase == "full":
        first = selected[0] if selected else None
        if first is not None and EXPERIMENTS[first]["worst"] + EMERGENCY_RESERVE \
                > limits.max_orders:
            console.err("REFUSED: --max-orders is too low for the first "
                        "selected experiment plus the final-step reserve")
            rec.close()
            return EXIT_REFUSED
    _install_signal_handlers()

    results, halt, extras = execute_run(lab, selected, limits, phase, manifest)
    captured: Dict[str, Any] = {}
    final: Dict[str, Any]
    try:
        if halt is not None:
            console.err(f"HALT {halt.code}: {halt.detail}")
            try:
                gw.enter_final_mode()
                captured = capture_state(lab, f"{halt.code}: {halt.detail}")
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, KeyboardInterrupt):
                    pass
                captured = {"error": f"{type(exc).__name__}: {exc}"}
        final = final_step(lab, allow_open_links=extras["allow_open"])
    except BaseException as exc:  # noqa: BLE001
        final = {"flat": False, "clean": False,
                 "error": f"{type(exc).__name__}: {exc}"}
    flat_ok = bool(final.get("flat")) and bool(final.get("clean"))
    if not flat_ok:
        code = EXIT_NOT_FLAT
    elif halt is not None:
        code = halt.exit_code
    else:
        code = EXIT_OK
    manifest["phase_history"][-1].update(ended_ms=now_ms(), exit_code=code)
    manifest.save()
    rows = conclusion_rows(results)
    report = {
        "schema": SCHEMA, "phase": phase, "run_id": run_id, "symbol": symbol,
        "host": TESTNET_HOSTNAME, "note": NOT_PROOF, "exit_code": code,
        "limits": limits.as_dict(), "preflight": facts,
        "order_creates_sent": gw.creates, "requests_sent": gw.requests,
        "cancels_sent": gw.cancels, "duration_s": round(gw.elapsed(), 3),
        "halt": None if halt is None else {"code": halt.code,
                                           "detail": halt.detail,
                                           "captured": halt.captured},
        "captured_venue_state": captured, "final_state": final,
        "experiments": results, "not_run": NOT_RUN_ALWAYS + manifest["not_run"],
        "recorded_facts_table": [{"id": a, "observed": b} for a, b in rows],
        "assumptions_unverified": list(ASSUMPTIONS)}
    name = "report.json" if phase == "full" else f"report_{phase}.json"
    report_path = os.path.join(out_dir, name)
    fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, redactor.dumps(report).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    rec.event("run_end", exit_code=code, final_state=final)
    rec.close()
    for line in render_table(rows):
        console.out(line)
    console.out(f"order-create requests sent: {gw.creates}   requests: "
                f"{gw.requests}   report: {report_path}")
    console.out("NOT RUN: " + "; ".join(i["id"] for i in
                                        NOT_RUN_ALWAYS + manifest["not_run"]
                                        if "id" in i) + " (reasons in report)")
    if code == EXIT_NOT_FLAT:
        banner = "!" * 72
        console.err(banner)
        console.err("NOT FLAT: the testnet account is NOT verified flat/clean. "
                    "Look at the venue now and close/cancel by hand.")
        console.err(f"final state: {json.dumps(final, default=str)[:1500]}")
        console.err(banner)
    elif phase == "submit" and code == EXIT_OK:
        console.out("SUBMITTED. One far-from-market post-only order is "
                    "resting. Next, in a NEW process: --phase recover "
                    f"--symbol {symbol} --out-dir {out_dir}")
    if halt is not None:
        console.err(f"exit {code}: halted on {halt.code}; venue state captured "
                    "in the report")
    return code


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--i-understand-this-places-testnet-orders",
                    dest="i_understand_this_places_testnet_orders",
                    action="store_true",
                    help="required: this tool places real TESTNET orders")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, limits and commands; zero network "
                         "calls")
    ap.add_argument("--self-check-offline", action="store_true",
                    help="run the whole harness against a FAKE venue (no "
                         "network, no credentials)")
    ap.add_argument("--phase", choices=("full", "submit", "recover"),
                    default="full")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--experiments", default="all",
                    help="comma list of E1..E11 (default all)")
    ap.add_argument("--max-notional-usd", type=float, default=None,
                    help=f"can only LOWER the {HARD_MAX_NOTIONAL_USD} ceiling")
    ap.add_argument("--max-orders", type=float, default=None,
                    help=f"can only LOWER the {HARD_MAX_ORDERS} ceiling")
    ap.add_argument("--max-duration-s", type=float, default=None,
                    help=f"can only LOWER the {HARD_MAX_DURATION_S}s ceiling")
    ap.add_argument("--max-experiments", type=float, default=None,
                    help=f"can only LOWER the {HARD_MAX_EXPERIMENTS} ceiling")
    return ap


def _default_cfg_loader():
    from tools.carry_preflight import load_testnet_config
    return load_testnet_config()


def _default_client_factory(cfg: Any):
    import persistence
    store = persistence.StateStore(os.path.join(
        tempfile.mkdtemp(prefix="order_identity_"), "state.db"))
    return bc.BybitClient(config=cfg, store=store)


def _self_check_parts():
    """The fake venue lives with the tests; absent from a deployed image."""
    tests_dir = os.path.join(os.path.dirname(HERE), "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import fake_experiment_venue as fev
    return fev


def main(argv: Optional[List[str]] = None, *,
         client_factory: Optional[Callable[[Any], Any]] = None,
         cfg_loader: Optional[Callable[[], Tuple[Any, Any]]] = None,
         env: Optional[Mapping[str, str]] = None,
         clock: Optional[Callable[[], float]] = None,
         now_ms: Optional[Callable[[], int]] = None,
         sleep: Optional[Callable[[float], None]] = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console(Redactor())
    if not re.fullmatch(r"[A-Z0-9]{2,20}USDT", args.symbol):
        console.err("REFUSED: --symbol must be a USDT linear symbol such as "
                    "BTCUSDT")
        return EXIT_REFUSED
    try:
        limits = Limits.resolve(args.max_notional_usd, args.max_orders,
                                args.max_duration_s, args.max_experiments)
    except ValueError as exc:
        console.err(f"REFUSED: {exc}")
        return EXIT_REFUSED
    args.limits = limits
    names = [n.strip().upper() for n in args.experiments.split(",") if n.strip()]
    if names == ["ALL"]:
        names = list(EXPERIMENTS)
    unknown = [n for n in names if n not in EXPERIMENTS]
    if unknown or not names or len(set(names)) != len(names):
        console.err(f"REFUSED: --experiments {args.experiments!r}: unknown, "
                    "empty or repeated entries")
        return EXIT_REFUSED
    args.experiments = names
    out_dir = args.out_dir or "runs/order_identity_1"
    if args.dry_run:
        for line in plan_lines(limits, args.symbol, names, out_dir):
            console.out(line)
        return EXIT_OK
    if not args.out_dir:
        console.err("--out-dir is required")
        return EXIT_CANNOT_START

    clock = clock or time.monotonic
    now_ms = now_ms or (lambda: int(time.time() * 1000))
    sleep = sleep or time.sleep

    if args.self_check_offline:
        fev = _self_check_parts()
        venue = fev.FakeVenue(clock=fev.FakeClock())
        args.i_understand_this_places_testnet_orders = True
        cfg = fev.fake_cfg()
        redactor = Redactor([venue.api_key, venue.api_secret])
        return run(args, venue, cfg, redactor, Console(redactor),
                   clock=venue.clock.monotonic, now_ms=venue.clock.now_ms,
                   sleep=venue.clock.sleep)

    if env is None:
        env = os.environ
    cfg_loader = cfg_loader or _default_cfg_loader
    try:
        cfg, refusal = cfg_loader()
    except Exception as exc:  # noqa: BLE001
        console.err(f"could not load the configuration: {type(exc).__name__}")
        return EXIT_CANNOT_START
    if refusal is not None:
        console.err(f"REFUSED: {refusal[0]}")
        return refusal[1] if refusal[1] else EXIT_REFUSED
    redactor = Redactor([getattr(cfg, "BYBIT_API_KEY", ""),
                         getattr(cfg, "BYBIT_API_SECRET", ""),
                         env.get("BYBIT_API_KEY", ""),
                         env.get("BYBIT_API_SECRET", "")])
    console = Console(redactor)
    why = static_refusal(args, cfg, env)
    if why:
        console.err(f"REFUSED: {why}. Nothing was placed; no request was made.")
        return EXIT_REFUSED
    client_factory = client_factory or _default_client_factory
    try:
        client = client_factory(cfg)
    except Exception as exc:  # noqa: BLE001
        console.err(f"could not build the venue client: {type(exc).__name__}")
        return EXIT_CANNOT_START
    redactor.add_secret(getattr(client, "api_key", ""))
    redactor.add_secret(getattr(client, "api_secret", ""))
    why = client_refusal(client)
    if why:
        console.err(f"REFUSED: {why}. Nothing was placed; no request was made.")
        return EXIT_REFUSED
    return run(args, client, cfg, redactor, console, clock=clock,
               now_ms=now_ms, sleep=sleep)


if __name__ == "__main__":
    import legacy_executor_guard as _LG
    raise SystemExit(_LG.run_guarded_cli(main, env=os.environ))
