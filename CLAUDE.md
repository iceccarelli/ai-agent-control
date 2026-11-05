# CLAUDE.md

Python (3.11 in production, 3.12 also tested) + a small C++ core. Everything lives under `bot/`.

## What this is
A Bybit BTC funding-carry bot (delta-neutral: short BTCUSDT linear perp against BTC the
client owns). **Paper/testnet only.** Live trading is impossible from this tree unless four
independent conditions are met by a human *and* the promotion gate is signed; the gate is
not signed and must stay `REFUSE` until the owner signs it.

## Hard rules for any change
- Never set `LIVE_TRADING_ACK`, `USE_TESTNET=0` or `PAPER_TRADING=0`; never weaken
  `config.is_live_authorized`, the promotion gate, or a transport guard.
- Which exchange a name means, and whether it is real money, is **`bot/venues.py`**. Do not
  derive "sandbox" from `venue != "mainnet"` anywhere. `eu` is registered **unqualified** and
  refused everywhere; do not add an order-capable venue without owner-collected evidence.
- `signals/funding_carry_fade_v1.py` is frozen (`tools/frozen_proof.py` pins it). The
  `candidate_carry_depth_v1` candidate is separate and order-less.
- FLAT means "the venue's own position read says flat". Never infer it from fill arithmetic.
- Do not read, print or commit credentials, `.env`, `runs/`, or any `*.db` / evidence jsonl.
  No test may open `bot/state/` (conftest refuses it). The suite is offline.
- `bot/fly.toml` is **generated**: edit `bot/tools/fly_stack.py`, then `python3 tools/fly_stack.py --render`.
- Anything that changes Fly, the exchange, Supabase, or branch protection is the owner's, not yours.

## Run
```bash
cd bot && pip install -r requirements.txt -c constraints.txt pytest
make -C ../cpp && REQUIRE_NATIVE_CORE=1 python -m pytest -q -p no:cacheprovider tests/test_X.py
python3 tools/fly_stack.py --check      # Fly config + image build context
python3 tools/secret_scan.py            # credential shapes (prints file:line:rule only)
python3 tools/frozen_proof.py           # frozen strategy digests
```
CI (`.github/workflows/carry-verify.yml`) lists the gate tests by hand; `rest-of-suite` runs every
other `tests/test_*.py`. A new test file goes in exactly one of them: add it to the list.
New top-level modules the app imports must also be added to **both** Dockerfiles' `COPY` lists.

## Docs
`bot/RUNBOOKS.md` (incidents), `bot/DEPLOYMENT.md`, `bot/README.md`, `bot/EDGE.md` if present.
