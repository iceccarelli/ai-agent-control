"""tools/stage_b_bars.resolve_closed_forward_bars: the one place that decides
"how many forward bars have closed" for every Stage B display/gate reader.

No network, no real artifacts/ touched.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

from stage_b_bars import resolve_closed_forward_bars  # noqa: E402


class TestResolveClosedForwardBars:
    def test_prefers_the_ceiling_value(self):
        doc = {
            "closed_forward_bars": 999,
            "ceiling": {"closed_forward_bars": 43},
            "forward_window": {"of_which_closed": 41},
        }
        assert resolve_closed_forward_bars(doc) == 43

    def test_falls_back_to_forward_window_when_ceiling_is_absent(self):
        doc = {"closed_forward_bars": 999, "forward_window": {"of_which_closed": 41}}
        assert resolve_closed_forward_bars(doc) == 41

    def test_falls_back_to_the_legacy_top_level_key(self):
        doc = {"closed_forward_bars": 41}
        assert resolve_closed_forward_bars(doc) == 41

    def test_a_freshly_promoted_nested_only_shadow_resolves_not_zero(self):
        """The real scorer schema: no top-level closed_forward_bars at all."""
        doc = {
            "forward_n_trades": 3,
            "ceiling": {"closed_forward_bars": 43},
            "forward_window": {"of_which_closed": 43},
        }
        assert resolve_closed_forward_bars(doc) == 43

    def test_none_or_empty_document_resolves_to_zero(self):
        assert resolve_closed_forward_bars(None) == 0
        assert resolve_closed_forward_bars({}) == 0

    def test_ceiling_present_but_bars_key_missing_falls_through(self):
        doc = {"ceiling": {}, "forward_window": {"of_which_closed": 41}}
        assert resolve_closed_forward_bars(doc) == 41
