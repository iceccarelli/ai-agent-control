"""Slice 70 — eight bars, ceiling 3, zero setups, and a guard that was gone.

WHAT THIS SLICE MEASURES
========================
`ceiling 3, setups 0 of 8, fills 0`. That is the finding. No second sentence is
entitled to more.

WHAT THIS SLICE FIXES, AND WHAT IT FOUND WHILE FIXING IT
========================================================
Two of my own tests failed at baseline, both for the same underlying reason,
and repairing them turned up a third and worse instance of the class.

1.  `test_the_open_bar_is_absent` (slice 69) carried a clock-derived assertion
    AND a hard-coded restatement that `2026-08-17` was absent. 08-17 closed.
    **A literal added beside a derived check is not corroboration; it is the
    only line that can go stale, and it will.** EDGE.md §53d.

2.  `test_this_slices_counts_came_from_files_not_the_manifest` (slice 69)
    asserted that a LIVE corpus count *equals* a count in a DATED artefact.
    True until a bar arrived. Slice 66's guard cannot see it — the right-hand
    side is an artefact lookup, not an integer literal — so the family grows
    its third member here: `TestNoTestEqualsALiveCountAgainstADatedArtefact`.

3.  While writing that guard, the sweep for *which* guards are actually
    running found that **`test_the_scoring_was_not_re_tuned_since_sliceNN` has
    not existed in this tree since slice 66** — and slice 67's verdict listed
    it in the evidence table as though it had run. Four slices of forward
    shadow shipped without the AST comparison that is supposed to make silent
    re-tuning of the scoring impossible.

    The property held: `TestTheScoringGuardCameBack` re-runs the comparison
    across 66→67→68→69→70 and `build`'s body is AST-identical throughout. That
    is verified here, not assumed. **The guard's absence is the defect; the
    scoring was not touched.**

    The fix is not "remember to carry the guard". It is
    `TestAVerdictMayNotCiteATestThatDoesNotExist`, which fails if any
    `test_*` name written into a verdict is absent from the test tree.

**A guard family grows one member per lesson.** It is now four:
live-absolute (66), foreign-slice constant (69), live-vs-dated (70), and
cited-but-absent (70).
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import corpus_prefix as cp  # noqa: E402
import market_data as md  # noqa: E402
import project_status as ps  # noqa: E402
import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

FRESHNESS = "artifacts/slice70_data_freshness.json"
FORWARD = "artifacts/slice70_forward_shadow.json"
GATE = "artifacts/slice70_promotion_gate.json"

WINDOW = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
          "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"]

PRIOR = {n: (f"artifacts/slice{n}_forward_shadow.json", bars)
         for n, bars in ((62, 1), (63, 1), (64, 2), (65, 3), (66, 4),
                         (67, 5), (68, 6), (69, 7))}


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


def _corpus():
    loaded, _b, _n = md.load_corpus(
        os.path.join(REPO, "data", "real_linear_1d"), verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded["BTCUSDT"]]
    funding = fb.load_funding(os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    return bars, funding, fb.load_folds()


# ===========================================================================
# 1 — the window reached eight
# ===========================================================================


class TestTheWindowReachedEight:

    def test_eight_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 8
        assert [s[:10] for s in result.appended_timestamps][:8] == WINDOW
        assert load(FRESHNESS)["after_t1_linear"] == 8
        assert load(FRESHNESS)["after_t1_dates"] == WINDOW

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice69"]
        assert delta["previous_after_t1_linear"] == 7
        assert delta["new_linear_bars_since_slice69"] == 1
        assert delta["the_window_grew"] is True

    def test_history_is_untouched_and_growth_is_an_append(self):
        folds = fb.load_folds()
        for path, result in cp.check_all().items():
            assert result.append_only, f"{path}: {result.why_not()}"
            assert result.appended_all_strictly_after_t1
        assert cp.check(cp.LINEAR_BTC).prefix_sha256 == \
            folds["sources"]["linear_bars"]["sha256_uncompressed"]
        assert cp.check(cp.FUNDING_BTC).prefix_sha256 == \
            folds["sources"]["funding"]["sha256_uncompressed"]
        assert load(FRESHNESS)["prefix_invariant"]["history_rewritten"] is False

    def test_only_btc_grew(self):
        grew = {p for p, r in cp.check_all().items() if r.extended}
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}
        for path, result in cp.check_all().items():
            if "BTC" not in path:
                assert result.appended_rows == 0, path

    def test_the_open_bar_is_absent(self):
        """Clock-derived and nothing else. No literal beside it. §53d."""
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_every_note_claim_is_true(self):
        payload = load(FRESHNESS)
        assert payload["note_claims_all_true"] is True
        assert payload["false_note_claims"] == []
        assert payload["human_note_is_evidence"] is False
        assert payload["claim_vs_files_discrepancy"] is False
        assert len(payload["human_note_claims_checked"]) >= 10

    def test_the_note_path_names_this_slice_and_is_derived(self):
        payload = load(FRESHNESS)
        assert payload["human_note_path"] == \
            "docs/human/HUMAN_DATA_NOTE_SLICE70.md"
        assert payload["human_note_present"] is True
        source = os.path.join(REPO, "tools", "slice70_freshness.py")
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "NOTE_PATH"
                    for t in node.targets))
        assert isinstance(assignment.value, ast.JoinedStr)
        slice_const = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "SLICE" for t in node.targets))
        assert slice_const.value.value == 70, (
            "SLICE froze at its predecessor's value — the very defect the "
            "derived path was meant to make impossible. EDGE.md §52a, §53d.")

    def test_the_note_reads_this_slices_file(self):
        note = os.path.join(REPO, load(FRESHNESS)["human_note_path"])
        with open(note, encoding="utf-8") as handle:
            text = handle.read()
        assert "Slice 70" in text
        assert "2026-08-17" in text

    def test_the_counts_came_from_files(self):
        """AMENDED BY SLICE 71: this artefact is now a DATED one.

        Written as an equality against slice 70's OWN artefact, which member
        three of the guard family explicitly permits: at authoring time the
        artefact had just been written from this disk in this run, and
        equality is the right relation for proving counts came from files
        rather than from a manifest.

        One slice later the same line is a live count equated to a dated
        record — the exact shape member three forbids. No AST sweep can catch
        that, because the code was correct when written and became wrong only
        by the passage of a slice. What catches it is the standing rule that
        the CURRENT slice owns the live apparatus, and the amendment is made
        under it. EDGE.md §54f.

        Direction of travel against disk; equality against the frozen record.
        """
        assert cp.check(cp.LINEAR_BTC).rows_on_disk >= \
            load(FRESHNESS)["linear_rows"]
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= \
            load(FRESHNESS)["funding_rows"]
        assert load(FRESHNESS)["linear_rows"] == 1469
        assert load(FRESHNESS)["after_t1_linear"] == 8


# ===========================================================================
# 2 — the two stale assertions, and the rule that replaces them
# ===========================================================================


class TestALiteralBesideADerivedCheckIsTheOnlyThingThatCanGoStale:

    AMENDED = "tests/test_slice69_seven_day_window.py"

    def _source(self):
        with open(os.path.join(REPO, self.AMENDED), encoding="utf-8") as h:
            return h.read()

    def test_the_open_bar_test_no_longer_restates_a_date(self):
        source = self._source()
        node = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "test_the_open_bar_is_absent")
        literals = [c.value for c in ast.walk(node)
                    if isinstance(c, ast.Constant)
                    and isinstance(c.value, str)
                    and re.fullmatch(r"2026-\d\d-\d\d", c.value)]
        assert literals == [], (
            "a hard-coded date is back beside the clock-derived check")

    def test_the_count_test_asserts_direction_not_equality(self):
        source = self._source()
        node = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.FunctionDef)
                    and n.name
                    == "test_this_slices_counts_came_from_files_not_the_manifest")
        live = [c for c in ast.walk(node)
                if isinstance(c, ast.Compare)
                and any(isinstance(o, ast.Attribute)
                        and o.attr == "rows_on_disk"
                        for o in [c.left])]
        assert live, "the live-count comparison vanished entirely"
        for compare in live:
            assert all(isinstance(op, ast.GtE) for op in compare.ops), (
                "a live corpus count is equated to a dated artefact again")

    def test_the_amendments_did_not_weaken_the_frozen_claim(self):
        """Slice 69's dated claims are still asserted — against the frozen
        artefact, which is where a dated claim belongs."""
        frozen = load("artifacts/slice69_data_freshness.json")
        assert frozen["after_t1_linear"] == 7
        assert frozen["linear_rows"] == 1468
        assert frozen["open_bar_absent_as_expected"] is True
        assert frozen["after_t1_dates"] == WINDOW[:7]

    def test_the_rule_is_written_down(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        assert "\n## 53d." in edge
        assert "\n## 53a." in edge


# ===========================================================================
# 3 — guard family, member three: live count vs DATED artefact
# ===========================================================================


class TestNoTestEqualsALiveCountAgainstADatedArtefact:
    """Slice 66 made `live_count == 5` unshippable. This makes
    `live_count == frozen_artefact["rows"]` unshippable too.

    The prefix invariant says a corpus may GROW and may never shrink. A test
    that equates a live count with a number recorded on a past date asserts
    that it has not grown — the opposite of the invariant — and passes only
    until the next bar. The correct relation against a dated record is `>=`.

    Equality against THIS slice's own artefact is legitimate and permitted:
    it was written from the same disk in the same run, and it is how a slice
    proves its counts came from files rather than from a manifest.
    """

    LIVE_ATTRS = ("rows_on_disk", "appended_rows")
    ANNOTATION = "live-vs-dated-ok"

    @classmethod
    def _artefact_constants(cls, tree):
        """Module-level NAME -> artefact path, for resolving `load(FRESHNESS)`."""
        found = {}
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and "artifacts/" in node.value.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = node.value.value
        return found

    @classmethod
    def _artefact_of(cls, node, constants):
        """The artefact path a subscript expression reads from, if any."""
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id in constants:
                return constants[inner.id]
            if (isinstance(inner, ast.Constant)
                    and isinstance(inner.value, str)
                    and "artifacts/" in inner.value):
                return inner.value
        return None

    @classmethod
    def _offences(cls, path):
        own = re.search(r"slice(\d+)_", os.path.basename(path))
        current = int(own.group(1)) if own else None
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        lines = text.splitlines()
        constants = cls._artefact_constants(ast.parse(text))

        found = []
        for node in ast.walk(ast.parse(text, filename=path)):
            if not (isinstance(node, ast.Compare)
                    and len(node.ops) == 1
                    and isinstance(node.ops[0], ast.Eq)):
                continue
            operands = [node.left, node.comparators[0]]
            live = [o for o in operands
                    if isinstance(o, ast.Attribute)
                    and o.attr in cls.LIVE_ATTRS
                    and isinstance(o.value, ast.Call)]
            other = [o for o in operands if o not in live]
            if not live or not other:
                continue
            artefact = cls._artefact_of(other[0], constants)
            if artefact is None:
                continue                       # slice 66's guard owns literals
            named = re.search(r"slice(\d+)_", artefact)
            if named and current is not None and int(named.group(1)) == current:
                continue                       # this slice's own artefact
            context = "\n".join(lines[max(0, node.lineno - 4):node.lineno])
            if cls.ANNOTATION in context:
                continue
            found.append((node.lineno, artefact))
        return found

    def _test_files(self):
        base = os.path.join(REPO, "tests")
        return [os.path.join(base, n) for n in sorted(os.listdir(base))
                if n.startswith("test_") and n.endswith(".py")]

    def test_no_test_in_the_tree_equates_a_live_count_to_a_dated_record(self):
        offences = {os.path.basename(p): self._offences(p)
                    for p in self._test_files()}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_the_guard_catches_the_defect_it_was_written_for(self):
        """The exact shape that failed at this slice's baseline."""
        import tempfile

        cases = {
            'F = "artifacts/slice69_data.json"\n'
            'def t():\n'
            '    assert cp.check(cp.LINEAR_BTC).rows_on_disk == load(F)["r"]\n':
                1,
            'F = "artifacts/slice70_data.json"\n'
            'def t():\n'
            '    assert cp.check(cp.LINEAR_BTC).rows_on_disk == load(F)["r"]\n':
                0,
            'F = "artifacts/slice69_data.json"\n'
            'def t():\n'
            '    assert cp.check(cp.LINEAR_BTC).rows_on_disk >= load(F)["r"]\n':
                0,
            'F = "artifacts/slice69_data.json"\n'
            'def t():\n'
            '    # live-vs-dated-ok: deliberate\n'
            '    assert cp.check(cp.LINEAR_BTC).rows_on_disk == load(F)["r"]\n':
                0,
        }
        for source, expected in cases.items():
            directory = tempfile.mkdtemp()
            name = os.path.join(directory, "test_slice70_probe.py")
            with open(name, "w", encoding="utf-8") as handle:
                handle.write(source)
            try:
                assert len(self._offences(name)) == expected, source
            finally:
                os.unlink(name)
                os.rmdir(directory)

    def test_the_sweep_reached_the_whole_test_tree(self):
        names = {os.path.basename(p) for p in self._test_files()}
        assert "test_slice69_seven_day_window.py" in names
        assert len(names) >= 40


