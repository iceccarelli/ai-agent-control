"""Every file a Docker image ships must trigger that image's real-container validation on a pull request.

WHY THIS EXISTS. ``autopilot-image.yml`` runs only when a path in its ``on.pull_request.paths`` filter changes. The filter named ``ms_*.py``, ``exec_evidence.py`` and ``economic_model.py``, but
``Dockerfile.autopilot`` also ships ~70 top-level modules (``md_store.py``, ``md_transport.py``, ``carry_*.py`` ...), ``signals/`` and ~65 tools. A change to ``md_store.py`` (the recorder's hash-chained store,
which the autopilot image imports) therefore reached ``main`` with NO image validation at all, and a green ``carry-verify`` said nothing about the autopilot image (it builds only ``Dockerfile``).

THE CHAIN THIS PINS. ``test_*_image.py`` already prove  import closure of the entry point  ⊆  the Dockerfile's COPY list  (a module the image imports but does not copy crashes the container).
This file proves  the Dockerfile's COPY list  ⊆  the workflow's trigger paths  (a change to anything the image ships runs the real build and smoke). Together: any change to code the image
runs, or to the dependency pins it installs, runs its validation.

WHAT IT DOES NOT PROVE. That the smoke script is a good one (``deploy/*-image-smoke.sh`` and the per-image structural tests own that), that a change OUTSIDE the Docker build context cannot affect the image,
or that CI ran green: only that the validation is TRIGGERED. GitHub evaluates ``paths`` against the changed-file list; a pull request with 300+ changed files runs every workflow regardless.

The path-filter dialect used here is the strict subset ``*`` (any characters except ``/``), ``**`` (any characters) and literals. A pattern using any other GitHub filter syntax (``?``, ``+``, ``[]``, ``!``)
is refused rather than guessed, so a "covered" verdict can never rest on a pattern this file does not understand.
"""
import glob
import json
import os
import re
import shlex
from typing import Dict, List, NamedTuple, Optional, Sequence, Set

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
REPO = os.path.dirname(BOT)
WORKFLOWS = os.path.join(REPO, ".github", "workflows")


class Image(NamedTuple):
    workflow: Optional[str]      # the workflow that builds and runs this image for real; None = no real-container check exists (a tracked gap)
    filtered: bool               # does that workflow carry an `on.pull_request.paths` filter?
    fly: str                     # the Fly app config that deploys it (a change there changes what runs)


