"""Signal definitions, each behind its own module boundary.

A signal here produces **flags** — "did this rule want to open a trade on this
bar" — and nothing else. Scoring, schedules and every R come from
``tools/skill_test.py``, the machinery validated in slice 23. Keeping signals
flag-only is what lets a new candidate be measured by exactly the instrument
that judged the last one, with no forked arithmetic to drift.

The classical ``technical_analysis`` analyser is NOT in this package. It is
CLOSED (``STAGE1_VERDICT.md``) and stays where it is, unmodified, so its ABSENT
result remains reproducible.
"""
