"""MultiSymbolAuthority: a NEW, separately reviewed entry authority for the multi-symbol layer, enforced at the final venue-request boundary.

It is NOT the BTC carry's ``EntryAuthority`` and does not touch it: ``entry_authority``/``bybit_connection``/``carry_*`` are unchanged and keep their gates. It speaks the same
protocol ``BybitClient._authorize_new_exposure`` consults on ``client.entry_authority`` (``authorize(body, *, venue)`` raising ``entry_authority.EntryRefused``, ``exit_refusal``,
``spot_sell_keeps_hedge``, ``max_leverage``, ``bound_transport``); the client turns a refusal into ``OrderNotSent`` (nothing reaches the transport).

NOT INSTALLED BY DEFAULT ANYWHERE. Nothing in ``main.py`` / ``bybit_connection`` / ``entry_authority`` / ``carry_*`` imports this module (a test greps for it). Using it is an explicit
call: ``MultiSymbolAuthority(...)`` then ``install(client, authority)``.

What an order must satisfy (every refusal is a stable code; all are reported):
  * the authority was built from a well-formed ``ms_risk`` policy (closed schema, explicit symbols, SANDBOX venue); no policy = no authority (``AuthorityConfigError``);
  * the venue the client names equals the policy venue and is a registered sandbox (``venues.is_sandbox``): mainnet, ``eu``, unknown and mis-cased names are refused;
  * the kill switch, re-read NOW (unreadable = engaged);
  * the order is EXACTLY one leg of a registered, risk-approved group: ``orderLinkId`` = ``<group_id>-<leg_id>``, same category/symbol/side/orderType/qty/price/timeInForce;
    the body keys are the closed ``entry_authority`` allow-list and nothing the leg does not state (no ``isLeverage``/``positionIdx``/``marketUnit``/trigger fields);
  * at send time, with the account and market state re-read and their age checked against the policy, ``ms_risk.risk_assess`` of the group's remaining legs on top of the account
    PLUS every other still-pending reservation approves (per-symbol, aggregate, gross, net, unhedged, leverage, active groups). A stale account or market refuses;
  * a durable, flock-serialised reservation per ``orderLinkId`` (``entry_authority.ReservationStore``): an identical resend is how a lost acknowledgement is resolved; the same id
    with a different order is a conflict.
  Exits: a reduce-only order must be a perp order on a symbol the group opened, on the opposite side, no larger than the entry leg, link ``<opened_group_id>-<new_leg_id>``.
  Exits are not blocked by staleness or by the kill switch (refusing to reduce risk is the unsafe choice). A spot SELL is an exit only if ``hedge_reader`` can show it keeps the
  hedge; without one it is refused.

TRANSPORT BINDING. The authority is bound to ONE transport (``bound_transport``); the client approves nothing on another. Construction is refused unless the transport is an
in-process SIMULATION (``is_simulated_transport``: a WHITELIST of concrete classes, never a self-declared attribute) or ALL of: the owner's testnet capability is enabled (``ms_testnet_adapter.enabled()``, False unless an owner-set capability file with
a signed-off manifest digest exists), the venue is the registry's ``testnet``, a dedicated testnet credential variable name is declared (generic Bybit names refused) and the
transport is EXACTLY ``ms_testnet_transport.TestnetTransport`` (a ``CapabilityTransport`` for the TESTNET_EXECUTOR role, dedicated credential class) and the binding names its dedicated key variable ``BYBIT_TESTNET_API_KEY``. The capability file is created only by the owner; in this repository state it does not exist, so only simulated transports are admitted.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import entry_authority as EA  # noqa: E402
import ms_risk  # noqa: E402
import runtime_capability as RC  # noqa: E402
import venues as _venues  # noqa: E402
from ms_types import LegPlan, MarketSnapshot, OrderGroupPlan  # noqa: E402

EntryRefused = EA.EntryRefused
ID_PART = re.compile(r"[A-Za-z0-9_]{1,24}")
LINK_SEP = "-"
HEDGE_SLACK = EA.HEDGE_SLACK
GENERIC_CREDENTIAL_NAMES = frozenset({"BYBIT_API_KEY", "BYBIT_API_SECRET"})
_CRED_NAME = re.compile(r"[A-Z][A-Z0-9_]{5,63}")
_BAD_NAME_WORDS = ("MAINNET", "LIVE", "DEMO", "PROD", "EU")
_OPTIONAL_BODY_KEYS = frozenset({"timeInForce", "reduceOnly", "price"})


class AuthorityConfigError(ValueError):
    """The authority cannot be built (no policy, bad policy, wrong transport, missing testnet capability): there is nothing to approve with."""


def ms_risk_slack() -> Decimal:
    return Decimal(str(ms_risk.EXIT_QTY_SLACK))


def link_id(group_id: str, leg_id: str) -> str:
    return f"{group_id}{LINK_SEP}{leg_id}"


def policy_digest(policy: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


_NET_ROOTS = frozenset({"urllib", "urllib3", "http", "requests", "socket", "websocket", "websockets", "ssl", "aiohttp", "httpx", "ftplib", "smtplib", "xmlrpc", "asyncio"})
_SIMULATED_REGISTRY: Dict[type, str] = {}        # exact class -> why it is trusted; ONLY populated by register_simulated_transport (never by an attribute a class declares)
#: Classes accepted WITHOUT registration, by exact (module, qualname) and only when ``sys.modules[module].<qualname>`` is that very class (so a look-alike elsewhere is not it).
_BUILTIN_SIMULATED = (("backtest", "SimulatedExchange"), ("ms_pipeline", "SimulatedTransport"))


def _root(name: Any) -> str:
    return str(name).split(".", 1)[0]


def simulated_transport_class_refusals(cls: Any) -> List[str]:
    """Why ``cls`` may NOT be a simulated transport (empty = it may be registered). Structural, not self-declared: no class in the MRO is (or derives from) ``RequestsTransport`` /
    ``CapabilityTransport``, none lives in a network library, and none of their modules holds a network library, a socket/HTTP/TLS symbol or an HTTP session object as a module
    global. This is a reviewable boundary, not a sandbox: a class can still hide a connection in a function body, which is why REGISTRATION is explicit and happens in code a
    reviewer reads (this module and tests); a class that merely declares a marker is nothing."""
    if not isinstance(cls, type):
        return ["NOT_A_CLASS"]
    out: List[str] = []
    try:
        import bybit_connection as _bc
        banned_bases: Tuple[type, ...] = (_bc.RequestsTransport, RC.CapabilityTransport)
    except Exception:                                              # noqa: BLE001 - cannot establish what a real transport is: refuse everything
        return ["REAL_TRANSPORT_CLASSES_UNAVAILABLE"]
    if issubclass(cls, banned_bases):
        out.append("DERIVES_FROM_REAL_TRANSPORT")
    for c in cls.__mro__[:-1]:
        if _root(getattr(c, "__module__", "")) in _NET_ROOTS:
            out.append(f"NETWORK_MODULE_CLASS:{c.__module__}")
        mod = sys.modules.get(getattr(c, "__module__", ""))
        for name, val in list(vars(mod).items()) if mod is not None else []:
            if name.startswith("__"):
                continue
            owners = [getattr(val, "__name__", "")] if isinstance(val, type(os)) else [getattr(val, "__module__", None) or "", getattr(type(val), "__module__", "") or ""]
            if any(_root(o) in _NET_ROOTS for o in owners) and name != "_NET_ROOTS":
                out.append(f"MODULE_TOUCHES_NETWORK:{c.__module__}.{name}")
    return list(dict.fromkeys(out))


def register_simulated_transport(cls: Any) -> Any:
    """Whitelist ONE concrete class as an in-process simulation (exact class: a subclass is not covered). Refused (``AuthorityConfigError``) for a class that
    ``simulated_transport_class_refusals`` rejects. Returns ``cls`` (usable as a decorator). Registration changes nothing about what the class can do: it only states, in code a
    reviewer reads, that this class carries no request."""
    bad = simulated_transport_class_refusals(cls)
    if bad:
        raise AuthorityConfigError("SIMULATED_TRANSPORT_REFUSED:" + ",".join(bad)[:200])
    _SIMULATED_REGISTRY[cls] = f"{cls.__module__}.{cls.__qualname__}"
    return cls


def _is_builtin_simulated(cls: type) -> bool:
    for mod, qn in _BUILTIN_SIMULATED:
        m = sys.modules.get(mod)
        if m is not None and cls.__module__ == mod and cls.__qualname__ == qn and getattr(m, qn, None) is cls:
            return True
    return False


def is_simulated_transport(t: Any) -> bool:
    """True only for an instance whose EXACT class is whitelisted: ``backtest.SimulatedExchange``, ``ms_pipeline.SimulatedTransport`` or a class passed to
    ``register_simulated_transport``; and only if that class (and the instance) still pass the structural checks. ``IN_PROCESS_SIMULATION`` is NOT consulted: it is an honour-system
    attribute any class can claim. A real HTTP transport, a capability wrapper, a subclass of either, an instance holding a network/session object and anything unregistered are False."""
    try:
        cls = type(t)
        if cls not in _SIMULATED_REGISTRY and not _is_builtin_simulated(cls):
            return False
        if simulated_transport_class_refusals(cls):
            return False
        import bybit_connection as _bc
        if isinstance(t, (_bc.RequestsTransport, RC.CapabilityTransport)) or hasattr(t, "_session"):
            return False
        for v in list(getattr(t, "__dict__", {}).values()):            # an instance holding a network object / session under ANY attribute name
            if _root(getattr(type(v), "__module__", "")) in _NET_ROOTS or isinstance(v, (_bc.RequestsTransport, RC.CapabilityTransport)) or "session" in type(v).__name__.lower():
                return False
        return True
    except Exception:                                              # noqa: BLE001 - cannot establish it: not simulated
        return False


def credential_name_refusals(name: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(name, str) or not _CRED_NAME.fullmatch(name):
        return ["CREDENTIAL_NAME_INVALID"]
    if name in GENERIC_CREDENTIAL_NAMES:
        out.append("CREDENTIAL_NAME_GENERIC")
    if "TESTNET" not in name:
        out.append("CREDENTIAL_NAME_NOT_TESTNET")
    if any(w in name for w in _BAD_NAME_WORDS):
        out.append("CREDENTIAL_NAME_NON_TESTNET_WORD")
    return out


@dataclass(frozen=True)
class TestnetBinding:
    """Everything beyond 'it is a simulation' that a non-simulated transport needs. Constructing one grants nothing: the authority re-checks the capability itself."""
    __test__ = False                                               # not a pytest class
    credential_env_name: str


# ---------------------------------------------------------------------------------------------------- durable group ledger

class _GroupLedger:
    """Append-only, flock-serialised JSONL of group registrations / settlements / resolutions. Unreadable = refuse (never guess what was registered)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _fd(self) -> int:
        return os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)

    @staticmethod
    def _parse(raw: bytes) -> List[Dict[str, Any]]:
        rows = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict) or "event" not in row or "group_id" not in row:
                    raise ValueError("bad row")
                rows.append(row)
        return rows

    def read(self) -> List[Dict[str, Any]]:
        fd = self._fd()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            with open(self.path, "rb") as fh:
                return self._parse(fh.read())
        finally:
            os.close(fd)

    def append_if(self, decide: Callable[[List[Dict[str, Any]]], Tuple[Optional[str], Optional[Dict[str, Any]]]]) -> Optional[str]:
        """Under ONE exclusive flock: read every row, let ``decide(rows) -> (refusal, row_to_append)`` judge, and append the row only if there is no refusal. The check and the
        write cannot interleave with another process's (a reservation is atomic). An unreadable ledger refuses (``GROUP_LEDGER_UNREADABLE``)."""
        fd = self._fd()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with open(self.path, "rb") as fh:
                try:
                    rows = self._parse(fh.read())
                except ValueError:
                    return "GROUP_LEDGER_UNREADABLE"
            why, row = decide(rows)
            if why is None and row is not None:
                os.write(fd, (json.dumps(dict(row), sort_keys=True) + "\n").encode("utf-8"))
                os.fsync(fd)
            return why
        finally:
            os.close(fd)

    def append(self, row: Mapping[str, Any]) -> None:
        fd = self._fd()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, (json.dumps(dict(row), sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


def _plan_to_row(plan: OrderGroupPlan) -> Dict[str, Any]:
    d = asdict(plan)
    d["legs"] = [asdict(l) for l in plan.legs]
    return d


def _plan_from_row(d: Mapping[str, Any]) -> OrderGroupPlan:
    d = dict(d)
    d["legs"] = tuple(LegPlan(**l) for l in d["legs"])
    return OrderGroupPlan(**d)


# ---------------------------------------------------------------------------------------------------- the authority

class MultiSymbolAuthority:
    def __init__(self, *, policy: Optional[Mapping[str, Any]], transport: Any, state_dir: str, strategy: str,
                 state_provider: Callable[[], Mapping[str, Any]], snapshot_provider: Callable[[], Mapping[str, MarketSnapshot]],
                 kill_switch_reader: Callable[[], Tuple[bool, str]], clock: Callable[[], float] = time.time,
                 hedge_reader: Optional[Callable[[str], Tuple[float, float]]] = None, testnet: Optional[TestnetBinding] = None) -> None:
        if policy is None:
            raise AuthorityConfigError("NO_APPROVED_POLICY")
        bad = ms_risk.policy_refusals(policy)
        if bad:
            raise AuthorityConfigError("POLICY_REFUSED:" + ",".join(bad))
        if not (isinstance(strategy, str) and strategy.strip() and strategy == strategy.strip()):
            raise AuthorityConfigError("STRATEGY_REQUIRED")
        if not state_dir or not callable(state_provider) or not callable(snapshot_provider) or not callable(kill_switch_reader) or not callable(clock):
            raise AuthorityConfigError("PROVIDERS_REQUIRED")
        if transport is None:
            raise AuthorityConfigError("TRANSPORT_REQUIRED")
        self.mode = "SIMULATED"
        if not is_simulated_transport(transport):
            self.mode = "TESTNET"
            self._require_testnet(policy, transport, testnet)
        self.policy: Dict[str, Any] = json.loads(json.dumps(policy))          # a private deep copy: later edits of the caller's dict change nothing
        self.policy_digest = policy_digest(self.policy)
        self.strategy = strategy
        self.venue = self.policy["venue"]
        self.bound_transport = transport
        self._state, self._snaps, self._kill, self._clock, self._hedge = state_provider, snapshot_provider, kill_switch_reader, clock, hedge_reader
        os.makedirs(state_dir, exist_ok=True)
        self._groups = _GroupLedger(os.path.join(state_dir, "ms_groups.jsonl"))
        self._reservations = EA.ReservationStore(os.path.join(state_dir, "ms_reservations.jsonl"))
        self.refusals: List[Tuple[str, Tuple[str, ...]]] = []

    # ---- construction guard
    @staticmethod
    def _require_testnet(policy: Mapping[str, Any], transport: Any, testnet: Optional[TestnetBinding]) -> None:
        why: List[str] = []
        try:
            import ms_testnet_adapter as _ad
            enabled = _ad.enabled() is True
        except Exception:                                          # noqa: BLE001
            enabled = False
        if not enabled:
            why.append("TESTNET_CAPABILITY_DISABLED")
        if policy.get("venue") != "testnet" or not _venues.is_sandbox("testnet"):
            why.append("VENUE_NOT_TESTNET")
        if testnet is None:
            why.append("TESTNET_BINDING_MISSING")
        else:
            why += credential_name_refusals(testnet.credential_env_name)
        # ADMISSION SEAM: the real testnet transport is admitted ONLY as the exact ``ms_testnet_transport.TestnetTransport`` class (TESTNET_EXECUTOR capability, testnet registry host,
        # dedicated credential class) AND with the binding naming exactly its dedicated key variable. A generic ``CapabilityTransport`` (any inner object) is no longer enough.
        try:
            import ms_testnet_transport as _tt
            exact = _tt.is_testnet_executor_transport(transport)
            if testnet is not None and testnet.credential_env_name != _tt.KEY_ENV:
                why.append("CREDENTIAL_NAME_NOT_THE_DEDICATED_TESTNET_KEY")
        except Exception:                                          # noqa: BLE001
            exact = False
        if not exact:
            why.append("TRANSPORT_NOT_TESTNET_EXECUTOR_CAPABILITY")
        if why:
            raise AuthorityConfigError("TRANSPORT_NOT_SIMULATED:" + ",".join(why))

    # ---- helpers
    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _rows(self) -> List[Dict[str, Any]]:
        return self._groups.read()

    def _registered(self, rows: Sequence[Mapping[str, Any]]) -> Dict[str, OrderGroupPlan]:
        return {r["group_id"]: _plan_from_row(r["plan"]) for r in rows if r["event"] == "group"}

    @staticmethod
    def _split_link(link: Any) -> Optional[Tuple[str, str]]:
        if type(link) is not str or not EA.LINK_ID.fullmatch(link):
            return None
        parts = link.split(LINK_SEP)
        if len(parts) != 2 or not all(ID_PART.fullmatch(p) for p in parts):
            return None
        return parts[0], parts[1]

    def max_leverage(self) -> int:
        """The most leverage the policy allows (floor), for the client's set-leverage bound."""
        return int(math.floor(self.policy["max_leverage"]))

    # ---- registration
    def plan_refusals(self, plan: OrderGroupPlan) -> List[str]:
        out: List[str] = []
        if not ID_PART.fullmatch(str(plan.group_id)):
            out.append("GROUP_ID_INVALID")
        for l in plan.legs:
            if not ID_PART.fullmatch(str(l.leg_id)):
                out.append(f"LEG_ID_INVALID:{l.leg_id}")
            elif len(link_id(plan.group_id, l.leg_id)) > 36:
                out.append(f"LINK_ID_TOO_LONG:{l.leg_id}")
            if l.reduce_only:
                out.append(f"EXIT_LEG_IN_ENTRY_GROUP:{l.leg_id}")
        return out

    def register_group(self, plan: OrderGroupPlan) -> ms_risk.RiskDecision:
        """Risk-assess the plan NOW (fresh state) and, only if approved, persist it so its legs may be authorised. Idempotent for an identical plan; a different plan under a used
        group_id is refused."""
        bad = self.plan_refusals(plan)
        if bad:
            return ms_risk.RiskDecision(False, tuple(bad), {})
        try:
            rows = self._rows()
        except (OSError, ValueError):
            return ms_risk.RiskDecision(False, ("GROUP_LEDGER_UNREADABLE",), {})
        known = self._registered(rows)
        if plan.group_id in known:
            same = _plan_to_row(known[plan.group_id]) == _plan_to_row(plan)
            return ms_risk.RiskDecision(same, () if same else ("GROUP_ID_REUSED_DIFFERENT_PLAN",), {})
        decision = self._assess(plan, plan.legs, rows)
        if decision.approved:
            self._groups.append({"event": "group", "group_id": plan.group_id, "plan": _plan_to_row(plan), "policy_digest": self.policy_digest, "strategy": self.strategy,
                                 "at_ms": self._now_ms()})
        return decision

    def settle_leg(self, group_id: str, leg_id: str) -> None:
        """The account's own position read now reflects this leg: stop counting it as a pending reservation (otherwise it is counted twice, which is conservative)."""
        self._groups.append({"event": "settle", "group_id": group_id, "leg_id": leg_id, "at_ms": self._now_ms()})

    @staticmethod
    def _resolved_sets(rows: Sequence[Mapping[str, Any]]) -> Tuple[set, set]:
        """(ever_resolved, effectively_resolved). A ``reopen`` after the last ``resolve`` makes the group effectively unresolved again (a LATE FILL after it was closed): it may then be
        EXITED (reduce-only) and still counts as active, but ``ever_resolved`` keeps refusing new ENTRY orders for it."""
        ever: set = set()
        eff: set = set()
        for r in rows:
            if r["event"] == "resolve":
                ever.add(r["group_id"])
                eff.add(r["group_id"])
            elif r["event"] == "reopen":
                eff.discard(r["group_id"])
        return ever, eff

    def is_resolved(self, group_id: str) -> bool:
        """True while the group is effectively resolved. An unreadable ledger reads as resolved (the cautious answer for 'may I treat it as closed')."""
        try:
            return group_id in self._resolved_sets(self._rows())[1]
        except (OSError, ValueError):
            return True

    def reopen_group(self, group_id: str) -> bool:
        """A resolved group has exposure again (a fill arrived after it was closed). Appends a ``reopen`` so its EXITS are authorised again; entries stay refused. False if it was not
        resolved (nothing to reopen) or the ledger cannot be read. Never weakens an exit check: the reduce-only / quantity / symbol bounds still apply."""
        try:
            rows = self._rows()
        except (OSError, ValueError):
            return False
        if group_id not in self._resolved_sets(rows)[1]:
            return False
        self._groups.append({"event": "reopen", "group_id": group_id, "at_ms": self._now_ms()})
        return True

    def resolve_group(self, group_id: str) -> None:
        """The group is closed / abandoned and flat per the venue's own read: it no longer counts as pending or active."""
        self._groups.append({"event": "resolve", "group_id": group_id, "at_ms": self._now_ms()})

    # ---- risk at the boundary
    def _assess(self, plan: OrderGroupPlan, legs: Sequence[LegPlan], rows: Sequence[Mapping[str, Any]]) -> ms_risk.RiskDecision:
        try:
            state = self._state()
            snaps = self._snaps()
            if not isinstance(state, Mapping) or not isinstance(snaps, Mapping):
                raise TypeError("providers must return mappings")
            state = json.loads(json.dumps(state, allow_nan=True))
        except Exception:                                          # noqa: BLE001
            return ms_risk.RiskDecision(False, ("STATE_UNAVAILABLE",), {})
        registered = self._registered(rows)
        resolved = self._resolved_sets(rows)[1]
        settled = {(r["group_id"], r["leg_id"]) for r in rows if r["event"] == "settle"}
        reserved_links = self._reserved_links()
        # RESERVATION POLICY. A registered group's legs are reserved AS A WHOLE from the moment it is registered (sent or not), until each leg settles or the group resolves: a
        # group registered LATER therefore sees them and is refused at registration if it does not fit, instead of squeezing an in-flight group out between its legs. Priority is
        # registration order: a group counts every EARLIER unresolved group's legs, never a later one's (the later one was admitted on top of it). Its own legs already sent
        # (reserved at the boundary, not part of ``legs``) count too.
        order = list(registered)
        earlier = order[:order.index(plan.group_id)] if plan.group_id in registered else order
        positions = state.get("positions")
        if isinstance(positions, dict):
            counted = [(g, l) for g in earlier if g not in resolved for l in registered[g].legs]
            if plan.group_id in registered and plan.group_id not in resolved:
                counted += [(plan.group_id, l) for l in registered[plan.group_id].legs
                            if l.leg_id not in {x.leg_id for x in legs} and link_id(plan.group_id, l.leg_id) in reserved_links]
            for gid, l in counted:
                if (gid, l.leg_id) in settled:
                    continue
                px = _leg_ref_notional(l, snaps.get(l.symbol))
                key = (("SPOT:" + l.symbol) if l.category == "spot" else l.symbol) + "#pending:" + link_id(gid, l.leg_id)        # '#...' is stripped by ms_risk
                amt = None if px is None else (px if l.side == "Buy" else -px)                       # unknown stays unknown
                positions[key] = {"category": l.category, "notional_usd": amt}
        og = state.get("open_groups")
        others = len([g for g in earlier if g not in resolved and g != plan.group_id])
        if type(og) is int and og >= 0:
            state["open_groups"] = max(og, others)
        return ms_risk.risk_assess(replace(plan, legs=tuple(legs), created_ms=self._now_ms()), state, self.policy, snaps)

    def _reserved_links(self) -> set:
        try:
            with open(self._reservations.path, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return set()
        out = set()
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("event") != "resolve":
                    out.add(row.get("link"))
        return out

    # ---- exits
    def _opened_leg(self, body: Mapping[str, Any]) -> Tuple[Optional[LegPlan], Optional[str]]:
        sp = self._split_link(body.get("orderLinkId"))
        if sp is None:
            return None, "EXIT_LINK_ID_INVALID"
        try:
            rows = self._rows()
        except (OSError, ValueError):
            return None, "GROUP_LEDGER_UNREADABLE"
        plan = self._registered(rows).get(sp[0])
        if plan is None:
            return None, "EXIT_GROUP_NOT_OPENED"
        if sp[0] in self._resolved_sets(rows)[1]:
            return None, "EXIT_GROUP_RESOLVED"
        if any(l.leg_id == sp[1] for l in plan.legs):
            return None, "EXIT_LINK_REUSES_ENTRY_LEG"
        match = [l for l in plan.legs if l.symbol == body.get("symbol") and l.category == body.get("category")]
        if not match:
            return None, "EXIT_SYMBOL_NOT_OPENED_BY_GROUP"
        return match[0], None

    def exit_refusal(self, body: Mapping[str, Any]) -> Optional[str]:
        """None if this exit order is bound to a symbol the group opened (see module docstring); else a refusal code. Used by the client for orders that are not exposure."""
        try:
            leg, why = self._opened_leg(body)
            if why:
                return why
            assert leg is not None
            spot = EA.is_spot_sell(body)
            if not spot and not (EA._truthy(body.get("reduceOnly")) and body.get("category") == "linear"):
                return "EXIT_NOT_REDUCE_ONLY"
            if (leg.category == "spot") != spot or (spot and body.get("category") != "spot"):
                return "EXIT_CATEGORY_MISMATCH"
            if body.get("side") == leg.side:
                return "EXIT_SAME_SIDE_AS_ENTRY"
            if body.get("orderType") not in self.policy["allowed_order_types"]:
                return "EXIT_ORDER_TYPE_NOT_ALLOWED"
            q = body.get("qty")
            if not (type(q) is str and EA._PLAIN_DECIMAL.fullmatch(q) and Decimal(q) > 0):
                return "EXIT_QTY_INVALID"
            if Decimal(q) > Decimal(leg.qty) * ms_risk_slack():
                return "EXIT_QTY_EXCEEDS_ENTRY"
            if not (set(body) <= EA.ORDER_BODY_KEYS) or EA.body_value_refusals(body):
                return "EXIT_BODY_NOT_ALLOWED"
            why = self._exit_price_refusal(body, leg)
            if why:
                return why
            return self._reserve_exit(body, leg)
        except Exception:                                          # noqa: BLE001
            return "EXIT_NOT_BOUND:unreadable"

    def _exit_price_refusal(self, body: Mapping[str, Any], leg: LegPlan) -> Optional[str]:
        """A Limit exit must be priced within ``ms_risk.EXIT_MAX_PRICE_DEVIATION`` of the CURRENT mark of its category (read now); a mark that cannot be read refuses it (a Market
        reduce-only exit needs no price). A Market exit is allowed only reduce-only (perp, checked above) or as a held-inventory spot sell."""
        if body.get("orderType") != "Limit":
            return None
        try:
            snap = self._snaps().get(body.get("symbol"))
            mark = None if snap is None else ms_risk._pos(snap.spot_mark if body.get("category") == "spot" else snap.mark)
            px = body.get("price")
            if mark is None or not (type(px) is str and EA._PLAIN_DECIMAL.fullmatch(px)) or Decimal(px) <= 0:
                return "EXIT_PRICE_UNVERIFIABLE"
            return "EXIT_PRICE_OUT_OF_BAND" if abs(float(px) - mark) > ms_risk.EXIT_MAX_PRICE_DEVIATION * mark else None
        except Exception:                                          # noqa: BLE001
            return "EXIT_PRICE_UNVERIFIABLE"

    @staticmethod
    def _exit_outstanding(rows: Sequence[Mapping[str, Any]], group_id: str, symbol: str, category: str, *, except_link: Optional[str] = None) -> Decimal:
        """Exit quantity already reserved or consumed for (group, symbol, category): reserved minus released. A FILL consumes its reservation (it stays counted: the position
        is smaller and the entry quantity is spent); only ``release_exit`` gives quantity back."""
        total = Decimal(0)
        for r in rows:
            if r["event"] == "exit" and r["group_id"] == group_id and r["symbol"] == symbol and r["category"] == category and r["link"] != except_link:
                total += Decimal(r["qty"])
            elif r["event"] == "exit_release" and r["group_id"] == group_id and r["symbol"] == symbol and r["category"] == category and r["link"] != except_link:
                total -= Decimal(r["qty"])
        return total

    def _reserve_exit(self, body: Mapping[str, Any], leg: LegPlan) -> Optional[str]:
        """Reserve this exit's quantity in the durable ledger, atomically (one flock): the CUMULATIVE exit quantity of the group for the symbol, over every link id, may not exceed
        ``ms_risk.EXIT_QTY_SLACK`` x the entry leg's quantity. An identical resend of a link is idempotent (not counted twice); the same link with a different order is a conflict;
        a released link is spent."""
        gid, _lid = self._split_link(body.get("orderLinkId")) or ("", "")
        link, symbol, category, qty = body["orderLinkId"], str(body["symbol"]), str(body["category"]), Decimal(body["qty"])
        canon = {"side": body.get("side"), "orderType": body.get("orderType"), "price": body.get("price"), "qty": str(body["qty"])}

        def decide(rows: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
            same = [r for r in rows if r["event"] == "exit" and r["link"] == link]
            if same:
                if any(r["event"] == "exit_release" and r["link"] == link for r in rows):
                    return "EXIT_LINK_RELEASED", None
                prior = same[0]
                return (None, None) if {k: prior.get(k) for k in canon} == canon and prior["symbol"] == symbol and prior["category"] == category else ("EXIT_LINK_CONFLICT", None)
            if self._exit_outstanding(rows, gid, symbol, category) + qty > Decimal(leg.qty) * ms_risk_slack():
                return "EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY", None
            return None, {"event": "exit", "group_id": gid, "link": link, "symbol": symbol, "category": category, "qty": str(body["qty"]), "at_ms": self._now_ms(), **canon}
        try:
            return self._groups.append_if(decide)
        except OSError:
            return "EXIT_RESERVATION_LEDGER_UNAVAILABLE"

    def exit_reserved(self, order_link_id: str) -> bool:
        """True while the link holds an OPEN exit reservation (reserved minus released is positive). A read; False when the ledger cannot be read."""
        try:
            rows = self._rows()
        except (OSError, ValueError):
            return False
        res = [r for r in rows if r["event"] == "exit" and r["link"] == order_link_id]
        return bool(res) and self._exit_left(rows, order_link_id, res[0]) > 0

    @staticmethod
    def _exit_left(rows: Sequence[Mapping[str, Any]], link: str, res: Mapping[str, Any]) -> Decimal:
        return Decimal(res["qty"]) - sum((Decimal(r["qty"]) for r in rows if r["event"] == "exit_release" and r["link"] == link), Decimal(0))

    def record_exit_fill(self, order_link_id: str, qty: str) -> bool:
        """Record that ``qty`` of the link's reservation FILLED: it stays consumed and a bare ``release_exit`` will never give it back. Never records more than is still open."""
        def decide(rows: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
            res = [r for r in rows if r["event"] == "exit" and r["link"] == order_link_id]
            if not res:
                return "NO_SUCH_EXIT", None
            q = Decimal(str(qty))
            filled = sum((Decimal(r["qty"]) for r in rows if r["event"] == "exit_fill" and r["link"] == order_link_id), Decimal(0))
            if not q.is_finite() or q <= 0 or q > self._exit_left(rows, order_link_id, res[0]) - filled:
                return "FILL_QTY_INVALID", None
            return None, {"event": "exit_fill", "group_id": res[0]["group_id"], "link": order_link_id, "symbol": res[0]["symbol"], "category": res[0]["category"], "qty": str(q),
                          "at_ms": self._now_ms()}
        try:
            return self._groups.append_if(decide) is None
        except (OSError, ValueError, ArithmeticError):
            return False

    def release_exit(self, order_link_id: str, unfilled_qty: Optional[str] = None) -> bool:
        """Give back exit quantity that did NOT fill (order rejected, cancelled, expired; or the unfilled remainder of a partial fill). Only a link that currently holds an OPEN
        reservation can release anything. ``unfilled_qty`` None = the whole UNFILLED remainder (reserved - released - recorded fills); a stated quantity may not exceed that either:
        fill-consumed quantity is never given back. False if there was nothing to release."""
        done: List[bool] = []

        def decide(rows: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
            res = [r for r in rows if r["event"] == "exit" and r["link"] == order_link_id]
            if not res:
                return "NO_SUCH_EXIT", None
            filled = sum((Decimal(r["qty"]) for r in rows if r["event"] == "exit_fill" and r["link"] == order_link_id), Decimal(0))
            left = self._exit_left(rows, order_link_id, res[0]) - filled
            want = left if unfilled_qty is None else Decimal(str(unfilled_qty))
            if not want.is_finite() or want <= 0 or want > left:
                return "RELEASE_QTY_INVALID", None
            done.append(True)
            return None, {"event": "exit_release", "group_id": res[0]["group_id"], "link": order_link_id, "symbol": res[0]["symbol"], "category": res[0]["category"],
                          "qty": str(want), "at_ms": self._now_ms()}
        try:
            return self._groups.append_if(decide) is None and bool(done)
        except (OSError, ValueError, ArithmeticError):
            return False

    def spot_sell_keeps_hedge(self, body: Mapping[str, Any]) -> bool:
        """True only if the venue's own reads (``hedge_reader(symbol) -> (perp short size, spot inventory)``) show the sell leaves the perp covered AND the sell is bound to a symbol
        a group opened. No reader, a failed read or anything unusual is False (the sell is then refused as an entry)."""
        try:
            if self._hedge is None or not EA.is_spot_sell(body) or body.get("marketUnit") not in (None, "baseCoin"):
                return False
            leg, why = self._opened_leg(body)
            if why or leg is None or leg.category != "spot":
                return False
            qty = Decimal(str(body["qty"]))
            perp, spot = (float(x) for x in self._hedge(str(body["symbol"])))
            if not (qty.is_finite() and qty > 0 and math.isfinite(perp) and math.isfinite(spot) and perp >= 0 and spot >= 0):
                return False
            return (spot - float(qty)) >= perp * (1.0 - HEDGE_SLACK)
        except Exception:                                          # noqa: BLE001
            return False

    # ---- the entry decision
    def authorize(self, body: Mapping[str, Any], *, venue: str) -> EA.OrderIntent:
        intent = EA.intent_from_body(body)                           # the shared grammar: closed keys, plain decimals, link charset, boolean reduceOnly
        reasons: List[str] = []
        if venue != self.venue or type(venue) is not str or not _venues.is_sandbox(venue):
            reasons.append(f"VENUE_NOT_ALLOWED:{str(venue)[:20]}")
            self._refuse(intent, reasons)
        if not EA.increases_exposure(body):
            if EA.is_spot_sell(body) and not self.spot_sell_keeps_hedge(body):
                self._refuse(intent, ["SPOT_SELL_NOT_PROVEN_HEDGE_KEEPING"])        # the client routes a spot sell here precisely because that was not shown
            why = self.exit_refusal(body)
            if why:
                self._refuse(intent, [why])
            return intent
        try:
            engaged, _why = self._kill()
        except Exception:                                          # noqa: BLE001
            engaged = None
        if engaged is None:
            self._refuse(intent, ["KILL_SWITCH_UNREADABLE"])
        if engaged:
            self._refuse(intent, ["KILL_SWITCH_ENGAGED"])
        sp = self._split_link(intent.order_link_id)
        if sp is None:
            self._refuse(intent, ["ORDER_LINK_ID_NOT_GROUP_LEG"])
        try:
            rows = self._rows()
        except (OSError, ValueError):
            self._refuse(intent, ["GROUP_LEDGER_UNREADABLE"])
        assert sp is not None
        plan = self._registered(rows).get(sp[0])
        if plan is None:
            self._refuse(intent, ["GROUP_NOT_REGISTERED"])
        assert plan is not None
        if any(r["event"] == "resolve" and r["group_id"] == sp[0] for r in rows):
            self._refuse(intent, ["GROUP_RESOLVED"])
        leg = next((l for l in plan.legs if l.leg_id == sp[1]), None)
        if leg is None:
            self._refuse(intent, ["LEG_NOT_IN_GROUP"])
        assert leg is not None
        reasons += self._body_mismatches(body, leg)
        if reasons:
            self._refuse(intent, reasons)
        remaining = [l for l in plan.legs if link_id(plan.group_id, l.leg_id) == intent.order_link_id or link_id(plan.group_id, l.leg_id) not in self._reserved_links()]
        decision = self._assess(plan, remaining, rows)
        if not decision.approved:
            self._refuse(intent, ["RISK:" + r for r in decision.reasons])
        try:
            why = self._reservations.reserve(intent, max_unresolved=max(1, sum(len(p.legs) for p in self._registered(rows).values())), now_ms=self._now_ms())
        except OSError:
            why = "RESERVATION_LEDGER_UNAVAILABLE"
        if why:
            self._refuse(intent, [why])
        return intent

    def _refuse(self, intent: EA.OrderIntent, reasons: Sequence[str]) -> None:
        self.refusals.append((intent.order_link_id, tuple(reasons)))
        raise EntryRefused(list(reasons))

    @staticmethod
    def _body_mismatches(body: Mapping[str, Any], leg: LegPlan) -> List[str]:
        out: List[str] = []
        want = {"category": leg.category, "symbol": leg.symbol, "side": leg.side, "orderType": leg.order_type, "qty": leg.qty}
        for k, v in want.items():
            if body.get(k) != v or type(body.get(k)) is not str:
                out.append(f"BODY_FIELD_NOT_PLAN:{k}")
        if leg.order_type == "Limit":
            if body.get("price") != leg.price or type(body.get("price")) is not str:
                out.append("BODY_FIELD_NOT_PLAN:price")
            tif = "PostOnly" if leg.post_only else leg.time_in_force
            if "timeInForce" in body and body["timeInForce"] != tif:
                out.append("BODY_FIELD_NOT_PLAN:timeInForce")
        elif "price" in body and body["price"] not in (None, ""):
            out.append("BODY_FIELD_NOT_PLAN:price")
        if "reduceOnly" in body and body["reduceOnly"] is not False:
            out.append("BODY_FIELD_NOT_PLAN:reduceOnly")
        extra = sorted(str(k) for k in body if k not in want and k not in _OPTIONAL_BODY_KEYS and k != "orderLinkId")
        out += [f"UNKNOWN_BODY_FIELD:{k[:30]}" for k in extra]
        return out


def _leg_ref_notional(leg: LegPlan, snap: Optional[MarketSnapshot]) -> Optional[float]:
    px = ms_risk._ref_price(leg, snap)
    return None if px is None or not re.fullmatch(r"[0-9]+(\.[0-9]+)?", leg.qty) else float(leg.qty) * px


def install(client: Any, authority: MultiSymbolAuthority) -> None:
    """The ONLY way the authority reaches a client: explicit, and only onto the client whose transport it is bound to and whose venue is the policy's. Never called by library code."""
    if not isinstance(authority, MultiSymbolAuthority):
        raise AuthorityConfigError("NOT_A_MULTI_SYMBOL_AUTHORITY")
    if getattr(client, "transport", None) is not authority.bound_transport:
        raise AuthorityConfigError("CLIENT_TRANSPORT_IS_NOT_THE_BOUND_TRANSPORT")
    if getattr(client, "venue", None) != authority.venue:
        raise AuthorityConfigError("CLIENT_VENUE_IS_NOT_THE_POLICY_VENUE")
    client.entry_authority = authority