#: EVERY Dockerfile in bot/ must be listed (a new image is an explicit decision), with the workflow that validates it.
IMAGES: Dict[str, Image] = {
    "Dockerfile": Image("carry-verify.yml", False, "fly.toml"),                    # job `production-image`; carry-verify has no path filter, it runs on every pull request
    "Dockerfile.autopilot": Image("autopilot-image.yml", True, "fly.autopilot.toml"),
    "Dockerfile.research": Image("research-image.yml", True, "fly.research.toml"),
    "Dockerfile.drill": Image("drill-image.yml", True, "fly.drill.toml"),          # the recorder / monitor image: static closure tests AND a real-container workflow (offline, --network none)
}
#: images with NO real-container workflow, each with the reason. Empty today: add an entry (and the test below keeps it honest) only for an image that genuinely has none.
KNOWN_GAPS: Dict[str, str] = {}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------------------------------------ Dockerfile COPY sources
def copy_sources(dockerfile_text: str) -> List[str]:
    """The build-context sources of every ``COPY``/``ADD`` (multi-stage ``--from`` copies are not context files)."""
    text = re.sub(r"\\\s*\n", " ", dockerfile_text)
    out: List[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*(COPY|ADD)\s+(.*)$", line, re.I)
        if not m:
            continue
        rest = m.group(2).strip()
        tokens = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
        flags = [t for t in tokens if t.startswith("--")]
        if any(f.startswith("--from") for f in flags):
            continue
        args = [t for t in tokens if not t.startswith("--")]
        assert len(args) >= 2, f"a COPY/ADD with no destination: {line!r}"
        for src in args[:-1]:
            assert not re.match(r"^[a-z]+://", src, re.I), f"a remote ADD source is not a context file: {src!r}"
            out.append(src)
    return out


def expand(root: str, sources: Sequence[str]) -> Set[str]:
    """Repo-relative file paths (``bot/...``) that the sources name: a file, a directory (recursively) or a glob."""
    files: Set[str] = set()
    for src in sources:
        hits = glob.glob(os.path.join(root, src), recursive=True) if re.search(r"[*?\[]", src) else [os.path.join(root, src)]
        assert hits, f"a COPY source matches nothing: {src}"
        for hit in hits:
            if os.path.isdir(hit):
                for dirpath, dirs, names in os.walk(hit):
                    dirs[:] = [d for d in dirs if d != "__pycache__"]
                    files.update(os.path.relpath(os.path.join(dirpath, n), os.path.dirname(root)) for n in names if not n.endswith(".pyc"))
            else:
                assert os.path.isfile(hit), f"a COPY source does not exist: {src}"
                files.add(os.path.relpath(hit, os.path.dirname(root)))
    return files


# ------------------------------------------------------------------------------------------------ workflow trigger filter
def pull_request_paths(workflow_text: str) -> Optional[List[str]]:
    """``on.pull_request.paths`` as a list, or None when the pull_request trigger has no path filter. ``paths-ignore`` and any shape this reader does not understand FAIL LOUDLY: an empty parse must
    never read as 'covered'."""
    lines = workflow_text.splitlines()

    def indent(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def live(s: str) -> bool:
        return bool(s.strip()) and not s.lstrip().startswith("#")

    start = next((i for i, s in enumerate(lines) if re.match(r"^on:\s*$", s)), None)
    assert start is not None, "no `on:` block (a flow-style or quoted `on` is not understood: fail rather than guess)"
    pr: Optional[int] = None
    for i in range(start + 1, len(lines)):
        if not live(lines[i]):
            continue
        if indent(lines[i]) == 0:
            break
        if re.match(r"^  pull_request:\s*$", lines[i]):
            pr = i
            break
        assert not re.match(r"^  pull_request:\s*\S", lines[i]), f"an inline pull_request trigger is not understood: {lines[i]!r}"
    assert pr is not None, "the workflow has no pull_request trigger"
    block: List[str] = []
    for s in lines[pr + 1:]:
        if live(s) and indent(s) <= 2:
            break
        if live(s):
            block.append(s)
    assert not any(re.match(r"^\s+paths-ignore:", s) for s in block), "paths-ignore is not understood here: express the filter with `paths`"
    keys = [i for i, s in enumerate(block) if re.match(r"^\s+paths:\s*$", s)]
    assert not any(re.match(r"^\s+paths:\s*\S", s) for s in block), "an inline `paths:` list is not understood: use a block list"
    if not keys:
        return None
    base = indent(block[keys[0]])
    out: List[str] = []
    for s in block[keys[0] + 1:]:
        if indent(s) <= base:
            break
        m = re.match(r"^\s+-\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))\s*(?:#.*)?$", s)
        assert m, f"a `paths` entry this reader does not understand: {s!r}"
        out.append(next(g for g in m.groups() if g is not None))
    assert out, "an empty `paths:` list"
    return out


def pattern_regex(pattern: str):
    assert not pattern.startswith("!"), f"a negated filter pattern is not understood: {pattern!r}"
    assert not re.search(r"[?+\[\]\\]", pattern), f"filter syntax outside the strict subset (`*`, `**`, literals): {pattern!r}"
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out)


def matches(pattern: str, path: str) -> bool:
    return pattern_regex(pattern).fullmatch(path) is not None


def uncovered(files: Sequence[str], patterns: Sequence[str]) -> List[str]:
    regexes = [pattern_regex(p) for p in patterns]
    return sorted(f for f in files if not any(r.fullmatch(f) for r in regexes))


# ------------------------------------------------------------------------------------------------ what every image's validation must be triggered by
def required_trigger_files(dockerfile: str, image: Image, workflow: str) -> Set[str]:
    sources = copy_sources(_read(os.path.join(BOT, dockerfile)))
    files = expand(BOT, sources)
    files.add(f"bot/{dockerfile}")                                              # the recipe itself
    fly = os.path.join(BOT, image.fly)
    if os.path.isfile(fly):
        files.add(f"bot/{image.fly}")                                           # what deploys it
    if os.path.isfile(os.path.join(BOT, ".dockerignore")):
        files.add("bot/.dockerignore")                                          # it decides which COPY sources reach the build
    files.add(f".github/workflows/{workflow}")                                  # a change to the check itself must run the check
    return files


# ------------------------------------------------------------------------------------------------ the pins
def test_every_dockerfile_is_mapped_to_the_workflow_that_validates_it():
    found = {os.path.basename(p) for p in glob.glob(os.path.join(BOT, "Dockerfile*"))}
    assert found == set(IMAGES), f"a Dockerfile without an explicit validation decision (or a stale entry): found {sorted(found)}, mapped {sorted(IMAGES)}"
    for name, img in IMAGES.items():
        if img.workflow is None:
            assert name in KNOWN_GAPS, f"{name} has no validating workflow and is not a documented gap"
        else:
            assert os.path.isfile(os.path.join(WORKFLOWS, img.workflow)), f"{name}: workflow {img.workflow} does not exist"
    assert set(KNOWN_GAPS) <= {n for n, i in IMAGES.items() if i.workflow is None}, "a gap is listed for an image that now has a workflow: delete the gap entry"


def test_a_gap_stays_a_gap_until_a_workflow_that_builds_the_image_is_mapped():
    """If someone adds a workflow that builds a gap's Dockerfile, this fails until the map is updated and the gap entry deleted (so the guard cannot rot silently)."""
    for name in KNOWN_GAPS:
        for wf in sorted(glob.glob(os.path.join(WORKFLOWS, "*.yml"))):
            body = _read(wf)
            building = re.search(r"docker\s+build[^\n]*-f\s+\S*" + re.escape(name) + r"\b", body) or re.search(r"file:\s*\S*" + re.escape(name) + r"\b", body)
            assert not building, f"{os.path.basename(wf)} builds {name}: map it in IMAGES and delete its KNOWN_GAPS entry"


def test_a_filtered_image_workflow_is_triggered_by_every_file_its_image_ships():
    problems = []
    for name, img in IMAGES.items():
        if img.workflow is None or not img.filtered:
            continue
        patterns = pull_request_paths(_read(os.path.join(WORKFLOWS, img.workflow)))
        assert patterns is not None, f"{img.workflow} is mapped as filtered but has no `paths` filter: set filtered=False in IMAGES"
        missing = uncovered(sorted(required_trigger_files(name, img, img.workflow)), patterns)
        if missing:
            problems.append(f"{img.workflow} ({name}) is not triggered by {len(missing)} shipped file(s), e.g. {missing[:6]}")
    assert not problems, "; ".join(problems) + ". Widen the workflow's `on.pull_request.paths` so a change to anything the image ships runs its real-container check."


def test_an_unfiltered_image_workflow_really_runs_on_every_pull_request_and_builds_its_image():
    for name, img in IMAGES.items():
        if img.workflow is None or img.filtered:
            continue
        text = _read(os.path.join(WORKFLOWS, img.workflow))
        assert pull_request_paths(text) is None, f"{img.workflow} is mapped as unfiltered but now carries a paths filter: set filtered=True so its coverage is checked"
        assert re.search(r"^  pull_request:\s*$", text, re.M), f"{img.workflow} has no pull_request trigger"
        assert re.search(r"docker\s+build[^\n]*" + re.escape(name) + r"|bot/" + re.escape(name), text), f"{img.workflow} does not build {name}"


def test_an_image_workflow_runs_its_smoke_script_against_a_script_that_exists():
    for name, img in IMAGES.items():
        if img.workflow is None or not img.filtered:
            continue
        text = _read(os.path.join(WORKFLOWS, img.workflow))
        m = re.search(r"sh\s+(deploy/[\w.-]+\.sh)\b", text)
        assert m, f"{img.workflow} does not run a deploy/*.sh smoke script"
        assert os.path.isfile(os.path.join(BOT, m.group(1))), f"{img.workflow} runs {m.group(1)} which does not exist"
        assert uncovered([f"bot/{m.group(1)}"], pull_request_paths(text) or []) == [], f"a change to {m.group(1)} does not trigger {img.workflow}"


# ------------------------------------------------------------------------------------------------ the guard's own machinery, with mutations
def test_the_path_filter_dialect_matches_like_github_for_the_subset_it_accepts():
    assert matches("bot/*.py", "bot/md_store.py") and not matches("bot/*.py", "bot/tools/ms_x.py")
    assert matches("bot/tools/ms_*.py", "bot/tools/ms_edge_gate.py") and not matches("bot/tools/ms_*.py", "bot/tools/mx_edge.py")
    assert matches("bot/deploy/**", "bot/deploy/autopilot-entrypoint.sh") and matches("bot/deploy/**", "bot/deploy/sub/x.sh")
    assert matches("bot/signals/**", "bot/signals/funding_carry_fade_v1.py") and not matches("bot/signals/**", "bot/tools/x.py")
    assert matches("bot/deploy/research-*", "bot/deploy/research-entrypoint.sh") and not matches("bot/deploy/research-*", "bot/deploy/autopilot-entrypoint.sh")
    assert matches("bot/Dockerfile.autopilot", "bot/Dockerfile.autopilot") and not matches("bot/Dockerfile.autopilot", "bot/Dockerfile.autopilot.bak")
    assert not matches("bot/*.py", "bot/md_store.pyc") and not matches("bot/*.py", "botXmd_store.py")      # `.` and `/` are literals


def test_filter_syntax_outside_the_strict_subset_is_refused_not_guessed():
    for bad in ("!bot/x.py", "bot/tools/ms_?.py", "bot/tools/ms_+.py", "bot/tools/[ms]_a.py", "bot\\tools\\x.py"):
        try:
            pattern_regex(bad)
        except AssertionError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_a_filter_the_reader_does_not_understand_fails_loudly_instead_of_reading_as_empty():
    base = "name: x\non:\n  pull_request:\n    paths:\n      - \"bot/a.py\"\n  workflow_dispatch:\n"
    assert pull_request_paths(base) == ["bot/a.py"]
    assert pull_request_paths("name: x\non:\n  pull_request:\n  push:\n    branches: [main]\n") is None
    for broken in ("name: x\non:\n  pull_request:\n    paths: [\"bot/a.py\"]\n",                    # inline list
                   "name: x\non:\n  pull_request:\n    paths-ignore:\n      - \"docs/**\"\n",        # paths-ignore
                   "name: x\non:\n  push:\n    branches: [main]\n",                                   # no pull_request trigger
                   "name: x\non: [pull_request]\n",                                                  # flow style
                   "name: x\non:\n  pull_request:\n    paths:\n      - bot/a.py extra\n",            # unparseable entry
                   "name: x\non:\n  pull_request:\n    paths:\n"):                                   # empty list
        try:
            pull_request_paths(broken)
        except AssertionError:
            continue
        raise AssertionError(f"read as understood: {broken!r}")


def test_copy_parsing_handles_continuations_flags_json_form_and_skips_stage_copies():
    df = ("FROM x AS base\nCOPY a.py b.py \\\n     c.py ./\nCOPY --chown=bot:bot signals/ ./signals/\nCOPY --from=base /x /y\n"
          "COPY [\"d.py\", \"e e.py\", \"./\"]\n# COPY commented.py ./\nRUN echo COPY not.py ./\n")
    assert copy_sources(df) == ["a.py", "b.py", "c.py", "signals/", "d.py", "e e.py"]


def test_a_new_copy_the_filter_does_not_name_is_reported_with_its_file():
    """Mutation: the pre-fix autopilot filter against the real Dockerfile reports the md_* modules, signals, carry_* and the rest."""
    old_filter = ["bot/Dockerfile.autopilot", "bot/fly.autopilot.toml", "bot/deploy/**", "bot/tools/ms_*.py", "bot/tools/exec_evidence.py", "bot/economic_model.py", "bot/requirements.txt",
                  "bot/constraints.txt", ".github/workflows/autopilot-image.yml"]
    missing = uncovered(sorted(required_trigger_files("Dockerfile.autopilot", IMAGES["Dockerfile.autopilot"], "autopilot-image.yml")), old_filter)
    assert "bot/md_store.py" in missing and "bot/md_transport.py" in missing and "bot/md_wire.py" in missing, missing
    assert any(m.startswith("bot/signals/") for m in missing) and "bot/carry_engine.py" in missing and "bot/tools/market_history.py" in missing
    assert "bot/economic_model.py" not in missing and "bot/tools/ms_edge_gate.py" not in missing


def test_the_required_set_for_each_image_contains_what_it_must():
    req = required_trigger_files("Dockerfile.autopilot", IMAGES["Dockerfile.autopilot"], "autopilot-image.yml")
    for must in ("bot/Dockerfile.autopilot", "bot/fly.autopilot.toml", "bot/.dockerignore", ".github/workflows/autopilot-image.yml", "bot/requirements.txt", "bot/constraints.txt",
                 "bot/deploy/autopilot-entrypoint.sh", "bot/md_store.py", "bot/tools/ms_testnet_autopilot.py"):
        assert must in req, must
    assert len(req) > 100
