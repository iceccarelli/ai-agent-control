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

The manifest lists EVERY file the image ships under the application root (not only the entry-point import closure: an order-capable script the supervisor spawns by path, a candidate signal, a file dropped by sftp
are all covered), plus the entrypoint scripts named at build time. ``verify`` reports the first failure with a closed ``code``. A first-party-looking import with no file at build time REFUSES the build (a closure file absent
at build time used to be dropped silently and the manifest still verified). The class/app check is a DEPLOY-MISTAKE detector (an executor image on the recorder app, or run with no app name at all), not a security boundary:
the environment is set by whoever controls the machine.

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
import stat
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
#: the closed vocabulary of ``code`` (the monitor, the alerts and the owner act on a code, not on prose)
CODES = ("OK", "MANIFEST_MISSING", "MANIFEST_UNREADABLE", "SHA_UNKNOWN", "CLASS_MISMATCH", "APP_MISMATCH", "APP_NOT_SET", "PYTHON_MISMATCH", "PACKAGES_MISMATCH", "FILE_CHANGED", "FILE_MISSING", "FILE_EXTRA",
         "CLOSURE_UNLISTED", "FINGERPRINT_INCONSISTENT")
#: what is NOT part of the shipped file set: the manifest itself and interpreter caches
_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
#: first-party-looking imports that are legitimately absent from an image: ``signals/funding_carry_fade_v1.py`` lazily imports ``sweep_geometry`` on a research-only path it never reaches in an image (the frozen file cannot be edited)
UNRESOLVED_ALLOWED = {"sweep_geometry"}


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
    """sha256 of the static import closure of ``entries`` (the sub-digests ``closure_fingerprint`` / ``recorder_fingerprint`` are over these)."""
    return {rel: IM._sha(os.path.join(root, rel)) for rel in IM.closure(root, entries)}


def shipped_files(root: str) -> List[str]:
    """EVERY regular source/data file the image ships under the application root, relative and sorted, except the manifest and interpreter caches (``__pycache__``, ``*.pyc``). A script the supervisor spawns by
    path, a candidate signal and a plain file dropped under /app are in this set: the manifest does not trust the import closure to be the whole image. NOT covered (a detector of deploy mistakes, not a
    defence against someone with a shell on the machine): a planted bytecode file in ``__pycache__`` or a stray ``*.pyc`` (the interpreter writes caches at run time, so they cannot be pinned), a symlinked
    directory (``os.walk`` does not follow it), and the manifest's own ``git_sha`` field (self-attested)."""
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for f in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
            if rel == MANIFEST_NAME or f.endswith(".pyc"):
                continue
            out.append(rel)
    return out


def unresolved_imports(root: str, rels: Sequence[str]) -> List[str]:
    """First-party-looking imports of the given files that resolve to NOTHING: not the standard library, not an installed distribution, not a file of ``root`` (or ``root/tools``). ``image_manifest.closure`` silently skips
    such a module, so without this gate an image missing ``md_wire.py`` would build a manifest that lists 102 files and later verify. Names in ``UNRESOLVED_ALLOWED`` are the one pinned exception."""
    try:
        from importlib import metadata
        third_party = set(metadata.packages_distributions())
    except Exception:  # noqa: BLE001
        third_party = set()
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    out = set()
    for rel in rels:
        if not rel.endswith(".py"):
            continue
        try:
            names = IM._imports(os.path.join(root, rel))
        except (OSError, SyntaxError, ValueError):
            out.add(f"{rel} (unparseable)")
            continue
        for mod in names:
            top = mod.split(".")[0]
            if top in stdlib or top in third_party or top in UNRESOLVED_ALLOWED or top == "__future__":
                continue
            if any(os.path.isfile(os.path.join(root, c)) for c in IM._candidates(mod)):
                continue
            # a dotted import of a first-party PACKAGE member (signals.x) resolves through its package directory
            if os.path.isdir(os.path.join(root, top)) or os.path.isfile(os.path.join(root, f"{top}.py")) or os.path.isfile(os.path.join(root, "tools", f"{top}.py")):
                continue
            out.add(f"{top} (imported by {rel})")
    return sorted(out)


