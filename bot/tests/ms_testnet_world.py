"""Shared offline harness for the testnet execution tests: a tmp owner capability file, a fake Bybit testnet behind the REAL transport stack, an injected clock/sleep/prompt.
No network, no real credential; the canary key/secret live only in the fake."""
from __future__ import annotations

import io
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, os.path.join(BOT, "tools"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_testnet_adapter as AD  # noqa: E402
import ms_testnet_session as S  # noqa: E402
import ms_testnet_transport as TT  # noqa: E402
from ms_fake_bybit_testnet import FAKE_KEY, FAKE_SECRET, FakeBybitTestnet  # noqa: E402


class SimClock:
    def __init__(self) -> None:
        self.t = int(time.time() * 1000)

    def now(self) -> int:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += int(s * 1000)


class World:
    def __init__(self, tmp_path: Any, monkeypatch: Any, symbols: Tuple[str, ...] = ("BTCUSDT", "ETHUSDT"), lower: Optional[Dict[str, float]] = None, grant: bool = True) -> None:
        self.tmp = tmp_path
        self.clock = SimClock()
        monkeypatch.setattr(time, "sleep", lambda s: None)               # the client's retry backoff must not wait
        self.fake = FakeBybitTestnet(now_ms=self.clock.now)
        self.state_dir = str(tmp_path / "state")
        self.cap_path = str(tmp_path / "home" / "ms_testnet_capability.json")
        monkeypatch.setattr(AD, "DEFAULT_CAPABILITY_PATH", self.cap_path)
        self.manifest = AD.generate(candidate_id="cand_1", spec_digest="a" * 64, symbols=list(symbols), lower=lower,
                                    credential_env={"api_key": TT.KEY_ENV, "api_secret": TT.SECRET_ENV})
        if grant:
            self.write_capability()
        self.env: Dict[str, str] = {TT.KEY_ENV: FAKE_KEY, TT.SECRET_ENV: FAKE_SECRET}
        self.typed: List[str] = []
        self.out: List[str] = []

    def write_capability(self, manifest: Optional[Dict[str, Any]] = None) -> None:
        m = dict(manifest or self.manifest)
        m["authorisation"] = "GRANTED_BY_OWNER:test-ref"
        os.makedirs(os.path.dirname(self.cap_path), exist_ok=True)
        with open(self.cap_path, "w", encoding="utf-8") as fh:
            json.dump({"schema": AD.CAPABILITY_SCHEMA, "manifest": m, "manifest_plan_sha256": m["plan_sha256"]}, fh)
        os.chmod(self.cap_path, 0o600)

    def run(self, *argv: str, env: Optional[Dict[str, str]] = None, inner: Any = "fake", typed: Optional[str] = None) -> int:
        self.out = []
        answers = list(self.typed) if typed is None else [typed]
        return S.main(["--state-dir", self.state_dir, *argv], env=self.env if env is None else env, inner_transport=self.fake if inner == "fake" else inner,
                      clock_ms=self.clock.now, sleep=self.clock.sleep, input_fn=lambda _p: answers.pop(0) if answers else "", out=self.out.append)

    def text(self) -> str:
        return "\n".join(self.out)

    def record(self) -> Dict[str, Any]:
        with open(os.path.join(self.state_dir, "testnet_session.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def plan_doc(self) -> Dict[str, Any]:
        with open(os.path.join(self.state_dir, "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def ready(self, symbol: str = "BTCUSDT", notional: float = 90.0, **plan_kw: Any) -> str:
        """preflight + plan; returns the plan digest."""
        assert self.run("preflight") == 0, self.text()
        extra: List[str] = []
        for k, v in plan_kw.items():
            extra += [f"--{k.replace('_', '-')}", str(v)]
        assert self.run("plan", "--symbol", symbol, "--notional-usd", str(notional), *extra) == 0, self.text()
        return self.plan_doc()["plan_digest"]

    def execute(self, digest: Optional[str] = None, **kw: Any) -> int:
        d = digest or self.plan_doc()["plan_digest"]
        return self.run("execute", "--confirm-plan-digest", d, typed=kw.pop("typed", d), **kw)

    def requests_to(self, path: str) -> List[Any]:
        return [r for r in self.fake.requests if r[1] == path]


def begin(w: "World", *, submit: bool = True, group_plan: Any = None) -> Tuple[Any, Any, Dict[str, Any]]:
    """Wire the full stack (as ``execute`` does after its gate) WITHOUT driving it: (Run, Ctx, record). ``group_plan`` may replace the plan's group (custom legs)."""
    a = S._parser().parse_args(["--state-dir", w.state_dir, "status"])
    ctx = S.Ctx(a, w.env, w.fake, w.clock.now, w.clock.sleep, lambda _p: "", w.out.append)
    ctx.plan_poll = 1
    manifest = S._load_capability_manifest()[0]
    plan = w.plan_doc()
    run = S.Run(ctx, plan, manifest)
    if group_plan is not None:
        run.group_plan = group_plan
        run.gid = group_plan.group_id
    eng = run.make_engine(recover=False)
    run.client.sync_time()
    view = run.ex.venue_state()
    sym = plan["symbol"]
    run.baseline = {f"spot:{sym}": float((view["positions"] or {}).get(f"spot:{sym}", 0.0))}
    rec = S.load_record(ctx)
    decision = run.authority.register_group(run.group_plan)
    assert decision.approved, decision.reasons
    now = w.clock.now()
    eng.plan_group(run.group_plan, now)
    eng.approve_group(run.gid, decision, now)
    if submit:
        eng.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=now)
        run.bookkeep()
    return run, ctx, rec


def restart(w: "World", ctx: Any, gid: Optional[str] = None) -> Any:
    """A NEW process: fresh Run (new client/transport/authority/engine) recovering from the on-disk journal."""
    manifest = S._load_capability_manifest()[0]
    run = S.Run(ctx, w.plan_doc(), manifest)
    if gid:
        run.gid = gid
    run.client.sync_time()
    run.make_engine(recover=True)
    return run


def resting_plan(w: "World", gid: str = "gr1") -> Any:
    """A group whose legs REST (non-crossing GTC limits) so fills can be driven over time."""
    from dataclasses import replace
    import ms_types as T
    pl = w.plan_doc()
    gp = T.OrderGroupPlan(**{**pl["group"], "legs": tuple(T.LegPlan(**l) for l in pl["group"]["legs"])})
    legs = (T.LegPlan("p", "BTCUSDT", "linear", "Sell", "0.001", "Limit", "60100.0", False, False, "GTC"),
            T.LegPlan("s", "BTCUSDT", "spot", "Buy", "0.001", "Limit", "59900.00", False, False, "GTC"))
    return replace(gp, group_id=gid, legs=legs, max_wait_ms=3_600_000)
