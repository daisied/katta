# Runtime Data

This directory is mounted as a persistent volume at runtime.

- Files created during usage (`logs/`, `history/`, `*.db`, `memory.md`, etc.) are intentionally git-ignored.
- Template files in this folder are committed so new users can bootstrap quickly.

## First Run Behavior

`app/main.py` auto-creates missing runtime files using these templates:

- `memory.template.md` -> `memory.md`
- `sources.example.json` -> `sources.json`
- `permissions.example.json` -> `permissions.json`
- `startup.example.sh` -> `startup.sh`
