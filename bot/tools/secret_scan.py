#!/usr/bin/env python3
"""Scan tracked files for credential-shaped content. Offline, standard library only.

    python3 tools/secret_scan.py            # scan `git ls-files`
    python3 tools/secret_scan.py PATH ...   # scan these files

It prints ``path:line: rule`` and NEVER the matched text, so the output of a scan
cannot itself leak what it found (CI logs are public). Exit 1 if anything matched,
2 if a file list could not be obtained, 3 if a non-binary file could not be scanned
(a scan that skipped something is not a pass). Text that is not valid UTF-8 is scanned anyway.

This is a net for the obvious shapes, not a proof of absence. A generic high-entropy
detector is deliberately not attempted: it would flag the sha256 digests this
repository stores everywhere and teach people to ignore the tool.
"""
from __future__ import annotations

import os
import hashlib
import re
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

#: (rule name, pattern). Patterns match credential SHAPES. Placeholder values
#: (``your_api_key_xxxx``, ``changeme``, ``example``) are filtered out by ``_is_placeholder``, which
#: judges the WHOLE value, never a substring.
RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("private-key-block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")),          # RSA / EC / OPENSSH / ENCRYPTED / PGP ... PRIVATE KEY [BLOCK]
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b")),
    ("slack-webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9]{6,}/[A-Za-z0-9]{6,}/[A-Za-z0-9]{16,}")),
    ("fly-token", re.compile(r"\bfo1_[A-Za-z0-9_-]{20,}\b|\bFlyV1 fm[12]_[A-Za-z0-9+/=_-]{20,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{40,}\b")),
    ("sb-secret-key", re.compile(r"\bsb_secret_[A-Za-z0-9_-]{16,}")),
    ("stripe-live-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}|\bwhsec_[A-Za-z0-9]{24,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # an assignment of a long literal to a name that says it is a credential. The
    # value is group 1; quotes are optional (env / YAML style); a name that contains
    # "test" or "testnet" is still a credential name.
    ("credential-assignment", re.compile(
        r"""(?ix)\b(?:[a-z0-9_]*api[_-]?key|[a-z0-9_]*api[_-]?secret|[a-z0-9_]*secret[_-]?key|
            access[_-]?token|auth[_-]?token|password|passwd)\b["']?\s*[:=]\s*["']?([A-Za-z0-9/+_=-]{16,})["']?""")),
    # a bare ``"secret": "<value>"`` / ``secret = '<value>'`` (JSON, YAML, Python). Stricter value test below.
    ("secret-literal", re.compile(
        r"""(?ix)(?<![a-z0-9_-])secret["']?\s*[:=]\s*["']?([A-Za-z0-9/+_=-]{16,})(?![\w(.\[])""")),
    # ``*_SECRET=``, ``*_TOKEN=``, ``client_secret=``, ``TOKEN=``, ``SUPABASE_SERVICE_ROLE_KEY=``, ``aws_secret_access_key=``.
    ("secret-assignment", re.compile(
        r"""(?ix)\b(?:(?:[a-z0-9]+[_-])+(?:secret|token)|token|[a-z0-9_]*service[_-]?role[_-]?key|
            [a-z0-9_]*aws[_-]?secret[_-]?access[_-]?key)["']?\s*[:=]\s*["']?([A-Za-z0-9/+_=-]{16,})(?![\w(.\[])""")),
    # ---- red-team round 3 additions. Each is appended (RULES[1] must stay the AWS id rule). Group 1 is the secret part.
    # a Supabase personal access token: ``sbp_`` + 40 hex
    ("supabase-pat", re.compile(r"\bsbp_[0-9a-f]{40}\b")),
    # a URL that carries a password: postgresql://user:pw@host, DATABASE_URL=..., redis://:pw@host (empty user). The password may itself contain ``@``, ``!``, ``#``.
    ("credentialed-url", re.compile(r"""\b[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@'"]*:([^/\s'"]{3,})@[^\s/@'"]+""")),
    # ``Authorization: Bearer <36+ chars>`` / ``bearer <token>`` (a header or a curl line); a template or placeholder is filtered
    ("bearer-token", re.compile(r"(?i)\bbearer[ \t]+([A-Za-z0-9._~+/=-]{36,})")),
    # a QUOTED value (any characters, so ``&!#.@`` are fine) assigned to password / passwd / passphrase / pwd / pw
    ("password-literal", re.compile(r"""(?ix)(?<![a-z0-9])(?:[a-z0-9_]{0,24}(?:password|passwd|passphrase)|pwd|pw)["']?\s*[:=]\s*["']([^"'\n]{6,})["']""")),
    # an UNQUOTED ``pw=`` / ``pwd=`` / ``password=`` / ``passphrase=`` value (libpq DSN, query string, env file)
    ("password-assignment", re.compile(r"""(?ix)(?<![a-z0-9])(?:[a-z0-9_]{0,24}(?:password|passwd|passphrase)|pwd|pw)\s*=\s*([^\s"'&;,()]{6,})(?![\w(.\[])""")),
)

