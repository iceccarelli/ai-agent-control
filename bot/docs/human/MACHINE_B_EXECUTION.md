# MACHINE B — EXECUTION HOST

This computer owns:
- `ai-agent-control` git checkout
- Bybit/paper runtime, corpus refresh, control_plane_tick
- Claude Code builder sessions that edit THIS repo

This computer does NOT own:
- Ecowoods OpenClaw multi-agent brain (that is MACHINE A)

Law:
- No LLM imports on order path
- allows_live only via human ACCEPT files
- OpenClaw agents on MACHINE A talk to this repo via **git push/pull**, not by sharing a filesystem