# ===========================================================================
# 4 — the guard that was gone for four slices
# ===========================================================================


class TestTheScoringGuardCameBack:
    """`test_the_scoring_was_not_re_tuned_since_sliceNN` existed in slices
    63, 64, 65 and 66, and then stopped being carried forward.

    Slice 67's verdict listed it in the evidence table anyway. So for slices
    67, 68, 69 and 70 the forward shadow shipped without the AST comparison
    that is supposed to make silent re-tuning of the scoring impossible, and
    a verdict said otherwise.

    Restored here, and — because a guard restored is not the same as a
    property verified — run backwards across every slice it was missing from.
    """

    SUBSTITUTIONS = {
        "forward_shadow_observation/8": "forward_shadow_observation/9",
        "slice69_forward_shadow.json": "slice70_forward_shadow.json",
        "slice69_forward_shadow.log": "slice70_forward_shadow.log",
        "window_grew_since_slice68": "window_grew_since_slice69",
        "slice68_forward_shadow.json": "slice69_forward_shadow.json",
        "SLICE 69 — FORWARD SHADOW SEGMENT":
            "SLICE 70 — FORWARD SHADOW SEGMENT",
        "EDGE.md §52b, before this tool ran":
            "EDGE.md §53b, before this tool ran",
    }

    # Every top-level payload key whose EXPRESSION differs from slice 69's,
    # with the reason. The test requires this set to match exactly: an
    # undeclared change fails, and so does a declaration for a key that did
    # not actually change.
    DECLARED_CHANGES = {
        "slice": "the artefact's own slice number, 69 -> 70. Declared rather "
                 "than normalised away: the one number that MUST change is "
                 "worth seeing in this list every slice.",
        "ceiling": "the pre-declared ceiling advances 2 -> 3 and says so",
        "why_neither_moved": "now cites BOTH near misses, as the mission asks",
        "why_not_closer": "derived from the bar count instead of saying 'one'",
    }
    DECLARED_ADDITIONS = {
        "funding_setup_table": "per-bar rate/setup table the mission asks for",
    }

    @classmethod
    def _split(cls, name, substitutions):
        class Normalise(ast.NodeTransformer):
            def visit_Constant(self, node):          # noqa: N802
                if isinstance(node.value, str):
                    value = node.value
                    for before, after in substitutions.items():
                        value = value.replace(before, after)
                    return ast.copy_location(ast.Constant(value), node)
                return node

        with open(os.path.join(REPO, "tools", name), encoding="utf-8") as h:
            tree = Normalise().visit(ast.parse(h.read()))
        build = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "build")
        *before, final = build.body
        assert isinstance(final, ast.Return)
        before = [s for s in before
                  if not (isinstance(s, ast.Expr)
                          and isinstance(s.value, ast.Constant)
                          and isinstance(s.value.value, str))]
        payload = {k.value: ast.dump(v)
                   for k, v in zip(final.value.keys, final.value.values)}
        return [ast.dump(s) for s in before], payload

    def test_the_scoring_was_not_re_tuned_since_slice69(self):
        # The substitutions rewrite the PREDECESSOR's slice-numbered
        # strings into this slice's, so only genuine changes survive as
        # differences. Applied to both files; they are a no-op on the newer.
        old_body, old_payload = self._split(
            "slice69_forward_shadow.py", self.SUBSTITUTIONS)
        new_body, new_payload = self._split(
            "slice70_forward_shadow.py", self.SUBSTITUTIONS)
        assert old_body == new_body, "the SCORING changed between slices"
        assert set(old_payload) - set(new_payload) == set(), "a key was DROPPED"
        drifted = {k for k, v in old_payload.items()
                   if new_payload.get(k) != v}
        assert drifted == set(self.DECLARED_CHANGES), sorted(drifted)
        added = set(new_payload) - set(old_payload)
        assert added == set(self.DECLARED_ADDITIONS), sorted(added)

    def test_the_scoring_did_not_drift_while_the_guard_was_absent(self):
        """The property, checked for the four slices that had no guard.

        Normalising only the slice-numbered strings, `build`'s body must be
        AST-identical at every step and no emitted key may be dropped. This
        is the evidence that the guard's absence cost nothing in substance —
        stated as a verified fact rather than an assumption.
        """
        def norm(name, cur, prev):
            subs = {f"slice{cur}": "sliceCUR", f"slice{prev}": "slicePREV",
                    f"SLICE {cur}": "SLICE CUR", f"SLICE {prev}": "SLICE PREV"}
            body, payload = self._split(name, subs)
            body = [re.sub(r"forward_shadow_observation/\d+",
                           "observation/V", s) for s in body]
            return body, payload

        for prev, cur in ((66, 67), (67, 68), (68, 69), (69, 70)):
            old_body, old_payload = norm(
                f"slice{prev}_forward_shadow.py", prev, prev - 1)
            new_body, new_payload = norm(
                f"slice{cur}_forward_shadow.py", cur, prev)
            assert old_body == new_body, (prev, cur, "SCORING BODY CHANGED")
            assert set(old_payload) - set(new_payload) == set(), (prev, cur)

    def test_the_absence_spanned_four_slices_and_is_recorded(self):
        """Not minimised to the slice that happened to notice."""
        missing = []
        for number in (67, 68, 69):
            matches = [n for n in os.listdir(os.path.join(REPO, "tests"))
                       if n.startswith(f"test_slice{number}_")]
            assert matches, number
            source = ""
            for name in matches:
                with open(os.path.join(REPO, "tests", name),
                          encoding="utf-8") as handle:
                    source += handle.read()
            if "the SCORING changed between slices" not in source:
                missing.append(number)
        assert missing == [67, 68, 69], missing

    def test_slice67s_verdict_cited_a_test_that_did_not_exist(self):
        """Stated plainly, because it is the more serious half of this."""
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE67.md")
        with open(path, encoding="utf-8") as handle:
            verdict = handle.read()
        assert "test_the_scoring_was_not_re_tuned_since_slice66" in verdict
        # Defined functions, via the AST — not a text search, which this
        # very file would satisfy by quoting the name.
        defined = set()
        for name in os.listdir(os.path.join(REPO, "tests")):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(REPO, "tests", name),
                      encoding="utf-8") as handle:
                for node in ast.walk(ast.parse(handle.read())):
                    if isinstance(node, ast.FunctionDef):
                        defined.add(node.name)
        assert "test_the_scoring_was_not_re_tuned_since_slice66" \
            not in defined, (
                "the cited test now exists; retire this test rather than "
                "inverting it")
        assert "test_the_scoring_was_not_re_tuned_since_slice69" in defined