def build_manifest(root: str, *, git_sha: str, environment_class: str, built_at_utc: Optional[str] = None, config_files: Sequence[str] = (), extra_files: Sequence[str] = (),
                   packages: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The manifest the image bakes in at build time. Refuses an unknown class, an unusable SHA (a build that cannot say what it was built from does not produce a manifest) and an unresolved first-party import
    (a closure file missing from the image). ``extra_files`` are shipped files OUTSIDE the application root that are part of the image's behaviour (the entrypoint scripts): listed as ``@/abs/path``."""
    if environment_class not in ENV_CLASSES:
        raise ValueError(f"environment_class must be one of {ENV_CLASSES}, not {environment_class!r}")
    if not _SHA40.match(git_sha or "") or set(git_sha) == {"0"}:
        raise ValueError("git_sha must be the 40-hex commit the image is built from (pass `--build-arg BUILD_COMMIT=$(git rev-parse HEAD)`); 'unknown' and the all-zero SHA are refused")
    shipped = shipped_files(root)
    gaps = unresolved_imports(root, shipped)
    if gaps:
        raise ValueError(f"{len(gaps)} import(s) resolve to nothing (a closure file is absent from this image): " + ", ".join(gaps[:8]))
    closure_files = file_hashes(root, ENTRY_SETS[environment_class])
    files = {rel: IM._sha(os.path.join(root, rel)) for rel in shipped}
    for ex in extra_files:
        files["@" + ex] = IM._sha(ex)
    rec_entries = ENTRY_SETS["READ_ONLY_RECORDER"]
    recorder = _digest(sorted(file_hashes(root, rec_entries).items())) if all(os.path.isfile(os.path.join(root, e)) for e in rec_entries) else None
    cfg = {os.path.basename(c): IM._sha(c) for c in config_files}
    return {"schema": SCHEMA, "git_sha": git_sha, "environment_class": environment_class, "entry_points": list(ENTRY_SETS[environment_class]),
            "built_at_utc": built_at_utc or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "python_minor": python_minor(),
            "package_fingerprint": package_fingerprint(packages), "n_packages": len(installed_packages() if packages is None else packages),
            "closure_fingerprint": _digest(sorted(closure_files.items())), "recorder_fingerprint": recorder, "files_fingerprint": _digest(sorted(files.items())),
            "config_fingerprint": _digest(sorted(cfg.items())) if cfg else None, "config_files": cfg, "files": dict(sorted(files.items()))}


#: the platform identity the check reads from the process (set by Fly itself): the app name, the image reference and the machine version. Nothing else, and none is a secret.
IDENTITY_ENV = ("FLY_APP_NAME", "FLY_IMAGE_REF", "FLY_MACHINE_VERSION")


def _result(status: str, detail: str, man: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None, differing: Sequence[str] = (), code: str = "OK") -> Dict[str, Any]:
    env = env or {}
    return {"status": status, "code": code, "detail": detail[:400], "git_sha": (man or {}).get("git_sha"), "environment_class": (man or {}).get("environment_class"), "built_at_utc": (man or {}).get("built_at_utc"),
            "recorder_fingerprint": (man or {}).get("recorder_fingerprint"), "closure_fingerprint": (man or {}).get("closure_fingerprint"), "files_fingerprint": (man or {}).get("files_fingerprint"),
            "package_fingerprint": (man or {}).get("package_fingerprint"), "config_fingerprint": (man or {}).get("config_fingerprint"), "config_fingerprint_verifiable_at_runtime": False,
            "image_ref": env.get("FLY_IMAGE_REF"), "app": env.get("FLY_APP_NAME"), "machine_version": env.get("FLY_MACHINE_VERSION"), "differing": list(differing)[:20]}


def verify_runtime(root: str, *, manifest_path: Optional[str] = None, expect: Optional[str] = None, env: Optional[Mapping[str, str]] = None, packages: Optional[Sequence[str]] = None,
                   require_app: Optional[bool] = None) -> Dict[str, Any]:
    """Re-check the manifest against the files and packages actually present. Never raises: any failure to establish provenance is MISSING or MISMATCH, with a closed ``code``.
    ``require_app``: the Fly app name must be set and match the image class (default: required for a TESTNET_EXECUTOR image, optional otherwise)."""
    process = os.environ
    env = {k: process[k] for k in IDENTITY_ENV if k in process} if env is None else env       # ONLY the three named, non-secret platform identity variables: the whole environment (secrets included) is never copied
    path = manifest_path or os.path.join(root, MANIFEST_NAME)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return _result("MISSING", f"no runtime manifest at {path} ({type(exc).__name__}): the image cannot say what it was built from", env=env, code="MANIFEST_MISSING")
    if size > MAX_MANIFEST_BYTES:
        return _result("MISMATCH", f"the manifest is {size} bytes (limit {MAX_MANIFEST_BYTES}): refused", env=env, code="MANIFEST_UNREADABLE")
    try:
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return _result("MISMATCH", f"the manifest is unreadable ({type(exc).__name__})", env=env, code="MANIFEST_UNREADABLE")
    if not isinstance(man, dict) or man.get("schema") != SCHEMA or not isinstance(man.get("files"), dict) or not man["files"]:
        return _result("MISMATCH", "the manifest is not a runtime_manifest/v1 with a file list", man if isinstance(man, dict) else None, env, code="MANIFEST_UNREADABLE")
    cls = man.get("environment_class")
    if cls not in ENV_CLASSES:
        return _result("MISMATCH", f"the manifest's environment class {cls!r} is not one of {ENV_CLASSES}", man, env, code="CLASS_MISMATCH")
    sha = man.get("git_sha")
    if not isinstance(sha, str) or not _SHA40.match(sha) or set(sha) == {"0"}:
        return _result("MISMATCH", f"the manifest's git sha {sha!r} is not a 40-hex commit: the build could not say what it was built from", man, env, code="SHA_UNKNOWN")
    if expect is not None and cls != expect:
        return _result("MISMATCH", f"this image is a {cls} image; this process requires {expect}", man, env, code="CLASS_MISMATCH")
    app = env.get("FLY_APP_NAME")
    need_app = (cls == "TESTNET_EXECUTOR") if require_app is None else require_app
    if need_app and not app:
        return _result("MISMATCH", f"a {cls} image runs only in its Fly app (FLY_APP_NAME is not set)", man, env, code="APP_NOT_SET")
    if app:
        if app not in APP_CLASS:
            return _result("MISMATCH", f"Fly app {app!r} is not an approved runtime for any image class", man, env, code="APP_MISMATCH")
        if APP_CLASS[app] != cls:
            return _result("MISMATCH", f"Fly app {app!r} is a {APP_CLASS[app]} runtime and this is a {cls} image", man, env, code="APP_MISMATCH")
    if man.get("python_minor") != python_minor():
        return _result("MISMATCH", f"built for Python {man.get('python_minor')}, running Python {python_minor()}", man, env, code="PYTHON_MISMATCH")
    if man.get("package_fingerprint") != package_fingerprint(packages):
        return _result("MISMATCH", "the installed dependency set is not the one the image was built with", man, env, code="PACKAGES_MISMATCH")
    if _digest(sorted(man["files"].items())) != man.get("files_fingerprint"):
        return _result("MISMATCH", "the manifest's own file fingerprint does not match its file list", man, env, code="FINGERPRINT_INCONSISTENT")
    changed: List[str] = []
    missing: List[str] = []
    for rel, want in sorted(man["files"].items()):
        p = rel[1:] if rel.startswith("@") else os.path.join(root, rel)
        try:
            have = IM._sha(p)
        except OSError:
            missing.append(f"{rel} (missing)")
            continue
        if have != want:
            changed.append(f"{rel} (changed)")
    if changed or missing:
        first = (changed or missing)[:5]
        return _result("MISMATCH", f"{len(changed)} shipped file(s) changed and {len(missing)} missing since the build: " + ", ".join(first), man, env, changed + missing, code="FILE_CHANGED" if changed else "FILE_MISSING")
    extra = [f"{r} (unlisted)" for r in shipped_files(root) if r not in man["files"]]
    if extra:
        return _result("MISMATCH", f"{len(extra)} file(s) under the application root are not in the manifest: " + ", ".join(extra[:5]), man, env, extra, code="FILE_EXTRA")
    try:
        now_closure = set(IM.closure(root, ENTRY_SETS[cls]))
    except SystemExit as exc:
        return _result("MISMATCH", f"the entry points cannot be resolved here: {exc}", man, env, code="CLOSURE_UNLISTED")
    unlisted = sorted(now_closure - set(man["files"]))
    if unlisted:
        return _result("MISMATCH", f"{len(unlisted)} file(s) the entry points import are not in the manifest: " + ", ".join(unlisted[:5]), man, env, [f"{e} (unlisted)" for e in unlisted], code="CLOSURE_UNLISTED")
    if _digest(sorted((r, man["files"][r]) for r in now_closure)) != man.get("closure_fingerprint"):
        return _result("MISMATCH", "the manifest's closure fingerprint does not match its files", man, env, code="FINGERPRINT_INCONSISTENT")
    return _result("VERIFIED", f"{len(man['files'])} shipped files and {man.get('n_packages')} packages match the build of {sha[:12]} ({cls})", man, env, code="OK")


def compare_manifest(man: Mapping[str, Any], root: str) -> Dict[str, Any]:
    """OFFLINE, for the owner: what a machine's manifest (``cat /app/RUNTIME_MANIFEST.json`` saved to a file) says against THIS checkout. For every file the image shipped: MATCH, DIFFERENT (same path, other bytes) or
    ABSENT_IN_CHECKOUT; and the closure files of this checkout the image does NOT contain (ABSENT_ON_MACHINE: the exact answer to "does the running image contain this code?"). Reads the manifest and the
    checkout only. It proves what the IMAGE was built from, not what the machine runs (that is ``verify`` inside the machine plus the registry digest)."""
    cls = man.get("environment_class")
    files = man.get("files") if isinstance(man.get("files"), Mapping) else {}
    different, match, absent_checkout = [], 0, []
    for rel, sha in sorted(files.items()):
        if str(rel).startswith("@"):
            continue
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            absent_checkout.append(rel)
        elif IM._sha(p) == sha:
            match += 1
        else:
            different.append(rel)
    absent_machine: List[str] = []
    if cls in ENTRY_SETS:
        absent_machine = sorted(rel for rel in file_hashes(root, ENTRY_SETS[cls]) if rel not in files)
    identical = not different and not absent_machine
    return {"schema": "runtime_compare/v1", "manifest_git_sha": man.get("git_sha"), "environment_class": cls, "built_at_utc": man.get("built_at_utc"),
            "verdict": "IMAGE_CONTAINS_THIS_CHECKOUTS_CLOSURE" if identical else "IMAGE_DIFFERS_FROM_THIS_CHECKOUT", "match": match, "different": different, "absent_on_machine": absent_machine,
            "absent_in_checkout": absent_checkout,
            "reading": "a statement about the image's build inputs, not about what a machine is running now; it says nothing about the economics"}


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
    b.add_argument("--extra", action="append", default=[], help="a shipped file OUTSIDE the application root to list (an entrypoint script, as an absolute path); repeatable")
    v = sub.add_parser("verify", help="re-check the manifest against the files and packages present (exit 0 VERIFIED, 2 otherwise)")
    v.add_argument("--root", default=BOT)
    v.add_argument("--manifest", default=None)
    v.add_argument("--expect", default=None, choices=ENV_CLASSES)
    v.add_argument("--json", action="store_true")
    c = sub.add_parser("compare", help="OFFLINE: compare a manifest saved from a machine with this checkout (exit 0 identical closure, 1 differs)")
    c.add_argument("--manifest", required=True)
    c.add_argument("--root", default=BOT)
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "compare":
        try:
            with open(a.manifest, encoding="utf-8") as fh:
                man = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"cannot read the manifest: {type(exc).__name__}", file=sys.stderr)
            return 2
        if not isinstance(man, dict) or man.get("schema") != SCHEMA:
            print("not a runtime_manifest/v1", file=sys.stderr)
            return 2
        res = compare_manifest(man, a.root)
        if a.json:
            print(json.dumps(res, indent=2, sort_keys=True))
        else:
            print(f"{res['verdict']}: manifest sha {str(res['manifest_git_sha'])[:12]} ({res['environment_class']}, built {res['built_at_utc']}); match {res['match']}, different {len(res['different'])}, absent on the machine {len(res['absent_on_machine'])}")
            for k in ("different", "absent_on_machine"):
                for rel in res[k][:30]:
                    print(f"  {k.upper()}: {rel}")
        return 0 if res["verdict"].startswith("IMAGE_CONTAINS") else 1
    if a.cmd == "build":
        try:
            man = build_manifest(a.root, git_sha=a.git_sha, environment_class=a.environment_class, built_at_utc=a.built_at, config_files=a.config, extra_files=a.extra)
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
