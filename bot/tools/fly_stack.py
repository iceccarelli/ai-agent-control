#!/usr/bin/env python3
"""The runtime, as code: one machine, one volume, one book — and a checker.

WHY THIS EXISTS AT ALL
======================
0038 wrote a 27-resource CloudFormation stack for a process that makes three
decisions a day and needs one outbound TLS connection. That was ceremony. What
this book actually needs from a host is short:

    one process, never two           two machines is two hedges
    a disk that survives a deploy    the ledger is the only thing that knows
                                     a position exists (INVENTORY D3)
    a fixed outbound IP              so the venue key can be pinned to it
    secrets at runtime, never baked  this image's predecessor shipped keys
    it comes back when it dies       main.py exits 1 on an unreachable venue

Fly gives all five in about forty lines. This module is those forty lines as a
dict, plus the checker that asserts the five properties the book depends on,
because a deployment nobody can test is a deployment nobody should trust.

THE HAZARD THAT IS SPECIFIC TO FLY
==================================
Fly's job is keeping web apps available, and every default points that way:

    deploy strategy `rolling` is the DEFAULT; `bluegreen` and `canary` boot a
    new machine ALONGSIDE the old one. For an HTTP server that is zero
    downtime. Here it is two `CarryEngine`s, two cold starts, and a second
    $100 short against inventory that backs one — each with a ledger saying
    everything is fine.

    `auto_stop_machines` stops a machine nothing is talking to. This process
    has no public port on purpose, so that setting would stop a book holding
    a live hedge and leave the client naked.

Both are refused by `check()`, not by a comment.

    python3 tools/fly_stack.py --check      # the committed fly.toml
    python3 tools/fly_stack.py --render     # write bot/fly.toml
    python3 tools/fly_stack.py --commands   # the exact deploy sequence
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Where the ledger lives inside the image. Must agree with the Dockerfile's
#: VOLUME and with STATE_DB_PATH, or a deploy quietly starts writing the
#: position to a directory that is not on the disk.
STATE_DIR = "/app/state"

#: Keys whose presence in a committed [env] block is a leaked credential.
SECRET_SHAPED = re.compile(
    r"(API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|PRIVATE_KEY|CREDENTIAL)",
    re.IGNORECASE)

#: Values that would arm live trading from a file in the repository. Arming is
#: a deliberate operator act with four independent conditions; a `git push`
#: is not one of them.
ARMING_KEYS = {"LIVE_TRADING_ACK": None, "PAPER_TRADING": "0",
               "USE_TESTNET": "0"}


# -- the Docker build context ---------------------------------------------
#
# The filter that was never applied to the thing it protects. `.dockerignore`
# removes files from the CONTEXT before the Dockerfile runs, so a COPY of an
# excluded path fails at build time with "file not found" — which is how an
# image that nobody could build sat under an ECS task definition, a one-off
# connector task and a whole Phase D drill plan.

def dockerignore_patterns(root: str = ROOT) -> List[str]:
    path = os.path.join(root, ".dockerignore")
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def excluded_by(patterns: List[str], relpath: str) -> bool:
    """Docker's context filter, to the precision this repo needs.

    Last matching pattern wins, `!` negates, and a pattern naming a directory
    excludes everything beneath it. Deliberately conservative: it reports an
    exclusion whenever a plain reading of the file would, so it errs towards
    complaining about an image that might build rather than blessing one that
    cannot.
    """
    relpath = relpath.strip("/")
    verdict = False
    for raw in patterns:
        negated = raw.startswith("!")
        pattern = raw[1:] if negated else raw
        pattern = pattern.strip("/")
        if not pattern:
            continue
        hit = (fnmatch.fnmatch(relpath, pattern)
               or relpath.startswith(pattern + "/")
               or fnmatch.fnmatch(os.path.basename(relpath), pattern)
               and "/" not in pattern)
        if hit:
            verdict = not negated
    return verdict


def copy_sources(root: str = ROOT) -> List[str]:
    """Every source token of every COPY in the Dockerfile."""
    path = os.path.join(root, "Dockerfile")
    if not os.path.exists(path):
        return []
    text = re.sub(r"\\\n", " ", open(path, encoding="utf-8").read())
    out: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("COPY"):
            continue
        tokens = [t for t in line.split()[1:] if not t.startswith("--")]
        out.extend(tokens[:-1])            # the last token is the destination
    return out


def context_conflicts(root: str = ROOT) -> List[str]:
    """COPY sources that .dockerignore removes from the context. Non-empty
    means the image cannot build."""
    patterns = dockerignore_patterns(root)
    return [src for src in copy_sources(root) if excluded_by(patterns, src)]


# -- the machine ----------------------------------------------------------

def template(*, app: str = "carry-book", region: str = "fra",
             volume: str = "carry_state", memory_mb: int = 512,
             book_mode: str = "carry", borrow_apr: str = "0.0",
             execution_mode: str = "overlay",
             execution_style: str = "taker") -> Dict[str, Any]:
    """One machine. No public port. A disk. Comes back when it dies."""
    return {
        "app": app,
        # ONE region. A Fly volume lives on one host in one region; a second
        # region means a second volume, a second ledger and a second book.
        "primary_region": region,
        "build": {"dockerfile": "Dockerfile"},
        # IMMEDIATE, never rolling. See the module docstring: every other
        # strategy can have two machines alive at once, and two machines is
        # two hedges.
        "deploy": {"strategy": "immediate"},
        # main.py exits 1 when it cannot reach the venue (measured 0042). That
        # is correct — and it must come back and try again, or a thirty-second
        # venue blip silently ends the book.
        "restart": [{"policy": "on-failure", "retries": 10}],
        "env": {
            "BOOK_MODE": book_mode,
            "CARRY_BORROW_APR": borrow_apr,
            "CARRY_EXECUTION_MODE": execution_mode,
            "CARRY_EXECUTION_STYLE": execution_style,
            # SAFE BY DEFAULT, and not changeable by pushing this file: a
            # value here that armed live trading is refused by check().
            "USE_TESTNET": "1",
            "PAPER_TRADING": "1",
            "ENABLE_HEALTH_SERVER": "1",
            "HEALTHCHECK_PORT": "8081",
            "LOG_LEVEL": "INFO",
            "STATE_DB_PATH": f"{STATE_DIR}/trading_state.db",
        },
        # The ledger. Losing it does not open a second hedge — cold start
        # reconciles against the venue and HALTS on a disagreement (0037) —
        # but it does mean a human has to come and look.
        "mounts": [{"source": volume, "destination": STATE_DIR}],
        # NO [http_service] AND NO [[services]]. This process has nothing to
        # serve and no business being reachable from the internet; the health
        # check below runs inside Fly's private network.
        "checks": {
            "health": {
                "type": "http", "port": 8081, "method": "get",
                "path": "/health", "interval": "30s", "timeout": "5s",
                # Cold start reconciles against the venue BEFORE the server
                # binds. A grace period shorter than that kills the container
                # mid-reconciliation.
                "grace_period": "180s",
            }
        },
        "vm": [{"size": "shared-cpu-1x", "memory": f"{memory_mb}mb",
                "cpus": 1}],
        # SIGTERM reaches Python (the Dockerfile uses exec form) and the
        # timeout is long enough for a graceful shutdown to finish writing.
        "kill_signal": "SIGTERM",
        "kill_timeout": "60s",
    }


def seconds(value: Any) -> float:
    """`"180s"`, `"3m"`, `180` -> 180.0."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    units = {"s": 1.0, "m": 60.0, "h": 3600.0}
    if text and text[-1] in units:
        try:
            return float(text[:-1]) * units[text[-1]]
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def check(cfg: Dict[str, Any]) -> List[str]:
    """Every property this book depends on, asserted. Empty means deployable."""
    problems: List[str] = []
    env = dict(cfg.get("env") or {})

    # -- two machines is two books ---------------------------------------
    strategy = (cfg.get("deploy") or {}).get("strategy")
    if strategy != "immediate":
        problems.append(
            f"deploy.strategy is {strategy!r}: rolling (Fly's DEFAULT), "
            "bluegreen and canary can all have two machines alive at once, "
            "and two machines is two CarryEngines, two cold starts and two "
            "hedges against inventory that backs one. Must be 'immediate'.")
    processes = cfg.get("processes") or {}
    if len(processes) > 1:
        problems.append(
            f"{len(processes)} process groups: Fly runs a machine per group, "
            "so this is the two-books hazard with a different name.")
    if not cfg.get("primary_region"):
        problems.append(
            "no primary_region: a volume lives on one host in one region, so "
            "an unpinned app can grow a second region, a second volume, a "
            "second ledger and a second book.")

    # -- never stopped under an open hedge -------------------------------
    for block in ("http_service", "services"):
        section = cfg.get(block)
        if section:
            problems.append(
                f"[{block}] is declared: this process serves nothing and must "
                "not be reachable from the public internet. It also drags in "
                "auto_stop/auto_start, which would stop a machine holding a "
                "live hedge because no HTTP request arrived.")
    restart = (cfg.get("restart") or [{}])[0].get("policy")
    if restart not in ("on-failure", "always"):
        problems.append(
            f"restart policy {restart!r}: main.py exits 1 when it cannot "
            "reach the venue, so a policy that leaves it down turns a "
            "thirty-second blip into a book that stopped reconciling.")

    # -- the ledger survives a deploy ------------------------------------
    mounts = cfg.get("mounts") or []
    if len(mounts) != 1:
        problems.append(
            f"{len(mounts)} volume mounts: exactly one is required. The "
            "ledger is the only thing that knows a position exists "
            "(INVENTORY D3); none means amnesia, two means ambiguity.")
    else:
        dest = mounts[0].get("destination")
        if dest != STATE_DIR:
            problems.append(
                f"volume mounted at {dest!r}, not {STATE_DIR!r}, which is what "
                "the Dockerfile declares as VOLUME.")
        db = env.get("STATE_DB_PATH", "")
        if not db.startswith(str(dest) + "/"):
            problems.append(
                f"STATE_DB_PATH {db!r} is not on the mounted volume {dest!r}: "
                "the position would be written to the container filesystem "
                "and lost on every deploy.")

    # -- a deploy cannot arm live trading or leak a key ------------------
    for key in env:
        if SECRET_SHAPED.search(key):
            problems.append(
                f"env key {key!r} is secret-shaped: credentials are set with "
                "`fly secrets set`, which stores them encrypted and injects "
                "them at runtime. This file is in git.")
    for key, forbidden in ARMING_KEYS.items():
        if key in env and (forbidden is None or str(env[key]) == forbidden):
            problems.append(
                f"env {key}={env[key]!r} would arm live trading from a file in "
                "the repository. Arming is a deliberate act with four "
                "independent conditions; `git push` is not one of them.")

    # -- the book gets what build_bot demands ----------------------------
    if env.get("BOOK_MODE") == "carry":
        for key in ("CARRY_BORROW_APR", "CARRY_EXECUTION_MODE"):
            if not env.get(key):
                problems.append(
                    f"BOOK_MODE=carry without {key}: build_bot raises, which "
                    "on Fly is not an error message but a crash loop with a "
                    "restart policy behind it.")

    # -- health -----------------------------------------------------------
    checks = cfg.get("checks") or {}
    if not checks:
        problems.append("no [checks]: nothing would notice a wedged process.")
    for name, spec in checks.items():
        port = spec.get("port")
        declared = env.get("HEALTHCHECK_PORT")
        if declared and str(port) != str(declared):
            problems.append(
                f"check {name!r} probes port {port} but HEALTHCHECK_PORT is "
                f"{declared}: the probe would fail forever and the machine "
                "would be restarted forever.")
        if seconds(spec.get("grace_period", 0)) < 120:
            problems.append(
                f"check {name!r} grace_period {spec.get('grace_period')!r} is "
                "under the 120s the Dockerfile allows for startup: cold start "
                "reconciles against the venue BEFORE the health server binds, "
                "and killing the container mid-reconciliation is how amnesia "
                "loops begin.")

    # -- size -------------------------------------------------------------
    vms = cfg.get("vm") or []
    for vm in vms:
        mem = str(vm.get("memory", "0")).lower().replace("mb", "")
        try:
            if float(mem) < 512:
                problems.append(
                    f"vm memory {vm.get('memory')!r}: numpy, scipy and "
                    "scikit-learn are in requirements.txt and 256mb OOM-kills "
                    "during import, which reads as a crash loop.")
        except ValueError:
            problems.append(f"vm memory {vm.get('memory')!r} is unparseable")

    if cfg.get("kill_signal") != "SIGTERM":
        problems.append(
            f"kill_signal {cfg.get('kill_signal')!r}: the Dockerfile uses exec "
            "form so SIGTERM reaches Python and graceful shutdown runs.")
    return problems


