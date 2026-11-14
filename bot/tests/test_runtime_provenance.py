"""Round 22: an image states what it is and the runtime re-checks it. The owner's recorder image was ~49 closure files behind `main` and nothing in the image could say so: provenance must not depend on
human memory. These tests build a manifest from the REAL repository closure, then break it in every way a deployment can go wrong and require a fail-closed answer each time."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]

import image_manifest as IM  # noqa: E402
import runtime_provenance as RP  # noqa: E402

SHA = "f86e8db56386cb8b927c26741d2455fdff2cb0c4"
PKGS = ["numpy==2.4.6", "requests==2.34.2"]


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A copy of ONLY the recorder closure (the files an image would ship), so a test can change one without touching the repository."""
    root = tmp_path_factory.mktemp("app")
    for rel in IM.closure(BOT, RP.ENTRY_SETS["READ_ONLY_RECORDER"]):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(os.path.join(BOT, rel), dst)
    return str(root)


@pytest.fixture
def app(tree, tmp_path):
    """A fresh copy of the closure tree per test (tests mutate it) with a built manifest."""
    root = tmp_path / "app"
    shutil.copytree(tree, root)
    man = RP.build_manifest(str(root), git_sha=SHA, environment_class="READ_ONLY_RECORDER", built_at_utc="2026-11-14T12:00:00Z", packages=PKGS)
    (root / RP.MANIFEST_NAME).write_text(json.dumps(man))
    return str(root)


def verify(root, **kw):
    kw.setdefault("packages", PKGS)
    kw.setdefault("env", {})
    return RP.verify_runtime(root, **kw)


def edit_manifest(root, fn):
    p = os.path.join(root, RP.MANIFEST_NAME)
    man = json.load(open(p))
    fn(man)
    open(p, "w").write(json.dumps(man))


def test_a_clean_build_is_verified_and_answers_the_five_questions(app):
    res = verify(app, expect="READ_ONLY_RECORDER", env={"FLY_APP_NAME": "carry-drill", "FLY_IMAGE_REF": "registry.fly.io/carry-drill:deployment-01ABC", "FLY_MACHINE_VERSION": "22"})
    assert res["status"] == "VERIFIED", res
    assert res["git_sha"] == SHA and res["environment_class"] == "READ_ONLY_RECORDER"                                  # 1. what source commit
    assert res["image_ref"] == "registry.fly.io/carry-drill:deployment-01ABC" and res["machine_version"] == "22"         # 2. the platform's own reference (the digest is bound out of band)
    assert len(res["recorder_fingerprint"]) == 64                                                                       # 3. what recorder implementation
    assert len(res["package_fingerprint"]) == 64 and res["closure_fingerprint"]                                         # 4. what dependency closure
    assert res["app"] == "carry-drill" and res["config_fingerprint_verifiable_at_runtime"] is False                    # 5. approved for this runtime
    man = json.load(open(os.path.join(app, RP.MANIFEST_NAME)))
    assert len(man["files"]) >= 80 and "tools/market_recorder.py" in man["files"] and man["built_at_utc"] == "2026-11-14T12:00:00Z" and man["python_minor"] == RP.python_minor()


def test_the_recorder_fingerprint_is_the_real_repository_closure_fingerprint(app):
    expected = RP._digest(sorted(RP.file_hashes(BOT, RP.ENTRY_SETS["READ_ONLY_RECORDER"]).items()))
    assert json.load(open(os.path.join(app, RP.MANIFEST_NAME)))["recorder_fingerprint"] == expected


@pytest.mark.parametrize("bad", ["unknown", "", "0" * 40, "F86E8DB56386CB8B927C26741D2455FDFF2CB0C4", SHA[:12], SHA + "0", "g" * 40])
def test_a_build_that_cannot_say_what_it_was_built_from_produces_no_manifest(tree, bad):
    with pytest.raises(ValueError):
        RP.build_manifest(tree, git_sha=bad, environment_class="READ_ONLY_RECORDER", packages=PKGS)


def test_an_unknown_environment_class_is_refused_at_build(tree):
    with pytest.raises(ValueError):
        RP.build_manifest(tree, git_sha=SHA, environment_class="PRODUCTION_LIVE", packages=PKGS)


