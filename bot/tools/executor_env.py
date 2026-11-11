#!/usr/bin/env python3
"""The ONE place the single-writer guard's inputs are read from the process environment, so no other tool has to read it.

The repository's rule is that ``config.load()`` is the only environment reader (two structural tests pin it). The single-writer authority (``legacy_executor_guard`` / ``ms_executor_lock``) needs a few
NAMED, non-exchange variables at a command-line entry: where the shared Supabase lease lives (``SUPABASE_URL`` / ``SUPABASE_SERVICE_ROLE_KEY`` / ``SUPABASE_OWNER_ID``), which machine this is
(``FLY_MACHINE_ID``, for the lease holder name) and where the local account lock is (``MS_EXECUTOR_LOCK_PATH``). This module hands exactly those, as a plain dict, to the command-line entry that
asks; nothing else is copied, nothing is written, nothing is logged, and no exchange credential can pass through it. It is listed in ``tests/test_environment_read_surface.py`` with this reason."""
from __future__ import annotations

import os
from typing import Dict

KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_OWNER_ID", "FLY_MACHINE_ID", "MS_EXECUTOR_LOCK_PATH")


def guard_env() -> Dict[str, str]:
    source = os.environ
    return {k: source[k] for k in KEYS if k in source}
