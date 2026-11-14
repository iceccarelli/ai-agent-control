#!/usr/bin/env python3
"""OFFLINE deployability check for the two Fly apps whose images must prove what they run: ``carry-autopilot`` (the Testnet executor) and ``carry-drill`` (the read-only recorder + monitor).

Round 22. A green CI run and a built image are not a deployment. This tool answers one narrow question from the repository alone: **do the committed pieces of this app agree with each other and with
what the code needs**, so that an owner who deploys them is not defeated by a file that was never exercised together. It reads files only: no network, no flyctl, no Supabase, no Bybit, no environment
value, no secret (secret NAMES are compared, never values), and it writes nothing.

  T1 toml       the Fly toml has the app name, region, ONE volume at /data, no public service, the Testnet environment and no secret or arming name
  T2 dockerfile the Dockerfile the toml names exists, takes BUILD_COMMIT WITHOUT a default and records the runtime manifest, and its entrypoint verifies it for the right image class
  T3 paths      every file the Dockerfile COPYs exists in this checkout
  T4 secrets    the secret NAMES the app needs are named in the owner runbook; no arming name or generic exchange credential appears in any deployment file
  T5 rpc        (executor) every Supabase RPC the code calls is created by a committed migration
  T6 deploy     the documented deploy command carries ``--build-arg BUILD_COMMIT`` and names the app's own toml

The verdict is ``OFFLINE_CHECKS_PASS_OWNER_STEPS_PENDING`` or ``NO_GO``. It is NEVER "deployed", "ready" or "go": whether the app exists, the volume is the right size, the secrets are set, the migrations are
applied, the machine runs the image it was built from and the venue is reachable are OWNER steps this tool cannot see (listed in ``owner_steps``).

Usage:  python3 tools/deploy_check.py [--app carry-autopilot|carry-drill|all] [--root BOT_DIR] [--json]       Exit: 0 pass, 1 NO_GO, 2 usage."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.11 is the production baseline
    tomllib = None  # type: ignore[assignment]

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
SCHEMA = "deploy_check/v1"
PASS_OWNER_PENDING, NO_GO = "OFFLINE_CHECKS_PASS_OWNER_STEPS_PENDING", "NO_GO"
#: environment NAME PREFIXES that must never appear in a deployment file of these apps (Mainnet arming switches and the generic, non-Testnet exchange credentials)
FORBIDDEN_PREFIXES = ("LIVE_", "BYBIT_API_")
APPS: Dict[str, Dict[str, Any]] = {
    "carry-autopilot": {
        "toml": "fly.autopilot.toml", "dockerfile": "Dockerfile.autopilot", "entrypoint": "deploy/autopilot-entrypoint.sh", "image_class": "TESTNET_EXECUTOR", "volume": "autopilot_data", "restart": "always",
        "env": {"USE_TESTNET": "1", "BYBIT_VENUE": "testnet", "HOME": "/data"},
        "secrets": ("BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_OWNER_ID"),
        "deploy_doc": "deploy/safe-deploy.sh", "needs_rpc": True,
    },
    "carry-drill": {
        "toml": "fly.drill.toml", "dockerfile": "Dockerfile.drill", "entrypoint": "deploy/drill-entrypoint.sh", "image_class": "READ_ONLY_RECORDER", "volume": "drill_runs", "restart": "on-failure",
        "env": {"USE_TESTNET": "1", "PAPER_TRADING": "1", "BYBIT_VENUE": "testnet"},
        "secrets": (), "deploy_doc": "RUNBOOKS.md", "needs_rpc": False,
    },
}
OWNER_STEPS = (
    "the Fly app exists and is the one named in the toml (fly apps list)",
    "the volume exists, is the size the storage sizing in RUNBOOKS requires and is attached (fly volumes list -a <app>)",
    "the secrets are set on the app: NAMES only, typed by the owner (fly secrets list -a <app>); this tool never sees a value",
    "the Supabase migrations are applied to the production project (supabase migration list), and the schema `carry` is exposed to the service key",
    "the deploy is run by the owner from a clean checkout of a commit contained in origin/main, with --build-arg BUILD_COMMIT=<that sha>",
    "the RUNNING machine's image is the one built (fly image show -a <app>; `python3 tools/runtime_provenance.py verify --root /app` inside the machine) and its digest matches what was built",
    "the venue is reachable from Frankfurt and the account is the Testnet account the capability names (autopilot-ctl status; venue-flat proof)",
    "for the executor: the owner's capability, grant and capital lineage exist (never created by this repository)",
)


def _read(root: str, rel: str) -> Optional[str]:
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _noncomment(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _row(cid: str, ok: bool, detail: str, **facts: Any) -> Dict[str, Any]:
    return {"id": cid, "ok": bool(ok), "detail": detail[:300], **({"facts": facts} if facts else {})}


def _toml(root: str, rel: str) -> Tuple[Optional[Dict[str, Any]], str]:
    text = _read(root, rel)
    if text is None:
        return None, f"{rel} is missing"
    if tomllib is None:
        return None, "tomllib is unavailable on this interpreter (Python 3.11+ is the production baseline)"
    try:
        return tomllib.loads(text), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{rel} does not parse: {type(exc).__name__}"


def check_toml(root: str, name: str, spec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    doc, err = _toml(root, spec["toml"])
    if doc is None:
        return [_row("T1_TOML", False, err)]
    why: List[str] = []
    if doc.get("app") != name:
        why.append(f"app is {doc.get('app')!r}, expected {name!r}")
    if doc.get("primary_region") != "fra":
        why.append(f"primary_region is {doc.get('primary_region')!r}, expected 'fra' (Bybit blocks GitHub-hosted runners, not Frankfurt)")
    mounts = doc.get("mounts") if isinstance(doc.get("mounts"), list) else ([doc["mounts"]] if isinstance(doc.get("mounts"), dict) else [])
    if len(mounts) != 1 or mounts[0].get("destination") != "/data" or mounts[0].get("source") != spec["volume"]:
        why.append(f"expected exactly one volume {spec['volume']!r} at /data, found {[(m.get('source'), m.get('destination')) for m in mounts]}")
    if "http_service" in doc or "services" in doc:
        why.append("a public service is declared (these apps serve nothing)")
    if (doc.get("build") or {}).get("dockerfile") != spec["dockerfile"]:
        why.append(f"build.dockerfile is {(doc.get('build') or {}).get('dockerfile')!r}, expected {spec['dockerfile']!r}")
    restart = doc.get("restart")
    pol = (restart[0] if isinstance(restart, list) and restart else restart or {}).get("policy") if restart else None
    if pol != spec["restart"]:
        why.append(f"restart policy is {pol!r}, expected {spec['restart']!r}")
    env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
    for k, v in spec["env"].items():
        if str(env.get(k)) != v:
            why.append(f"env {k} is {env.get(k)!r}, expected {v!r}")
    bad_env = sorted(k for k in env if str(k).startswith(FORBIDDEN_PREFIXES))
    if bad_env:
        why.append(f"forbidden environment name(s) in the toml: {bad_env}")
    secretish = sorted(k for k in env if re.search(r"(SECRET|TOKEN|PASSWORD|API_KEY|SERVICE_ROLE)", str(k)))
    if secretish:
        why.append(f"secret-looking name(s) in [env] (secrets are set with `fly secrets set`, never in the toml): {secretish}")
    return [_row("T1_TOML", not why, "; ".join(why) or f"{spec['toml']}: app, region, one /data volume, no service, {spec['restart']} restart, Testnet environment, no secret or arming name")]


def check_dockerfile(root: str, name: str, spec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    text = _read(root, spec["dockerfile"])
    if text is None:
        return [_row("T2_DOCKERFILE", False, f"{spec['dockerfile']} is missing")]
    code = "\n".join(_noncomment(text))
    why: List[str] = []
    arg = re.search(r"^\s*ARG\s+BUILD_COMMIT(\S*)\s*$", code, re.M)
    if not arg:
        why.append("no `ARG BUILD_COMMIT`")
    elif arg.group(1):
        why.append(f"BUILD_COMMIT has a default ({arg.group(1)!r}): an image could be built without saying what it was built from")
    if "runtime_provenance.py build" not in code:
        why.append("the image build does not record the runtime manifest (tools/runtime_provenance.py build)")
    if "--environment-class " + spec["image_class"] not in code:
        why.append(f"the manifest is not recorded as class {spec['image_class']}")
    ep = _read(root, spec["entrypoint"])
    if ep is None:
        why.append(f"{spec['entrypoint']} is missing")
    elif f"runtime_provenance.py verify" not in ep or f"--expect {spec['image_class']}" not in ep:
        why.append(f"the entrypoint does not verify provenance for class {spec['image_class']} before starting")
    elif "exit 78" not in ep:
        why.append("the entrypoint does not refuse (exit 78) when provenance fails")
    return [_row("T2_DOCKERFILE", not why, "; ".join(why) or f"{spec['dockerfile']}: BUILD_COMMIT without a default, manifest recorded as {spec['image_class']}, entrypoint verifies it and refuses on failure")]


def _copy_sources(dockerfile: str) -> List[str]:
    """Source paths of every COPY (continuations joined), excluding ``--from=`` stage copies; the last token of each COPY is its destination."""
    joined = re.sub(r"\\\n", " ", dockerfile)
    out: List[str] = []
    for ln in joined.splitlines():
        s = ln.strip()
        if not s.upper().startswith("COPY "):
            continue
        toks = [t for t in s.split()[1:] if not t.startswith("--")]
        if any(t.startswith("--from=") for t in s.split()[1:]):
            continue
        out.extend(toks[:-1])
    return out


def check_paths(root: str, name: str, spec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    text = _read(root, spec["dockerfile"])
    if text is None:
        return [_row("T3_PATHS", False, f"{spec['dockerfile']} is missing")]
    missing = [p for p in _copy_sources(text) if not os.path.exists(os.path.join(root, p.rstrip("/")))]
    return [_row("T3_PATHS", not missing, ("COPY source(s) missing from this checkout: " + ", ".join(missing[:8])) if missing else f"every COPY source of {spec['dockerfile']} exists")]


def check_secrets(root: str, name: str, spec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    why: List[str] = []
    runbook = _read(root, "RUNBOOKS.md") or ""
    for s in spec["secrets"]:
        if s not in runbook:
            why.append(f"secret name {s} is not mentioned in RUNBOOKS.md")
    for rel in (spec["toml"], spec["dockerfile"], spec["entrypoint"]):
        body = "\n".join(_noncomment(_read(root, rel) or ""))
        for m in sorted(set(re.findall(r"\b((?:LIVE_|BYBIT_API_)[A-Z0-9_]*)\b", body))):
            why.append(f"{rel} names the forbidden variable {m}")
    return [_row("T4_SECRET_NAMES", not why, "; ".join(why) or ("required secret NAMES are documented (" + ", ".join(spec["secrets"]) + "); no arming or generic credential name in any deployment file" if spec["secrets"]
                                                               else "no secrets are required; no arming or generic credential name in any deployment file"))]


def rpc_names(root: str) -> List[str]:
    src = _read(root, "tools/exec_evidence.py") or ""
    return sorted(set(re.findall(r"\.rpc\(\s*[\"']([a-z_0-9]+)[\"']", src)))


def migration_functions(root: str) -> List[str]:
    d = os.path.join(root, "..", "supabase", "migrations")
    names: List[str] = []
    try:
        files = sorted(f for f in os.listdir(d) if f.endswith(".sql"))
    except OSError:
        return names
    for f in files:
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                names += re.findall(r"create\s+(?:or\s+replace\s+)?function\s+(?:[a-z_]+\.)?([a-z_0-9]+)", fh.read(), re.I)
        except OSError:
            continue
    return sorted(set(names))


def check_rpc(root: str, name: str, spec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if not spec["needs_rpc"]:
        return [_row("T5_RPC", True, "this app calls no Supabase RPC")]
    used, have = rpc_names(root), set(migration_functions(root))
    missing = [n for n in used if n not in have]
    if not used:
        return [_row("T5_RPC", False, "no RPC call was found in tools/exec_evidence.py: the check cannot say what the code needs")]
    return [_row("T5_RPC", not missing, ("RPC(s) the code calls that no committed migration creates: " + ", ".join(missing)) if missing else f"every RPC the code calls ({', '.join(used)}) is created by a committed migration; whether the migrations are APPLIED is an owner step",
                 rpcs=used)]


def check_deploy(root: str, name: str, spec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    doc = _read(root, spec["deploy_doc"]) or ""
    cmds = [ln.strip() for ln in re.sub(r"\\\n", " ", doc).splitlines() if re.search(r"\bfly deploy\b", ln) and spec["toml"] in ln]
    good = [c for c in cmds if "--build-arg BUILD_COMMIT=" in c]
    why = "" if good else (f"no documented `fly deploy ... {spec['toml']}` command carries --build-arg BUILD_COMMIT (found {len(cmds)} deploy command(s) naming the toml)")
    return [_row("T6_DEPLOY_COMMAND", bool(good), why or f"{spec['deploy_doc']}: the documented deploy command names {spec['toml']} and passes --build-arg BUILD_COMMIT")]


def check_app(root: str, name: str) -> Dict[str, Any]:
    spec = APPS[name]
    rows: List[Dict[str, Any]] = []
    for fn in (check_toml, check_dockerfile, check_paths, check_secrets, check_rpc, check_deploy):
        try:
            rows += fn(root, name, spec)
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is a failed check
            rows.append(_row(fn.__name__.upper(), False, f"the check could not run: {type(exc).__name__}"))
    return {"app": name, "image_class": spec["image_class"], "checks": rows, "ok": all(r["ok"] for r in rows)}


def run(root: str, apps: Sequence[str]) -> Dict[str, Any]:
    res = [check_app(root, a) for a in apps]
    ok = all(r["ok"] for r in res)
    return {"schema": SCHEMA, "overall": PASS_OWNER_PENDING if ok else NO_GO, "apps": res, "owner_steps": list(OWNER_STEPS),
            "claim": "the committed files agree with each other and with what the code needs; this is NOT a deployment, NOT a readiness statement and NOT evidence that anything runs: every owner step above is unverified here"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--app", default="all", choices=("all", *APPS))
    ap.add_argument("--root", default=BOT, help="the bot/ directory of a checkout")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"not a directory: {args.root}", file=sys.stderr)
        return 2
    out = run(args.root, list(APPS) if args.app == "all" else [args.app])
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        for a in out["apps"]:
            print(f"== {a['app']} ({a['image_class']}): {'OK' if a['ok'] else 'NOT OK'}")
            for c in a["checks"]:
                print(f"  {'PASS' if c['ok'] else 'FAIL'} {c['id']:20} {c['detail']}")
        print("OVERALL:", out["overall"])
        print("OWNER STEPS (not verifiable here):")
        for s in out["owner_steps"]:
            print("  -", s)
    return 0 if out["overall"] == PASS_OWNER_PENDING else 1


if __name__ == "__main__":
    raise SystemExit(main())
