"""The one registry that says which exchange a name means, and whether it is real money.

Before this module the same question was answered in several places, each
slightly differently, and "is this real money?" was never asked directly: it was
derived as ``venue != "mainnet"`` (config, the REST client, the transport
backstops), so any venue added to the tuple would have been treated as a
SANDBOX and would have sent real-money orders with no acknowledgement, no
promotion gate and no transport refusal. Here the answer is data:

* a venue is ``SANDBOX`` or ``LIVE``, stated once;
* every host (REST, private WebSocket, the public market-data environment its
  prices come from) is derived from the same row, so they cannot be cross-wired;
* anything that is **not a registered sandbox** needs live authorisation: an
  unknown name, an unqualified venue and every ``LIVE`` venue all fail closed.

``eu`` (Bybit EU, ``api.bybit.eu``) is deliberately **not** a usable venue. The
official EU documentation that could be read establishes no derivatives API, and
nothing here may infer product availability from the global documentation. It is
registered as *unqualified*: it can be named, it is refused with a reason, and
`venue_qualification` turns owner-collected read-only evidence into the only
verdict that could ever change that. Every GUARD that decides whether an order may leave
reads this table, so a venue added here is guarded by default. (Some read-only
reporting tools keep their own host lists for their evidence formats; those never
send an order and are not authorities on what is real money.)

Pure: no imports beyond the standard library, no I/O, no environment reads
(``config`` is the only module allowed to read the environment).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

SANDBOX = "sandbox"
LIVE = "live"
KINDS = (SANDBOX, LIVE)

#: The market-data environments the public feed knows (md_feed / md_state / md_store).
FEED_ENVIRONMENTS = ("mainnet", "testnet")


class UnknownVenue(ValueError):
    """The name is not in the registry at all."""


class VenueNotQualified(ValueError):
    """The name is known but has not been qualified for use; it is refused."""


@dataclass(frozen=True)
class Venue:
    name: str
    kind: str                      # SANDBOX | LIVE
    rest: str                      # https base for REST
    ws_private: str                # wss URL of the private stream
    #: Which public market-data environment carries this venue's prices. Demo
    #: trading matches against REAL mainnet prices, so demo reads the mainnet feed.
    feed_environment: str
    #: The label stamped on execution records for this venue. Demo shares the
    #: testnet label (a known conflation, kept for compatibility with databases
    #: that already recorded it); it is NOT a claim that demo is testnet.
    execution_environment: str
    note: str = ""

    @property
    def host(self) -> str:
        return urlsplit(self.rest).hostname or ""

    @property
    def is_live(self) -> bool:
        return self.kind == LIVE

    @property
    def ws_private_host(self) -> str:
        return urlsplit(self.ws_private).hostname or ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"venue {self.name!r}: kind {self.kind!r}")
        if urlsplit(self.rest).scheme != "https" or not self.host:
            raise ValueError(f"venue {self.name!r}: rest must be an https URL")
        if urlsplit(self.ws_private).scheme != "wss" or not self.ws_private_host:
            raise ValueError(f"venue {self.name!r}: ws_private must be a wss URL")
        if self.feed_environment not in FEED_ENVIRONMENTS:
            raise ValueError(f"venue {self.name!r}: feed environment {self.feed_environment!r}")


@dataclass(frozen=True)
class Unqualified:
    name: str
    reason: str
    documented_rest: Optional[str]
    evidence_needed: Tuple[str, ...]


_QUALIFIED: Dict[str, Venue] = {
    "mainnet": Venue("mainnet", LIVE, "https://api.bybit.com",
                     "wss://stream.bybit.com/v5/private", "mainnet", "mainnet",
                     "Bybit global, real money"),
    "testnet": Venue("testnet", SANDBOX, "https://api-testnet.bybit.com",
                     "wss://stream-testnet.bybit.com/v5/private", "testnet", "testnet",
                     "Bybit global testnet, fake money, its own thin books"),
    "demo": Venue("demo", SANDBOX, "https://api-demo.bybit.com",
                  "wss://stream-demo.bybit.com/v5/private", "mainnet", "testnet",
                  "Bybit demo trading: real mainnet prices, simulated matching, no real money"),
}

_UNQUALIFIED: Dict[str, Unqualified] = {
    "eu": Unqualified(
        "eu",
        "Bybit EU: the official EU documentation that could be read describes no trading API "
        "and no derivatives product; BTCUSDT linear perpetual availability to an EEA retail "
        "account is NOT established, so the carry strategy is not deployable on it",
        "https://api.bybit.eu",
        ("official EU documentation naming the REST, public WebSocket and private WebSocket hosts",
         "a signed READ-ONLY /v5/user/query-api result showing the key's product permissions",
         "a public instruments-info for BTCUSDT linear returning retCode 0 and status Trading",
         "written confirmation from Bybit EU that derivatives are offered to this account")),
}


def qualified_names() -> Tuple[str, ...]:
    return tuple(_QUALIFIED)


def sandbox_names() -> Tuple[str, ...]:
    return tuple(n for n, v in _QUALIFIED.items() if not v.is_live)


def live_names() -> Tuple[str, ...]:
    return tuple(n for n, v in _QUALIFIED.items() if v.is_live)


def unqualified() -> Dict[str, Unqualified]:
    return dict(_UNQUALIFIED)


def get(name: object) -> Venue:
    """The venue called `name`, or a refusal that says why."""
    key = str(name).strip().lower() if isinstance(name, str) else ""
    if key in _QUALIFIED:
        return _QUALIFIED[key]
    if key in _UNQUALIFIED:
        u = _UNQUALIFIED[key]
        raise VenueNotQualified(f"venue {key!r} is not qualified: {u.reason}")
    raise UnknownVenue(f"venue {name!r} is not one of {qualified_names()}")


def requires_live_authorisation(name: object) -> bool:
    """True for everything that is not a REGISTERED SANDBOX: every live venue,
    every unqualified venue and every unknown name. The default is the cautious
    answer, so a venue added to the registry as live (or mistyped) is guarded."""
    key = str(name).strip().lower() if isinstance(name, str) else ""
    v = _QUALIFIED.get(key)
    return v is None or v.is_live


def is_sandbox(name: object) -> bool:
    return not requires_live_authorisation(name)


def feed_environment(name: object) -> str:
    """Which public feed environment must drive an engine on this venue. Raises for
    anything unknown or unqualified: there is no fall-back to a flag."""
    return get(name).feed_environment


def execution_environment(name: object) -> str:
    return get(name).execution_environment


def rest_for(name: object) -> str:
    return get(name).rest


def ws_private_for(name: object) -> str:
    return get(name).ws_private


def rest_hosts() -> Dict[str, str]:
    return {n: v.host for n, v in _QUALIFIED.items()}


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