def test_a_missing_manifest_is_MISSING_never_a_pass(tmp_path):
    res = verify(str(tmp_path))
    assert res["status"] == "MISSING" and "cannot say what it was built from" in res["detail"]


@pytest.mark.parametrize("junk", ["not json", "[]", "{}", '{"schema": "other/v9", "files": {"a": "b"}}', '{"schema": "runtime_manifest/v1", "files": {}}'])
def test_a_malformed_manifest_is_MISMATCH(app, junk):
    open(os.path.join(app, RP.MANIFEST_NAME), "w").write(junk)
    assert verify(app)["status"] == "MISMATCH"


def test_an_oversized_manifest_is_refused(app):
    with open(os.path.join(app, RP.MANIFEST_NAME), "w") as fh:
        fh.write(" " * (RP.MAX_MANIFEST_BYTES + 1))
    res = verify(app)
    assert res["status"] == "MISMATCH" and "limit" in res["detail"]


def test_an_unusable_sha_or_class_in_the_manifest_is_MISMATCH(app):
    edit_manifest(app, lambda m: m.update(git_sha="unknown"))
    assert verify(app)["status"] == "MISMATCH" and "not a 40-hex" in verify(app)["detail"]
    edit_manifest(app, lambda m: m.update(git_sha=SHA, environment_class="LIVE"))
    assert verify(app)["status"] == "MISMATCH"


def test_a_changed_or_missing_closure_file_is_MISMATCH_and_named(app):
    target = os.path.join(app, "md_store.py")
    with open(target, "a") as fh:
        fh.write("\n# edited after the build\n")
    res = verify(app)
    assert res["status"] == "MISMATCH" and "md_store.py (changed)" in res["differing"], res
    os.remove(os.path.join(app, "md_store.py"))
    assert "md_store.py (missing)" in verify(app)["differing"]


def test_a_file_under_the_root_that_the_manifest_does_not_list_is_FILE_EXTRA(app):
    """A file the manifest does not list (dropped by sftp, or an import the build never saw) is refused, whether or not an entry point imports it."""
    def drop(m):
        m["files"].pop("md_seal.py") if "md_seal.py" in m["files"] else m["files"].pop(sorted(m["files"])[-1])
        m["files_fingerprint"] = RP._digest(sorted(m["files"].items()))
    edit_manifest(app, drop)
    res = verify(app)
    assert res["status"] == "MISMATCH" and res["code"] == "FILE_EXTRA" and any("(unlisted)" in d for d in res["differing"]), res
    assert "not in the manifest" in res["detail"]


def test_a_tampered_manifest_whose_own_fingerprint_disagrees_is_MISMATCH(app):
    edit_manifest(app, lambda m: m.update(closure_fingerprint="0" * 64))
    res = verify(app)
    assert res["status"] == "MISMATCH" and res["code"] == "FINGERPRINT_INCONSISTENT" and "closure fingerprint" in res["detail"]


def test_a_different_python_or_dependency_set_is_MISMATCH(app):
    edit_manifest(app, lambda m: m.update(python_minor="2.7"))
    assert verify(app)["status"] == "MISMATCH" and "Python" in verify(app)["detail"]
    edit_manifest(app, lambda m: m.update(python_minor=RP.python_minor()))
    res = verify(app, packages=PKGS + ["evil==1.0"])
    assert res["status"] == "MISMATCH" and "dependency" in res["detail"]


def test_an_image_is_approved_only_for_the_app_of_its_class(app):
    assert verify(app, env={"FLY_APP_NAME": "carry-drill"})["status"] == "VERIFIED"
    wrong = verify(app, env={"FLY_APP_NAME": "carry-autopilot"})
    assert wrong["status"] == "MISMATCH" and "TESTNET_EXECUTOR" in wrong["detail"], "a recorder image on the executor app is not approved"
    assert verify(app, env={"FLY_APP_NAME": "some-other-app"})["status"] == "MISMATCH"
    assert verify(app, expect="TESTNET_EXECUTOR")["status"] == "MISMATCH"