#: Words a placeholder value may be made of. A value is a placeholder only when EVERY ``_ - / + =``-separated
#: token of it is one of these (or a run of x / 0 / a short number), so a real key that merely CONTAINS ``test``
#: or ``example`` somewhere is still a finding.
_PLACEHOLDER_WORDS = frozenset((
    "your", "my", "the", "a", "an", "api", "key", "keys", "secret", "token", "password", "goes", "here", "value",
    "example", "placeholder", "changeme", "dummy", "fake", "test", "testnet", "sample", "redacted", "replace", "me",
    "with", "real", "insert", "put", "for", "bearer", "service", "role", "xxx", "todo", "none", "null", "empty",
    "abcdef", "abcdefghijklmnop", "1234567890", "1234567890123456"))
_TOKEN_SPLIT = re.compile(r"[_/+=-]+")


def _is_placeholder(value: str) -> bool:
    tokens = [t for t in _TOKEN_SPLIT.split(value.lower()) if t]
    if not tokens:
        return True
    for t in tokens:
        if t in _PLACEHOLDER_WORDS or re.fullmatch(r"x+|0+|\d{1,3}", t):
            continue
        return False
    return True


#: Rules whose group 1 must additionally look like a literal secret: not a placeholder, and mixed letters and
#: digits (an identifier such as ``build_authorization_header`` is code, not a credential).
_STRICT_VALUE_RULES = frozenset(("secret-literal", "secret-assignment", "password-assignment"))
_ASSIGNMENT_RULES = frozenset(("credential-assignment", "secret-literal", "secret-assignment", "password-literal", "password-assignment"))
#: rules whose group 1 is judged by ``_credential_value`` (not a placeholder / template / code expression)
_CREDVALUE_RULES = frozenset(("credentialed-url", "bearer-token", "password-literal"))
_TEMPLATE = re.compile(r"^(?:\$\{?\w*\}?|<[^>]*>|\{\{.*\}\}|\{\w*\}|%\(?\w*\)?[sd]|\.\.\.|\*+|\w*\(\))$")


def _credential_value(rule: str, value: str) -> bool:
    """A password / URL password / bearer token that is a real-looking literal: not a template (``${PW}``, ``<password>``, ``{{ x }}``, ``%s``), not made only of placeholder words
    (the words may be separated by spaces or punctuation: "your password here"), and for a bearer token mixed letters/digits or mixed case."""
    v = value.strip()
    if not v or _TEMPLATE.match(v) or v.startswith(("$", "{{", "<")):
        return False
    words = [t for t in re.split(r"[^A-Za-z0-9]+", v.lower()) if t]
    if not words or all(w in _PLACEHOLDER_WORDS or w in ("user", "pass", "postgres", "localhost", "host", "pw", "pwd", "passwd", "secret", "passphrase", "pass", "phrase") or re.fullmatch(r"x+|0+|\d{1,3}|\*+", w) for w in words):
        return False
    if rule == "bearer-token":
        return (any(c.isdigit() for c in v) and any(c.isalpha() for c in v)) or (any(c.isupper() for c in v) and any(c.islower() for c in v))
    return True


def _looks_like_secret(value: str) -> bool:
    return (not _is_placeholder(value)) and any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


_AWS_SECRET_TOKEN = re.compile(r"(?<![A-Za-z0-9/+=_-])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=_-])")
_AWS_ID = RULES[1][1]
#: lines either side of an access-key id in which a bare 40-character secret is reported
_AWS_WINDOW = 2


