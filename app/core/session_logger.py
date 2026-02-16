"""
Session Logger - writes human-readable Markdown logs of every agent session.

Each session gets its own .md file in /app/app/data/logs/.
Files are named like: 2026-02-06_12-37_chat.md

Log format is designed to be readable by both humans and LLMs reviewing past sessions.
Old logs are auto-pruned after a configurable retention period.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

LOGS_DIR = "/app/app/data/logs"
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))


def _ensure_log_dir():
    os.makedirs(LOGS_DIR, exist_ok=True)


def _truncate(text: str, max_len: int = 500) -> str:
    """Truncate long tool results for readability."""
    if not text:
        return "(empty)"
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... ({len(text) - max_len} chars truncated)"


class SessionLogger:
    """Logs a single agent session to a Markdown file."""

    def __init__(self, session_type: str = "chat", trigger: str = ""):
        """
        Args:
            session_type: "chat"
            trigger: What started this session (user message preview)
        """
        _ensure_log_dir()
        now = datetime.now(timezone.utc)
        self.start_time = now
        timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
        self.filename = os.path.join(LOGS_DIR, f"{timestamp}_{session_type}.md")
        self.session_type = session_type
        self._lines = []

        # Write header
        self._lines.append(f"# Session: {session_type.title()}")
        self._lines.append(f"**Started:** {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        self._lines.append(f"**Type:** {session_type}")
        if trigger:
            safe_trigger = trigger[:200].replace('\n', ' ')
            self._lines.append(f"**Trigger:** {safe_trigger}")
        self._lines.append("")
        self._lines.append("---")
        self._lines.append("")

        self._flush()

    def log_turn_start(self, turn: int):
        self._lines.append(f"## Turn {turn}")
        self._lines.append("")
        self._flush()

    def log_model_response(self, content: str | None, tool_calls: list | None):
        """Log what the model said/did."""
        if content and content.strip():
            self._lines.append("### Response")
            self._lines.append("")
            self._lines.append(content.strip())
            self._lines.append("")

        if tool_calls:
            self._lines.append(f"### Tool Calls ({len(tool_calls)})")
            self._lines.append("")
            for tc in tool_calls:
                name = tc.function.name
                try:
                    raw = tc.function.arguments
                    if raw and raw.strip():
                        args = json.loads(raw)
                    else:
                        args = {}
                except (json.JSONDecodeError, AttributeError):
                    args = {"_raw": tc.function.arguments}

                # Format args nicely
                if args:
                    args_str = json.dumps(args, indent=2, ensure_ascii=False)
                else:
                    args_str = "(no args)"

                self._lines.append(f"**{name}**")
                self._lines.append("```json")
                self._lines.append(args_str)
                self._lines.append("```")
                self._lines.append("")

        self._flush()

    def log_tool_result(self, tool_name: str, result: str):
        """Log the result of a tool execution."""
        truncated = _truncate(str(result), max_len=800)
        self._lines.append(f"**Result: {tool_name}**")
        self._lines.append("```")
        self._lines.append(truncated)
        self._lines.append("```")
        self._lines.append("")
        self._flush()

    def log_event(self, event: str):
        """Log a misc event (empty response nudge, error, etc.)."""
        self._lines.append(f"> {event}")
        self._lines.append("")
        self._flush()

    def close(self, final_response: str | None = None):
        """Finalize the session log."""
        now = datetime.now(timezone.utc)
        duration = (now - self.start_time).total_seconds()

        self._lines.append("---")
        self._lines.append("")
        self._lines.append(f"**Ended:** {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        self._lines.append(f"**Duration:** {duration:.1f}s")

        if final_response and final_response.strip():
            self._lines.append("")
            self._lines.append("### Final Response")
            self._lines.append("")
            self._lines.append(final_response.strip())

        self._lines.append("")
        self._flush()

    def _flush(self):
        """Write current buffer to file."""
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.write('\n'.join(self._lines))
        except Exception as e:
            logger.error(f"Failed to write session log: {e}")


def prune_old_logs():
    """Delete session logs older than LOG_RETENTION_DAYS."""
    _ensure_log_dir()
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)
    pruned = 0
    for fname in os.listdir(LOGS_DIR):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(LOGS_DIR, fname)
        try:
            # Parse date from filename: 2026-02-06_12-37-00_chat.md
            date_part = fname[:19]  # "2026-02-06_12-37-00"
            file_date = datetime.strptime(date_part, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                os.remove(fpath)
                pruned += 1
        except (ValueError, OSError) as e:
            logger.debug(f"Skipping log file {fname}: {e}")
    if pruned:
        logger.info(f"Pruned {pruned} old session log(s)")
    return pruned
