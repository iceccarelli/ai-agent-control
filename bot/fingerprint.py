"""What exactly is running: a digest of the code, the venue/mode, and where it came from.

Pure standard library. It reads the source files next to it and nothing else, and it
carries no secret: only booleans, the venue name and digests. The point is that an
operator (or `fly status` plus `/health`) can compare what is RUNNING with what was
reviewed, so a drifted image, a wrong venue or an unexpected mode shows up as a
difference of a few strings instead of being discovered after a trade.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional

_ROOT = os.path.dirname(os.path.abspath(__file__))
_cache: Dict[str, str] = {}


def code_digest(root: Optional[str] = None) -> str:
    """sha256 over the sorted (name, sha256(bytes)) of every top-level ``*.py`` in the
    application directory. Computed once per process per root."""
    root = root or _ROOT
    if root in _cache:
        return _cache[root]
    outer = hashlib.sha256()
    for name in sorted(n for n in os.listdir(root) if n.endswith(".py")):
        with open(os.path.join(root, name), "rb") as handle:
            outer.update(name.encode() + b"\0" + hashlib.sha256(handle.read()).digest())
    _cache[root] = outer.hexdigest()
    return _cache[root]


#: The only config fields that go into the fingerprint. No credential-shaped name
#: belongs here and a test pins that.
SAFE_FIELDS = ("BYBIT_VENUE", "USE_TESTNET", "PAPER_TRADING", "BOOK_MODE", "CARRY_EXECUTION_MODE",
               "CARRY_EXECUTION_STYLE", "CARRY_ENTRY_BENEFIT_MODE", "MARKET_FEED_ENABLED",
               # the economics the entry and exit decide on: the funding exit in force, and whether the
               # owner STATED a cost of capital (a bare 0.0 is not a statement)
               "CARRY_EXIT_POLICY", "CARRY_OPPORTUNITY_COST_APR", "CARRY_OPPORTUNITY_COST_APR_SUPPLIED")


def config_summary(cfg: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in SAFE_FIELDS:
        value = getattr(cfg, name, None)
        out[name] = value if isinstance(value, (bool, int, float, str)) or value is None else str(type(value).__name__)
    return out


def runtime_fingerprint(cfg: Any, root: Optional[str] = None) -> Dict[str, Any]:
    summary = config_summary(cfg)
    commit = getattr(cfg, "BUILD_COMMIT", "") or ""
    image = getattr(cfg, "FLY_IMAGE_REF", "") or ""
    try:
        digest = code_digest(root)
    except OSError:
        digest = "unavailable"          # provenance must never make /health fail
    return {
        "code_sha256": digest,
        "config": summary,
        "config_sha256": hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest(),
        "build_commit": commit if isinstance(commit, str) and commit else "unknown",
        "image_ref": image if isinstance(image, str) and image else "unknown",
    }