def _is_aws_secret_shape(tok: str) -> bool:
    return (any(c.isupper() for c in tok) and any(c.islower() for c in tok) and any(c.isdigit() for c in tok)
            and not _is_placeholder(tok))


#: Findings reviewed by a human and known to be fixtures: phrase-shaped dummy values
#: handed to test doubles (``"super_secret_value_1"``), not credentials. Pinned by
#: file, rule AND count, so a NEW credential-shaped literal in the same file is
#: still reported. Never extend this to a real value; rotate the credential instead.
REVIEWED_FIXTURES = {
    # Reviewed when the scanner stopped treating a substring ("fake", "secret") as a placeholder: canary / fake-named test constants.
    ("bot/tests/test_monitor_self_heal.py", "secret-literal"): 1,
    ("bot/tests/fake_experiment_venue.py", "credential-assignment"): 2,
    ("bot/tests/test_close_testnet_experiment.py", "credential-assignment"): 2,
    ("bot/tests/test_memory.py", "credential-assignment"): 2,
    ("bot/tests/test_paper_agent_certification.py", "credential-assignment"): 1,
    ("bot/tests/test_owner_tools.py", "credential-assignment"): 1,
    ("bot/tests/test_project_status.py", "credential-assignment"): 1,
    # Reviewed in the economic-engine integration: the manifest names environment-variable NAMES (a mapping of "api_key" -> a variable name, never a value), and its test feeds
    # an obviously fake literal to prove that credential VALUES are never echoed. Count-pinned like the rest.
    ("bot/tools/testnet_action_manifest.py", "credential-assignment"): 1,
    ("bot/tests/test_testnet_action_manifest.py", "credential-assignment"): 1,
}

#: The content digest of each reviewed group (see ``group_digest``). Regenerate ONLY after a human has read the flagged lines again.
REVIEWED_DIGESTS = {
    ("bot/tests/test_monitor_self_heal.py", "secret-literal"): "5e1df44d2d5ad068835bfa6944eda8b5df6031922dd60c7a5c477f798b3d6ff4",
    ("bot/tests/fake_experiment_venue.py", "credential-assignment"): "aa6b0f47f5cb93dd9353e210cdaac595a97d185b15992fc5883720782d572ebc",
    ("bot/tests/test_close_testnet_experiment.py", "credential-assignment"): "613f434ab8805d7a3aab38fd7b5aad86db5b4b30762f3fc80e00e6562447251b",
    ("bot/tests/test_memory.py", "credential-assignment"): "5caa1bf4e449b05d20fb3f90f6be6ed78cb2c933d4f9e27f402c980521d6c172",
    ("bot/tests/test_owner_tools.py", "credential-assignment"): "8d4449ac924a67c46e5fc9dc174449d75ca554508cde81b52b3b73af83991a74",
    ("bot/tests/test_paper_agent_certification.py", "credential-assignment"): "527b14928e02a674446f1015bd92e1c6a688fa53d5a4f4ee58a28b610d4b90e1",
    ("bot/tests/test_project_status.py", "credential-assignment"): "2e26b54c73ad4c545245975a65aa611ebfca508c25421869e5921834294d32a9",
    ("bot/tests/test_testnet_action_manifest.py", "credential-assignment"): "fa5186263142bd8bc66d67a44d2083c2c14538fb9002f1b0357dda631d2088d3",
    ("bot/tools/testnet_action_manifest.py", "credential-assignment"): "8e33765a20632892946e42a21aac81b09bd75e4c895de12a5c53e4b56e90f07b",
}

#: Files that are binary or generated and carry no reviewable text.
SKIP_SUFFIXES = (".gz", ".so", ".png", ".jpg", ".pdf", ".zip", ".db", ".pyc")
MAX_BYTES = 2_000_000


def tracked_files(root: str) -> Optional[List[str]]:
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], check=True,
                             capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [os.path.join(root, p) for p in out.decode("utf-8", "replace").split("\0") if p]


#: sha256 of the FLAGGED LINE's text for the findings of the last scan, keyed (path, line, rule). Used only to bind a reviewed fixture to the exact content that was reviewed.
LINE_DIGESTS: dict = {}


