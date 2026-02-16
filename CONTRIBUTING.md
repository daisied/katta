# Contributing

## Setup

1. Clone the repository.
2. Copy `.env.example` to `.env` and configure required values.
3. Install dev tooling:

```bash
make bootstrap
```

## Development Rules

- Keep private/runtime artifacts out of commits (`app/data/*` runtime files, logs, DBs).
- Add or update tests for behavior changes.
- Run before opening a PR:

```bash
make ci
```

## Pull Requests

- Keep PRs focused and small.
- Describe behavior changes and risk.
- Include screenshots/log snippets for UX/runtime changes.
- Reference related issues (`Fixes #123`).

## Commit Style

Use clear, imperative messages:

- `fix: validate manage_access id handling`
- `docs: rewrite quickstart and security model`
