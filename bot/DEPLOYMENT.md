# DEPLOYMENT.md: RETIRED (Round 21)

**This document is retired. It is not an operating procedure and it authorises
nothing.** Do not follow instructions you remember from an earlier version of it.
The earlier text described a path to live capital (arming live trading, a
final live step, scaling). That path is not part of the current
mandate: this repository is **paper and Bybit Testnet only**, and nothing in it
arms Mainnet. The earlier text remains available in git history as history, not
as guidance.

Where the live instructions are now:

| Question | Authoritative place |
|---|---|
| What runs where, and what may place an order | the *Operating topology* block, byte-identical in `bot/README.md`, `bot/RUNBOOKS.md` and `docs/MONEY_MISSION.md` |
| Current verified state, blockers, owner-only steps | `docs/MONEY_MISSION.md` section 0 |
| Incident and operational procedures | `bot/RUNBOOKS.md` |
| Hard controls (no Mainnet, no weakened gate, one executor) | `CLAUDE.md` |
| Testnet credentials | `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET` only; a generic `BYBIT_API_KEY` or `BYBIT_API_SECRET` is not a Testnet credential and is ignored |

## The one statement kept from the earlier text: the original credentials

`.env.production.ORIGINAL_DO_NOT_DEPLOY` held live Bybit credentials in the
SOURCE this repository was imported from, and the original Dockerfile copied them
into every image (`COPY . /app`, no `.dockerignore`). A scan of this repository's
full git history (612 commits at the time) found no such file and no credential
value, so the exposure is in that original upload/source and in any image built
from it, not provably in this history. Nothing can show the keys were never in a
file or image, so **treat them as fully compromised.** Whether they were rotated
is UNKNOWN to the repository: it is the owner's to confirm in the Bybit console.

1. Confirm in the Bybit console that the original keys are revoked.
2. Check that account (read-only, in the exchange's own UI) for open positions,
   resting orders and margin loans the old bot may have created; close and repay
   manually if any.
3. Purge any image that contains the file.
4. Any future credential is a dedicated Testnet key with the minimum scope and no
   withdrawal permission, injected at runtime only (never a file in an image,
   never a value in a Dockerfile), and never pasted into a chat, ticket or log.

The kill switch is cleared by a human, never by code (`bot/RUNBOOKS.md` section
12). A credential is never a grant: the owner's capability and grant are separate
and are created only by the owner.
