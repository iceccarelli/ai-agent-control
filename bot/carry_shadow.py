#!/usr/bin/env python3
"""Forward-shadow runtime for the carry book: the real engine, a simulated venue.

WHAT THIS IS
============
A repository-native runtime that feeds fresh market observations to the REAL
``CarryEngine.on_candle`` through the REAL ``take_snapshot`` / ``assert_fresh``
path, fills whatever the engine asks for against a SIMULATED venue, and keeps
the resulting book, events, provenance and modelled economics in a SEPARATE
SQLite file. Nothing in the strategy, entry gate, sizing, funding streaks,
unwind or risk thresholds is reimplemented here: this module supplies a venue,
a clock and a ledger, and the engine does the deciding.

WHAT IT CANNOT DO (enforced by construction and by tests)
=========================================================
* It has no exchange client. ``ShadowBroker`` holds no HTTP object, imports no
  ``bybit_connection`` / ``carry_broker`` / ``trading_engine``, and refuses (by
  raising) any attribute that is not on its explicit allow-list, so an order,
  cancel, amend or leverage call has nothing to reach. Its "orders" are rows in
  the shadow database.
* The only network read is ``PublicVenueReader`` (``--live``): unauthenticated
  GETs on four public market-data paths. It carries no key and no signature.
* It never opens the account's trading-state store or ledger. A database path
  that looks like one is refused.

THE ECONOMICS ARE MODELLED, NOT REAL
====================================
Every figure is labelled ``measured`` (taken from a venue observation),
``estimated`` (derived from measured values by a declared model),
``assumed`` (a declared constant that was not read from the venue) or
``unavailable`` (no honest number exists; excluded from the net and counted).
A shadow database is bound to ONE evidence class at creation
(``live_public``, ``supplied``, ``historical_replay``, ``synthetic_fixture``)
and refuses observations of any other, so a replay can never be mixed into a
forward book. Only ``live_public`` observations were collected by this process;
only they can ever be called forward evidence, and even then the P&L is a model.

EXIT CODES
==========
  0  step recorded, or a duplicate observation (no-op)
  1  internal fault; the step was rolled back
  2  usage / unreadable input
  3  observation refused: stale, incomplete or inconsistent (nothing booked)
  4  configuration, evidence class or database refused (frozen-config mismatch)
  5  the shadow book is HALTED by the engine; a human must clear it

    python3 carry_shadow.py step --db state/carry_shadow.db --origin live_public \\
        --venue testnet --live --borrow-apr 0.0 --execution-mode overlay \\
        --inventory-btc 0.01 --margin-multiple 5
    python3 carry_shadow.py report --db state/carry_shadow.db
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA = "carry_shadow/1"

EXIT_OK, EXIT_FAULT, EXIT_USAGE, EXIT_REFUSED_INPUT, EXIT_CONFIG, EXIT_HALTED = (
    0, 1, 2, 3, 4, 5)

ORIGINS = ("live_public", "supplied", "historical_replay", "synthetic_fixture")
#: Origins whose freshness may be judged against the observation's own clock.
#: Anything that claims to be a present-day observation is judged against the
#: wall clock, so a stale file can never pass as a fresh market.
OBSERVATION_CLOCK_ORIGINS = ("historical_replay", "synthetic_fixture")

#: Cost / value provenance classes.
MEASURED, ESTIMATED, ASSUMED, UNAVAILABLE = (
    "measured", "estimated", "assumed", "unavailable")

SYMBOL = "BTCUSDT"
#: Declared, not read from the venue (these are the published BTCUSDT rules).
ASSUMED_LINEAR_RULES = {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 0.0}
ASSUMED_SPOT_RULES = {"qty_step": 1e-6, "min_qty": 4.8e-5, "min_notional": 1.0}

#: Public market-data paths, and nothing else, that the live reader may touch.
PUBLIC_PATHS = ("/v5/market/time", "/v5/market/tickers",
                "/v5/market/funding/history")
#: Testnet only. There is deliberately no mainnet entry: this project's boundary
#: is Bybit testnet, and a runtime that cannot name the mainnet host cannot be
#: pointed at it by a flag. ("historical" below is a label, not a host.)
VENUE_BASE = {"testnet": "https://api-testnet.bybit.com"}

REQUIRED_OBS = ("perp_mark", "spot_mark", "funding_bps", "funding_print_bps",
                "funding_print_ms", "observed_at_s")


class ShadowRefusal(RuntimeError):
    """A refusal with the exit code the CLI must return."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ShadowIsolationError(AttributeError):
    """The shadow venue was asked for a capability it deliberately lacks."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def _finite_pos(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def code_revision() -> Dict[str, Any]:
    """The source revision, or an explicit 'unavailable'. Never invented."""
    env = os.environ.get("CODE_REVISION", "").strip()
    if env:
        return {"revision": env, "dirty": None, "source": "env:CODE_REVISION"}
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE,
                             capture_output=True, text=True, timeout=5)
        if rev.returncode == 0 and rev.stdout.strip():
            dirty = subprocess.run(["git", "status", "--porcelain",
                                    "--", "."], cwd=HERE, capture_output=True,
                                   text=True, timeout=5)
            return {"revision": rev.stdout.strip(),
                    "dirty": bool(dirty.stdout.strip()), "source": "git"}
    except Exception:  # noqa: BLE001
        pass
    return {"revision": "unavailable", "dirty": None, "source": "none"}


# ---------------------------------------------------------------------------
# configuration (frozen per database)
# ---------------------------------------------------------------------------


def build_config(*, origin: str, venue: str, borrow_apr: float,
                 execution_mode: str, inventory_btc: Optional[float],
                 margin_multiple: Optional[float],
                 notional_usd: Optional[float] = None) -> Dict[str, Any]:
    """Everything that defines the strategy and the model, as one dict.

    The fingerprint of this dict is stored with the book; a later step with a
    different one is refused, so a shadow book cannot quietly change what it is
    measuring. Engine and gate constants are READ from the modules, not set.
    """
    import carry_costs
    import carry_engine
    import carry_risk
    import shadow

    cap = float(shadow.SHADOW_MAX_NOTIONAL_USD)
    notional = cap if notional_usd is None else float(notional_usd)
    if origin not in ORIGINS:
        raise ShadowRefusal(EXIT_USAGE, f"origin must be one of {ORIGINS}")
    if notional > cap:
        raise ShadowRefusal(
            EXIT_CONFIG, f"notional ${notional} exceeds the ${cap} cap")
    if execution_mode not in carry_engine.EXECUTION_MODES:
        raise ShadowRefusal(EXIT_USAGE, f"execution mode {execution_mode!r}")
    if execution_mode == carry_engine.OVERLAY and not _finite_pos(inventory_btc):
        raise ShadowRefusal(
            EXIT_USAGE, "overlay needs --inventory-btc (the modelled client "
                        "BTC being hedged; an assumption, recorded as one)")
    return {
        "schema": SCHEMA, "origin": origin, "venue": venue,
        "execution_mode": execution_mode, "execution_style": "taker",
        "borrow_apr": float(borrow_apr), "notional_usd": notional,
        "inventory_btc": None if inventory_btc is None else float(inventory_btc),
        "margin_multiple_assumed": (None if margin_multiple is None
                                    else float(margin_multiple)),
        "liquidation_check": "not_modelled",
        "assumed_fee_bps": {"spot_taker": carry_costs.TAKER_BPS_SPOT,
                            "perp_taker": carry_costs.TAKER_BPS_PERP},
        "assumed_lot_rules": {"linear": ASSUMED_LINEAR_RULES,
                              "spot": ASSUMED_SPOT_RULES},
        "engine_constants": {
            "DELTA_BAND": carry_engine.DELTA_BAND,
            "MIN_MARGIN_MULTIPLE": carry_engine.MIN_MARGIN_MULTIPLE,
            "MIN_ENTRY_FUNDING_BPS": carry_engine.MIN_ENTRY_FUNDING_BPS,
            "NEGATIVE_FUNDING_EXIT_PRINTS":
                getattr(carry_engine, "NEGATIVE_FUNDING_EXIT_PRINTS", None),
            "ASSUMED_HOLD_DAYS": carry_costs.ASSUMED_HOLD_DAYS,
            "EWMA_ALPHA": carry_costs.EWMA_ALPHA,
            "EWMA_MIN_PRINTS": carry_costs.EWMA_MIN_PRINTS,
            "MAX_ENTRIES_PER_UTC_DAY": carry_risk.MAX_ENTRIES_PER_UTC_DAY,
            "PAIR_MIN_MARGIN_MULTIPLE": carry_risk.MIN_MARGIN_MULTIPLE,
        },
    }


def config_fingerprint(config: Dict[str, Any]) -> str:
    return _sha(config)


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------


def validate_observation(raw: Any) -> Dict[str, Any]:
    """Complete and self-consistent, or a refusal. Nothing is defaulted."""
    if not isinstance(raw, dict):
        raise ShadowRefusal(EXIT_REFUSED_INPUT, "observation is not an object")
    missing = [k for k in REQUIRED_OBS if raw.get(k) is None]
    if missing:
        raise ShadowRefusal(EXIT_REFUSED_INPUT,
                            f"incomplete observation, missing {missing}")
    obs = dict(raw)
    for key in ("perp_mark", "spot_mark", "observed_at_s"):
        if not _finite_pos(obs[key]):
            raise ShadowRefusal(EXIT_REFUSED_INPUT, f"{key}={obs[key]!r}")
        obs[key] = float(obs[key])
    for key in ("funding_bps", "funding_print_bps"):
        try:
            obs[key] = float(obs[key])
        except (TypeError, ValueError):
            raise ShadowRefusal(EXIT_REFUSED_INPUT, f"{key}={obs[key]!r}")
        if not math.isfinite(obs[key]):
            raise ShadowRefusal(EXIT_REFUSED_INPUT, f"{key} is not finite")
    try:
        obs["funding_print_ms"] = int(obs["funding_print_ms"])
    except (TypeError, ValueError):
        raise ShadowRefusal(EXIT_REFUSED_INPUT, "funding_print_ms not an int")
    if obs["funding_print_ms"] <= 0:
        raise ShadowRefusal(EXIT_REFUSED_INPUT, "funding_print_ms <= 0")
    for key in ("perp_bid", "perp_ask", "venue_time_s", "margin_multiple"):
        if obs.get(key) is not None:
            if not _finite_pos(obs[key]):
                raise ShadowRefusal(EXIT_REFUSED_INPUT, f"{key}={obs[key]!r}")
            obs[key] = float(obs[key])
    if obs.get("perp_bid") is not None and obs.get("perp_ask") is not None \
            and obs["perp_bid"] >= obs["perp_ask"]:
        raise ShadowRefusal(EXIT_REFUSED_INPUT, "perp bid >= ask (crossed)")
    prints = obs.get("settled_prints")
    if prints is not None:
        try:
            obs["settled_prints"] = [(float(b), int(m)) for b, m in prints]
        except (TypeError, ValueError):
            raise ShadowRefusal(EXIT_REFUSED_INPUT, "settled_prints malformed")
    return obs


def observation_key(obs: Dict[str, Any]) -> str:
    """Identity of a market observation. Origin and labels are not part of it."""
    fields = {k: obs.get(k) for k in (
        "perp_mark", "spot_mark", "funding_bps", "funding_print_bps",
        "funding_print_ms", "observed_at_s", "venue_time_s", "perp_bid",
        "perp_ask", "margin_multiple")}
    return _sha(fields)


class PublicVenueReader:
    """Unauthenticated GETs on public market-data paths. No key, no signature."""

    def __init__(self, venue: str, *, timeout_s: float = 10.0,
                 http_get: Any = None) -> None:
        if venue not in VENUE_BASE:
            raise ShadowRefusal(EXIT_USAGE, f"venue must be one of "
                                            f"{sorted(VENUE_BASE)}")
        self.venue, self.base = venue, VENUE_BASE[venue]
        self.timeout_s = timeout_s
        self._get = http_get

    def _fetch(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if path not in PUBLIC_PATHS:
            raise ShadowIsolationError(f"{path} is not a public market path")
        if self._get is None:
            import requests
            self._get = lambda url, params, timeout: requests.get(
                url, params=params, timeout=timeout)
        resp = self._get(self.base + path, params, self.timeout_s)
        resp.raise_for_status()
        body = resp.json()
        if int(body.get("retCode", -1)) != 0:
            raise ShadowRefusal(EXIT_REFUSED_INPUT,
                                f"{path}: retCode {body.get('retCode')}")
        return body.get("result") or {}

    def observe(self) -> Dict[str, Any]:
        try:
            return self._observe()
        except ShadowRefusal:
            raise
        except ShadowIsolationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ShadowRefusal(
                EXIT_REFUSED_INPUT,
                f"the venue could not be read ({type(exc).__name__}: {exc}); "
                "nothing was observed and nothing was booked")

    def _observe(self) -> Dict[str, Any]:
        t0 = time.time()
        server = self._fetch("/v5/market/time", {})
        lin = (self._fetch("/v5/market/tickers", {
            "category": "linear", "symbol": SYMBOL}).get("list") or [None])[0]
        spot = (self._fetch("/v5/market/tickers", {
            "category": "spot", "symbol": SYMBOL}).get("list") or [None])[0]
        hist = self._fetch("/v5/market/funding/history", {
            "category": "linear", "symbol": SYMBOL, "limit": 8}).get("list")
        if not lin or not spot or not hist:
            raise ShadowRefusal(EXIT_REFUSED_INPUT,
                                "venue returned an empty market view")
        rows = sorted(((float(r["fundingRate"]) * 1e4,
                        int(r["fundingRateTimestamp"])) for r in hist),
                      key=lambda p: p[1])
        now = time.time()
        return {
            "perp_mark": float(lin["markPrice"]),
            "spot_mark": float(spot["lastPrice"]),
            "funding_bps": float(lin["fundingRate"]) * 1e4,
            "funding_print_bps": rows[-1][0], "funding_print_ms": rows[-1][1],
            "observed_at_s": now,
            "venue_time_s": int(server.get("timeSecond", 0)) or None,
            "perp_bid": float(lin["bid1Price"]) if lin.get("bid1Price") else None,
            "perp_ask": float(lin["ask1Price"]) if lin.get("ask1Price") else None,
            "settled_prints": rows,
            "source": f"bybit-public-rest:{self.base}",
            "fetch_started_s": t0,
        }


# ---------------------------------------------------------------------------
# the simulated venue
# ---------------------------------------------------------------------------


class ShadowBroker:
    """A venue made of one observation. It has no client and no network.

    Only the names in ``ALLOWED`` exist. Anything else (``_request``,
    ``create_order``, ``cancel_all``, ``amend_order``, ``client`` ...) raises
    ``ShadowIsolationError`` instead of silently doing nothing, so a code path
    that reaches for an exchange capability fails loudly in tests.
    """

    ALLOWED = frozenset({
        "get_mark", "get_spot_mark", "get_funding_bps", "get_funding_print",
        "get_margin_multiple", "get_lot_rules", "get_fee_rates",
        "get_spot_inventory", "get_perp_position", "get_open_carry_orders",
        "get_book_top", "get_venue_time_s", "place_market", "place_post_only",
        "cancel_order", "load", "drain", "set_holdings"})

    def __init__(self, config: Dict[str, Any]) -> None:
        self._cfg = config
        self._obs: Optional[Dict[str, Any]] = None
        self._key = ""
        self._perp_short = 0.0
        self._inventory = float(config.get("inventory_btc") or 0.0)
        self._acquired_spot = 0.0
        self._n = 0
        self._fills: List[Dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        raise ShadowIsolationError(
            f"ShadowBroker has no {name!r}: the shadow venue cannot place, "
            "cancel or amend an exchange order, or reach an exchange client")

    # state the runtime sets from its own book
    def set_holdings(self, *, perp_short: float, acquired_spot: float) -> None:
        self._perp_short, self._acquired_spot = float(perp_short), float(acquired_spot)

    def load(self, obs: Dict[str, Any], key: str) -> None:
        self._obs, self._key, self._n = obs, key, 0

    def drain(self) -> List[Dict[str, Any]]:
        out, self._fills = self._fills, []
        return out

    def _o(self) -> Dict[str, Any]:
        if self._obs is None:
            raise ShadowIsolationError("no observation loaded")
        return self._obs

    # reads the snapshot and engine make ---------------------------------
    def get_mark(self, symbol: str) -> float:
        return self._o()["perp_mark"]

    def get_spot_mark(self, symbol: str) -> float:
        return self._o()["spot_mark"]

    def get_funding_bps(self, symbol: str) -> float:
        return self._o()["funding_bps"]

    def get_funding_print(self, symbol: str) -> Tuple[float, int]:
        o = self._o()
        return o["funding_print_bps"], o["funding_print_ms"]

    def get_venue_time_s(self) -> float:
        value = self._o().get("venue_time_s")
        if value is None:
            raise ShadowIsolationError("the observation carries no venue time")
        return value

    def get_margin_multiple(self, symbol: str) -> float:
        o = self._o()
        if o.get("margin_multiple") is not None:
            return o["margin_multiple"]
        assumed = self._cfg.get("margin_multiple_assumed")
        if assumed is None:
            raise ShadowIsolationError(
                "margin multiple unavailable: not observed and none assumed")
        return float(assumed)

    def get_lot_rules(self, symbol: str, product: str) -> Dict[str, float]:
        return dict(self._cfg["assumed_lot_rules"][
            "linear" if product == "linear" else "spot"])

    def get_fee_rates(self, symbol: str, product: str) -> Dict[str, float]:
        fees = self._cfg["assumed_fee_bps"]
        taker = fees["perp_taker"] if product == "linear" else fees["spot_taker"]
        return {"maker_bps": 2.0, "taker_bps": float(taker)}

    def get_spot_inventory(self, symbol: str) -> float:
        return float(self._inventory + self._acquired_spot)

    def get_perp_position(self, symbol: str) -> float:
        return abs(self._perp_short)

    def get_open_carry_orders(self, spot_symbol: str, perp_symbol: str) -> list:
        return []

    def get_book_top(self, symbol: str, product: str) -> Dict[str, float]:
        o = self._o()
        if product == "linear" and o.get("perp_bid") and o.get("perp_ask"):
            return {"bid": o["perp_bid"], "ask": o["perp_ask"]}
        raise ShadowIsolationError("no book top in this observation")

    # simulated orders: rows, not requests --------------------------------
    def place_market(self, *, symbol: str, side: str, qty: float,
                     product: str) -> Optional[Dict[str, Any]]:
        if not _finite_pos(qty):
            return None
        o = self._o()
        price = o["perp_mark"] if product == "linear" else o["spot_mark"]
        self._n += 1
        link = f"shadow-{self._key[:12]}-{self._n}"
        if product == "linear":
            self._perp_short += qty if side == "Sell" else -qty
        self._fills.append({
            "link": link, "side": side, "product": product, "qty": float(qty),
            "price": float(price), "spot_mark": o["spot_mark"],
            "perp_mark": o["perp_mark"], "bid": o.get("perp_bid"),
            "ask": o.get("perp_ask")})
        fee_usd = qty * price * self.get_fee_rates(symbol, product)[
            "taker_bps"] / 1e4
        # the unit the venue states it in: a spot BUY fee is in the coin
        fee = fee_usd / price if (product == "spot" and side == "Buy") else fee_usd
        return {"filled_qty": float(qty), "avg_price": float(price),
                "order_link_id": link, "fee": fee}

    def place_post_only(self, *, symbol: str, side: str, qty: float,
                        price: float, product: str) -> None:
        raise ShadowIsolationError(
            "the shadow venue models taker execution only (maker fill "
            "probability is unmeasured)")

    def cancel_order(self, *, symbol: str, order_link_id: str,
                     product: str) -> None:
        raise ShadowIsolationError("nothing rests in the shadow venue")


# ---------------------------------------------------------------------------
# the separate shadow book
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS shadow_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_observations (
  key TEXT PRIMARY KEY, seq INTEGER NOT NULL, source_ts_ms INTEGER NOT NULL,
  wall_ts_ms INTEGER NOT NULL, origin TEXT NOT NULL, source TEXT, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_steps (
  seq INTEGER PRIMARY KEY, obs_key TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_fills (
  link TEXT PRIMARY KEY, seq INTEGER NOT NULL, obs_key TEXT NOT NULL,
  side TEXT NOT NULL, product TEXT NOT NULL, qty REAL NOT NULL,
  price REAL NOT NULL, spot_mark REAL NOT NULL, fee_usd REAL NOT NULL,
  fee_class TEXT NOT NULL, slippage_usd REAL, slippage_class TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_funding (
  opened_ms INTEGER NOT NULL, print_ms INTEGER NOT NULL, seq INTEGER NOT NULL,
  funding_usd REAL NOT NULL, financing_usd REAL NOT NULL, notional_usd REAL NOT NULL,
  print_bps REAL NOT NULL, PRIMARY KEY (opened_ms, print_ms));
CREATE TABLE IF NOT EXISTS shadow_state (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, kind TEXT NOT NULL,
  obs_key TEXT, payload TEXT NOT NULL);
"""

