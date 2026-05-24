"""Bash tool: execute shell commands under run_dir."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from src.agent.tools import BaseTool
from src.tools.path_utils import safe_run_dir

_OUTPUT_LIMIT = 50_000
_DEFAULT_TIMEOUT = 120


class BashTool(BaseTool):
    """Execute shell commands in the working directory."""

    name = "bash"
    description = "Execute a shell command in the working directory. Use for installing packages, running scripts, or inspecting files."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
        },
        "required": ["command"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        """Execute a shell command.

        Args:
            **kwargs: Must include command. Optional run_dir used as cwd.

        Returns:
            JSON string with stdout, stderr, and exit_code.
        """
        command = kwargs["command"]
        cwd_raw = kwargs.get("run_dir")

        # Architectural gap #3 (Codex audit): re-validate run_dir on EVERY
        # call. Previously execute() trusted whatever the caller injected,
        # which means a future entrypoint that forgets include_shell_tools=
        # False could land here with an arbitrary cwd (e.g. system32, /etc).
        # safe_run_dir() rejects UNC paths AND restricts to allowed roots.
        # Fail-CLOSED on missing/invalid: the tool refuses to run rather
        # than silently dropping cwd and executing in the worker's CWD.
        cwd = None
        if cwd_raw is not None:
            try:
                cwd = str(safe_run_dir(cwd_raw))
            except (ValueError, OSError) as exc:
                return json.dumps({
                    "status": "error",
                    "error": (
                        f"bash refused: run_dir {cwd_raw!r} failed the allow-list "
                        f"check ({exc}). Set ALLOWED_RUN_ROOTS or pass a path "
                        f"inside the swarm runs root."
                    ),
                }, ensure_ascii=False)
        else:
            # No run_dir → refuse rather than running in the worker's CWD,
            # which is the MCP server process root and contains secrets.
            return json.dumps({
                "status": "error",
                "error": (
                    "bash refused: no run_dir provided. Every bash invocation "
                    "must scope to a swarm worker run directory."
                ),
            }, ensure_ascii=False)

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_DEFAULT_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )
            stdout = result.stdout[:_OUTPUT_LIMIT] if len(result.stdout) > _OUTPUT_LIMIT else result.stdout
            stderr = result.stderr[:_OUTPUT_LIMIT] if len(result.stderr) > _OUTPUT_LIMIT else result.stderr
            return json.dumps({
                "status": "ok" if result.returncode == 0 else "error",
                "exit_code": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }, ensure_ascii=False)
        except subprocess.TimeoutExpired:
            return json.dumps({
                "status": "error",
                "error": f"Command timed out after {_DEFAULT_TIMEOUT}s",
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error": str(exc),
            }, ensure_ascii=False)