def test_the_executor_class_is_built_from_the_executor_closure(tmp_path):
    root = str(tmp_path / "app")                       # what the autopilot image ships (assembled from its COPY lines), not the whole repository with its tests
    os.makedirs(root)
    _assemble("Dockerfile.autopilot", root)
    man = RP.build_manifest(root, git_sha=SHA, environment_class="TESTNET_EXECUTOR", packages=PKGS)
    assert man["entry_points"] == ["tools/ms_testnet_autopilot.py"] and "tools/ms_testnet_autopilot.py" in man["files"] and "economic_model.py" in man["files"]
    assert man["recorder_fingerprint"] is None, "the executor image holds no recorder: no recorder fingerprint is claimed for it"


def test_the_cli_builds_and_verifies_and_refuses(tree, tmp_path, capsys):
    root = str(tmp_path / "cliapp")
    shutil.copytree(tree, root)
    assert RP.main(["build", "--git-sha", SHA, "--environment-class", "READ_ONLY_RECORDER", "--root", root]) == 0
    assert os.path.isfile(os.path.join(root, RP.MANIFEST_NAME))
    # the installed packages of THIS interpreter were fingerprinted at build, so verify here (no package override) must agree
    assert RP.main(["verify", "--root", root, "--expect", "READ_ONLY_RECORDER"]) == 0
    assert RP.main(["verify", "--root", root, "--expect", "TESTNET_EXECUTOR"]) == 2
    assert RP.main(["build", "--git-sha", "unknown", "--environment-class", "READ_ONLY_RECORDER", "--root", root, "--out", str(tmp_path / "x.json")]) == 2
    assert not os.path.exists(tmp_path / "x.json")
    assert RP.main(["verify", "--root", str(tmp_path / "nothing")]) == 2
    capsys.readouterr()


def test_verify_never_raises_whatever_it_is_given(tmp_path):
    for root in (str(tmp_path / "missing"), "/", str(tmp_path)):
        res = RP.verify_runtime(root, env={})
        assert res["status"] in RP.STATUSES and res["status"] != "VERIFIED"
    d = tmp_path / "dirmanifest"
    d.mkdir()
    assert RP.verify_runtime(str(tmp_path), manifest_path=str(d), env={})["status"] in ("MISSING", "MISMATCH")


# ---------------------------------------------------------------------------------------- the Dockerfiles: the build step would succeed, and the runtime gate is in the entrypoint
def _copy_pairs(dockerfile):
    """(sources, dest) for every COPY line of a Dockerfile, with backslash continuations joined."""
    text = open(os.path.join(BOT, dockerfile), encoding="utf-8").read().replace("\\\n", " ")
    out = []
    for ln in text.splitlines():
        parts = ln.split()
        if parts and parts[0] == "COPY" and not any(p.startswith("--") for p in parts[1:2]):
            out.append((parts[1:-1], parts[-1]))
    return out


def _assemble(dockerfile, root):
    """Lay out /app the way the image's COPY lines do (only what lands under /app), without Docker."""
    for srcs, dest in _copy_pairs(dockerfile):
        if not dest.startswith("./") and not dest.startswith("/app"):
            continue
        for src in srcs:
            s = os.path.join(BOT, src)
            d = os.path.join(root, dest[2:] if dest.startswith("./") else dest[5:])
            if src.endswith("/"):
                shutil.copytree(s, os.path.join(d), dirs_exist_ok=True)
            else:
                os.makedirs(d if dest.endswith("/") else os.path.dirname(d) or root, exist_ok=True)
                shutil.copy2(s, d if not dest.endswith("/") else os.path.join(d, os.path.basename(src)))


