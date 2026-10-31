#!/usr/bin/env python3
"""Read-only: which owner policy inputs are set, from the environment you run it in.

Run it where the settings live (for the Fly app:
`flyctl ssh console -C "python3 tools/owner_policy_check.py"`). It loads the
same settings the bot reads, parses ONLY the owner's policy settings (a bad
unrelated setting cannot hide them, and no key, secret or other setting is read
or printed), and exits 0 when the evidence inputs are complete and usable, 3
when anything is missing or unusable. It contacts no venue, opens no database
and suggests no value. Complete inputs are necessary, not sufficient: under
'enforce' entries stay refused until real execution samples and reconciled
cycles also exist (see execution_samples_report): the discovery window and the drawdown limit are the
owner's to choose.
"""
from __future__ import annotations

import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config as _config  # noqa: E402
import owner_policy  # noqa: E402


_FIELDS = (("CARRY_EVIDENCE_DISCOVERY_START_EPOCH", {}),
           ("CARRY_EVIDENCE_DISCOVERY_END_EPOCH", {}),
           ("CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD", {"positive": True}))


def read_policy(env):
    """The owner's inputs, parsed on their own: a bad UNRELATED setting cannot
    hide them and nothing but these names is read. A malformed owner setting is
    reported by NAME only (its value is never echoed)."""
    values, bad = {}, []
    for name, kw in _FIELDS:
        try:
            values[name] = _config.parse_optional_float(env, name, **kw)
        except _config.ConfigError:
            values[name] = None
            bad.append(name)
    values["CARRY_ENTRY_BENEFIT_MODE"] = str(
        env.get("CARRY_ENTRY_BENEFIT_MODE", "enforce")).strip().lower() or "enforce"
    values["CARRY_INCOME_HAIRCUT_FRACTION"] = None
    return types.SimpleNamespace(**values), bad


def main(argv=None, env=None) -> int:
    env = dict(os.environ if env is None else env)
    cfg, bad = read_policy(env)
    status = owner_policy.owner_policy_status(cfg)
    if bad:
        status["unparseable_settings"] = bad
        for name in bad:
            if name not in status["missing_or_unusable"]:
                status["missing_or_unusable"].append(name)
        status["policy_inputs_complete"] = False
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status["policy_inputs_complete"] else 3


if __name__ == "__main__":
    sys.exit(main())