# -- rendering ------------------------------------------------------------

def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace('"', '\\"') + '"'


def to_toml(cfg: Dict[str, Any]) -> str:
    """Just enough TOML for this document. `tomllib` reads it back, and a test
    asserts the round trip, so the renderer cannot drift from the checker."""
    lines: List[str] = []
    for key, value in cfg.items():
        if isinstance(value, (dict, list)):
            continue
        lines.append(f"{key} = {_value(value)}")
    for key, value in cfg.items():
        if isinstance(value, dict):
            nested = {k: v for k, v in value.items() if isinstance(v, dict)}
            flat = {k: v for k, v in value.items() if not isinstance(v, dict)}
            if flat or not nested:
                lines.append(f"\n[{key}]")
                for k, v in flat.items():
                    lines.append(f"{k} = {_value(v)}")
            for name, spec in nested.items():
                lines.append(f"\n[{key}.{name}]")
                for k, v in spec.items():
                    lines.append(f"{k} = {_value(v)}")
        elif isinstance(value, list):
            for item in value:
                lines.append(f"\n[[{key}]]")
                for k, v in item.items():
                    lines.append(f"{k} = {_value(v)}")
    return "\n".join(lines).strip() + "\n"


HEADER = """\
# GENERATED by tools/fly_stack.py --render. Edit that, not this.
#
# tests/test_fly_runtime.py parses this file and runs fly_stack.check() on it,
# so a hand edit that weakens one of the properties below fails the suite:
#
#   deploy.strategy = immediate   two machines is two hedges
#   exactly one [[mounts]]        the ledger is the only thing that knows
#   no [http_service]             nothing to serve; and auto_stop would stop
#                                 a machine holding a live hedge
#   PAPER_TRADING / USE_TESTNET   a git push may not arm live trading
#   no secret-shaped env keys     credentials come from `fly secrets set`
"""

