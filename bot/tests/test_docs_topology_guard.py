"""Round 21: the active documentation must not tell anyone to do what the operating contract forbids.

An old instruction in a README or a runbook is as dangerous as code: somebody pastes it. This guard scans the
ACTIVE documents and scripts for copy-paste instructions that arm Mainnet, run an order-capable loop in a
Codespace, deploy or resize the stopped legacy book, use generic Bybit names as Testnet credentials, or start the
autopilot app that does not exist, and it requires ONE topology statement that is byte-identical everywhere.

What is scanned: the files in ACTIVE_FILES (and every shell script under scripts/ and bot/scripts/). What is NOT:
`docs/MONEY_MISSION.md` log rounds and `reports/` are HISTORY (each states the date it was written); inside them
only a region between `<!-- docs-scan:active -->` and `<!-- docs-scan:end -->` is scanned. History is allowed to
describe an old instruction; it is not allowed to be where a person copies it from, which is why the active
documents carry the rule and the topology block.

Exemptions are explicit and narrow: a line that carries a prohibition ("never", "do not", "refused", "retired",
...) is prose about the rule, not an instruction, EXCEPT inside a fenced code block, where every line is executable
and only `docs-scan:allow <reason>` on that line permits it.
"""
import glob
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
REPO = os.path.dirname(BOT)

ACTIVE_FILES = [
    "CLAUDE.md",
    "bot/README.md",
    "bot/RUNBOOKS.md",
    "bot/DEPLOYMENT.md",
    "bot/MARKET_CATEGORIES.md",
    "docs/TESTNET_QUALIFICATION_PROPOSAL.md",
    "reports/codespaces-checklist.md",
    "Makefile",
]
#: scripts that inject a hostile environment (a Mainnet selector, a live switch, generic key names) to PROVE the image refuses it
NEGATIVE_TEST_SCRIPTS = {"bot/deploy/autopilot-image-smoke.sh", "bot/deploy/drill-image-smoke.sh", "scripts/verify_drill_runner.sh"}
HOSTILE_ENV_RULES = {"MAINNET_ARMING_VARIABLE", "MAINNET_HOST_OR_ARMING_FLAG", "GENERIC_KEY_AS_TESTNET_CREDENTIAL"}
HISTORICAL_FILES = ["docs/MONEY_MISSION.md"]            # scanned only between the active markers
TOPOLOGY_FILES = ["bot/README.md", "bot/RUNBOOKS.md", "docs/MONEY_MISSION.md"]
BEGIN, END = "<!-- topology:begin -->", "<!-- topology:end -->"
ACTIVE_BEGIN, ACTIVE_END = "<!-- docs-scan:active -->", "<!-- docs-scan:end -->"

#: a line that states a prohibition is prose about the rule, not an instruction
PROHIBITION = re.compile(r"\b(never|must not|do not|don't|does not|did not|not set|is not|are not|not a|not an|not the|refus\w+|forbid\w*|retired|stays? (?:unset|stopped|off)|unset|"
                         r"out of scope|history|historical|stale|disabled|not part|cannot|can no longer|no longer|nothing)\b", re.I)

#: (id, compiled pattern, what is wrong). Each matches a copy-paste instruction.
RULES = [
    ("MAINNET_ARMING_VARIABLE", re.compile(r"(LIVE_TRADING_ACK\s*=|USE_TESTNET\s*=\s*0|PAPER_TRADING\s*=\s*0)"),
     "sets a Mainnet arming variable (CLAUDE.md: never set LIVE_TRADING_ACK, USE_TESTNET=0 or PAPER_TRADING=0)"),
    ("MAINNET_HOST_OR_ARMING_FLAG", re.compile(r"(api\.bybit\.com|BYBIT_VENUE\s*=\s*mainnet|drill\.py\s+--arm|--live-arm)"),
     "points a command at Mainnet or arms a drill"),
    ("CODESPACE_ORDER_LOOP", re.compile(r"(tmux\s+new[^\n]*(auto|loop)|ms_testnet_autopilot\.py\s+loop|ms_testnet_autopilot\.py\s+grant|autopilot-ctl\s+grant\b[^\n]*Codespace)"),
     "starts the order-capable loop or types a grant in a Codespace"),
    ("LEGACY_BOOK_COMMAND", re.compile(r"(fly\s+(deploy|apps\s+create|machine\s+start|machine\s+run|scale|volumes\s+extend)[^\n]*carry-book|carry-book[^\n]*(fly\s+deploy|machine\s+start|volumes\s+extend))"),
     "starts, deploys or resizes the stopped legacy carry-book"),
    ("GENERIC_KEY_AS_TESTNET_CREDENTIAL", re.compile(r"\bBYBIT_API_(KEY|SECRET)\b"),
     "names a generic key as if it were a Testnet credential (only BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET are)"),
    ("PS_NOT_IN_DRILL_IMAGE", re.compile(r"ps\s+-eo"),
     "uses `ps`, which the drill image does not contain (read /proc instead: tools/recorder_forensics.py commands)"),
    ("TIMER_WITH_ARMING", re.compile(r"testnet_session_timer\.sh\s+\d"),
     "runs the retired order-capable timer"),
]
#: generic names may appear in a line that says what they are (a reference, not an instruction)
GENERIC_OK = re.compile(r"(generic|not (?:a )?testnet|not testnet|never used|never a fallback|are ignored|names only|names that|legacy|which account)", re.I)


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _active_text(rel, text):
    """The text of a document that the scan applies to. A historical document contributes only its marked regions."""
    if rel not in HISTORICAL_FILES:
        return text
    out, on = [], False
    for ln in text.split("\n"):
        if ACTIVE_BEGIN in ln:
            on = True
            continue
        if ACTIVE_END in ln:
            on = False
            continue
        out.append(ln if on else "")
    return "\n".join(out)