@pytest.mark.parametrize("dockerfile,env_class,entrypoint", [("Dockerfile.drill", "READ_ONLY_RECORDER", "deploy/drill-entrypoint.sh"), ("Dockerfile.autopilot", "TESTNET_EXECUTOR", "deploy/autopilot-entrypoint.sh")])
def test_the_dockerfile_build_step_would_produce_a_manifest_that_verifies_and_the_entrypoint_gates_on_it(dockerfile, env_class, entrypoint, tmp_path, monkeypatch):
    if env_class == "TESTNET_EXECUTOR":
        monkeypatch.setenv("FLY_APP_NAME", "carry-autopilot")                 # Fly sets it on the real machine; an executor image refuses to verify without it
    text = open(os.path.join(BOT, dockerfile), encoding="utf-8").read()
    # build-time: the commit is a REQUIRED build argument (no default: an unset one makes the build REFUSE), the manifest is built for this image's class, and the configuration is fingerprinted
    assert "\nARG BUILD_COMMIT\n" in text and "ARG BUILD_COMMIT=" not in text
    assert f"--environment-class {env_class}" in text and '--git-sha "$BUILD_COMMIT"' in text and f"--config /tmp/provenance/{dockerfile}" in text
    assert text.index("ARG BUILD_COMMIT") > text.index("tools/runtime_provenance.py ") or "tools/runtime_provenance.py tools/image_manifest.py" in text
    # the files the RUN step needs are copied BEFORE it, and the image really holds the closure: assemble /app from the COPY lines and run the same build + the entrypoint's verify
    root = str(tmp_path / "app")
    os.makedirs(root)
    _assemble(dockerfile, root)
    assert os.path.isfile(os.path.join(root, "tools", "runtime_provenance.py")) and os.path.isfile(os.path.join(root, "tools", "image_manifest.py"))
    assert RP.main(["build", "--git-sha", SHA, "--environment-class", env_class, "--root", root]) == 0, "the Dockerfile's RUN step cannot succeed: a closure file is not COPYed"
    assert RP.main(["verify", "--root", root, "--expect", env_class]) == 0
    other = "TESTNET_EXECUTOR" if env_class == "READ_ONLY_RECORDER" else "READ_ONLY_RECORDER"
    assert RP.main(["verify", "--root", root, "--expect", other]) == 2
    # run-time: the entrypoint refuses (exit 78) before it does anything else
    ep = open(os.path.join(BOT, entrypoint), encoding="utf-8").read()
    body = [ln for ln in ep.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert body[0] == "set -eu"
    assert body[1].startswith("python3 /app/tools/runtime_provenance.py verify --root /app --expect " + env_class) and "exit 78" in body[1]
    assert "setpriv" in body[-1], "the gate comes before the privilege drop and the exec"


def test_the_smoke_scripts_require_the_build_to_refuse_without_a_commit_and_to_refuse_a_tampered_image():
    for script, tamper in (("deploy/drill-image-smoke.sh", "md_store.py"), ("deploy/autopilot-image-smoke.sh", "ms_edge_gate.py")):
        t = open(os.path.join(BOT, script), encoding="utf-8").read()
        assert "-noprov" in t and "built WITHOUT BUILD_COMMIT" in t, script
        assert '--build-arg BUILD_COMMIT="$(git rev-parse HEAD)"' in t, script
        assert "runtime_provenance.py verify" in t and tamper in t and "-eq 78" in t, script


# ---------------------------------------------------------------------------------------- Round 22 review: the manifest covers EVERYTHING the image ships, and a missing closure file refuses the build
EXTRAS = ["tools/drill.py", "tools/close_testnet_short.py", "tools/evidence_reconcile.py", "tools/recon_summary.py", "signals/__init__.py"]


@pytest.fixture
def full_app(app):
    """The recorder closure PLUS shipped files the entry points do not import: two order-capable scripts the supervisor stack spawns by path, report tools and a signal package."""
    for rel in EXTRAS:
        src = os.path.join(BOT, rel)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(os.path.join(app, rel)), exist_ok=True)
            shutil.copy2(src, os.path.join(app, rel))
    man = RP.build_manifest(app, git_sha=SHA, environment_class="READ_ONLY_RECORDER", packages=PKGS)
    open(os.path.join(app, RP.MANIFEST_NAME), "w").write(json.dumps(man))
    return app


def test_the_manifest_lists_every_shipped_file_not_only_the_import_closure(full_app):
    man = json.load(open(os.path.join(full_app, RP.MANIFEST_NAME)))
    for rel in EXTRAS:
        if os.path.isfile(os.path.join(BOT, rel)):
            assert rel in man["files"], rel
    assert set(man["files"]) == set(RP.shipped_files(full_app))
    assert verify(full_app)["status"] == "VERIFIED"


