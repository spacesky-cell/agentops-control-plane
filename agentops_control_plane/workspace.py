from __future__ import annotations

import shutil
import zipfile
from pathlib import Path


DEFAULT_EXCLUDES = {
    ".git",
    ".agentops",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".venv",
}


class WorkspaceManager:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.workspaces_dir = self.base_dir / "workspaces"
        self.snapshots_dir = self.base_dir / "snapshots"
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def create(self, run_id: str, source: str | Path | None = None) -> Path:
        workspace = self.workspaces_dir / run_id
        if workspace.exists():
            shutil.rmtree(workspace)
        if source:
            source_path = Path(source).resolve()
            shutil.copytree(source_path, workspace, ignore=self._ignore)
        else:
            workspace.mkdir(parents=True)
        return workspace

    def snapshot(self, run_id: str, workspace: str | Path, label: str) -> Path:
        workspace_path = Path(workspace).resolve()
        archive = self.snapshots_dir / f"{run_id}-{label}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in workspace_path.rglob("*"):
                relative = path.relative_to(workspace_path)
                if path.is_dir() or any(part in DEFAULT_EXCLUDES for part in relative.parts):
                    continue
                zf.write(path, relative)
        return archive

    def safe_path(self, workspace: str | Path, relative: str) -> Path:
        root = Path(workspace).resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Path escapes workspace: {relative}") from exc
        return candidate

    def _ignore(self, _directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in DEFAULT_EXCLUDES}

