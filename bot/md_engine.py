"""The single function that turns stored records into market state.

`Engine.apply(record)` is the only code path that changes a `MarketState`. The
live feed calls it right after appending a record to the store (under one lock,
so log order is application order); a replay calls it on the records read back.
Same function, same inputs, same state: that is why a replayed session
reproduces every logged view exactly (`replay`).
"""
from __future__ import annotations

import dataclasses
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

import md_events as E
import md_rest as R
import md_state as S
import md_store as ST
import md_wire as W


@dataclass
class ApplyOutcome:
    incidents: List[E.Incident] = field(default_factory=list)
    rejections: List[E.Rejection] = field(default_factory=list)


class Engine:
    def __init__(self, state: S.MarketState, store: Optional[ST.MarketStore] = None) -> None:
        self.state = state
        self.store = store
        self.lock = threading.RLock()
        self.last_pong_mono: Dict[str, float] = {}
        self.sub_failures: List[str] = []
        self._unlogged = 0

    # -- live helpers: append then apply, atomically ------------------------------------

    def record(self, kind: str, *, wall_ms: Union[int, Callable[[], int]],
               mono_s: Union[float, Callable[[], float]], category: str = "",
               conn_id: str = "", topic: str = "", payload: str) -> ApplyOutcome:
        """Append, then apply, atomically. `wall_ms` / `mono_s` may be callables:
        they are then sampled INSIDE the lock, so the log's order, its timestamps
        and the order of application all agree (a clock sampled before a contended
        lock can arrive out of order, and a stepped wall clock can run backwards).

        A store failure never kills the caller. The input is still applied (the
        market is live) but the state is flagged `store_fault`: entries need a
        reproducible record and stop until the log is writable again, at which
        point a `store_gap` marker invalidates every book so live and replay
        converge on the same unsynced state."""
        if self.store is None:
            raise RuntimeError("no store attached; use apply() on a Record")
        with self.lock:
            w = int(wall_ms() if callable(wall_ms) else wall_ms)
            m = float(mono_s() if callable(mono_s) else mono_s)
            if self._unlogged and kind != ST.VIEW:
                try:
                    gap = self.store.append(ST.CONN, wall_ms=w, mono_s=m, category="", conn_id="",
                                            topic="", payload=json.dumps(
                                                {"event": "store_gap", "unlogged": self._unlogged}))
                except Exception as exc:  # noqa: BLE001 - still down
                    self.state.store_fault = f"{type(exc).__name__}: {exc}"[:120]
                    self._unlogged += 1
                    return self.apply(ST.Record(0, kind, w, m, category, conn_id, topic, payload))
                self._unlogged = 0
                self.apply(gap)
            try:
                rec = self.store.append(kind, wall_ms=w, mono_s=m, category=category,
                                        conn_id=conn_id, topic=topic, payload=payload)
            except Exception as exc:  # noqa: BLE001 - disk full, locked, corrupt: do not die
                self.state.store_fault = f"{type(exc).__name__}: {exc}"[:120]
                self._unlogged += 1
                return self.apply(ST.Record(0, kind, w, m, category, conn_id, topic, payload))
            return self.apply(rec)

    def start_session(self, wall_ms: Union[int, Callable[[], int]],
                      mono_s: Union[float, Callable[[], float]]) -> ApplyOutcome:
        """Write down what this run is (symbol, depth, environment, policy) and
        start from a fresh state. A replay meets the same record and does the same,
        so a store that spans restarts replays exactly as it ran."""
        st = self.state
        payload = json.dumps({"event": "session_start", "symbol": st.symbol, "depth": st.depth,
                              "environment": st.environment,
                              "policy": dataclasses.asdict(st.policy)}, sort_keys=True)
        return self.record(ST.CONN, wall_ms=wall_ms, mono_s=mono_s, payload=payload)

    def log_view(self, now_mono_s: float, now_wall_ms: int) -> S.MarketView:
        """Compute the view and write it to the log at this exact point in the
        stream (so a replay can recompute and compare it)."""
        with self.lock:
            view = self.state.view(now_mono_s, now_wall_ms)
            if self.store is not None and not self._unlogged:
                try:
                    self.store.append(ST.VIEW, wall_ms=now_wall_ms, mono_s=now_mono_s,
                                      payload=ST.view_payload(view.to_dict(), self.store._next_id - 1))
                except Exception as exc:  # noqa: BLE001
                    self.state.store_fault = f"{type(exc).__name__}: {exc}"[:120]
                    self._unlogged += 1
            return view

    # -- the one state transition ------------------------------------------------------------

    def apply(self, rec: ST.Record) -> ApplyOutcome:
        out = ApplyOutcome()
        if rec.kind == ST.FRAME:
            ctx = W.FrameContext(rec.id, rec.category, rec.conn_id, rec.wall_ms, rec.mono_s)
            parsed = W.parse_frame(rec.payload, ctx)
            out.rejections += list(parsed.rejections)
            if parsed.control is not None:
                self._control(rec, parsed.control, out)
            out.incidents += self.state.ingest(parsed, category=rec.category)
        elif rec.kind == ST.CONN:
            self._conn(rec, out)
        elif rec.kind == ST.REST:
            self._rest(rec, out)
        return out

    def _session_start(self, info: Dict[str, Any]) -> None:
        try:
            known = {f.name for f in dataclasses.fields(S.MarketPolicy)}
            policy = S.MarketPolicy(**{k: v for k, v in (info.get("policy") or {}).items() if k in known})
            self.state = S.MarketState(str(info["symbol"]), policy, int(info["depth"]),
                                       str(info["environment"]))
        except (KeyError, TypeError, ValueError):
            return
        self.last_pong_mono.clear()
        self.sub_failures.clear()
        self._unlogged = 0

    def _control(self, rec: ST.Record, ctl: Dict[str, Any], out: ApplyOutcome) -> None:
        op = ctl.get("op")
        if op == "ping" or ctl.get("ret_msg") == "pong" or op == "pong":
            if ctl.get("success", True) is not False:
                self.last_pong_mono[rec.category] = rec.mono_s
            return
        if op in ("subscribe", "unsubscribe") and ctl.get("success") is False:
            out.incidents.append(E.Incident(E.SUBSCRIBE_FAILED, f"{op}: {ctl.get('ret_msg')!r}",
                                            rec.wall_ms, rec.mono_s, rec.conn_id, rec.topic,
                                            {"category": rec.category}))
            self.sub_failures.append(str(ctl.get("ret_msg")))

    def _conn(self, rec: ST.Record, out: ApplyOutcome) -> None:
        try:
            info = json.loads(rec.payload)
            event = info["event"]
        except (ValueError, KeyError, TypeError):
            return
        if event == "session_start":
            self._session_start(info)
            return
        if event == "store_gap":
            # inputs were applied live but never logged: nothing the books hold is
            # reproducible. Invalidate them; they resync from fresh snapshots.
            for c in (E.LINEAR, E.SPOT):
                self.state.invalidate_book(c, "STORE_GAP")
                self.state.tickers[c].reset()
            self.state.store_fault = None
            out.incidents.append(E.Incident(E.RESYNC_REQUESTED,
                                            f"store gap: {info.get('unlogged')} unlogged inputs",
                                            rec.wall_ms, rec.mono_s, "", "", {"category": ""}))
            return
        cat = rec.category
        if cat not in self.state.connected:
            return
        if event == "connected":
            self.state.on_connected(cat)
            self.last_pong_mono[cat] = rec.mono_s
            kind = E.CONNECTED
        elif event == "disconnected":
            self.state.on_disconnected(cat)
            kind = E.DISCONNECTED
        elif event == "invalidate":
            self.state.invalidate_book(cat, str(info.get("reason", "INVALIDATED")))
            kind = E.RESYNC_REQUESTED
        else:
            return
        out.incidents.append(E.Incident(kind, str(info.get("reason", "")), rec.wall_ms, rec.mono_s,
                                        rec.conn_id, rec.topic, {"category": cat}))

    def _rest(self, rec: ST.Record, out: ApplyOutcome) -> None:
        try:
            info = json.loads(rec.payload)
            endpoint, params = info["endpoint"], info.get("params") or {}
            status, body = int(info["http_status"]), info["body"]
        except (ValueError, KeyError, TypeError):
            return
        ctx = W.FrameContext(rec.id, rec.category, "rest", rec.wall_ms, rec.mono_s, E.SOURCE_REST)
        parsed = R.parse_rest(endpoint, params, status, body, ctx)
        out.rejections += list(parsed.rejections)
        for r in parsed.rejections:
            self.state.rejections[r.reason] = self.state.rejections.get(r.reason, 0) + 1
            kind = E.REST_UNAVAILABLE if r.reason in (E.GEO_BLOCKED, E.RATE_LIMITED) else None
            if kind:
                out.incidents.append(E.Incident(kind, r.reason, rec.wall_ms, rec.mono_s, "rest",
                                                endpoint, {"category": rec.category}))
        if parsed.specs:
            self.state.set_specs(list(parsed.specs))
        if parsed.prints:
            self.state.set_last_print(max(parsed.prints, key=lambda p: p.settled_ms))
        if parsed.touch:
            cat, bid, ask, _ts = parsed.touch
            self.state.set_rest_touch(cat, bid, ask, rec.mono_s)
        if parsed.specs or parsed.prints or parsed.touch:
            out.incidents.append(E.Incident(E.REST_BOOTSTRAPPED, endpoint, rec.wall_ms, rec.mono_s,
                                            "rest", endpoint, {"category": rec.category}))