def scan(text):
    """Violations in one document's active text: [(line_no, rule_id, why, line)]."""
    found, in_fence = [], False
    for n, ln in enumerate(text.split("\n"), 1):
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if "docs-scan:allow" in ln:
            continue
        prohib = bool(PROHIBITION.search(ln)) and not in_fence
        for rid, rx, why in RULES:
            if not rx.search(ln):
                continue
            if rid == "GENERIC_KEY_AS_TESTNET_CREDENTIAL":
                if GENERIC_OK.search(ln) or prohib:
                    continue
            elif prohib:
                continue
            found.append((n, rid, why, ln.strip()[:160]))
    return found


def _scripts():
    out = []
    for pat in ("scripts/*.sh", "bot/scripts/*.sh", "bot/deploy/*.sh"):
        out += [os.path.relpath(p, REPO) for p in sorted(glob.glob(os.path.join(REPO, pat)))]
    return out


# --------------------------------------------------------------------------------------- the scan itself

@pytest.mark.parametrize("rel", ACTIVE_FILES + HISTORICAL_FILES)
def test_no_active_document_carries_a_forbidden_instruction(rel):
    text = _active_text(rel, _read(rel))
    found = scan(text)
    assert not found, f"{rel} carries an instruction the operating contract forbids:\n" + "\n".join(f"  line {n} [{rid}] {why}: {ln}" for n, rid, why, ln in found)


def test_the_scan_sees_the_real_files():
    """A scan that silently reads nothing proves nothing."""
    for rel in ACTIVE_FILES + HISTORICAL_FILES:
        assert os.path.exists(os.path.join(REPO, rel)), rel
    assert len(_read("bot/RUNBOOKS.md")) > 100_000
    assert _scripts(), "no shell scripts were found to scan"


@pytest.mark.parametrize("rel", _scripts())
def test_no_shell_script_arms_mainnet_or_starts_the_retired_paths(rel):
    """Scripts are all code: a prohibition word on a line does not excuse it. Comments are skipped; a heredoc that prints text is code."""
    bad = []
    hostile = rel in NEGATIVE_TEST_SCRIPTS
    for n, ln in enumerate(_read(rel).split("\n"), 1):
        s = ln.strip()
        if not s or s.startswith("#") or "docs-scan:allow" in s:
            continue
        for rid, rx, why in RULES:
            if rid == "PS_NOT_IN_DRILL_IMAGE":
                continue
            if hostile and rid in HOSTILE_ENV_RULES:
                continue          # these scripts feed a hostile environment ON PURPOSE to prove the image refuses it
            if hostile and rid == "CODESPACE_ORDER_LOOP" and re.search(r"\bgrep\b", s):
                continue          # a smoke script that GREPS the container's PID 1 for the loop is a check, not a launch
            if rx.search(s) and not (rid == "GENERIC_KEY_AS_TESTNET_CREDENTIAL" and GENERIC_OK.search(s)):
                bad.append((n, rid, s[:140]))
    # retired scripts print a REFUSED notice; that text is checked by name below
    bad = [b for b in bad if "REFUSED" not in b[2]]
    assert not bad, f"{rel}: {bad}"


# --------------------------------------------------------------------------------------- self-test of the scanner

