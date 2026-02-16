<p align="center">
  <img src="docs/assets/katta-logo.png" alt="Katta Logo" width="180">
</p>
<h1 align="center">Katta</h1>
<p align="center"><strong>Personal research agent that actually digs deep, with permanent memory.</strong></p>
<p align="center">Deploy in minutes. Ask almost anything. Get grounded answers from multiple sources.</p>

## Why This Exists

Most LLM workflows felt unreliable in real usage:

- confident hallucinations
- shallow source coverage
- weak traceability when wrong

Katta is built to reduce that failure mode with tool-grounded deep research, source diversity, and persistent memory.

## Quick Setup

### 1. Prerequisites

- Docker + Docker Compose
- Discord bot token
- OpenRouter or MiniMax API key

### 2. Configure

```bash
cp .env.example .env
```

Set these values in `.env`:

- `DISCORD_BOT_TOKEN`
- `ALLOWED_USER_ID`
- `OPENROUTER_API_KEY` or `MINIMAX_API_KEY`

### 3. Deploy

```bash
docker compose up -d --build
docker compose logs -f katta
```

### 4. Use It

DM your bot:

- `What tools do you have available?`
- `Research the latest SearXNG deployment best practices`

## Big Impact, Minimal Setup

- Grounded-by-default workflow: tools first, synthesis second.
- Multi-source deep research: web + Reddit + X + page fetch.
- Freshness-oriented retrieval for fast-moving topics.
- Persistent memory and runtime state across restarts.
- Self-hosted deployment with a production-style structure.

## Security Model

Katta is a trusted self-hosted agent.

- Admin user has full tool access.
- Non-admin users are restricted to search/read tools.
- Sensitive file paths are blocked.
- Secret values are redacted from outputs.
- `KATTA_COMMAND_MODE=safe` blocks dangerous shell patterns.

Use `trusted` mode only when you intentionally want wider shell capability.

## Project Structure

```text
app/
  core/
    agent.py
    tools.py
    scout.py
  interfaces/
    discord_bot.py
  main.py
  data/
    committed templates only

docs/
  ARCHITECTURE.md
  EVALUATION.md
  RELEASE_CHECKLIST.md
  ROADMAP.md

tests/
  security + utility tests
```

## Quality Gates

CI runs:

- `ruff check .`
- `pytest`
- `python -m compileall app`

## Dev Workflow

```bash
make bootstrap
make ci
```

## Release Checklist

1. Confirm `.env` is not committed.
2. Confirm `app/data` contains no runtime artifacts.
3. Run `make ci`.
4. Validate startup from a clean clone.

Detailed checklist: `docs/RELEASE_CHECKLIST.md`

## License

MIT (`LICENSE`)
