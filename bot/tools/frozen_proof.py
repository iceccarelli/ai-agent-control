#!/usr/bin/env python3
"""Prove the frozen strategy and its freeze artefacts are byte-identical to what was frozen.

    python3 tools/frozen_proof.py           # prints a JSON verdict, exit 0 only if all match

READ-ONLY and offline. It compares SHA-256 digests of the frozen source and the
freeze/stage-1 artefacts against (a) the digests pinned in this file and (b) the newest
``artifacts/slice*_sha256_manifest.txt`` that names them. A digest here changes only
when a human decides the frozen rule has legitimately changed, which for a frozen rule
means: never. This tool does not judge whether the rule makes money; that is the job
of out-of-sample evidence, and nothing here supplies it.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
from typing import Dict, List, Optional

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: path (relative to bot/) -> sha256, as frozen. Independent of every manifest.
PINNED: Dict[str, str] = {
    "signals/funding_carry_fade_v1.py":
        "73a52d456d3cdec6f3130b3e0894eb9567e6e75f1fe7822f62cc35d5c76d345e",
    "artifacts/slice56_funding_carry_fade_freeze.json":
        "7eadd56f46b6c87bc52916ac72131af6e4cfc41aa530733a6ea120899694855c",
    "artifacts/slice55_stage1_funding_carry_fade_v1.json":
        "d923923d8de3b88710297ff5f986733cdfe719ad69333510e445bba8faa4938f",
}

_ROW = re.compile(r"^([0-9a-f]{64})\s+(?:\*)?(?:bot/)?(\S+)\s*$")


def sha256_file(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def newest_manifest(bot: str = BOT) -> Optional[str]:
    found = glob.glob(os.path.join(bot, "artifacts", "slice*_sha256_manifest.txt"))
    return max(found, key=lambda p: int(re.search(r"slice(\d+)_", os.path.basename(p)).group(1))) if found else None


def manifest_digests(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            m = _ROW.match(line.strip())
            if m:
                out[m.group(2)] = m.group(1)
    return out


def prove(bot: str = BOT) -> Dict[str, object]:
    manifest_path = newest_manifest(bot)
    listed = manifest_digests(manifest_path) if manifest_path else {}
    rows: List[Dict[str, object]] = []
    for rel, pinned in PINNED.items():
        actual = sha256_file(os.path.join(bot, rel))
        in_manifest = listed.get(rel)
        rows.append({"path": rel, "pinned": pinned, "actual": actual,
                     "matches_pin": actual == pinned,
                     "manifest_digest": in_manifest,
                     "matches_manifest": (None if in_manifest is None else actual == in_manifest)})
    ok = all(r["matches_pin"] for r in rows) and all(r["matches_manifest"] in (True, None) for r in rows)
    return {"ok": ok, "manifest": os.path.relpath(manifest_path, bot) if manifest_path else None,
            "files": rows,
            "note": "A match proves the frozen files are unchanged. It says nothing about "
                    "whether the frozen rule earns anything."}


def main() -> int:
    verdict = prove()
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