PLANTED_BAD = [
    "In the Codespace, `tmux new -s auto` then `python3 tools/ms_testnet_autopilot.py loop`.",
    "Arm it: fly secrets set --app carry-book PAPER_TRADING=0",
    "export LIVE_TRADING_ACK=I-understand",
    "Then run fly deploy --app carry-book --strategy immediate",
    "fly secrets set --app carry-drill BYBIT_API_KEY=... BYBIT_API_SECRET=...",
    "fly ssh console -a carry-drill -C \"ps -eo pid,user,args\"",
    "python3 tools/ms_testnet_autopilot.py grant --max-sessions 2 --max-notional-usd 400",
    "BYBIT_VENUE=mainnet python3 main.py",
]
PLANTED_OK = [
    "Never set `LIVE_TRADING_ACK`, `USE_TESTNET=0` or `PAPER_TRADING=0`.",
    "Mainnet is out of scope: `LIVE_TRADING_ACK=` is never set by anything in this repository.",
    "The generic `BYBIT_API_KEY` / `BYBIT_API_SECRET` are not Testnet credentials.",
    "carry-book is stopped; do not run fly deploy against carry-book.",
    "`carry-autopilot` does not exist and is not deployed.",
]


@pytest.mark.parametrize("bad", PLANTED_BAD)
def test_the_scanner_flags_each_planted_dangerous_instruction(bad):
    assert scan(bad), f"the scanner missed: {bad}"


@pytest.mark.parametrize("ok", PLANTED_OK)
def test_the_scanner_does_not_flag_prohibition_prose(ok):
    assert not scan(ok), f"false positive: {ok}"


def test_a_fenced_block_is_never_excused_by_a_prohibition_word():
    text = "Never do this:\n```bash\nexport PAPER_TRADING=0\n```\n"
    assert scan(text), "an executable line in a fence must be flagged even if the prose above says never"
    allowed = "```bash\nexport PAPER_TRADING=0   # docs-scan:allow a test fixture, never executed\n```\n"
    assert not scan(allowed)


def test_a_mutation_of_the_real_runbook_is_caught():
    """Plant the Round-21 defect back into the real RUNBOOKS text: the scan must go red, and the clean text must be green."""
    real = _read("bot/RUNBOOKS.md")
    assert not scan(real)
    mutated = real + "\n- Run it in the Codespace: `tmux new -s auto` then `python3 tools/ms_testnet_autopilot.py loop`.\n"
    assert scan(mutated)
    mutated2 = real + "\n```bash\nfly secrets set -a carry-book PAPER_TRADING=0\n```\n"
    assert scan(mutated2)


def test_history_is_exempt_only_outside_the_active_markers():
    hist = "Round 5 said: set PAPER_TRADING=0 then run drill.py --arm.\n"
    assert not scan(_active_text("docs/MONEY_MISSION.md", hist))
    marked = f"{ACTIVE_BEGIN}\nset PAPER_TRADING=0\n{ACTIVE_END}\nset PAPER_TRADING=0\n"
    assert len(scan(_active_text("docs/MONEY_MISSION.md", marked))) == 1


# --------------------------------------------------------------------------------------- one topology statement

def _block(rel):
    t = _read(rel)
    assert t.count(BEGIN) == 1 and t.count(END) == 1, f"{rel} must contain exactly one topology block"
    return t[t.index(BEGIN):t.index(END) + len(END)]


def test_the_topology_block_is_byte_identical_everywhere():
    blocks = {rel: _block(rel) for rel in TOPOLOGY_FILES}
    first = blocks[TOPOLOGY_FILES[0]]
    for rel, b in blocks.items():
        assert b == first, f"{rel}'s topology block differs from {TOPOLOGY_FILES[0]}'s: one authoritative statement, copied byte for byte"


def test_the_topology_block_states_the_current_operating_model():
    b = _block("bot/RUNBOOKS.md")
    for needle in ("Codespace or laptop", "**No.**", "Not selected and not deployed", "`carry-autopilot` does not exist", "`carry-drill` (Fly)", "read-only market-data recorder and monitor",
                   "`carry-book` (Fly)", "**Stopped.**", "`carry-research` (Fly)", "**Does not exist.**", "Mainnet is out of scope", "BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET",
                   "single-writer lease", "capability and a grant"):
        assert needle in b, needle
    # the statement of what is NOT done may not be softened into an invitation
    for banned in ("may be started", "you can start", "feel free"):
        assert banned not in b


