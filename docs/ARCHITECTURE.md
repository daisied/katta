# Architecture

## Components

- `app/interfaces/discord_bot.py`
  Handles Discord events, access control, and per-channel history.
- `app/core/agent.py`
  Orchestrates LLM calls, tool loops, memory injection, and guardrails.
- `app/core/tools.py`
  Implements shell/file/network/research/tooling primitives exposed to the agent.
- `app/core/scout.py`
  Optional source scanner and ranking pipeline.
- `app/main.py`
  Runtime bootstrap and process entrypoint.

## Runtime Data Model

Runtime state is stored in `app/data/` (mounted volume):

- `memory.md`: durable agent memory
- `permissions.json`: allowlist for users/channels
- `sources.json`: scout source config
- `logs/`, `history/`, `*.db`: runtime artifacts

The repository only tracks templates, not runtime state.

## Security Boundaries

- Admin user gets full tool access.
- Non-admin users are restricted to public search/read tools.
- Secret file patterns and environment variables are blocked/redacted.
- Optional command safe mode can block dangerous shell patterns.

## Operational Flow

1. `main.py` bootstraps runtime files and startup tasks.
2. Discord message arrives.
3. Access context resolved (admin/whitelist/public).
4. Agent executes tool loop and returns response.
5. History and session logs are persisted to volume.
