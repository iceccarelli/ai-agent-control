#!/usr/bin/env python3
"""Read-only: which owner policy inputs are set, from the environment you run it in.

Run it where the settings live (for the Fly app:
`flyctl ssh console -C "python3 tools/owner_policy_check.py"`). It loads the
same configuration the bot loads, prints ONLY the policy fields (no key, secret
or other setting), and exits 0 when the evidence inputs are complete and usable,
3 when anything is missing or unusable. It contacts no venue, opens no database
and suggests no value: the discovery window and the drawdown limit are the
owner's to choose.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config as _config  # noqa: E402
import owner_policy  # noqa: E402


def main(argv=None, env=None) -> int:
    try:
        cfg = _config.load(dict(os.environ if env is None else env))
    except _config.ConfigError as exc:
        print(json.dumps({"loaded": False, "error": str(exc)}))
        return 2
    status = owner_policy.owner_policy_status(cfg)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status["evidence_can_qualify"] else 3


if __name__ == "__main__":
    sys.exit(main())
