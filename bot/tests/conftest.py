"""Suite-wide fixtures.

The one thing here is the data-read ledger kill switch.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _disable_data_read_ledger():
    """A test run must not fabricate reads.

    `market_data.load_corpus` records every corpus read into
    artifacts/data_read_ledger.json so that a future holdout can be certified
    untouched. The suite loads corpora hundreds of times, and none of those
    loads informed a hypothesis — they are assertions about code, not looks at
    data. Left unsuppressed they would bury the six reads that actually matter
    under noise, and a ledger nobody can read is a ledger nobody uses.

    Set for the session and restored afterwards, so an interactive run in the
    same shell is still recorded.
    """
    previous = os.environ.get("LEDGER_DISABLED")
    os.environ["LEDGER_DISABLED"] = "1"
    yield
    if previous is None:
        os.environ.pop("LEDGER_DISABLED", None)
    else:
        os.environ["LEDGER_DISABLED"] = previous