def test_the_runbook_does_not_tell_a_codespace_to_run_the_loop_or_type_a_grant():
    t = _read("bot/RUNBOOKS.md")
    assert "tmux new -s auto" not in t
    assert "ms_testnet_autopilot.py grant" not in t
    assert "Where it runs: ONLY on the one selected Testnet executor, never in a Codespace" in t
    assert "**Arm it (once).**" not in t and "arm the Codespace" not in t


def test_every_carry_autopilot_command_comes_after_its_not_deployed_banner():
    t = _read("bot/RUNBOOKS.md")
    early = t.index("**Status (Round 21): every `-a carry-autopilot` command from here to the end of \"Fly autopilot: owner procedure\" assumes an app that does not exist today")
    banner = t.index("**STATUS (Round 21, owner-observed 2026-11-14): the `carry-autopilot` Fly app does NOT exist")
    proc = t.index("**Fly autopilot: owner procedure (laptop-free).**")
    assert early < banner < proc
    # every `fly ... -a carry-autopilot` command in the whole runbook sits after the first banner
    first_cmd = min((m.start() for m in re.finditer(r"fly [^\n`]*-a carry-autopilot", t)), default=len(t))
    assert first_cmd > early, "a carry-autopilot command appears before the NOT-DEPLOYED banner"


def test_the_proposal_states_that_the_executor_does_not_exist():
    t = _read("docs/TESTNET_QUALIFICATION_PROPOSAL.md")
    assert "Prerequisite 0 (Round 21): the executor does not exist yet" in t
    assert t.index("Prerequisite 0") < t.index("fly ssh console -a carry-autopilot")


def test_every_fly_app_the_proposal_names_is_a_known_app():
    """The proposal may name an app only if the repository knows it: carry-autopilot (fly_stack / fly.autopilot.toml), carry-drill, carry-book."""
    t = _read("docs/TESTNET_QUALIFICATION_PROPOSAL.md")
    known = set()
    for rel in ("bot/fly.autopilot.toml", "bot/fly.drill.toml", "bot/fly.toml", "bot/fly.research.toml"):
        m = re.search(r'^app\s*=\s*"([^"]+)"', _read(rel), re.M)
        assert m, rel
        known.add(m.group(1))
    named = set(re.findall(r"-a ([a-z][a-z0-9-]+)", t)) | set(re.findall(r"--app ([a-z][a-z0-9-]+)", t))
    assert named, "the proposal no longer names an app; update this test"
    assert named <= known, (named, known)


# --------------------------------------------------------------------------------------- retired documents and scripts

def test_deployment_md_is_a_tombstone_that_keeps_only_the_credential_statement():
    t = _read("bot/DEPLOYMENT.md")
    assert t.startswith("# DEPLOYMENT.md: RETIRED"), t[:60]
    assert "treat them as fully compromised" in t
    for gone in ("Gate 4", "Gate 5", "Gate 6", "microscopic", "Arming the learned policy", "aws secretsmanager", "export BYBIT_API_KEY", "LIVE_TRADING_ACK", "USE_TESTNET", "PAPER_TRADING"):
        assert gone not in t, gone
    assert "```" not in t, "a retired document carries no code fence to copy from"
    assert len(t.splitlines()) < 60


def test_every_reference_to_deployment_md_calls_it_retired_or_cites_only_the_credential_statement():
    for rel in ("bot/MARKET_CATEGORIES.md", "bot/RUNBOOKS.md", "bot/README.md", "CLAUDE.md"):
        for ln in _read(rel).split("\n"):
            if "DEPLOYMENT.md" in ln:
                assert re.search(r"retired|tombstone", ln, re.I), f"{rel}: a live pointer into the retired DEPLOYMENT.md: {ln[:160]}"


@pytest.mark.parametrize("rel", ["scripts/deploy.sh", "bot/scripts/testnet_session_timer.sh"])
def test_the_retired_scripts_refuse_and_name_nothing_executable(rel):
    t = _read(rel)
    code = [l.strip() for l in t.split("\n") if l.strip() and not l.strip().startswith("#")]
    assert code[-1] == "exit 2"
    assert any("REFUSED" in l for l in code)
    for forbidden in ("flyctl", "fly ", "curl", "python3", "PAPER_TRADING", "--arm"):
        assert forbidden not in "\n".join(code), (rel, forbidden)


def test_the_retired_workflow_is_marked_retired_and_still_gated_off():
    t = _read(".github/workflows/carry-drill-testnet.yml")
    assert t.startswith("# RETIRED AS GUIDANCE (Round 21)")
    assert "CARRY_DRILL_ENABLED" in t and "vars.CARRY_DRILL_ENABLED == 'true'" in t.replace('"', "'")