# ===========================================================================
# 5 — guard family, member four: a verdict may not cite a missing test
# ===========================================================================


class TestAVerdictMayNotCiteATestThatDoesNotExist:
    """The structural fix for what slice 67 shipped.

    An evidence table is the most load-bearing prose in this programme: it is
    where a claim is tied to the thing that checks it. A row naming a test
    that does not exist is worse than no row, because it reads as a stronger
    guarantee than an unsupported claim would.

    Scoped to THIS slice's verdict, which is the one this slice can fix.
    Applying it retroactively would fail on slice 67's, which is a historical
    record and must not be edited.
    """

    VERDICT = "STAGE1_VERDICT_SLICE70.md"
    NAME = re.compile(r"\btest_[a-z0-9_]+\b")

    # A verdict may legitimately QUOTE a name that does not exist — this one
    # does, to show what slice 67 cited. Marking the line is the difference
    # between exhibiting a missing test and citing one, and the annotation is
    # not a loophole: a marked name that DOES exist fails too, so the marker
    # cannot be used to launder a citation that quietly broke.
    ANNOTATION = "known-missing"

    def _test_names_in_the_tree(self):
        names = set()
        base = os.path.join(REPO, "tests")
        for entry in os.listdir(base):
            if not (entry.startswith("test_") and entry.endswith(".py")):
                continue
            with open(os.path.join(base, entry), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    names.add(node.name)
            names.add(entry[:-3])
        return names

    def test_every_test_this_verdict_cites_exists(self):
        path = os.path.join(REPO, self.VERDICT)
        if not os.path.isfile(path):
            return                     # written in STEP 6; checked on re-run
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        cited, exhibited = set(), set()
        for line in lines:
            names = set(self.NAME.findall(line))
            (exhibited if self.ANNOTATION in line else cited).update(names)
        present = self._test_names_in_the_tree()
        assert sorted(cited - exhibited - present) == [], sorted(
            cited - exhibited - present)
        # The marker means "this name is absent, deliberately shown".
        assert sorted(exhibited & present) == [], sorted(exhibited & present)

    def test_the_guard_would_have_caught_slice67s_row(self):
        cited = {"test_the_scoring_was_not_re_tuned_since_slice66"}
        assert cited - self._test_names_in_the_tree() == cited

    def test_the_sweep_can_see_a_name_it_should_find(self):
        assert "test_every_test_this_verdict_cites_exists" in \
            self._test_names_in_the_tree()


# ===========================================================================
# 6 — ceiling 3, setups 0 of 8, fills 0
# ===========================================================================


class TestCeilingThreeZeroSetupsZeroFills:

    def test_the_ceiling_is_three(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 8
        assert ceiling["max_possible_forward_closed_trades"] == 3
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§53b" in ceiling["declared_in"]

    def test_the_ceiling_is_arithmetic(self):
        payload = load(FRESHNESS)
        assert max(0, payload["after_t1_linear"] - fb.HORIZON) == 3
        assert fb.HORIZON == 5

    def test_it_is_not_two_and_not_four(self):
        """Both adjacent errors the mission names, asserted against."""
        ceiling = load(FORWARD)["ceiling"]["max_possible_forward_closed_trades"]
        assert ceiling != 2, "2 was slice 69's ceiling, at N=7"
        assert ceiling < 4, "4 would need N=9"

    def test_slice69_ran_at_ceiling_two(self):
        assert load("artifacts/slice69_forward_shadow.json")["ceiling"][
            "max_possible_forward_closed_trades"] == 2

    def test_every_prior_slice_ceiling_is_recorded_and_lower(self):
        ceilings = [load(f)["ceiling"]["max_possible_forward_closed_trades"]
                    for _n, (f, _b) in sorted(PRIOR.items())]
        assert ceilings == [0, 0, 0, 0, 0, 0, 1, 2]
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 3

    def test_zero_setups_on_all_eight_bars(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["forward_bars"] == 8
        assert regime["funding_setups_in_window"] == 0
        rates = regime["rate_at_each_forward_decision"]
        assert sorted(rates) == WINDOW
        assert all(abs(v) < fb.FUND_ABS for v in rates.values())

    def test_the_per_bar_table_covers_every_date(self):
        table = load(FORWARD)["funding_setup_table"]
        assert [row["date"] for row in table] == WINDOW
        assert all(row["funding_setup_present"] is False for row in table)
        assert all(row["shortfall_vs_fund_abs"] > 0 for row in table)

    def test_the_rule_stood_aside_on_all_eight(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 8
        assert all(row["funding_setup_present"] is False for row in aside)
        assert load(FORWARD)["forward_decisions"][
            "flagged_bars_in_window"] == []
        assert load(FORWARD)["forward_decisions"][
            "candidates_after_schedule"] == []

    def test_no_trade_and_no_observation(self):
        forward = load(FORWARD)
        assert forward["forward_n_trades"] == 0
        assert forward["is_forward_observation"] is False
        assert forward["trades"] == []
        assert forward["forward_mean_net_r"] is None

    def test_growth_and_observation_remain_independent(self):
        forward = load(FORWARD)
        assert forward["extension_present"] is True
        assert forward["is_forward_observation"] is False
        assert "A BAR ARRIVING IS NOT AN OBSERVATION" in \
            prose(forward["labelling_rule"])

    def test_the_finding_says_eight_and_not_six_or_seven(self):
        """The mission forbids finding text still saying six or seven."""
        for text in (prose(load(FRESHNESS)["delta_since_slice69"]["finding"]),
                     prose(load(FORWARD)["window_grew_since_slice69"]["note"]),
                     prose(load(FRESHNESS)["forward_funding_regime"][
                         "finding"])):
            assert "SIX BARS" not in text.upper()
            assert "SEVEN BARS" not in text.upper()
        finding = prose(load(FRESHNESS)["delta_since_slice69"]["finding"])
        assert "EIGHT BARS" in finding.upper()
        assert "ceiling 3, setups 0 of 8, fills 0" in finding

    def test_no_clamping_no_slicing_no_invention(self):
        forward = load(FORWARD)
        assert forward["forward_decisions"]["exit_clamping_to_corpus_end"] \
            is False
        assert forward["forward_window"]["bar_array_sliced"] is False
        assert forward["forward_window"]["invented_future_bars"] == 0
        assert forward["bars_fabricated"] == 0
        assert load(FRESHNESS)["bars_fabricated"] == 0
        assert load(FRESHNESS)["corpus_appended_to_by_this_tool"] is False

    def test_the_forward_monitors_report_insufficient_data(self):
        states = {r["name"]: r["state"]
                  for r in load(FORWARD)["forward_monitor_readings"]}
        assert set(states.values()) == {"INSUFFICIENT_DATA"}
        assert load(FORWARD)["forward_monitor_status"] == "INSUFFICIENT_DATA"

    def test_the_historical_m4_warn_was_not_rebranded(self):
        historical = load("artifacts/slice59_forward_shadow.json")
        m4 = next(r for r in historical["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert m4["state"] == "WARN"
        assert "deliberately NOT recomputed" in \
            prose(load(FORWARD)["monitor_scope_note"])


# ===========================================================================
# 7 — two near misses, neither rescued
# ===========================================================================


class TestNeitherNearMissWasRescued:

    def test_fund_abs_is_unchanged(self):
        assert fb.FUND_ABS == 0.0001
        assert load(FORWARD)["fund_abs"] == 0.0001
        assert load(FORWARD)["fund_abs_moved_this_slice"] is False
        assert load(FRESHNESS)["forward_funding_regime"][
            "parameters_moved_because_of_this"] is False

    def test_the_join_is_unchanged(self):
        assert load(FORWARD)["join_changed_this_slice"] is False
        assert "CLOSE" in load(FORWARD)["join_rule"]
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)

        def on(date):
            return next(i for i in window
                        if dt.datetime.fromtimestamp(
                            bars[i].start_ms / 1000.0,
                            tz=dt.timezone.utc).strftime("%Y-%m-%d") == date)

        assert rates[on("2026-08-12")] == 0.00006601
        assert rates[on("2026-08-17")] == 0.00009202

    def test_the_08_12_print_still_equals_fund_abs_and_still_missed(self):
        prints = load(FRESHNESS)["forward_funding_regime"][
            "prints_at_or_above_fund_abs"]
        assert [p["funding_time"] for p in prints] == \
            ["2026-08-12T00:00:00+00:00"]
        assert prints[0]["equals_fund_abs_exactly"] is True

    def test_the_08_17_print_stood_at_the_close_and_still_missed(self):
        """The second near miss, and the more instructive one.

        08-12 could only have been rescued by changing the JOIN. 08-17 stood
        at its bar's close, so only a lower FUND_ABS would capture it. Between
        them the two available rescues are exhausted and neither is taken.
        """
        row = next(r for r in load(FORWARD)["funding_setup_table"]
                   if r["date"] == "2026-08-17")
        assert row["funding_at_decision"] == 0.00009202
        assert row["funding_setup_present"] is False
        assert 0 < row["shortfall_vs_fund_abs"] < 1e-5

    def test_the_reason_neither_moved_cites_both(self):
        why = prose(load(FORWARD)["why_neither_moved"])
        assert "2026-08-12T00:00Z" in why
        assert "2026-08-17T16:00Z" in why
        assert "0.00009202" in why
        assert "fitting the rule to the data" in why
        assert "void the clear" in why

    def test_the_horizon_and_fingerprint_are_unchanged(self):
        assert fb.HORIZON == 5
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"
        assert load(FORWARD)["thresholds_moved_this_slice"] is False


# ===========================================================================
# 8 — the pack, and everything frozen
# ===========================================================================


class TestThePackAndTheFreezes:

    def test_no_restoration_was_needed_again(self):
        assert not os.path.exists(os.path.join(
            REPO, "artifacts", "slice70_restored_from_slice69.json"))
        assert load(FRESHNESS)["pack_regression"][
            "occurred_this_slice"] is False

    def test_the_pack_base_claim_is_true_this_time(self):
        claim = load(FRESHNESS)["pack_base_claim"]
        assert claim["true"] is True
        assert claim["was_false_when_first_made_in_slice67"] is True

    def test_edge_carries_every_section_from_45_to_53(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for section in range(45, 54):
            assert f"\n## {section}a." in edge, section

    def test_the_meta_guards_are_all_enforced(self):
        from test_slice66_four_day_window import (  # noqa: PLC0415
            TestNoTestPinsALiveAbsolute as Absolute)
        from test_slice69_seven_day_window import (  # noqa: PLC0415
            TestNoToolCarriesAnotherSlicesConstant as Foreign)

        absolute = Absolute()
        assert {os.path.basename(p): absolute._offences(p)
                for p in absolute._test_files()
                if absolute._offences(p)} == {}

        foreign = Foreign()
        offences = {os.path.basename(p): foreign._offences(p)
                    for p in foreign._tools()
                    if os.path.basename(p).startswith("slice70_")}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_the_frozen_pack_is_intact(self):
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00
        assert load(FORWARD)["constants_fingerprint_matches_frozen"] is True
        assert load(FORWARD)["schedule_mode"] == "one_entry_per_contiguous_run"
        assert load(FORWARD)["caps_changed_this_slice"] is False

    def test_the_monitor_thresholds_are_unchanged(self):
        thresholds = shadow.MONITOR_THRESHOLDS
        assert thresholds["M1_rolling_trades"]["warn_below"] == 0.0
        assert thresholds["M1_rolling_trades"]["alert_below"] == -0.25
        assert thresholds["M2_rolling_days"]["window_days"] == 90
        assert thresholds["M3_concentration"]["warn_above"] == 0.60
        assert thresholds["M4_halves"]["min_trades"] == 20

    def test_eleven_families_stay_frozen(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert load(FORWARD)["frozen_absent_count"] == 11
        assert fb.UNIVERSE == ("BTCUSDT",)

    def test_the_oos_clear_was_not_re_scored(self):
        assert sha256("artifacts/slice57_oos_edge_BTCUSDT_summary.json") == \
            "28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51"
        assert load(FORWARD)["cleared_edge_re_scored_this_slice"] is False
        assert load(FORWARD)["is_stage1_evidence"] is False
        assert load(FORWARD)["registration_eligible"] is False
        assert fb.folds_sha256() == \
            "ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c"
        assert fb.folds_are_unmodified() is True

    def test_no_edge_measurement_ran_this_slice(self):
        for name in sorted(os.listdir(os.path.join(REPO, "artifacts"))):
            if not name.startswith("slice70_") or not name.endswith(".json"):
                continue
            text = json.dumps(load(f"artifacts/{name}"))
            for banned in ("m1_percentile", "replicates",
                           "control_attestation", "EDGE_EVIDENCE_POSITIVE"):
                assert banned not in text, (name, banned)

    def test_the_gate_refuses(self):
        assert pg.promotion_gate_allows_live() is False
        assert load(GATE)["promotion_gate_allows_live"] is False
        assert load(GATE)["items_complete"] == 2
        assert load(GATE)["items_total"] == 8
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert load(GATE)["minimums_moved_this_slice"] is False
        assert load(GATE)["items_complete_unchanged_from_slice_69"] is True

    def test_the_gate_refuses_to_credit_three_possible_trades(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "3 POSSIBLE TRADES DO NOT ADVANCE THIS GATE" in evidence
        assert "never the numerator of what has happened" in evidence
        assert load(GATE)["checklist"]["forward_shadow_clean"][
            "forward_trades_to_date"] == 0

    def test_no_human_item_was_completed_in_code(self):
        assert load(GATE)["human_items_completed_in_code"] == 0
        assert load(GATE)["signatures_forged"] is False
        human = [item for item in load(GATE)["evaluated"]["items"]
                 if item["owner"].startswith("human")]
        assert human and not any(item["complete"] for item in human)

    def test_live_is_dark(self):
        status = ps.current()
        assert bool(status.live_authorized) is False
        assert bool(status.models_current_present) is False
        assert status.policy_mode == "off"
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_not_closer_to_autonomy(self):
        for path in (FORWARD, GATE):
            assert load(path)["closer_to_autonomous_profit_agent"] is False

    def test_the_broadcast_is_still_stopped_and_the_residue_still_there(self):
        audit = load(FRESHNESS)["manifests"]
        assert audit["broadcast_occurred_this_slice"] is False
        assert audit["measured_product_entries_are_accurate"] is True
        assert audit["count_disagreeing"] == 4
        assert audit["not_edited_by_this_slice"] is True
        moved = audit["declared_rows_that_moved_this_slice"]
        assert moved and all("BTC" in path for path in moved)

    def test_the_manifest_finding_is_derived_not_narrated(self):
        """It claimed 'the manifests were updated this slice by BROADCASTING'
        for three slices after that stopped being true. Now it is computed."""
        finding = prose(load(FRESHNESS)["manifests"]["finding"])
        assert "Broadcast to non-BTC symbols this slice: False" in finding
        assert "were updated this slice by BROADCASTING" not in finding

    def test_the_funding_seam_is_still_unfilled(self):
        assert load(FRESHNESS)["funding_seam"]["missing_prints"] == \
            ["2026-08-09T16:00:00+00:00"]

    def test_no_fetch_was_attempted(self):
        assert load(FRESHNESS)["fetch_attempted"] is False