@pytest.mark.parametrize("rel", [r for r in EXTRAS if os.path.isfile(os.path.join(BOT, r))])
def test_a_changed_file_that_no_entry_point_imports_is_still_refused(full_app, rel):
    """REPRODUCED (Round 22 review): editing tools/drill.py or tools/close_testnet_short.py (order-capable, path-spawned) left the manifest VERIFIED."""
    with open(os.path.join(full_app, rel), "a") as fh:
        fh.write("\n# edited after the build\n")
    res = verify(full_app)
    assert res["status"] == "MISMATCH" and res["code"] == "FILE_CHANGED" and f"{rel} (changed)" in res["differing"], res


def test_a_file_dropped_into_the_image_after_the_build_is_refused(full_app):
    with open(os.path.join(full_app, "tools", "dropped_by_sftp.py"), "w") as fh:
        fh.write("print('hi')\n")
    res = verify(full_app)
    assert res["status"] == "MISMATCH" and res["code"] == "FILE_EXTRA" and "tools/dropped_by_sftp.py (unlisted)" in res["differing"]
    os.remove(os.path.join(full_app, "tools", "dropped_by_sftp.py"))
    assert verify(full_app)["status"] == "VERIFIED"


def test_a_deleted_shipped_file_is_FILE_MISSING(full_app):
    os.remove(os.path.join(full_app, "tools", "recon_summary.py")) if os.path.isfile(os.path.join(full_app, "tools", "recon_summary.py")) else os.remove(os.path.join(full_app, "tools", "drill.py"))
    res = verify(full_app)
    assert res["status"] == "MISMATCH" and res["code"] == "FILE_MISSING"


def test_a_closure_file_absent_at_BUILD_time_refuses_the_build_instead_of_being_dropped(tree, tmp_path):
    """REPRODUCED (Round 22 review): image_manifest.closure() skips a module whose file is absent, so an image missing md_wire.py built a manifest listing one file fewer and verified. The owner found exactly this by hand
    ('current recorder dependencies ABSENT on the machine')."""
    root = str(tmp_path / "app")
    shutil.copytree(tree, root)
    os.remove(os.path.join(root, "md_wire.py"))
    with pytest.raises(ValueError, match="resolve to nothing") as ei:
        RP.build_manifest(root, git_sha=SHA, environment_class="READ_ONLY_RECORDER", packages=PKGS)
    assert "md_wire" in str(ei.value)
    assert RP.main(["build", "--git-sha", SHA, "--environment-class", "READ_ONLY_RECORDER", "--root", root]) == 2


def test_the_one_pinned_unresolved_import_is_the_frozen_signals_lazy_research_path(tree):
    gaps = RP.unresolved_imports(tree, RP.shipped_files(tree))
    assert gaps == [], gaps
    assert RP.UNRESOLVED_ALLOWED == {"sweep_geometry"}


def test_a_closed_vocabulary_of_codes_covers_every_failure(app):
    seen = set()
    seen.add(verify(app)["code"])
    seen.add(verify(os.path.join(app, "nowhere"))["code"])
    edit_manifest(app, lambda m: m.update(git_sha="unknown"))
    seen.add(verify(app)["code"])
    edit_manifest(app, lambda m: m.update(git_sha=SHA, python_minor="2.7"))
    seen.add(verify(app)["code"])
    seen.add(verify(app, expect="TESTNET_EXECUTOR")["code"])
    assert seen <= set(RP.CODES) and {"OK", "MANIFEST_MISSING", "SHA_UNKNOWN", "PYTHON_MISMATCH", "CLASS_MISMATCH"} <= seen, seen