# -- replay ---------------------------------------------------------------------------------------

@dataclass
class ReplayReport:
    records: int = 0
    frames: int = 0
    views_checked: int = 0
    view_mismatches: List[int] = field(default_factory=list)
    incidents: List[E.Incident] = field(default_factory=list)
    chain_problems: List[str] = field(default_factory=list)
    final_digest: Any = None
    rejections: Dict[str, int] = field(default_factory=dict)

    @property
    def deterministic(self) -> bool:
        return not self.view_mismatches and not self.chain_problems


def replay(store: ST.MarketStore, *, symbol: str = "BTCUSDT",
           policy: Optional[S.MarketPolicy] = None, depth: int = 50,
           verify_chain: bool = True) -> ReplayReport:
    """Fold every stored record through a fresh state; recompute and compare each
    logged view. Any mismatch is a determinism failure."""
    eng = Engine(S.MarketState(symbol, policy, depth, store.environment or "mainnet"))
    rep = ReplayReport()
    if verify_chain:
        rep.chain_problems = store.verify()
    for rec in store.iter_records():
        rep.records += 1
        if rec.kind == ST.VIEW:
            logged = json.loads(rec.payload)["view"]
            fresh = json.loads(json.dumps(eng.state.view(rec.mono_s, rec.wall_ms).to_dict(),
                                          sort_keys=True))
            rep.views_checked += 1
            if fresh != logged:
                rep.view_mismatches.append(rec.id)
            continue
        if rec.kind == ST.FRAME:
            rep.frames += 1
        rep.incidents += eng.apply(rec).incidents
    rep.final_digest = eng.state.digest()
    rep.rejections = dict(eng.state.rejections)
    return rep
