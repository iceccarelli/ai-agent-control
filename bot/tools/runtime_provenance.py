#!/usr/bin/env python3
"""runtime_provenance.py -- what source, what dependencies and what role is this image, established by the image itself and re-checked at runtime. No network, no credential, no order.

    python3 tools/runtime_provenance.py build  --git-sha SHA --environment-class CLASS [--root /app] [--out /app/RUNTIME_MANIFEST.json] [--built-at ISO] [--config FILE ...]
    python3 tools/runtime_provenance.py verify [--root /app] [--manifest M] [--expect CLASS] [--json]       exit 0 VERIFIED, 2 MISMATCH or MISSING

The five questions the owner must never answer from memory:
  1. What source commit is this running?               ``git_sha`` (build argument; "unknown", the all-zero SHA and anything that is not 40 hex are REFUSED)
  2. What image digest is this?                         the registry digest cannot be inside the image it names: the runtime reports the platform's own reference (``FLY_IMAGE_REF``)
                                                         and the owner compares it with ``fly image show`` (RUNBOOKS "Runtime provenance"); the manifest binds the SOURCE, the platform binds the digest
  3. What recorder implementation is this?              ``recorder_fingerprint`` = sha256 over the sorted (path, sha256) pairs of the recorder's static import closure
                                                         (``image_manifest.closure``), re-hashed from the files actually present
  4. What dependency closure is this?                   ``package_fingerprint`` = sha256 over the sorted ``name==version`` of every installed distribution, plus the Python minor version
  5. Is this image approved for the current runtime?    ``environment_class`` (READ_ONLY_RECORDER / TESTNET_EXECUTOR / RESEARCH / PRODUCTION_PAPER) must equal the class of the Fly app it runs in
                                                         (``FLY_APP_NAME``): an executor image on the recorder app, or the reverse, is a MISMATCH

``verify`` FAILS CLOSED: a missing, unreadable, oversized, malformed or wrong-schema manifest, an unknown SHA, a different Python minor, a different dependency set, any file of the closure missing or changed,
any file the entry points now import that the manifest does not list, or a class that does not match the app is a MISMATCH or MISSING, never a pass. The config fingerprint (the Dockerfile and Fly
configuration the image was built with) is REPORTED; those files are not in the running image, so it is not re-verifiable there and is labelled so.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, BOT)

import image_manifest as IM  # noqa: E402

SCHEMA = "runtime_manifest/v1"
MANIFEST_NAME = "RUNTIME_MANIFEST.json"
MAX_MANIFEST_BYTES = 512 * 1024
ENV_CLASSES = ("READ_ONLY_RECORDER", "TESTNET_EXECUTOR", "RESEARCH", "PRODUCTION_PAPER")
#: the static import closure of each class's entry points (paths relative to the application root: ``bot/`` in the repository, ``/app`` in the image)
ENTRY_SETS: Dict[str, Sequence[str]] = {
    "READ_ONLY_RECORDER": IM.ENTRY_POINTS,
    "TESTNET_EXECUTOR": ("tools/ms_testnet_autopilot.py",),
    "RESEARCH": ("tools/research_service.py",),
    "PRODUCTION_PAPER": ("main.py",),
}
#: the Fly app each class may run in (``FLY_APP_NAME``). An app not listed here is not an approved runtime for ANY class.
APP_CLASS = {"carry-drill": "READ_ONLY_RECORDER", "carry-autopilot": "TESTNET_EXECUTOR", "carry-research": "RESEARCH", "carry-book": "PRODUCTION_PAPER"}
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
STATUSES = ("VERIFIED", "MISMATCH", "MISSING")


def _digest(pairs: Sequence[Any]) -> str:
    h = hashlib.sha256()
    for a, b in pairs:
        h.update(f"{a}\x00{b}\n".encode("utf-8"))
    return h.hexdigest()


def installed_packages() -> List[str]:
    """Every installed distribution as ``name==version`` (names normalised, sorted): the dependency closure the interpreter can actually import."""
    from importlib import metadata
    seen: Dict[str, str] = {}
    for d in metadata.distributions():
        name = (d.metadata["Name"] or "").strip().lower().replace("_", "-")
        if name:
            seen[name] = d.version
    return [f"{n}=={v}" for n, v in sorted(seen.items())]


def package_fingerprint(packages: Optional[Sequence[str]] = None) -> str:
    return hashlib.sha256("\n".join(sorted(installed_packages() if packages is None else packages)).encode("utf-8")).hexdigest()


def python_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def file_hashes(root: str, entries: Sequence[str]) -> Dict[str, str]:
    return {rel: IM._sha(os.path.join(root, rel)) for rel in IM.closure(root, entries)}


def build_manifest(root: str, *, git_sha: str, environment_class: str, built_at_utc: Optional[str] = None, config_files: Sequence[str] = (), packages: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The manifest the image bakes in at build time. Refuses an unknown class and an unusable SHA (a build that cannot say what it was built from does not produce a manifest)."""
    if environment_class not in ENV_CLASSES:
        raise ValueError(f"environment_class must be one of {ENV_CLASSES}, not {environment_class!r}")
    if not _SHA40.match(git_sha or "") or set(git_sha) == {"0"}:
        raise ValueError("git_sha must be the 40-hex commit the image is built from (pass `--build-arg BUILD_COMMIT=$(git rev-parse HEAD)`); 'unknown' and the all-zero SHA are refused")
    files = file_hashes(root, ENTRY_SETS[environment_class])
    rec_entries = ENTRY_SETS["READ_ONLY_RECORDER"]
    recorder = _digest(sorted(file_hashes(root, rec_entries).items())) if all(os.path.isfile(os.path.join(root, e)) for e in rec_entries) else None
    cfg = {os.path.basename(c): IM._sha(c) for c in config_files}
    return {"schema": SCHEMA, "git_sha": git_sha, "environment_class": environment_class, "entry_points": list(ENTRY_SETS[environment_class]),
            "built_at_utc": built_at_utc or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "python_minor": python_minor(),
            "package_fingerprint": package_fingerprint(packages), "n_packages": len(installed_packages() if packages is None else packages),
            "closure_fingerprint": _digest(sorted(files.items())), "recorder_fingerprint": recorder,
            "config_fingerprint": _digest(sorted(cfg.items())) if cfg else None, "config_files": cfg, "files": dict(sorted(files.items()))}


