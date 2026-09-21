"""stage_b_forward_accrual.sh must leave a usable --from path behind.

Static assertions only — this script requires real venue egress, so it is
never executed here. See docs/human/NO_GLUE_OPS.md #9.
"""
from __future__ import annotations

import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "stage_b_forward_accrual.sh")


def _script_text() -> str:
    with open(SCRIPT, encoding="utf-8") as handle:
        return handle.read()


class TestTheScratchPathSurvivesTheRun:
    def test_daily_forward_refresh_is_called_with_an_explicit_scratch_dir(self):
        text = _script_text()
        assert '"$PY" tools/daily_forward_refresh.py --scratch "$SCRATCH_DIR"' in text

    def test_the_scratch_dir_is_a_fixed_path_not_a_tempdir(self):
        text = _script_text()
        assert 'SCRATCH_DIR="state/forward_shadow_scratch"' in text
        assert "mktemp" not in text
        assert "tempfile.TemporaryDirectory()" not in text

    def test_the_promote_hint_names_a_from_flag_pointing_at_the_scratch_file(self):
        text = _script_text()
        assert "HUMAN_PROMOTE_HINT" in text
        assert "--from {scratch_file}" in text
        assert 'SCRATCH_FILE="$SCRATCH_DIR/forward.json"' in text

    def test_the_compare_block_uses_the_pinned_venv_python_not_bare_python3(self):
        text = _script_text()
        assert '"$PY" - "$REPORT" "$SCRATCH_FILE" <<' in text
        assert "\npython3 - " not in text
