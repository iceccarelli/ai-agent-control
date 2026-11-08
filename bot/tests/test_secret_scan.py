"""The secret scanner finds credential shapes, never prints them, and the repository is clean."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import secret_scan as SS  # noqa: E402

FAKE_AWS = "AKIA" + "Q7ZK3MWX9PLR2VNB"        # assembled so this file is not itself a finding
FAKE_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_it_finds_each_credential_shape_and_returns_only_rule_names():
    text = "\n".join([
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        f"aws = '{FAKE_AWS}'",
        f"token: {FAKE_GH}",
        'BYBIT_API_SECRET = "' + "q8Zr4TnLw2" + 'VxK9mB5cJd7"',
    ])
    rules = [r for _, r in SS.scan_text(text)]
    assert rules == ["private-key-block", "aws-access-key-id", "github-token", "credential-assignment"]


def test_the_report_never_contains_the_matched_text(tmp_path, capsys):
    f = tmp_path / "x.py"
    f.write_text(f'KEY = "{FAKE_AWS}"\n')
    SS.main([str(f)])
    out = capsys.readouterr().out
    assert FAKE_AWS not in out and "aws-access-key-id" in out


def test_placeholders_and_digests_are_not_findings():
    text = 'BYBIT_API_KEY = "your_api_key_goes_here_xxxx"\nsha = "' + "a" * 64 + '"\nAPI_KEY="..."\n'
    assert SS.scan_text(text) == []


def test_the_tracked_tree_is_clean():
    root = os.path.dirname(os.path.dirname(HERE))
    files = SS.tracked_files(root)
    if files is None:
        import pytest
        pytest.skip("not a git checkout")
    found = SS.apply_reviewed(SS.scan_paths(files), root)
    assert found == [], [f"{os.path.relpath(p, root)}:{n}: {r}" for p, n, r in found]


def test_a_new_literal_next_to_a_reviewed_fixture_is_still_reported(tmp_path):
    root = str(tmp_path)
    p = os.path.join(root, "bot/tests/test_memory.py")
    os.makedirs(os.path.dirname(p))
    lines = ['API_KEY = "' + "kQ7zWm3x" + 'Lp9vRn2b"'] * 3
    with open(p, "w") as fh:
        fh.write("\n".join(lines))
    found = SS.apply_reviewed(SS.scan_paths([p]), root)
    assert len(found) == 3          # reviewed count is 2, so the whole file is reported


def test_testnet_named_unquoted_and_yaml_credentials_are_found_but_placeholders_are_not():
    v = "kQ7zWm3x" + "Lp9vRn2bTy"
    for text in (f'BYBIT_TESTNET_API_KEY = "{v}"', f"BYBIT_API_KEY={v}", f"api_key: {v}",
                 f'"api_secret": "{v}"'):
        assert [r for _, r in SS.scan_text(text)] == ["credential-assignment"], text
    assert SS.scan_text('API_KEY="your_api_key_goes_here_xxxx"') == []


def test_a_clean_result_says_how_much_it_did_not_scan(tmp_path, capsys):
    f = tmp_path / "big.txt"
    f.write_bytes(b"a" * (SS.MAX_BYTES + 1))
    SS.main([str(f)])
    assert "1 not scanned" in capsys.readouterr().out


def test_a_reviewed_fixture_is_bound_to_its_CONTENT_not_only_its_count(tmp_path, monkeypatch):
    """Red team: the allowlist was keyed on (file, rule, count), so swapping the one reviewed fake literal for a real credential kept the count at 1 and the scan stayed green.
    (The literals are assembled at run time so this test file does not itself contain a credential-shaped assignment.)"""
    reviewed_value = "Kq8Zw3Rt7Yp2Lm9X" + "c4Vb6Nh1Jd5Wa7Ge"
    swapped_value = "Zk8Qw3Rt7Yp2Lm9X" + "c4Vb6Nh1Jd5Sa0Gf"
    f = tmp_path / "bot" / "tests" / "fx.py"; f.parent.mkdir(parents=True)
    f.write_text(f'API_KEY = "{reviewed_value}"\n')
    key = ("bot/tests/fx.py", "credential-assignment")
    found = SS.scan_paths([str(f)])
    assert [r for _p, _n, r in found] == ["credential-assignment"]
    monkeypatch.setitem(SS.REVIEWED_FIXTURES, key, 1)
    monkeypatch.setitem(SS.REVIEWED_DIGESTS, key, SS.group_digest(found))
    assert SS.apply_reviewed(SS.scan_paths([str(f)]), str(tmp_path)) == []                       # the reviewed content is accepted (positive control)
    f.write_text(f'API_KEY = "{swapped_value}"\n')                                              # the same count, DIFFERENT content
    again = SS.apply_reviewed(SS.scan_paths([str(f)]), str(tmp_path))
    assert [r for _p, _n, r in again] == ["credential-assignment"]
    monkeypatch.delitem(SS.REVIEWED_DIGESTS, key)                                                # a count with no content digest reviews nothing
    f.write_text(f'API_KEY = "{reviewed_value}"\n')
    assert SS.apply_reviewed(SS.scan_paths([str(f)]), str(tmp_path))


# ---------------------------------------------------------------------------
# red-team round 2: bypasses of the scanner itself. Every literal is assembled at run time so this
# file is not itself a finding.
# ---------------------------------------------------------------------------
R = "Zk3Qm9Xp2LdV7nBt5RwY8cHs1JfG4aEu"      # synthetic, random-looking, not a key


def _rules(text):
    return [r for _, r in SS.scan_text(text)]


def test_non_utf8_text_is_still_scanned_and_a_unreadable_file_fails_the_scan(tmp_path, capsys):
    f = tmp_path / "leak.txt"
    f.write_bytes(b"AWS=" + FAKE_AWS.encode() + b"\n\xff\n")          # ONE invalid byte used to skip the whole file
    assert [r for _p, _n, r in SS.scan_paths([str(f)])] == ["aws-access-key-id"]
    assert SS.main([str(f)]) == 1
    capsys.readouterr()
    # positive control: the same file without the bad byte and without a key is clean and exits 0
    g = tmp_path / "clean.txt"
    g.write_bytes(b"nothing here\n\xff\n")
    assert SS.main([str(g)]) == 0
    capsys.readouterr()
    # a non-binary file that cannot be scanned (here: over the size cap) must NOT read as a pass
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * (SS.MAX_BYTES + 1))
    assert SS.main([str(big)]) != 0
    assert "NOT SCANNED" in capsys.readouterr().out


def test_a_credential_after_column_2000_is_found():
    assert _rules((" " * 2100) + FAKE_AWS) == ["aws-access-key-id"]
    assert _rules(("a" * 5000)) == []                       # positive control: a long clean line stays clean


def test_long_pathological_lines_scan_in_bounded_time():
    import time
    t = time.time()
    SS.scan_text("api_key" * 20000 + "\n" + "A" * 200000 + "\n" + "=" * 100000)
    assert time.time() - t < 5


def test_new_credential_shapes_are_found():
    cases = {
        "sb-secret": "SUPABASE_KEY=sb_secret_" + R,
        "slack-webhook": "u=https://hooks.slack.com/services/T01ABCDEF/B01ABCDEFG/" + R[:24],
        "stripe-live-key": "STRIPE=sk_live_" + R,
        "stripe-live-key": "STRIPE=rk_live_" + R,
        "secret-literal": '{"secret": "%s"}' % R,
        "client-secret": "client_secret = '%s'" % R,
        "secret-assignment": "BYBIT_SECRET=" + R,
        "secret-assignment2": "SUPABASE_SERVICE_ROLE_KEY=" + R,
        "secret-assignment3": "TOKEN=" + R,
        "secret-assignment4": "export SLACK_BOT_TOKEN=" + R,
    }
    for name, text in cases.items():
        assert _rules(text), name


def test_aws_secret_key_next_to_an_access_key_id_is_found_but_a_lone_40_char_token_is_not():
    secret40 = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYq9Zk3Qm9X1"
    assert len(secret40) == 40
    text = f"aws_access_key_id = {FAKE_AWS}\naws_secret_access_key = {secret40}\n"
    assert "aws-secret-near-key-id" in _rules(text)
    bare = f"id = {FAKE_AWS}\n{secret40}\n"                    # no name on the secret line at all
    assert "aws-secret-near-key-id" in _rules(bare)
    assert "aws-secret-near-key-id" not in _rules(secret40 + "\n")        # not next to a key id: indistinguishable from a hash
    sha1 = "a" * 40
    assert "aws-secret-near-key-id" not in _rules(f"id = {FAKE_AWS}\n{sha1}\n")   # a hex digest is not a secret


def test_code_that_merely_names_a_secret_is_not_a_finding():
    for text in ('token = build_authorization_header(request)', "BYBIT_SECRET = os.environ['BYBIT_SECRET']",
                 'client_secret = "<your client secret>"', 'secret = "..."', "TOKEN=${{ secrets_placeholder }}",
                 "SUPABASE_SERVICE_ROLE_KEY=your_service_role_key_xxxx", "SLACK_BOT_TOKEN=xoxb-your-token-here"):
        assert _rules(text) == [], text


def test_the_placeholder_filter_matches_the_WHOLE_value_not_a_substring():
    # a real-looking value that merely CONTAINS a placeholder word used to be waved through
    for word in ("test", "fake", "example", "dummy", "sample", "abcdef", "xxxx"):
        v = R[:10] + word + R[10:24]
        assert _rules(f"API_KEY={v}") == ["credential-assignment"], word
        assert _rules(f"SERVICE_TOKEN={v}") == ["secret-assignment"], word
    # positive control: values that are ONLY placeholder words are still ignored
    for v in ("your_api_key_goes_here_xxxx", "changeme_changeme", "example_example_value", "xxxxxxxxxxxxxxxx",
              "dummy-dummy-dummy-dummy", "test_test_test_test"):
        assert _rules(f"API_KEY={v}") == [], v


def test_rule_set_has_a_mutation_guard():
    names = {n for n, _ in SS.RULES}
    for needed in ("sb-secret-key", "slack-webhook", "stripe-live-key", "secret-literal", "secret-assignment"):
        assert needed in names, needed


# ---- red-team round 3 (rt3-7): shapes the scanner missed. Every literal is assembled at run time so this file is not itself a finding.

def _rt3_cases():
    pw = "Hx9" + "!q#Lm.2" + "&v@Zt"                       # a password with & ! # . @ in it
    return {
        "database_url": ("DATABASE_" + "URL=post" + "gresql://app" + ":" + pw + "@db" + ".example" + ".com:5432/carry", "credentialed-url"),
        "postgres_plain": ("dsn = 'post" + "gresql://svc" + ":hunter2" + "pw@10.0.0.5/db'", "credentialed-url"),
        "redis_empty_user": ("REDIS_" + "URL=red" + "is://" + ":s3cr3t" + "Pw9@cache.internal:6379/0", "credentialed-url"),
        "password_special_chars": ('db_pass' + 'word = "' + pw + '"', "password-literal"),
        "password_json": ('{"pass' + 'word": "' + pw + '"}', "password-literal"),
        "passphrase_quoted": ('pass' + 'phrase: "' + "correct horse " + "battery9" + '"', "password-literal"),
        "pw_quoted": ("pw = '" + "Tr0ub4dor" + "&3'", "password-literal"),
        "pwd_unquoted": ("host=db user=app " + "pw" + "d=" + "Tr0ub4dor3x", "password-assignment"),
        "pw_unquoted": ("conn?" + "pw" + "=" + "Tr0ub4dor3x&ssl=1", "password-assignment"),
        "bearer_header": ("Author" + "ization: Bear" + "er " + "kQ7zWm3xLp9vRn2bTy5Hc8Jd4Fg6Aw1Se0Uq", "bearer-token"),
        "supabase_pat": ("SUPABASE_ACCESS_" + "TOKEN=sb" + "p_" + "0123456789abcdef" * 2 + "01234567", "supabase-pat"),
        "pgp_block": ("-----BEGIN " + "PGP PRIVATE KEY" + " BLOCK-----", "private-key-block"),
        "openssh_block": ("-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----", "private-key-block"),
        "encrypted_block": ("-----BEGIN " + "ENCRYPTED PRIVATE" + " KEY-----", "private-key-block"),
    }


def test_rt3_7_each_missed_shape_is_found_by_a_named_rule():
    for name, (text, rule) in _rt3_cases().items():
        assert rule in [r for _, r in SS.scan_text(text)], name


def test_rt3_7_the_report_for_these_shapes_never_prints_the_value(tmp_path, capsys):
    f = tmp_path / "x.txt"
    f.write_text("\n".join(t for t, _ in _rt3_cases().values()) + "\n")
    assert SS.main([str(f)]) == 1
    out = capsys.readouterr().out
    for t, _ in _rt3_cases().values():
        assert t not in out
    for frag in ("hunter2", "Tr0ub4dor", "kQ7zWm3x", "s3cr3t", "0123456789abcdef"):
        assert frag not in out


def test_rt3_7_positive_controls_templates_placeholders_and_code_are_not_findings():
    for text in ('DATABASE_URL=postgresql://user:password@localhost:5432/db', 'url = "postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}/db"',
                 "redis://:<password>@host:6379", 'password = "<your password>"', 'password = "changeme"', 'passphrase: "your passphrase here"',
                 'password = os.environ["PW"]', 'pw = _open01(power, "power")', "PWD=$(pwd)", "pwd=${PWD}", 'password = "{{ vault.pw }}"',
                 'Authorization: Bearer ${TOKEN}', 'headers = {"Authorization": "Bearer " + token}', "bearer = 'Bearer'", "Bearer abcdef",
                 "-----BEGIN PUBLIC KEY-----", 'sbp_ prefix', "https://example.com/path:80@x", "postgresql://localhost:5432/db", 'password = ""'):
        assert SS.scan_text(text) == [], text


def test_rt3_7_a_short_or_prose_password_literal_is_not_over_matched():
    # quoted values under six characters and ordinary prose are not credentials (the rule is a net, not a sieve)
    assert SS.scan_text('password = "abc"') == [] and SS.scan_text("the password is stored in vault") == []


def test_rt3_7_a_decoy_first_match_does_not_hide_a_later_real_one():
    text = 'url = "postgresql://user:password@localhost/x"; real = "post' + 'gresql://app' + ':kQ7zWm3xLp9v' + '@db.example.com/x"'
    assert "credentialed-url" in [r for _, r in SS.scan_text(text)]


def test_rt3_7_mutation_each_new_rule_is_load_bearing():
    import re as _re
    for rule in ("supabase-pat", "credentialed-url", "bearer-token", "password-literal", "password-assignment", "private-key-block"):
        probes = [t for t, r in _rt3_cases().values() if r == rule]
        assert probes, rule
        saved = SS.RULES
        try:
            SS.RULES = tuple((n, (_re.compile(r"(?!x)x") if n == rule else rx)) for n, rx in saved)
            missed = [t for t in probes if rule in [r for _, r in SS.scan_text(t)]]
        finally:
            SS.RULES = saved
        assert missed == [], rule
        assert all(rule in [r for _, r in SS.scan_text(t)] for t in probes), rule      # restored


def test_rt3_7_new_rules_scan_pathological_lines_in_bounded_time():
    import time
    t = time.time()
    lines = [("pass" + "word='") * 20000, ("p" + "w=") * 30000, "a://b:" * 30000, "a://:" * 30000 + "@", "bearer " + "a" * 300000, "x_" * 100000 + "p" + "w="]
    SS.scan_text("\n".join(lines))
    assert time.time() - t < 5


# ---- red-team round 4 (rt4-5): attestation / signing / HMAC / private / encryption key assignments and OWNER_ATTESTATION_KEY by name. Literals are assembled at run time.

def _rt4_cases():
    v = "Zk3Jd9QpL2m" + "N8xVb7YcR4tWe6H"
    hexv = "c0ffee12" + "34567890abcdef12"
    k = "OWNER_ATTEST" + "ATION_KEY"
    return {
        "shell_export": ("export " + k + "='" + hexv + "'", "owner-attestation-key"),
        "env_line": (k + "=" + hexv, "owner-attestation-key"),
        "env_letters_only": (k + "=" + "abcdefghijklmnopqrstuvwxyz", "owner-attestation-key"),
        "python_environ": ('os.environ["' + k + '"] = "' + v + '"', "owner-attestation-key"),
        "python_assign": ("owner_attest" + "_key = '" + v + "'", "owner-attestation-key"),
        "hmac_python": ("hmac_" + "key = \"" + v + "\"", "key-material-assignment"),
        "signing_yaml": ("signing_" + "key: " + v, "key-material-assignment"),
        "signing_json": ('{"signing_' + 'key": "' + v + '"}', "key-material-assignment"),
        "private_assign": ("private_" + "key = '" + v + "'", "key-material-assignment"),
        "encryption_env": ("encryption_" + "key=" + v, "key-material-assignment"),
        "attestation_env": ("ATTESTATION_" + "KEY=" + v, "key-material-assignment"),
        "camel": ("const signing" + "Key = '" + v + "'", "key-material-assignment"),
        "prefixed_name": ("MY_APP_HMAC_" + "KEY=" + v, "key-material-assignment"),
        "fly_secrets": ("fly secrets set " + k + "=" + hexv, "owner-attestation-key"),
    }


def test_rt4_5_each_key_assignment_shape_is_found_by_a_named_rule():
    for name, (text, rule) in _rt4_cases().items():
        assert rule in [r for _, r in SS.scan_text(text)], name


def test_rt4_5_the_report_never_prints_the_value(tmp_path, capsys):
    f = tmp_path / "x.env"
    f.write_text("\n".join(t for t, _ in _rt4_cases().values()) + "\n")
    rc = SS.main([str(f)])
    out = capsys.readouterr().out
    assert rc == 1 and "key-material-assignment" in out and "owner-attestation-key" in out
    for frag in ("Zk3Jd9QpL2m", "c0ffee12", "abcdefghijklmnopqrstuvwxyz"):
        assert frag not in out


def test_rt4_5_positive_controls_names_code_templates_and_placeholders_are_not_findings():
    k = "OWNER_ATTEST" + "ATION_KEY"
    for text in ('key = os.environ.get("' + k + '")', "signing_" + "key = load_signing_key()", "hmac_" + "key = None", k + "=<your key>", "signing_" + "key: ${SIGNING_KEY}",
                 'export ' + k + '="$KEY"', k + "=your_key_here", "private_" + "key = self._derive_private_key_bytes", "def hmac_" + "key(self):", "# rotate the " + k + " per the runbook",
                 "encryption_" + "key = 'changeme'", "signing_" + "key_path = '/etc/x'", "hmac_" + "key = b''"):
        assert SS.scan_text(text) == [], text


def test_rt4_5_mutation_each_new_rule_is_load_bearing():
    import re as _re
    for rule in ("key-material-assignment", "owner-attestation-key"):
        probes = [t for t, r in _rt4_cases().values() if r == rule]
        assert probes and all(rule in [r for _, r in SS.scan_text(t)] for t in probes), rule
        saved = SS.RULES
        try:
            SS.RULES = tuple((n, (_re.compile(r"(?!x)x") if n == rule else rx)) for n, rx in saved)
            assert not any(rule in [r for _, r in SS.scan_text(t)] for t in probes), rule
        finally:
            SS.RULES = saved
    assert SS.RULES[1][0] == "aws-access-key-id"


def test_rt4_5_new_rules_scan_pathological_lines_in_bounded_time():
    import time
    t = time.time()
    SS.scan_text("\n".join(["hmac_key" * 40000, "owner_attest" * 40000, "signing_key=" * 40000, "private_" * 100000 + "key", "a" * 300000 + "_key=" + "b" * 300000]))
    assert time.time() - t < 5
