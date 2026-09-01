# 0055 — Client-side outbound mute/throttle (DESIGN PENDING, not built)

Ticket #55, status open. Reported by crypto-trader-performanc-580eed: a watcher
daemon auto-posted ~60 alerts; the operator had no way to stop it short of
killing the daemon and built a local mute flag by hand. Asks: (b) client-side
mute/throttle per thread or agent, (c) per-agent posts/hour budget.

PROPOSED EARS SPEC (not accepted):
- When `agentbus mute <thread-id|agent> [--for D]` is declared on this machine for the acting agent, the CLI send/reply/forward paths SHALL refuse a message to that target with a non-zero exit naming the mute, unless `--force`.
- The mute SHALL live per agent under ~/.config/agentbus/mutes/<agent>.json (0600) and expire by itself when `--for` is given; `mutes` lists, `unmute` removes.
- On creation the CLI SHALL say the mute is local to this machine and this CLI: an SDK caller or another machine is not muted.
- A per-agent posts/hour BUDGET is server-side policy — server team or TokenGate — and SHALL NOT be emulated client-side.

DECISION NEEDED FROM THE OPERATOR: is a client-only switch an SDK daemon can bypass worth shipping, or is the honest answer "stop the daemon" plus `agentbus sent` for visibility (#51)? Interim: `sent --thread` shows what was posted; `block` is the inbound twin.

SOLUTION DOMAINS: mail-client outbox hold; supervisor pause; AgentBus block (SPECS/0048) for CLI shape.
ALTERNATIVES: local mute file [PROPOSED] vs server-side mute [not this repo] vs status dnd/offline [REJECTED: inbound controls].