def test_an_executor_image_runs_only_in_its_fly_app_and_a_missing_app_name_is_refused(tmp_path):
    """The class/app check is a DEPLOY-MISTAKE detector (the environment is set by whoever controls the machine). For an executor image the app name is REQUIRED."""
    root = str(tmp_path / "app")
    os.makedirs(root)
    _assemble("Dockerfile.autopilot", root)
    extras = [os.path.join(BOT, "deploy", "autopilot-entrypoint.sh"), os.path.join(BOT, "deploy", "autopilot-ctl.sh")]
    man = RP.build_manifest(root, git_sha=SHA, environment_class="TESTNET_EXECUTOR", packages=PKGS, extra_files=extras)
    open(os.path.join(root, RP.MANIFEST_NAME), "w").write(json.dumps(man))
    assert verify(root, expect="TESTNET_EXECUTOR", env={})["code"] == "APP_NOT_SET"
    assert verify(root, expect="TESTNET_EXECUTOR", env={"FLY_APP_NAME": "carry-autopilot"})["status"] == "VERIFIED"
    assert verify(root, env={"FLY_APP_NAME": "carry-drill"})["code"] == "APP_MISMATCH"
    assert any(k.startswith("@") and k.endswith("autopilot-entrypoint.sh") for k in man["files"]), "the entrypoint scripts are part of the image's behaviour and are listed"
    # an entrypoint script edited after the build is refused
    with open(extras[0], "rb") as fh:
        original = fh.read()
    mutated = tmp_path / "mutated-entrypoint.sh"
    mutated.write_bytes(original + b"\n# edited\n")
    man["files"]["@" + str(mutated)] = man["files"].pop("@" + extras[0])
    man["files_fingerprint"] = RP._digest(sorted(man["files"].items()))
    open(os.path.join(root, RP.MANIFEST_NAME), "w").write(json.dumps(man))
    res = verify(root, expect="TESTNET_EXECUTOR", env={"FLY_APP_NAME": "carry-autopilot"})
    assert res["status"] == "MISMATCH" and res["code"] == "FILE_CHANGED"


def test_the_default_environment_is_only_the_three_named_identity_variables(monkeypatch, tmp_path):
    """Round 22: with no mapping handed in the check reads the process environment, and must copy only the named non-secret platform identity (never a credential into a local that could be logged)."""
    assert RP.IDENTITY_ENV == ("FLY_APP_NAME", "FLY_IMAGE_REF", "FLY_MACHINE_VERSION")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "should-never-be-copied")
    monkeypatch.setenv("FLY_APP_NAME", "carry-drill")
    res = RP.verify_runtime(str(tmp_path))                  # no manifest: MISSING, and the result carries only identity fields
    assert res["status"] == "MISSING" and res["app"] == "carry-drill"
    assert "should-never-be-copied" not in json.dumps(res)


def test_compare_says_what_a_machines_manifest_differs_in_from_this_checkout(tmp_path):
    """The owner saves `cat /app/RUNTIME_MANIFEST.json` from the machine and compares it offline with a checkout: DIFFERENT files and closure files the image does not contain are named."""
    root = tmp_path / "img"
    root.mkdir()
    _assemble("Dockerfile.drill", str(root))
    man = RP.build_manifest(str(root), git_sha="a" * 40, environment_class="READ_ONLY_RECORDER")
    same = RP.compare_manifest(man, str(root))
    assert same["verdict"] == "IMAGE_CONTAINS_THIS_CHECKOUTS_CLOSURE" and same["different"] == [] and same["absent_on_machine"] == [] and same["match"] > 50
    changed = next(r for r in man["files"] if r.endswith("md_store.py"))
    with open(os.path.join(str(root), changed), "a", encoding="utf-8") as fh:
        fh.write("\n# edited after the image was built\n")
    d = RP.compare_manifest(man, str(root))
    assert d["verdict"] == "IMAGE_DIFFERS_FROM_THIS_CHECKOUT" and changed in d["different"]
    # a closure file of the checkout that the old image never shipped
    older = copy.deepcopy(man)
    del older["files"][changed]
    assert changed in RP.compare_manifest(older, str(root))["absent_on_machine"]
    f = tmp_path / "m.json"
    f.write_text(json.dumps(man))
    assert RP.main(["compare", "--manifest", str(f), "--root", str(root)]) == 1
    assert RP.main(["compare", "--manifest", str(tmp_path / "nope.json")]) == 2
