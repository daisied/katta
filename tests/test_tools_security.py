from __future__ import annotations

import json
from pathlib import Path

from app.core import tools


def test_is_sensitive_path_blocks_expected_patterns() -> None:
    assert tools._is_sensitive_path(".env")
    assert tools._is_sensitive_path("/tmp/my_token.txt")
    assert not tools._is_sensitive_path("/tmp/notes.txt")


def test_manage_access_requires_id_for_mutating_actions(tmp_path: Path, monkeypatch) -> None:
    permissions_file = tmp_path / "permissions.json"
    monkeypatch.setattr(tools, "PERMISSIONS_PATH", str(permissions_file))

    result = tools.manage_access("user", "allow")
    assert "id is required" in result.lower()


def test_manage_access_lifecycle(tmp_path: Path, monkeypatch) -> None:
    permissions_file = tmp_path / "permissions.json"
    monkeypatch.setattr(tools, "PERMISSIONS_PATH", str(permissions_file))

    assert "no allowed users" in tools.manage_access("user", "list").lower()

    allowed = tools.manage_access("user", "allow", 123, "alice")
    assert "successfully allowed" in allowed.lower()

    listed = tools.manage_access("user", "list")
    assert "alice (123)" in listed

    blocked = tools.manage_access("user", "block", 123)
    assert "successfully blocked" in blocked.lower()

    data = json.loads(permissions_file.read_text())
    assert data["allowed_users"] == []


def test_run_shell_command_blocks_env_dump() -> None:
    output = tools.run_shell_command("env")
    assert "blocked" in output.lower()


def test_run_shell_command_safe_mode_blocks_dangerous(monkeypatch) -> None:
    monkeypatch.setenv("KATTA_COMMAND_MODE", "safe")
    output = tools.run_shell_command("rm -rf /")
    assert "blocked by safe mode" in output.lower()


def test_run_script_argument_parsing(tmp_path: Path, monkeypatch) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / "echo_args.py"
    script_path.write_text("import sys\nprint('|'.join(sys.argv[1:]))\n", encoding="utf-8")

    monkeypatch.setattr(tools, "SCRIPTS_DIR", str(scripts_dir))

    output = tools.run_script("echo_args", '--name "alice bob"')
    assert "--name|alice bob" in output
