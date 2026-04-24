from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .config import PolicyConfig
from .workspace import DEFAULT_EXCLUDES, WorkspaceManager


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
        glob_pattern = pattern or "**/*"
        files: list[str] = []
        for path in workspace.glob(glob_pattern):
            if path.is_dir():
                continue
            if any(part in DEFAULT_EXCLUDES for part in path.relative_to(workspace).parts):
                continue
            files.append(path.relative_to(workspace).as_posix())
        return sorted(files)

    def read_file(self, workspace: Path, relative: str) -> str:
        path = self.workspace_manager.safe_path(workspace, relative)
        return path.read_text(encoding="utf-8")

    def write_file(self, workspace: Path, relative: str, content: str) -> dict[str, Any]:
        path = self.workspace_manager.safe_path(workspace, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text(content, encoding="utf-8")
        return {
            "path": relative,
            "created": before is None,
            "before_chars": len(before or ""),
            "after_chars": len(content),
        }

    def patch_text(self, workspace: Path, relative: str, old: str, new: str) -> dict[str, Any]:
        path = self.workspace_manager.safe_path(workspace, relative)
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise ValueError(f"Patch target text not found in {relative}")
        updated = text.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        return {
            "path": relative,
            "before_chars": len(text),
            "after_chars": len(updated),
            "replacements": 1,
        }

    def run_command(self, workspace: Path, command: str) -> dict[str, Any]:
        completed = subprocess.run(
            command,
            shell=True,
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

