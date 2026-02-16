#!/usr/bin/env python3
"""Simple offline runner for evaluating prompt response shape.

This script does not call Discord; it hits Agent.chat directly.
It is intended for controlled benchmark runs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.core.agent import Agent

PROMPTS_FILE = Path("scripts/prompts.json")
OUT_FILE = Path("scripts/eval_results.json")


async def main() -> None:
    if not PROMPTS_FILE.exists():
        raise FileNotFoundError(f"Missing prompt set: {PROMPTS_FILE}")

    prompts = json.loads(PROMPTS_FILE.read_text())
    if not isinstance(prompts, list):
        raise ValueError("prompts.json must be a JSON array of strings")

    agent = Agent()
    results = []

    for prompt in prompts:
        if not isinstance(prompt, str):
            continue
        response = await agent.chat(prompt, message_history=[], is_admin=True)
        results.append({"prompt": prompt, "response": response})

    OUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"Wrote {len(results)} results to {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
