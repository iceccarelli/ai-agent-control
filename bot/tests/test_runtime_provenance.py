"""Round 22: an image states what it is and the runtime re-checks it. The owner's recorder image was ~49 closure files behind `main` and nothing in the image could say so: provenance must not depend on
human memory. These tests build a manifest from the REAL repository closure, then break it in every way a deployment can go wrong and require a fail-closed answer each time."""
from __future__ import annotations

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


def test_a_file_the_entry_points_now_import_but_the_manifest_does_not_list_is_MISMATCH(app):
    def drop(m):
        m["files"].pop("md_seal.py", None) if "md_seal.py" in m["files"] else m["files"].pop(sorted(m["files"])[-1])
        m["closure_fingerprint"] = RP._digest(sorted(m["files"].items()))
    edit_manifest(app, drop)
    res = verify(app)
    assert res["status"] == "MISMATCH" and "not in the manifest" in res["detail"] and any("(unlisted)" in d for d in res["differing"]), res


def test_a_tampered_manifest_whose_own_fingerprint_disagrees_is_MISMATCH(app):
    edit_manifest(app, lambda m: m.update(closure_fingerprint="0" * 64))
    res = verify(app)
    assert res["status"] == "MISMATCH" and "closure fingerprint" in res["detail"]


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
    man = RP.build_manifest(BOT, git_sha=SHA, environment_class="TESTNET_EXECUTOR", packages=PKGS)
    assert man["entry_points"] == ["tools/ms_testnet_autopilot.py"] and "tools/ms_testnet_autopilot.py" in man["files"] and "economic_model.py" in man["files"]
    assert man["recorder_fingerprint"] is not None and man["closure_fingerprint"] != man["recorder_fingerprint"]


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
def test_the_dockerfile_build_step_would_produce_a_manifest_that_verifies_and_the_entrypoint_gates_on_it(dockerfile, env_class, entrypoint, tmp_path):
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