#: Tables the real account store uses; finding any of them means this file is
#: not a shadow book and must never be written to.
_REAL_STORE_TABLES = ("positions", "orders", "kill_switch", "carry_ledger",
                      "equity_state", "fills")


def _refuse_real_store(path: str) -> None:
    base = os.path.basename(path)
    if "trading_state" in base:
        raise ShadowRefusal(EXIT_CONFIG,
                            f"{path} looks like the account's trading-state store")
    if os.path.exists(path):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        clash = [t for t in _REAL_STORE_TABLES if t in names]
        if clash:
            raise ShadowRefusal(
                EXIT_CONFIG, f"{path} holds real-store tables {clash}; "
                             "refusing to treat it as a shadow book")


class ShadowStore:
    def __init__(self, path: str) -> None:
        _refuse_real_store(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.con = sqlite3.connect(path, isolation_level=None)
        self.con.executescript(_DDL)

    def close(self) -> None:
        self.con.close()

    def meta(self, key: str) -> Optional[str]:
        row = self.con.execute(
            "SELECT value FROM shadow_meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO shadow_meta VALUES (?,?)",
                         (key, value))

    def next_seq(self) -> int:
        row = self.con.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM shadow_steps").fetchone()
        return int(row[0])

    def event(self, kind: str, payload: Dict[str, Any],
              obs_key: Optional[str] = None) -> None:
        self.con.execute(
            "INSERT INTO shadow_events (ts_ms, kind, obs_key, payload) "
            "VALUES (?,?,?,?)",
            (int(time.time() * 1000), kind, obs_key, _canon(payload)))

    def load_state(self) -> Optional[Dict[str, Any]]:
        row = self.con.execute("SELECT payload FROM shadow_state").fetchone()
        return None if row is None else json.loads(row[0])

    def save_state(self, payload: Dict[str, Any]) -> None:
        self.con.execute("INSERT OR REPLACE INTO shadow_state VALUES (1,?)",
                         (_canon(payload),))


class _ShadowKill:
    """The kill switch the pair gate asks about: this book's own, in its file."""

    def __init__(self, store: ShadowStore) -> None:
        self.store = store

    def is_kill_switch_engaged(self) -> Tuple[bool, str]:
        why = self.store.meta("halted")
        return (why is not None, why or "")

    def trip(self, reason: str) -> None:
        if self.store.meta("halted") is None:
            self.store.set_meta("halted", reason)


@contextlib.contextmanager
def _clock(now_s: float) -> Iterator[None]:
    """Make ``MarketSnapshot.age_s`` measure against ``now_s``.

    ``assert_fresh`` reads the module's ``time.time``; the replay harness
    patches it the same way. Restored on exit, whatever happens.
    """
    import market_snapshot

    class _T:
        @staticmethod
        def time() -> float:
            return now_s

        def __getattr__(self, name: str) -> Any:
            return getattr(time, name)

    saved = market_snapshot.time
    market_snapshot.time = _T()
    try:
        yield
    finally:
        market_snapshot.time = saved


# ---------------------------------------------------------------------------
# the runtime
# ---------------------------------------------------------------------------


class ShadowRuntime:
    def __init__(self, db_path: str, config: Dict[str, Any]) -> None:
        self.config = config
        self.fingerprint = config_fingerprint(config)
        self.store = ShadowStore(db_path)
        self._bind()
        self.kill = _ShadowKill(self.store)
        self.broker = ShadowBroker(config)
        self.engine = self._build_engine()
        self._resume()

    def close(self) -> None:
        self.store.close()

    # -- binding: the book is frozen to one config and one evidence class --
    def _bind(self) -> None:
        s = self.store
        recorded = s.meta("config_fingerprint")
        if recorded is None:
            rev = code_revision()
            s.con.execute("BEGIN IMMEDIATE")
            s.set_meta("schema", SCHEMA)
            s.set_meta("config_fingerprint", self.fingerprint)
            s.set_meta("config", _canon(self.config))
            s.set_meta("origin", self.config["origin"])
            s.set_meta("created_ms", str(int(time.time() * 1000)))
            s.set_meta("code_revision_at_creation", _canon(rev))
            s.event("book_created", {"config_fingerprint": self.fingerprint,
                                     "origin": self.config["origin"],
                                     "code": rev})
            s.con.execute("COMMIT")
            return
        if s.meta("origin") != self.config["origin"]:
            raise ShadowRefusal(
                EXIT_CONFIG, f"this book holds {s.meta('origin')!r} "
                             f"observations; {self.config['origin']!r} refused")
        if recorded != self.fingerprint:
            raise ShadowRefusal(
                EXIT_CONFIG, "frozen configuration mismatch: this book was "
                             f"created with fingerprint {recorded[:16]}, "
                             f"this run has {self.fingerprint[:16]}")

    def _build_engine(self) -> Any:
        from carry_engine import CarryEngine
        from carry_risk import CarryRisk

        cfg = self.config
        engine = CarryEngine(
            broker=self.broker, kill_switch=self.kill.trip,
            max_notional_usd=cfg["notional_usd"], borrow_apr=cfg["borrow_apr"],
            execution_mode=cfg["execution_mode"], execution_style="taker",
            persist=self._persist, require_liquidation_check=False)
        engine.pair_risk = CarryRisk(store=self.kill,
                                     max_notional_usd=cfg["notional_usd"])
        return engine

    def _persist(self, state: Dict[str, Any]) -> None:
        risk = self.engine.pair_risk if hasattr(self, "engine") else None
        payload = {"engine": state}
        if risk is not None:
            payload["risk"] = {"entries": [d.isoformat() for d in risk._entries],
                               "broken": [d.isoformat() for d in risk._broken]}
        self.store.save_state(payload)

    def _holdings(self) -> Tuple[float, float]:
        pos = self.engine.position
        if pos is None:
            return 0.0, 0.0
        perp = pos.perp.filled_qty
        acquired = pos.spot.filled_qty if self.config["execution_mode"] == "acquire" else 0.0
        return perp, acquired

    def _resume(self) -> None:
        """Restart: take the persisted book back through the engine's own
        cold-start planner, against this book's simulated venue."""
        saved = self.store.load_state()
        if not saved:
            return
        risk = saved.get("risk") or {}
        self.engine.pair_risk._entries = [
            dt.date.fromisoformat(d) for d in risk.get("entries", [])]
        self.engine.pair_risk._broken = [
            dt.date.fromisoformat(d) for d in risk.get("broken", [])]
        state = saved["engine"]
        pos = (state or {}).get("position") or {}
        perp = float((pos.get("perp") or {}).get("filled_qty", 0.0))
        spot = float((pos.get("spot") or {}).get("filled_qty", 0.0))
        acquired = spot if self.config["execution_mode"] == "acquire" else 0.0
        self.broker.set_holdings(perp_short=perp, acquired_spot=acquired)
        decision = self.engine.cold_start(
            ledger=state, venue_perp_qty=perp,
            venue_spot_qty=self.broker.get_spot_inventory(SYMBOL),
            open_orders=[])
        if decision.action.value == "HALT":
            self.kill.trip(f"COLD_START: {decision.reason}")

    # -- one observation -----------------------------------------------------
    def step(self, raw_obs: Any, *, now_s: Optional[float] = None,
             source: str = "") -> Dict[str, Any]:
        from market_snapshot import take_snapshot

        obs = validate_observation(raw_obs)
        key = observation_key(obs)
        s = self.store
        if s.con.execute("SELECT 1 FROM shadow_observations WHERE key=?",
                         (key,)).fetchone():
            return {"outcome": "DUPLICATE_OBSERVATION", "obs_key": key,
                    "exit": EXIT_OK}
        origin = self.config["origin"]
        if now_s is None:
            now_s = time.time() if origin not in OBSERVATION_CLOCK_ORIGINS \
                else obs["observed_at_s"]
        elif origin not in OBSERVATION_CLOCK_ORIGINS and \
                abs(now_s - time.time()) > 5:
            raise ShadowRefusal(EXIT_USAGE, "an explicit clock is only "
                                            "allowed for replay/fixture books")
        if s.meta("halted") is not None:
            return {"outcome": "HALTED", "obs_key": key, "exit": EXIT_HALTED,
                    "reason": s.meta("halted")}

        perp0, acquired0 = self._holdings()
        self.broker.set_holdings(perp_short=perp0, acquired_spot=acquired0)
        self.broker.load(obs, key)
        clock = _clock(now_s)
        clock.__enter__()
        try:
            view = take_snapshot(self.broker, perp_symbol=SYMBOL,
                                 spot_symbol=SYMBOL, now_s=obs["observed_at_s"])
            view.assert_fresh()
        except Exception as exc:  # noqa: BLE001
            clock.__exit__(None, None, None)
            msg = f"{type(exc).__name__}: {exc}"
            s.con.execute("BEGIN IMMEDIATE")
            s.event("observation_refused", {"reason": msg, "origin": origin,
                                            "now_s": now_s}, key)
            s.con.execute("COMMIT")
            raise ShadowRefusal(EXIT_REFUSED_INPUT, msg)

        # the pair gate re-asserts freshness inside the engine, so the engine
        # runs under the same clock as the snapshot check above
        seq = s.next_seq()
        rev = code_revision()
        s.con.execute("BEGIN IMMEDIATE")
        try:
            warmed = 0
            if not self.engine._funding_history and obs.get("settled_prints"):
                warmed = self.engine.warm_funding_history(obs["settled_prints"])
            pos_obj = self.engine.position
            funding_before = pos_obj.funding_collected if pos_obj else 0.0
            opened_ms = pos_obj.opened_ms if pos_obj else None
            self.engine.snapshot = view
            source_ms = int((obs.get("venue_time_s") or obs["observed_at_s"]) * 1000)
            decision = self.engine.on_candle(
                mark=view.perp_mark, funding_bps=view.funding_print_bps,
                spot=view.spot_mark, timestamp_ms=source_ms,
                funding_print_ms=view.funding_print_ms)
            fills = self._book_fills(seq, key, self.broker.drain())
            funding_row = None
            if pos_obj is not None and opened_ms is not None:
                delta = pos_obj.funding_collected - funding_before
                if abs(delta) > 0.0:
                    funding_row = self._book_funding(
                        seq, opened_ms, view, pos_obj, delta)
            if decision.state.name == "HALTED":
                self.kill.trip(decision.reason)
            record = {
                "seq": seq, "obs_key": key, "origin": origin,
                "evidence_class": origin,
                "source": obs.get("source") or source or "unlabelled",
                "source_ts": {"observed_at_s": obs["observed_at_s"],
                              "venue_time_s": obs.get("venue_time_s"),
                              "funding_print_ms": obs["funding_print_ms"]},
                "wall_ts_ms": int(time.time() * 1000),
                "clock": "wall" if origin not in OBSERVATION_CLOCK_ORIGINS
                         else "observation",
                "code": rev, "config_fingerprint": self.fingerprint,
                "decision": {"action": str(decision.action),
                             "reason": str(decision.reason),
                             "state": decision.state.value,
                             "detail": _jsonable(decision.detail)},
                "intents_and_fills": fills, "funding": funding_row,
                "warmed_prints": warmed,
                "assumptions": self._assumptions(obs),
                "snapshot": view.as_dict(),
            }
            s.con.execute(
                "INSERT INTO shadow_observations VALUES (?,?,?,?,?,?,?)",
                (key, seq, source_ms, record["wall_ts_ms"], origin,
                 record["source"], _canon(obs)))
            s.con.execute("INSERT INTO shadow_steps VALUES (?,?,?)",
                          (seq, key, _canon(record)))
            self._persist(self.engine.to_state())
            s.con.execute("COMMIT")
        except Exception:
            s.con.execute("ROLLBACK")
            raise
        finally:
            clock.__exit__(None, None, None)
        code = EXIT_HALTED if decision.state.name == "HALTED" else EXIT_OK
        return {"outcome": "STEP_RECORDED", "obs_key": key, "seq": seq,
                "exit": code, "record": record}

    # -- bookkeeping ---------------------------------------------------------
    def _book_fills(self, seq: int, key: str,
                    fills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for f in fills:
            notional = f["qty"] * f["price"]
            bps = (self.config["assumed_fee_bps"]["perp_taker"]
                   if f["product"] == "linear"
                   else self.config["assumed_fee_bps"]["spot_taker"])
            fee = notional * bps / 1e4
            slip, slip_class = None, UNAVAILABLE
            if f["product"] == "linear" and f.get("bid") and f.get("ask"):
                # taker crosses to the touch: half the observed spread, per unit
                slip = f["qty"] * (f["ask"] - f["bid"]) / 2.0
                slip_class = ESTIMATED
            self.store.con.execute(
                "INSERT INTO shadow_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f["link"], seq, key, f["side"], f["product"], f["qty"],
                 f["price"], f["spot_mark"], fee, ASSUMED, slip, slip_class))
            out.append({**f, "fee_usd": fee, "fee_class": ASSUMED,
                        "fee_basis": f"public base-tier taker {bps} bps; "
                                     "account tier not read",
                        "slippage_usd": slip, "slippage_class": slip_class,
                        "fill_basis": "simulated at the observed mark; no "
                                      "order was sent anywhere"})
        return out

    def _book_funding(self, seq: int, opened_ms: int, view: Any, pos: Any,
                      delta: float) -> Optional[Dict[str, Any]]:
        notional = pos.perp.filled_qty * view.perp_mark
        financing = (self.config["borrow_apr"] / (365.0 * 3.0)) * notional
        cur = self.store.con.execute(
            "INSERT OR IGNORE INTO shadow_funding VALUES (?,?,?,?,?,?,?)",
            (opened_ms, view.funding_print_ms, seq, delta, financing, notional,
             view.funding_print_bps))
        if cur.rowcount == 0:
            return None                 # this print was already booked
        return {"print_ms": view.funding_print_ms, "funding_usd": delta,
                "funding_class": ESTIMATED,
                "funding_basis": "settled print rate (measured) x short qty x "
                                 "observed mark (settlement mark assumed)",
                "financing_usd": financing, "financing_class": ASSUMED,
                "financing_basis": f"CARRY_BORROW_APR={self.config['borrow_apr']}"}

    def _assumptions(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "fees": ASSUMED, "lot_rules": ASSUMED, "inventory_btc": ASSUMED,
            "margin_multiple": (MEASURED if obs.get("margin_multiple") is not None
                                else ASSUMED if self.config["margin_multiple_assumed"]
                                is not None else UNAVAILABLE),
            "slippage": (ESTIMATED if obs.get("perp_bid") and obs.get("perp_ask")
                         else UNAVAILABLE),
            "market_impact": UNAVAILABLE, "liquidation_distance": UNAVAILABLE,
            "fill_model": "taker at the observed mark; full fill",
        }

    # -- economics -----------------------------------------------------------
    def economics(self) -> Dict[str, Any]:
        return compute_economics(self.store.con, self.config)


def _jsonable(obj: Any) -> Any:
    return json.loads(_canon(obj))


def _avg_cost_realised(fills: Sequence[Tuple], short: bool) -> Tuple[float, float, float]:
    """Average-cost realised P&L of one instrument. ``fills`` rows are
    (side, qty, price). Returns (realised, open_qty, avg_price)."""
    qty = 0.0       # signed: negative = short
    avg = 0.0
    realised = 0.0
    for side, q, px in fills:
        signed = q if side == "Buy" else -q
        if qty == 0 or (qty > 0) == (signed > 0):
            new = qty + signed
            avg = (abs(qty) * avg + abs(signed) * px) / abs(new)
            qty = new
        else:
            closing = min(abs(signed), abs(qty))
            realised += closing * (px - avg) * (1 if qty > 0 else -1)
            qty += signed
            if abs(qty) < 1e-12:
                qty, avg = 0.0, 0.0
            elif (qty > 0) != (signed > 0) and abs(signed) > closing:
                avg = px
    return realised, qty, avg


def compute_economics(con: sqlite3.Connection,
                      config: Dict[str, Any]) -> Dict[str, Any]:
    """Modelled net economics from the shadow book only. Deterministic."""
    fills = con.execute(
        "SELECT seq, side, product, qty, price, spot_mark, fee_usd, "
        "slippage_usd, slippage_class FROM shadow_fills ORDER BY seq, link"
    ).fetchall()
    fund = con.execute("SELECT funding_usd, financing_usd FROM shadow_funding"
                       ).fetchall()
    last = con.execute("SELECT payload FROM shadow_observations "
                       "ORDER BY seq DESC LIMIT 1").fetchone()
    last_obs = json.loads(last[0]) if last else None

    perp = [(f[1], f[3], f[4]) for f in fills if f[2] == "linear"]
    perp_realised, perp_open, perp_avg = _avg_cost_realised(perp, short=True)

    # The hedged BTC. Overlay: the client's coin, valued at the spot mark each
    # time the short changes; acquire: the simulated spot fills themselves.
    if config["execution_mode"] == "overlay":
        inv = []
        for f in fills:
            if f[2] == "linear":
                inv.append(("Buy" if f[1] == "Sell" else "Sell", f[3], f[5]))
        hedge_realised, hedge_open, hedge_avg = _avg_cost_realised(inv, short=False)
    else:
        spot = [(f[1], f[3], f[4]) for f in fills if f[2] == "spot"]
        hedge_realised, hedge_open, hedge_avg = _avg_cost_realised(spot, short=False)

    unreal_perp = unreal_hedge = 0.0
    if last_obs and perp_open:
        unreal_perp = ((perp_avg - last_obs["perp_mark"]) * abs(perp_open)
                       if perp_open < 0
                       else (last_obs["perp_mark"] - perp_avg) * perp_open)
    if last_obs and hedge_open:
        unreal_hedge = (last_obs["spot_mark"] - hedge_avg) * hedge_open

    fees = sum(f[6] for f in fills)
    slip_known = sum(f[7] for f in fills if f[7] is not None)
    slip_unavailable = sum(1 for f in fills if f[7] is None)
    funding = sum(r[0] for r in fund)
    financing = sum(r[1] for r in fund)

    realised_net = (funding + perp_realised + hedge_realised
                    - fees - slip_known - financing)
    net_with_unrealised = realised_net + unreal_perp + unreal_hedge
    unavailable = ["market_impact", "liquidation_distance"]
    if slip_unavailable:
        unavailable.append(f"slippage on {slip_unavailable} fill(s)")
    return {
        "label": "MODELLED NET ECONOMICS - NOT REAL ACCOUNT P&L",
        "fills": len(fills), "funding_prints_booked": len(fund),
        "components_usd": {
            "funding": {"usd": funding, "class": ESTIMATED},
            "perp_realised": {"usd": perp_realised, "class": ESTIMATED},
            "hedged_btc_realised": {"usd": hedge_realised, "class": ESTIMATED},
            "fees": {"usd": -fees, "class": ASSUMED},
            "slippage": ({"usd": -slip_known, "class": ESTIMATED}
                         if fills and not slip_unavailable else
                         {"usd": None if not slip_known else -slip_known,
                          "class": UNAVAILABLE if not slip_known else
                          "partial:estimated+unavailable"}),
            "financing": {"usd": -financing, "class": ASSUMED},
            "market_impact": {"usd": None, "class": UNAVAILABLE},
            "unrealised_perp": {"usd": unreal_perp, "class": ESTIMATED},
            "unrealised_hedged_btc": {"usd": unreal_hedge, "class": ESTIMATED},
        },
        "net_realised_usd": realised_net,
        "net_including_unrealised_usd": net_with_unrealised,
        "unavailable_components_excluded": unavailable,
        "net_is_partial": True,
        "open_perp_qty": abs(perp_open),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_obs(path: str) -> Any:
    try:
        text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
        return json.loads(text)
    except (OSError, ValueError) as exc:
        raise ShadowRefusal(EXIT_USAGE, f"cannot read observation: {exc}")


def _config_from(args: argparse.Namespace) -> Dict[str, Any]:
    return build_config(
        origin=args.origin, venue=args.venue, borrow_apr=args.borrow_apr,
        execution_mode=args.execution_mode, inventory_btc=args.inventory_btc,
        margin_multiple=args.margin_multiple)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _summarise(result: Dict[str, Any]) -> Dict[str, Any]:
    rec = result.get("record") or {}
    return {k: v for k, v in {
        "outcome": result["outcome"], "obs_key": result["obs_key"][:16],
        "seq": result.get("seq"), "decision": rec.get("decision"),
        "fills": [{k: f[k] for k in ("side", "product", "qty", "price",
                                     "fee_usd", "slippage_usd")}
                  for f in rec.get("intents_and_fills", [])],
        "funding": rec.get("funding"), "reason": result.get("reason"),
    }.items() if v not in (None, [])}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("step", "replay", "report"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True,
                       help="the shadow book (never the account's store)")
        p.add_argument("--origin", required=True, choices=ORIGINS)
        p.add_argument("--venue", required=True,
                       choices=sorted(VENUE_BASE) + ["historical"])
        p.add_argument("--borrow-apr", type=float, required=True)
        p.add_argument("--execution-mode", required=True,
                       choices=("overlay", "acquire"))
        p.add_argument("--inventory-btc", type=float, default=None)
        p.add_argument("--margin-multiple", type=float, default=None)
        if name == "step":
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--live", action="store_true",
                           help="read public market data (no key, GET only)")
            g.add_argument("--observation", help="JSON file, or - for stdin")
        if name == "replay":
            p.add_argument("--observations", required=True,
                           help="JSONL, one observation per line")
    args = ap.parse_args(argv)

    rt: Optional[ShadowRuntime] = None
    try:
        # Everything that can refuse before an observation exists is decided
        # BEFORE the book is opened: opening a fresh path creates the book and
        # freezes it to this run's config and origin, so a usage error or an
        # unreadable venue must not leave behind a bound, empty book that then
        # refuses the corrected command.
        config = _config_from(args)
        obs: Any = None
        lines: List[str] = []
        if args.cmd == "step":
            if args.live:
                if args.origin != "live_public":
                    raise ShadowRefusal(EXIT_USAGE,
                                        "--live requires --origin live_public")
            elif args.origin == "live_public":
                raise ShadowRefusal(
                    EXIT_USAGE, "live_public means this process read the "
                                "venue; use --live, or another origin")
        if args.cmd == "replay":
            if args.origin == "live_public":
                raise ShadowRefusal(
                    EXIT_USAGE, "live_public means this process read the "
                                "venue; a replayed file cannot claim it")
            try:
                lines = open(args.observations, encoding="utf-8").read().splitlines()
            except OSError as exc:
                raise ShadowRefusal(EXIT_USAGE, str(exc))
        if args.cmd == "step":
            obs = (PublicVenueReader(args.venue).observe() if args.live
                   else _read_obs(args.observation))
        rt = ShadowRuntime(args.db, config)
        if args.cmd == "report":
            _print({"book": {"origin": rt.store.meta("origin"),
                             "config_fingerprint": rt.fingerprint,
                             "halted": rt.store.meta("halted"),
                             "steps": rt.store.next_seq() - 1},
                    "economics": rt.economics()})
            return EXIT_OK
        if args.cmd == "step":
            result = rt.step(obs, source=args.observation or "")
            _print(_summarise(result))
            return result["exit"]
        # replay
        last = EXIT_OK
        for n, line in enumerate(l for l in lines if l.strip()):
            result = rt.step(json.loads(line), source=f"{args.observations}:{n}")
            last = result["exit"]
            if last != EXIT_OK:
                _print(_summarise(result))
                return last
        _print({"replayed": len(lines), "economics": rt.economics()})
        return last
    except ShadowRefusal as exc:
        print(f"REFUSED ({exc.code}): {exc}", file=sys.stderr)
        return exc.code
    except Exception as exc:  # noqa: BLE001
        print(f"FAULT: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAULT
    finally:
        if rt is not None:
            rt.close()


if __name__ == "__main__":
    raise SystemExit(main())