def scan_text(text: str, _path: Optional[str] = None) -> List[Tuple[int, str]]:
    """`[(line_number, rule)]`. The matched text is never returned. Every line is scanned in full."""
    hits: List[Tuple[int, str]] = []
    lines = text.splitlines()

    def note(number: int, rule: str) -> None:
        if (number, rule) in hits:
            return
        hits.append((number, rule))
        if _path is not None:
            LINE_DIGESTS[(_path, number, rule)] = hashlib.sha256(lines[number - 1].strip().encode("utf-8")).hexdigest()

    for number, line in enumerate(lines, 1):
        assigned = False                        # a line already reported by a more specific rule is not reported again as a generic assignment
        for rule, pattern in RULES:
            if assigned and rule in _ASSIGNMENT_RULES:
                continue
            m = pattern.search(line)
            if not m:
                continue
            if rule == "credential-assignment" and _is_placeholder(m.group(1)):
                continue
            if rule in _CREDVALUE_RULES and not any(_credential_value(rule, x.group(1)) for x in pattern.finditer(line)):
                continue
            if rule in _STRICT_VALUE_RULES and not _looks_like_secret(m.group(1)):
                # the first match may be a decoy; look at the rest of the line for a real one
                if not any(_looks_like_secret(x.group(1)) for x in pattern.finditer(line)):
                    continue
            note(number, rule)
            assigned = True
    for i, line in enumerate(lines):
        if not _AWS_ID.search(line):
            continue
        for j in range(max(0, i - _AWS_WINDOW), min(len(lines), i + _AWS_WINDOW + 1)):
            if any(_is_aws_secret_shape(t) for t in _AWS_SECRET_TOKEN.findall(lines[j])):
                note(j + 1, "aws-secret-near-key-id")
    hits.sort()
    return hits


#: Files the last `scan_paths` could not read (too large / unreadable). A scan that skipped a
#: non-binary file did not look at everything, so `main` does not report it as a pass.
SKIPPED: List[str] = []


def scan_paths(paths: Iterable[str]) -> List[Tuple[str, int, str]]:
    found: List[Tuple[str, int, str]] = []
    SKIPPED.clear()
    LINE_DIGESTS.clear()
    for path in paths:
        if path.endswith(SKIP_SUFFIXES) or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > MAX_BYTES:
                SKIPPED.append(path)
                continue
            # errors="replace": ONE invalid byte must not exempt the whole file from the scan
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            SKIPPED.append(path)
            continue
        found.extend((path, n, rule) for n, rule in scan_text(text, path))
    return found


def group_digest(items: List[Tuple[str, int, str]]) -> str:
    """One digest over the flagged lines of a (file, rule) group, order-independent. A finding with no recorded line digest makes the group unreviewable."""
    parts = sorted(LINE_DIGESTS.get(i, "?") for i in items)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def apply_reviewed(found: List[Tuple[str, int, str]], root: str) -> List[Tuple[str, int, str]]:
    """Drop findings that exactly match a reviewed fixture entry: same file, same rule, same COUNT and the same CONTENT (a digest of the flagged lines). Replacing a reviewed fake
    with a different literal, real or not, changes the digest and the finding is reported again."""
    grouped: dict = {}
    for item in found:
        grouped.setdefault((os.path.relpath(item[0], root), item[2]), []).append(item)
    kept: List[Tuple[str, int, str]] = []
    for key, items in grouped.items():
        if REVIEWED_FIXTURES.get(key) == len(items) and REVIEWED_DIGESTS.get(key) == group_digest(items):
            continue
        kept.extend(items)
    return kept


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = args or tracked_files(root)
    if paths is None:
        print("could not list tracked files (is this a git checkout?)", file=sys.stderr)
        return 2
    found = apply_reviewed(scan_paths(paths), root)
    for path, line, rule in found:
        print(f"{os.path.relpath(path, root)}:{line}: {rule}")
    for path in SKIPPED:
        print(f"{os.path.relpath(path, root)}: NOT SCANNED (unreadable or over {MAX_BYTES} bytes)")
    print(f"secret scan: {len(found)} finding(s) in {len(list(paths))} file(s); "
          f"{len(SKIPPED)} not scanned (unreadable or over {MAX_BYTES} bytes; known binary suffixes are never read)")
    if found:
        return 1
    return 3 if SKIPPED else 0


if __name__ == "__main__":
    raise SystemExit(main())