def _result(status: str, detail: str, man: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None, differing: Sequence[str] = ()) -> Dict[str, Any]:
    env = env or {}
    return {"status": status, "detail": detail[:400], "git_sha": (man or {}).get("git_sha"), "environment_class": (man or {}).get("environment_class"), "built_at_utc": (man or {}).get("built_at_utc"),
            "recorder_fingerprint": (man or {}).get("recorder_fingerprint"), "closure_fingerprint": (man or {}).get("closure_fingerprint"), "package_fingerprint": (man or {}).get("package_fingerprint"),
            "config_fingerprint": (man or {}).get("config_fingerprint"), "config_fingerprint_verifiable_at_runtime": False,
            "image_ref": env.get("FLY_IMAGE_REF"), "app": env.get("FLY_APP_NAME"), "machine_version": env.get("FLY_MACHINE_VERSION"), "differing": list(differing)[:20]}


def verify_runtime(root: str, *, manifest_path: Optional[str] = None, expect: Optional[str] = None, env: Optional[Mapping[str, str]] = None, packages: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Re-check the manifest against the files and packages actually present. Never raises: any failure to establish provenance is MISSING or MISMATCH."""
    env = dict(os.environ) if env is None else env
    path = manifest_path or os.path.join(root, MANIFEST_NAME)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return _result("MISSING", f"no runtime manifest at {path} ({type(exc).__name__}): the image cannot say what it was built from", env=env)
    if size > MAX_MANIFEST_BYTES:
        return _result("MISMATCH", f"the manifest is {size} bytes (limit {MAX_MANIFEST_BYTES}): refused", env=env)
    try:
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return _result("MISMATCH", f"the manifest is unreadable ({type(exc).__name__})", env=env)
    if not isinstance(man, dict) or man.get("schema") != SCHEMA or not isinstance(man.get("files"), dict) or not man["files"]:
        return _result("MISMATCH", "the manifest is not a runtime_manifest/v1 with a file list", man if isinstance(man, dict) else None, env)
    cls = man.get("environment_class")
    if cls not in ENV_CLASSES:
        return _result("MISMATCH", f"the manifest's environment class {cls!r} is not one of {ENV_CLASSES}", man, env)
    sha = man.get("git_sha")
    if not isinstance(sha, str) or not _SHA40.match(sha) or set(sha) == {"0"}:
        return _result("MISMATCH", f"the manifest's git sha {sha!r} is not a 40-hex commit: the build could not say what it was built from", man, env)
    if expect is not None and cls != expect:
        return _result("MISMATCH", f"this image is a {cls} image; this process requires {expect}", man, env)
    app = env.get("FLY_APP_NAME")
    if app:
        if app not in APP_CLASS:
            return _result("MISMATCH", f"Fly app {app!r} is not an approved runtime for any image class", man, env)
        if APP_CLASS[app] != cls:
            return _result("MISMATCH", f"Fly app {app!r} is a {APP_CLASS[app]} runtime and this is a {cls} image", man, env)
    if man.get("python_minor") != python_minor():
        return _result("MISMATCH", f"built for Python {man.get('python_minor')}, running Python {python_minor()}", man, env)
    if man.get("package_fingerprint") != package_fingerprint(packages):
        return _result("MISMATCH", "the installed dependency set is not the one the image was built with", man, env)
    differing: List[str] = []
    for rel, want in sorted(man["files"].items()):
        p = os.path.join(root, rel)
        try:
            have = IM._sha(p)
        except OSError:
            differing.append(f"{rel} (missing)")
            continue
        if have != want:
            differing.append(f"{rel} (changed)")
    if differing:
        return _result("MISMATCH", f"{len(differing)} file(s) of the closure are missing or changed since the build: " + ", ".join(differing[:5]), man, env, differing)
    try:
        now_closure = set(IM.closure(root, ENTRY_SETS[cls]))
    except SystemExit as exc:
        return _result("MISMATCH", f"the entry points cannot be resolved here: {exc}", man, env)
    extra = sorted(now_closure - set(man["files"]))
    if extra:
        return _result("MISMATCH", f"{len(extra)} file(s) the entry points import are not in the manifest: " + ", ".join(extra[:5]), man, env, [f"{e} (unlisted)" for e in extra])
    if _digest(sorted(man["files"].items())) != man.get("closure_fingerprint"):
        return _result("MISMATCH", "the manifest's own closure fingerprint does not match its file list", man, env)
    return _result("VERIFIED", f"{len(man['files'])} closure files and {man.get('n_packages')} packages match the build of {sha[:12]} ({cls})", man, env)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write the runtime manifest at image build time")
    b.add_argument("--git-sha", required=True)
    b.add_argument("--environment-class", required=True, choices=ENV_CLASSES)
    b.add_argument("--root", default=BOT)
    b.add_argument("--out", default=None)
    b.add_argument("--built-at", default=None)
    b.add_argument("--config", action="append", default=[], help="a build configuration file to fingerprint (Dockerfile, fly toml); repeatable")
    v = sub.add_parser("verify", help="re-check the manifest against the files and packages present (exit 0 VERIFIED, 2 otherwise)")
    v.add_argument("--root", default=BOT)
    v.add_argument("--manifest", default=None)
    v.add_argument("--expect", default=None, choices=ENV_CLASSES)
    v.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        try:
            man = build_manifest(a.root, git_sha=a.git_sha, environment_class=a.environment_class, built_at_utc=a.built_at, config_files=a.config)
        except (ValueError, SystemExit) as exc:
            print(f"runtime_provenance: REFUSED: {exc}", file=sys.stderr)
            return 2
        out = a.out or os.path.join(a.root, MANIFEST_NAME)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(man, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, out)
        print(f"wrote {out}: {len(man['files'])} closure files, {man['n_packages']} packages, class {man['environment_class']}, sha {man['git_sha'][:12]}")
        return 0
    res = verify_runtime(a.root, manifest_path=a.manifest, expect=a.expect)
    print(json.dumps(res, indent=2, sort_keys=True) if a.json else f"{res['status']}: {res['detail']}")
    return 0 if res["status"] == "VERIFIED" else 2


if __name__ == "__main__":
    sys.exit(main())
