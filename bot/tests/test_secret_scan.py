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
