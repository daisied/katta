# Katta

Autonomous Discord research agent with tool use, grounded web retrieval, and persistent local memory.

## Why Katta Exists

I built Katta because too many LLM workflows felt unreliable in real use:

- Hallucinated facts delivered with confidence
- Weak source depth (single-search, shallow snippets)
- Poor traceability when answers were wrong

Katta is designed to do the opposite: minimum-hallucination deep searching for virtually anything, with explicit tool-grounded retrieval and operational guardrails.

## Why This Project Is Different

- Grounded-by-default workflow: tools first, synthesis second.
- Multi-source deep research pipeline (web + Reddit + X + page fetch).
- Freshness-oriented retrieval for fast-changing topics.
- Self-hosted, Docker-first runtime with persistent memory.
- Admin/non-admin access model for safer shared usage.
- Security controls for sensitive paths, output redaction, and command mode safety.

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose
- Discord bot token
- OpenRouter or MiniMax API key

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with:

- `DISCORD_BOT_TOKEN`
- `ALLOWED_USER_ID`
- `OPENROUTER_API_KEY` or `MINIMAX_API_KEY`

### 3. Launch

```bash
docker compose up -d --build
```

Tail logs:

```bash
docker compose logs -f katta
```

### 4. Verify

Send your bot a DM. Example prompts:

- `What tools do you have available?`
- `Research the latest SearXNG deployment best practices`

## Runtime Model

- Runtime state lives in `app/data/` (mounted volume).
- Repo tracks templates only; runtime artifacts are git-ignored.
- On first boot, `app/main.py` creates missing runtime files from templates.

## Security Model

Katta is designed for trusted self-hosted use.

- Admin user has full tool access.
- Non-admin users are restricted to search/read tool set.
- Secret values are redacted from tool output.
- Sensitive file paths are blocked.
- `KATTA_COMMAND_MODE=safe` blocks dangerous shell patterns.

Use `trusted` mode only when you intentionally need unrestricted shell behavior.

## Developer Workflow

Install dependencies:

```bash
make bootstrap
```

Run checks:

```bash
make ci
```

## Project Structure

```text
app/
  core/
    agent.py           # LLM loop, tool orchestration, prompt building
    tools.py           # Tool implementations (shell, web, file, research)
    scout.py           # Source scanning / scoring pipeline
  interfaces/
    discord_bot.py     # Discord event layer, permissions, history
  main.py              # Bootstrapping + entrypoint
  data/
    *.template / *.example files only in git

docs/
  ARCHITECTURE.md
  EVALUATION.md
  RELEASE_CHECKLIST.md

tests/
  unit tests for core safety and utility behavior
```

## CI / Quality Gates

GitHub Actions pipeline runs:

- `ruff check .`
- `pytest`
- `python -m compileall app`

## WOW Roadmap (Now Enabled)

- Evaluation harness scaffold (`scripts/eval_research.py` + `scripts/prompts.json`)
- Architecture and release docs
- Community + governance files (`LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`)
- Issue/PR templates for higher-signal collaboration

## Publishing Checklist

1. Confirm `.env` is not committed.
2. Confirm `app/data` has no runtime artifacts in commits.
3. Run `make ci`.
4. Validate end-to-end startup from a clean clone.
5. Tag and release.

See `docs/RELEASE_CHECKLIST.md`.
Planned product and quality upgrades live in `docs/ROADMAP.md`.

## License

MIT. See `LICENSE`.
