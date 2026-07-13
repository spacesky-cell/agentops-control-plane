from __future__ import annotations

import subprocess
import shlex
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import PolicyConfig
from .workspace import WorkspaceManager


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": "List files in the isolated run workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Optional glob pattern. Defaults to **/*."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the isolated run workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Write a UTF-8 text file in the isolated run workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "content": {"type": "string", "description": "New file content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "patch_text",
        "description": "Replace the first matching text occurrence in a workspace file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "old": {"type": "string", "description": "Existing text to replace."},
                "new": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_command",
        "description": "Run an allowlisted command in the isolated run workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line parsed into argv before execution."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]


def list_tool_definitions() -> list[dict[str, Any]]:
    return deepcopy(TOOL_DEFINITIONS)


class ToolExecutor:
    def __init__(self, workspace_manager: WorkspaceManager, config: PolicyConfig) -> None:
        self.workspace_manager = workspace_manager
        self.config = config

    def execute(self, workspace: Path, tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "list_files":
            return self.list_files(workspace, args.get("pattern"))
        if tool_name == "read_file":
            return self.read_file(workspace, str(args["path"]))
        if tool_name == "write_file":
            return self.write_file(workspace, str(args["path"]), str(args.get("content", "")))
        if tool_name == "patch_text":
            return self.patch_text(
                workspace,
                str(args["path"]),
                str(args.get("old", "")),
                str(args.get("new", "")),
            )
        if tool_name == "run_command":
            return self.run_command(workspace, str(args["command"]))
        raise ValueError(f"Unknown tool: {tool_name}")

    def list_files(self, workspace: Path, pattern: str | None = None) -> list[str]:
        return self.workspace_manager.list_files(workspace, pattern)

    def read_file(self, workspace: Path, relative: str) -> str:
        return self.workspace_manager.read_text(workspace, relative)

    def write_file(self, workspace: Path, relative: str, content: str) -> dict[str, Any]:
        before = self.workspace_manager.write_text(workspace, relative, content)
        return {
            "path": relative,
            "created": before is None,
            "before_chars": len(before or ""),
            "after_chars": len(content),
        }

    def patch_text(self, workspace: Path, relative: str, old: str, new: str) -> dict[str, Any]:
        text, updated = self.workspace_manager.patch_text(
            workspace, relative, old, new
        )
        return {
            "path": relative,
            "before_chars": len(text),
            "after_chars": len(updated),
            "replacements": 1,
        }

    def run_command(self, workspace: Path, command: str) -> dict[str, Any]:
        argv = shlex.split(command, posix=False)
        if not argv:
            raise ValueError("Missing command.")
        completed = subprocess.run(
            argv,
            shell=False,
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.config.max_command_seconds,
        )
        output = completed.stdout + completed.stderr
        if len(output) > self.config.max_output_chars:
            output = output[: self.config.max_output_chars] + "\n[output truncated]"
        return {
            "command": command,
            "exit_code": completed.returncode,
            "output": output,
        }