COMMANDS = """\
# ---------------------------------------------------------------- Phase D --
# Everything below is run ONCE, by a human, from the repo's bot/ directory.
# Nothing here trades: the image ships PAPER_TRADING=1 and the promotion gate
# is unsigned, so the carry broker refuses every order until both change.

cd bot

# 1. the app and the disk. --no-deploy first: the volume must exist before a
#    machine tries to mount it.
fly launch --no-deploy --name carry-book --region fra --copy-config
fly volumes create carry_state --region fra --size 1 --yes

# 2. THE OUTBOUND IP. Bybit keys are pinned to an address; this is that
#    address. App-scoped, so it survives machine recreation and deploys.
#    ~$3.60/month for the IPv4.
fly ips allocate-egress --region fra
fly ips list                      # <- pin the Bybit key to the IPv4 shown

# 3. credentials, encrypted, injected at runtime, never in a layer or a file.
fly secrets set BYBIT_API_KEY=... BYBIT_API_SECRET=...

# 4. deploy. --strategy immediate is also in fly.toml; passed again because
#    this is the flag that stops Fly running two books at once.
fly deploy --strategy immediate
fly logs

# 5. CAN THIS MACHINE REACH THE VENUE? The question a drill has to answer
#    before it can mean anything. Read-only; places nothing.
fly ssh console -C "python3 tools/connector_check.py --require-bybit-testnet"

# 6. the four invariants, printed from inside the running image.
fly ssh console -C "python3 tools/session_tail.py"

# 7. only now, and only after testnet BTC is in the wallet, does the book get
#    a venue to talk to. PAPER_TRADING=0 with USE_TESTNET=1 is TESTNET ONLY:
#    _carry_orders_permitted returns (True, "TESTNET"); mainnet additionally
#    requires live authorisation AND a signed promotion gate, and the gate is
#    False.
fly secrets set PAPER_TRADING=0     # USE_TESTNET stays 1
"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render", action="store_true",
                    help="write bot/fly.toml")
    ap.add_argument("--check", action="store_true",
                    help="check the committed bot/fly.toml")
    ap.add_argument("--commands", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "fly.toml"))
    args = ap.parse_args(argv)

    conflicts = context_conflicts(ROOT)
    if conflicts:
        print("THE IMAGE CANNOT BUILD. .dockerignore removes these COPY "
              "sources from the build context:", file=sys.stderr)
        for src in conflicts:
            print(f"    {src}", file=sys.stderr)
        return 1
    print(f"build context: all {len(copy_sources(ROOT))} COPY sources survive "
          ".dockerignore")

    if args.commands:
        print(COMMANDS)
        return 0

    cfg = template()
    if args.render:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(HEADER + "\n" + to_toml(cfg))
        print(f"wrote {args.out}")

    target = cfg
    if args.check:
        import tomllib
        path = os.path.join(ROOT, "fly.toml")
        if not os.path.exists(path):
            print("bot/fly.toml does not exist; run --render", file=sys.stderr)
            return 1
        with open(path, "rb") as handle:
            target = tomllib.load(handle)

    problems = check(target)
    for problem in problems:
        print(f"  PROBLEM  {problem}")
    print(f"\n{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
