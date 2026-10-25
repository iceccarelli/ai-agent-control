"""The runner verifier counts machines from the table `fly` really prints.

The fixtures are the operator's captured output from the first deploy (app
carry-drill, 2026-10-25), verbatim apart from trailing spaces. They reproduce the
defect: the pasted deploy script counted machines with `grep -cE '^[0-9a-f]{14}'`,
the rows start with a space, and a healthy app read as 0 machines.

Offline: a fake `fly` on PATH serves the captured text. Nothing here deploys,
starts, stops or orders anything.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
ROOT = os.path.dirname(BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import fly_machines as fm  # noqa: E402

MACHINE_LIST = (
    "1 machines have been retrieved from app carry-drill.\n"
    "View them in the UI here (https://fly.io/apps/carry-drill/machines/)\n"
    "\n"
    "carry-drill\n"
    " ID             │ NAME                  │ STATE   │ CHECKS │ REGION │ ROLE │ IMAGE                                             │ IP ADDRESS                      │ VOLUME               │ CREATED              │ LAST UPDATED         │ PROCESS GROUP │SIZE                \n"
    " d894911a069d58 │ throbbing-forest-9950 │ started │        │ fra    │      │ carry-drill:deployment-01M5SW92RJFV6RJVHG311X8S39 │ fdaa:c5:7da:a7b:5a8:f08f:80c5:2 │ vol_vz807ed2loyjy9zv │ 2026-10-25T15:54:47Z │ 2026-10-25T15:54:55Z │ app           │shared-cpu-1x:512MB \n"
)

VOLUMES_ONE = (
    " ID                   │ STATE   │ NAME       │ SIZE │ REGION │ ZONE │ ENCRYPTED │ ATTACHED VM │ CREATED AT     \n"
    " vol_vz807ed2loyjy9zv │ created │ drill_runs │ 1GB  │ fra    │ 2585 │ true      │             │ 19 minutes ago \n"
)
VOLUMES_TWO = VOLUMES_ONE + (
    " vol_vlyx82e0d2l3x8d4 │ created │ drill_runs │ 1GB  │ fra    │ 8e33 │ true      │             │ 18 minutes ago \n"
)

OLD_CHECK = re.compile(r"^[0-9a-f]{14}")          # the check that failed


def old_count(text):
    return sum(1 for line in text.splitlines() if OLD_CHECK.match(line))


def test_the_old_check_really_reports_zero_machines_on_the_captured_table():
    assert old_count(MACHINE_LIST) == 0              # the false FAIL, reproduced
    assert len(MACHINE_LIST.splitlines()) > 1


def test_the_parser_counts_the_one_machine():
    rows = fm.parse_table(MACHINE_LIST)
    assert [r["ID"] for r in rows] == ["d894911a069d58"]
    assert rows[0]["STATE"] == "started" and rows[0]["REGION"] == "fra"


def test_two_machines_are_counted_as_two():
    two = MACHINE_LIST + MACHINE_LIST.splitlines()[4].replace(
        "d894911a069d58", "e286e66b4d1128") + "\n"
    assert len(fm.parse_table(two)) == 2


def test_no_table_is_zero_rows_not_a_guess():
    for text in ("", "Error: app carry-drill has no started VMs.\n",
                 "1 machines have been retrieved from app carry-drill.\n",
                 "d894911a069d58 started\n"):
        assert fm.parse_table(text) == []


def test_a_truncated_row_is_not_a_machine():
    cut = MACHINE_LIST.rsplit("│", 3)[0] + "\n"
    assert fm.parse_table(cut) == []


def test_volume_filter_counts_by_name():
    assert len(fm.parse_table(VOLUMES_ONE)) == 1
    assert len(fm.parse_table(VOLUMES_TWO)) == 2
    assert len([r for r in fm.parse_table(VOLUMES_TWO)
                if r["NAME"] == "drill_runs"]) == 2


# -- the script, end to end, against a fake fly -----------------------------

def _fake_fly(tmp_path, *, machines, volumes, secrets="BYBIT_API_KEY\nBYBIT_API_SECRET\n",
              reach="HTTP 200"):
    fly = tmp_path / "fly"
    (tmp_path / "machines.txt").write_text(machines)
    (tmp_path / "volumes.txt").write_text(volumes)
    (tmp_path / "secrets.txt").write_text(secrets)
    fly.write_text(f"""#!/usr/bin/env bash
case "$1 $2" in
  "machine list") cat {tmp_path}/machines.txt ;;
  "volumes list") cat {tmp_path}/volumes.txt ;;
  "secrets list") cat {tmp_path}/secrets.txt ;;
  "ssh console") case "$*" in *market/time*) echo "{reach}" ;; *) echo "/data/runs/run-1" ;; esac ;;
  *) echo "unexpected fly call: $*" >&2; exit 99 ;;
esac
""")
    fly.chmod(fly.stat().st_mode | stat.S_IEXEC)
    return str(fly)


def _run(tmp_path, **kw):
    fly = _fake_fly(tmp_path, **kw)
    return subprocess.run(
        ["bash", os.path.join(ROOT, "scripts", "verify_drill_runner.sh")],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "FLY": fly})


def test_the_script_passes_on_the_captured_healthy_state(tmp_path):
    r = _run(tmp_path, machines=MACHINE_LIST, volumes=VOLUMES_ONE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "machines: 1" in r.stdout and "OK:" in r.stdout


def test_the_script_fails_on_a_second_volume(tmp_path):
    r = _run(tmp_path, machines=MACHINE_LIST, volumes=VOLUMES_TWO)
    assert r.returncode == 1 and "found 2" in r.stdout


def test_the_script_fails_when_no_machine_listed(tmp_path):
    r = _run(tmp_path, machines="No machines found\n", volumes=VOLUMES_ONE)
    assert r.returncode == 1 and "found 0" in r.stdout


def test_the_script_fails_when_the_machine_is_stopped(tmp_path):
    r = _run(tmp_path, machines=MACHINE_LIST.replace("started", "stopped"),
             volumes=VOLUMES_ONE)
    assert r.returncode == 1 and "not started" in r.stdout


def test_the_script_fails_when_the_testnet_is_blocked(tmp_path):
    r = _run(tmp_path, machines=MACHINE_LIST, volumes=VOLUMES_ONE,
             reach="HTTP 403")
    assert r.returncode == 1 and "not reachable" in r.stdout


def test_the_script_fails_when_a_secret_name_is_missing(tmp_path):
    r = _run(tmp_path, machines=MACHINE_LIST, volumes=VOLUMES_ONE,
             secrets="BYBIT_API_KEY\n")
    assert r.returncode == 1 and "BYBIT_API_SECRET is not set" in r.stdout


def test_the_script_is_read_only():
    text = open(os.path.join(ROOT, "scripts", "verify_drill_runner.sh"),
                encoding="utf-8").read()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for verb in ("deploy", "launch", "destroy", "machine stop", "machine start",
                 "machine restart", "secrets set", "volumes create",
                 "touch ", "rm ", "kill"):
        assert verb not in code, verb
