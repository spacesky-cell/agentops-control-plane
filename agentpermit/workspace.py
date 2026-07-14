from __future__ import annotations

import ctypes
import io
import os
import secrets
import stat
import zipfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .config import PolicyConfig, is_protected_path


_WIN_GENERIC_READ = 0x80000000
_WIN_FILE_SHARE_READ = 0x1
_WIN_FILE_SHARE_WRITE = 0x2
_WIN_OPEN_EXISTING = 3
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class WorkspaceIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class _DirectoryState:
    path: Path
    identity: tuple[int, int]
    role: str = "Directory"


@dataclass
class _DirectoryLease:
    state: _DirectoryState
    fd: int | None = None
    handle: int | None = None


@dataclass(frozen=True)
class CommandCwdLease:
    cwd: str
    pass_fds: tuple[int, ...] = ()


@dataclass(frozen=True)
class _PathAccess:
    path: Path
    parent: _DirectoryLease
    target_identity: tuple[int, int] | None
    target_mode: int | None
    protected_identities: frozenset[tuple[int, int]]


class WorkspaceManager:
    def __init__(self, base_dir: str | Path, config: PolicyConfig | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.config = config or PolicyConfig()
        self.workspaces_dir = self.base_dir / "workspaces"
        self.snapshots_dir = self.base_dir / "snapshots"
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        storage_stat = self._direct_directory_stat(
            self.workspaces_dir, "Workspace storage must be a direct directory."
        )
        self._storage_state = _DirectoryState(
            Path(os.path.abspath(self.workspaces_dir)),
            self._identity(storage_stat),
            "Workspace storage",
        )
        snapshots_stat = self._direct_directory_stat(
            self.snapshots_dir, "Snapshot storage must be a direct directory."
        )
        self._snapshots_state = _DirectoryState(
            Path(os.path.abspath(self.snapshots_dir)),
            self._identity(snapshots_stat),
            "Snapshot storage",
        )
        self._trusted_roots: dict[Path, tuple[int, int]] = {}

    def create(self, run_id: str, source: str | Path | None = None) -> Path:
        name = self._validate_run_id(run_id)
        with self._directory_lease(self._storage_state) as storage:
            workspace = storage.state.path / name
            if self._stat_child(storage, name) is not None:
                raise FileExistsError(f"Workspace already exists: {workspace}")
            created_identity = self._mkdir_child(storage, name, workspace)
            try:
                workspace_stat = self._stat_child(storage, name)
                if (
                    workspace_stat is None
                    or self._identity(workspace_stat) != created_identity
                ):
                    raise WorkspaceIntegrityError(
                        f"Workspace creation failed: {workspace}"
                    )
                workspace_state = _DirectoryState(
                    workspace, created_identity, "Workspace root"
                )
                with self._directory_lease(workspace_state):
                    self._trusted_roots[workspace] = workspace_state.identity
            except Exception:
                current = self._stat_child(storage, name)
                if current is not None and self._identity(current) == created_identity:
                    state = _DirectoryState(
                        workspace, created_identity, "Workspace root"
                    )
                    with self._directory_lease(state) as root:
                        self._remove_directory_contents(root)
                    self._rmdir_child(storage, name, workspace)
                raise
        try:
            if source:
                self._copy_source(Path(source), workspace)
        except Exception:
            self.remove_workspace(workspace, workspace_state.identity)
            raise
        return workspace

    def workspace_identity(self, workspace: str | Path) -> tuple[int, int]:
        return self._trusted_root(workspace).identity

    @contextmanager
    def command_cwd_lease(
        self, workspace: str | Path
    ) -> Iterator[CommandCwdLease]:
        root = self._trusted_root(workspace)
        with self._directory_lease(root) as lease:
            if os.name == "nt":
                yield CommandCwdLease(str(root.path))
                return
            if lease.fd is None:
                raise WorkspaceIntegrityError("Workspace command lease has no directory fd.")
            fd_path = self._fd_directory_path(lease.fd)
            yield CommandCwdLease(fd_path, (lease.fd,))

    def register_workspace(
        self, workspace: str | Path, authoritative_identity: tuple[int, int]
    ) -> Path:
        path = Path(os.path.abspath(workspace))
        if path.parent != self._storage_state.path:
            raise ValueError(f"Workspace root is outside workspace storage: {path}")
        file_stat = self._direct_directory_stat(
            path, "Workspace root must be a direct workspace root."
        )
        current = self._identity(file_stat)
        if current != authoritative_identity:
            raise WorkspaceIntegrityError(
                f"Workspace root does not match authoritative workspace identity: {path}"
            )
        self._trusted_roots[path] = authoritative_identity
        with self._directory_lease(
            _DirectoryState(path, authoritative_identity, "Workspace root")
        ):
            return path

    def remove_workspace(
        self,
        workspace: str | Path,
        authoritative_identity: tuple[int, int] | None = None,
    ) -> None:
        path = Path(os.path.abspath(workspace))
        if path.parent != self._storage_state.path:
            raise ValueError(f"Workspace root is outside workspace storage: {path}")
        with self._directory_lease(self._storage_state) as storage:
            current = self._stat_child(storage, path.name)
            if current is None:
                self._trusted_roots.pop(path, None)
                return
            identity = self._identity(current)
            expected = authoritative_identity or self._trusted_roots.get(path)
            if expected is None or identity != expected:
                raise WorkspaceIntegrityError(
                    f"Workspace root does not match authoritative workspace identity: {path}"
                )
            self._require_direct_directory(current, path)
            state = _DirectoryState(path, identity, "Workspace root")
            with self._directory_lease(state) as root:
                self._remove_directory_contents(root)
            self._rmdir_child(storage, path.name, path)
            self._trusted_roots.pop(path, None)

    def list_files(self, workspace: str | Path, pattern: str | None = None) -> list[str]:
        files: list[str] = []

        def collect(relative: Path, _content: bytes | None) -> None:
            if pattern is None or relative.match(pattern):
                files.append(relative.as_posix())

        self._walk_workspace(workspace, collect, read_content=False)
        return sorted(files)

    def snapshot(self, run_id: str, workspace: str | Path, label: str) -> Path:
        run_name = self._validate_filename_component(run_id)
        label_name = self._validate_filename_component(label)
        archive_name = f"{run_name}-{label_name}.zip"
        archive = self._snapshots_state.path / archive_name
        temp_name = f".{archive_name}.{secrets.token_hex(12)}.tmp"
        with self._directory_lease(self._snapshots_state) as storage:
            temp_created = False
            temp_fd = -1
            temp_identity: tuple[int, int] | None = None
            try:
                destination = self._stat_child(storage, archive_name)
                self._require_snapshot_destination(archive, destination)
                expected = self._identity(destination) if destination else None
                fd = self._open_file(
                    storage,
                    temp_name,
                    storage.state.path / temp_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    mode=0o600,
                )
                temp_fd = fd
                temp_stat = os.fstat(temp_fd)
                temp_identity = self._identity(temp_stat)
                self._require_single_link(temp_stat, storage.state.path / temp_name)
                temp_created = True
                with os.fdopen(os.dup(temp_fd), "w+b") as fileobj:
                    with zipfile.ZipFile(
                        fileobj, "w", compression=zipfile.ZIP_DEFLATED
                    ) as zf:
                        self._walk_workspace(
                            workspace,
                            lambda relative, content: zf.writestr(
                                relative.as_posix(), content or b""
                            ),
                            read_content=True,
                        )
                    fileobj.flush()
                current_temp_stat = os.fstat(temp_fd)
                if self._identity(current_temp_stat) != temp_identity:
                    raise WorkspaceIntegrityError(
                        f"Snapshot temp descriptor changed during access: {archive}"
                    )
                self._require_single_link(
                    current_temp_stat, storage.state.path / temp_name
                )
                if os.name == "nt":
                    os.close(temp_fd)
                    temp_fd = -1
                self._verify_directory_binding(storage)
                current = self._stat_child(storage, archive_name)
                self._require_snapshot_destination(archive, current)
                current_identity = self._identity(current) if current else None
                if current_identity != expected:
                    raise WorkspaceIntegrityError(
                        f"Snapshot destination changed during access: {archive}"
                    )
                self._replace_child(
                    storage, temp_name, archive_name, temp_identity
                )
                temp_created = False
                try:
                    self._verify_directory_binding(storage)
                    promoted = self._stat_child(storage, archive_name)
                    if (
                        promoted is None
                        or self._identity(promoted) != temp_identity
                    ):
                        raise WorkspaceIntegrityError(
                            f"Snapshot promotion identity changed: {archive}"
                        )
                    self._require_snapshot_destination(archive, promoted)
                except Exception as primary:
                    cleanup_error: Exception | None = None
                    try:
                        self._unlink_child_if_identity(
                            storage, archive_name, archive, temp_identity
                        )
                    except Exception as exc:  # noqa: BLE001 - preserve primary integrity error.
                        cleanup_error = exc
                    if cleanup_error is not None:
                        raise primary from cleanup_error
                    raise
            finally:
                if temp_fd >= 0:
                    os.close(temp_fd)
                if temp_created and temp_identity is not None:
                    self._unlink_child_if_identity(
                        storage,
                        temp_name,
                        storage.state.path / temp_name,
                        temp_identity,
                    )
        return archive

    def remove_snapshot(self, snapshot: str | Path) -> bool:
        """Remove one snapshot returned by this manager without following aliases."""
        path = Path(os.path.abspath(snapshot))
        if path.parent != self._snapshots_state.path:
            raise ValueError(f"Snapshot path is outside snapshot storage: {path}")
        name = self._validate_filename_component(path.name)
        with self._directory_lease(self._snapshots_state) as storage:
            current = self._stat_child(storage, name)
            if current is None:
                return False
            self._require_snapshot_destination(path, current)
            identity = self._identity(current)
            return self._unlink_child_if_identity(storage, name, path, identity)

    def safe_path(self, workspace: str | Path, relative: str) -> Path:
        with self._leased_path(workspace, relative) as access:
            return access.path

    def is_protected(self, relative: str | Path) -> bool:
        return is_protected_path(relative, self.config.protected_globs)

    def read_text(self, workspace: str | Path, relative: str) -> str:
        with self._leased_path(workspace, relative) as access:
            return self._decode_text(self._read_access(access))

    def write_text(self, workspace: str | Path, relative: str, content: str) -> str | None:
        with self._leased_path(workspace, relative, create_parents=True) as access:
            encoded = self._encode_text(content)
            if access.target_identity is None:
                fd = self._open_new(access)
                before = None
            else:
                fd = self._open_existing(access, os.O_RDWR)
                before = None
            with os.fdopen(fd, "r+b") as handle:
                if access.target_identity is not None:
                    before = self._decode_text(handle.read())
                self._verify_open_file(handle.fileno(), access, allow_created=True)
                handle.seek(0)
                handle.truncate()
                handle.write(encoded)
                handle.flush()
            return before

    def patch_text(
        self, workspace: str | Path, relative: str, old: str, new: str
    ) -> tuple[str, str]:
        with self._leased_path(workspace, relative) as access:
            fd = self._open_existing(access, os.O_RDWR)
            with os.fdopen(fd, "r+b") as handle:
                text = self._decode_text(handle.read())
                if old not in text:
                    raise ValueError(f"Patch target text not found in {relative}")
                updated = text.replace(old, new, 1)
                self._verify_open_file(handle.fileno(), access)
                handle.seek(0)
                handle.truncate()
                handle.write(self._encode_text(updated))
                handle.flush()
            return text, updated

    @contextmanager
    def _leased_path(
        self,
        workspace: str | Path,
        relative: str,
        *,
        create_parents: bool = False,
        protected_identities: frozenset[tuple[int, int]] | None = None,
    ) -> Iterator[_PathAccess]:
        root = self._trusted_root(workspace)
        parts = self._relative_parts(relative)
        with ExitStack() as stack:
            parent = stack.enter_context(self._directory_lease(root))
            if protected_identities is None:
                protected_identities = frozenset(
                    self._collect_protected_identities(parent, Path())
                )
            for part in parts[:-1]:
                child_path = parent.state.path / part
                child_stat = self._stat_child(parent, part)
                if child_stat is None:
                    if not create_parents:
                        raise FileNotFoundError(child_path)
                    self._mkdir_child(parent, part, child_path)
                    child_stat = self._stat_child(parent, part)
                    if child_stat is None:
                        raise WorkspaceIntegrityError(
                            f"Directory creation failed: {child_path}"
                        )
                self._require_direct_directory(child_stat, child_path)
                child_state = _DirectoryState(child_path, self._identity(child_stat))
                parent = stack.enter_context(self._directory_lease(child_state))
            target_name = parts[-1]
            target_path = parent.state.path / target_name
            target_stat = self._stat_child(parent, target_name)
            relative_path = Path(*parts)
            if self.is_protected(relative_path):
                raise ValueError(f"Protected path is not accessible: {relative}")
            if target_stat is not None and self._is_link_or_reparse(target_stat):
                raise ValueError(f"Protected path is not accessible: {relative}")
            current = self._stat_child(parent, target_name)
            expected = self._identity(target_stat) if target_stat is not None else None
            current_identity = self._identity(current) if current is not None else None
            if current_identity != expected:
                raise WorkspaceIntegrityError(
                    f"Path changed during access: {target_path}"
                )
            yield _PathAccess(
                target_path,
                parent,
                expected,
                target_stat.st_mode if target_stat is not None else None,
                protected_identities,
            )

    def _walk_workspace(
        self,
        workspace: str | Path,
        visitor: Callable[[Path, bytes | None], None],
        *,
        read_content: bool,
    ) -> None:
        root = self._trusted_root(workspace)
        with self._directory_lease(root) as lease:
            protected_identities = frozenset(
                self._collect_protected_identities(lease, Path())
            )
            self._walk_workspace_directory(
                lease,
                Path(),
                visitor,
                read_content=read_content,
                protected_identities=protected_identities,
            )

    def _walk_workspace_directory(
        self,
        directory: _DirectoryLease,
        relative: Path,
        visitor: Callable[[Path, bytes | None], None],
        *,
        read_content: bool,
        protected_identities: frozenset[tuple[int, int]],
    ) -> None:
        for name in self._child_names(directory):
            child_relative = relative / name
            child_path = directory.state.path / name
            child_stat = self._stat_child(directory, name)
            if child_stat is None:
                raise WorkspaceIntegrityError(
                    f"Path changed during access: {child_path}"
                )
            if self.is_protected(child_relative) or self._is_link_or_reparse(child_stat):
                continue
            current = self._stat_child(directory, name)
            if current is None or self._identity(current) != self._identity(child_stat):
                raise WorkspaceIntegrityError(
                    f"Path changed during access: {child_path}"
                )
            if stat.S_ISDIR(child_stat.st_mode):
                state = _DirectoryState(child_path, self._identity(child_stat))
                with self._directory_lease(state) as child_lease:
                    self._walk_workspace_directory(
                        child_lease,
                        child_relative,
                        visitor,
                        read_content=read_content,
                        protected_identities=protected_identities,
                    )
            elif stat.S_ISREG(child_stat.st_mode):
                allowed, content = self._read_leased_file(
                    directory,
                    name,
                    child_path,
                    self._identity(child_stat),
                    protected_identities,
                    read_content=read_content,
                )
                if allowed:
                    visitor(child_relative, content)

    def _copy_source(self, source: Path, workspace: Path) -> None:
        source_path = Path(os.path.abspath(source))
        source_stat = self._direct_directory_stat(
            source_path, "Source root must be a direct directory."
        )
        source_state = _DirectoryState(source_path, self._identity(source_stat))
        with self._directory_lease(source_state) as lease:
            protected_identities = frozenset(
                self._collect_protected_identities(
                    lease, Path(), change_prefix="Source file changed during copy"
                )
            )
            self._copy_source_directory(
                lease, Path(), workspace, protected_identities
            )

    def _copy_source_directory(
        self,
        source: _DirectoryLease,
        relative: Path,
        workspace: Path,
        protected_identities: frozenset[tuple[int, int]],
    ) -> None:
        for name in self._child_names(source):
            child_relative = relative / name
            child_path = source.state.path / name
            child_stat = self._stat_child(source, name)
            if child_stat is None:
                raise WorkspaceIntegrityError(
                    f"Source file changed during copy: {child_path}"
                )
            if self.is_protected(child_relative) or self._is_link_or_reparse(
                child_stat
            ):
                continue
            current = self._stat_child(source, name)
            if current is None or self._identity(current) != self._identity(child_stat):
                raise WorkspaceIntegrityError(
                    f"Source file changed during copy: {child_path}"
                )
            if stat.S_ISDIR(child_stat.st_mode):
                state = _DirectoryState(child_path, self._identity(child_stat))
                self._ensure_workspace_directory(workspace, child_relative)
                with self._directory_lease(state) as child_lease:
                    self._copy_source_directory(
                        child_lease,
                        child_relative,
                        workspace,
                        protected_identities,
                    )
            elif stat.S_ISREG(child_stat.st_mode):
                allowed, content = self._read_leased_file(
                    source,
                    name,
                    child_path,
                    self._identity(child_stat),
                    protected_identities,
                    read_content=True,
                )
                if allowed:
                    self._write_bytes(workspace, child_relative, content or b"")

    def _write_bytes(self, workspace: Path, relative: Path, content: bytes) -> None:
        with self._leased_path(
            workspace,
            str(relative),
            create_parents=True,
            protected_identities=frozenset(),
        ) as access:
            if access.target_identity is not None:
                raise FileExistsError(f"Destination already exists: {access.path}")
            fd = self._open_new(access)
            with os.fdopen(fd, "r+b") as handle:
                self._verify_open_file(handle.fileno(), access, allow_created=True)
                handle.write(content)
                handle.flush()

    def _ensure_workspace_directory(self, workspace: Path, relative: Path) -> None:
        marker = relative / ".agentpermit-directory-marker"
        with self._leased_path(
            workspace,
            str(marker),
            create_parents=True,
            protected_identities=frozenset(),
        ):
            return

    def _read_access(self, access: _PathAccess) -> bytes:
        fd = self._open_existing(access, os.O_RDONLY)
        with os.fdopen(fd, "rb") as handle:
            return handle.read()

    def _read_leased_file(
        self,
        parent: _DirectoryLease,
        name: str,
        path: Path,
        expected: tuple[int, int],
        protected_identities: frozenset[tuple[int, int]],
        *,
        read_content: bool,
    ) -> tuple[bool, bytes | None]:
        fd = self._open_file(parent, name, path, os.O_RDONLY)
        try:
            opened_stat = os.fstat(fd)
            opened = self._identity(opened_stat)
            if opened != expected:
                raise WorkspaceIntegrityError(
                    f"Source file changed during copy: {path}"
                )
            self._require_single_link(opened_stat, path)
            current = self._stat_child(parent, name)
            if current is None or self._identity(current) != expected:
                raise WorkspaceIntegrityError(
                    f"Source file changed during copy: {path}"
                )
            if opened in protected_identities:
                return False, None
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                return True, handle.read() if read_content else None
        finally:
            if fd >= 0:
                os.close(fd)

    def _open_existing(self, access: _PathAccess, flags: int) -> int:
        if access.target_identity is None:
            raise FileNotFoundError(access.path)
        fd = self._open_file(access.parent, access.path.name, access.path, flags)
        try:
            self._verify_open_file(fd, access)
        except Exception:
            os.close(fd)
            raise
        return fd

    def _open_new(self, access: _PathAccess) -> int:
        if access.target_identity is not None:
            raise FileExistsError(access.path)
        try:
            fd = self._open_file(
                access.parent,
                access.path.name,
                access.path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                mode=0o600,
            )
        except FileExistsError as exc:
            raise WorkspaceIntegrityError(
                f"Path changed during access: {access.path}"
            ) from exc
        try:
            self._verify_open_file(fd, access, allow_created=True)
        except Exception as primary:
            cleanup_error: Exception | None = None
            try:
                self._discard_created_file(access, fd)
            except Exception as exc:  # noqa: BLE001 - preserve primary integrity error.
                cleanup_error = exc
            finally:
                try:
                    os.close(fd)
                except Exception as exc:  # noqa: BLE001 - preserve primary integrity error.
                    if cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                raise primary from cleanup_error
            raise
        return fd

    def _verify_open_file(
        self, fd: int, access: _PathAccess, *, allow_created: bool = False
    ) -> None:
        self._verify_directory_binding(access.parent)
        opened_stat = os.fstat(fd)
        opened = self._identity(opened_stat)
        expected = access.target_identity
        if expected is not None and opened != expected:
            raise WorkspaceIntegrityError(
                f"Path changed during access: {access.path}"
            )
        current = self._stat_child(access.parent, access.path.name)
        if current is None or self._identity(current) != opened:
            raise WorkspaceIntegrityError(
                f"Path changed during access: {access.path}"
            )
        self._require_single_link(opened_stat, access.path)
        if opened in access.protected_identities:
            raise ValueError(f"Protected hardlink alias is not accessible: {access.path}")
        if expected is None and not allow_created:
            raise WorkspaceIntegrityError(
                f"Path changed during access: {access.path}"
            )

    def _verify_directory_binding(self, directory: _DirectoryLease) -> None:
        try:
            current = directory.state.path.lstat()
        except FileNotFoundError as exc:
            raise WorkspaceIntegrityError(
                f"{directory.state.role} changed during access: {directory.state.path}"
            ) from exc
        if (
            self._is_link_or_reparse(current)
            or not stat.S_ISDIR(current.st_mode)
            or self._identity(current) != directory.state.identity
        ):
            raise WorkspaceIntegrityError(
                f"{directory.state.role} changed during access: {directory.state.path}"
            )

    def _discard_created_file(self, access: _PathAccess, fd: int) -> None:
        if access.parent.fd is None:
            return
        opened = self._identity(os.fstat(fd))
        current = self._stat_child(access.parent, access.path.name)
        if current is not None and self._identity(current) == opened:
            os.unlink(access.path.name, dir_fd=access.parent.fd)

    @contextmanager
    def _directory_lease(
        self, state: _DirectoryState
    ) -> Iterator[_DirectoryLease]:
        if os.name == "nt":
            handle = self._win_open_directory(state.path)
            try:
                if self._win_handle_identity(handle) != state.identity:
                    raise WorkspaceIntegrityError(
                        f"{state.role} changed during access: {state.path}"
                    )
                yield _DirectoryLease(state, handle=handle)
            finally:
                self._win_close_handle(handle)
        else:
            fd = os.open(
                state.path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if self._identity(os.fstat(fd)) != state.identity:
                    raise WorkspaceIntegrityError(
                        f"{state.role} changed during access: {state.path}"
                    )
                yield _DirectoryLease(state, fd=fd)
            finally:
                os.close(fd)

    def _trusted_root(self, workspace: str | Path) -> _DirectoryState:
        path = Path(os.path.abspath(workspace))
        if path.parent != self._storage_state.path:
            raise ValueError(f"Workspace root is outside workspace storage: {path}")
        file_stat = self._direct_directory_stat(
            path, "Workspace root must be a direct workspace root."
        )
        identity = self._identity(file_stat)
        trusted = self._trusted_roots.get(path)
        if trusted is None:
            self._trusted_roots[path] = identity
        elif trusted != identity:
            raise WorkspaceIntegrityError(
                f"Workspace root changed during access: {path}"
            )
        return _DirectoryState(path, identity, "Workspace root")

    def _fd_directory_path(self, fd: int) -> str:
        for prefix in ("/proc/self/fd", "/dev/fd"):
            candidate = f"{prefix}/{fd}"
            if os.path.isdir(candidate):
                return candidate
        raise WorkspaceIntegrityError(
            "This POSIX platform cannot bind a command cwd to the workspace directory fd."
        )

    def _collect_protected_identities(
        self,
        directory: _DirectoryLease,
        relative: Path,
        *,
        change_prefix: str = "Path changed during access",
    ) -> set[tuple[int, int]]:
        protected: set[tuple[int, int]] = set()
        for name in self._child_names(directory):
            child_relative = relative / name
            child_path = directory.state.path / name
            child_stat = self._stat_child(directory, name)
            if child_stat is None:
                raise WorkspaceIntegrityError(
                    f"{change_prefix}: {child_path}"
                )
            if self._is_link_or_reparse(child_stat):
                continue
            protected_path = self.is_protected(child_relative)
            current = self._stat_child(directory, name)
            if current is None or self._identity(current) != self._identity(child_stat):
                raise WorkspaceIntegrityError(
                    f"{change_prefix}: {child_path}"
                )
            if stat.S_ISDIR(child_stat.st_mode):
                state = _DirectoryState(child_path, self._identity(child_stat))
                with self._directory_lease(state) as child:
                    protected.update(
                        self._collect_protected_identities(
                            child,
                            child_relative,
                            change_prefix=change_prefix,
                        )
                    )
            elif stat.S_ISREG(child_stat.st_mode) and protected_path:
                protected.add(self._identity(child_stat))
        return protected

    def _remove_directory_contents(self, directory: _DirectoryLease) -> None:
        for name in self._child_names(directory):
            child_path = directory.state.path / name
            child_stat = self._stat_child(directory, name)
            if child_stat is None:
                continue
            if stat.S_ISDIR(child_stat.st_mode) and not self._is_link_or_reparse(
                child_stat
            ):
                state = _DirectoryState(child_path, self._identity(child_stat))
                with self._directory_lease(state) as child:
                    self._remove_directory_contents(child)
                current = self._stat_child(directory, name)
                if current is None or self._identity(current) != state.identity:
                    raise WorkspaceIntegrityError(
                        f"Directory changed during cleanup: {child_path}"
                    )
                self._rmdir_child(directory, name, child_path)
            else:
                self._unlink_child(directory, name, child_path)

    def _child_names(self, directory: _DirectoryLease) -> list[str]:
        target: int | Path = (
            directory.fd if directory.fd is not None else directory.state.path
        )
        with os.scandir(target) as iterator:
            return sorted(entry.name for entry in iterator)

    def _stat_child(
        self, parent: _DirectoryLease, name: str
    ) -> os.stat_result | None:
        try:
            if parent.fd is not None:
                return os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            return (parent.state.path / name).lstat()
        except FileNotFoundError:
            return None

    def _mkdir_child(
        self, parent: _DirectoryLease, name: str, path: Path
    ) -> tuple[int, int]:
        try:
            if parent.fd is not None:
                os.mkdir(name, 0o700, dir_fd=parent.fd)
            else:
                os.mkdir(path, 0o700)
        except FileExistsError as exc:
            raise WorkspaceIntegrityError(
                f"Directory changed during access: {path}"
            ) from exc
        created = self._stat_child(parent, name)
        if created is None or not stat.S_ISDIR(created.st_mode):
            raise WorkspaceIntegrityError(f"Directory creation failed: {path}")
        try:
            self._verify_directory_binding(parent)
        except Exception:
            if (
                parent.fd is not None
                and created is not None
                and stat.S_ISDIR(created.st_mode)
            ):
                current = self._stat_child(parent, name)
                if current is not None and self._identity(current) == self._identity(
                    created
                ):
                    os.rmdir(name, dir_fd=parent.fd)
            raise
        return self._identity(created)

    def _unlink_child(
        self, parent: _DirectoryLease, name: str, path: Path
    ) -> None:
        try:
            if parent.fd is not None:
                os.unlink(name, dir_fd=parent.fd)
            else:
                os.unlink(path)
        except FileNotFoundError:
            return

    def _unlink_child_if_identity(
        self,
        parent: _DirectoryLease,
        name: str,
        path: Path,
        expected_identity: tuple[int, int],
    ) -> bool:
        current = self._stat_child(parent, name)
        if current is None or self._identity(current) != expected_identity:
            return False
        self._unlink_child(parent, name, path)
        return True

    def _rmdir_child(
        self, parent: _DirectoryLease, name: str, path: Path
    ) -> None:
        if parent.fd is not None:
            os.rmdir(name, dir_fd=parent.fd)
        else:
            os.rmdir(path)

    def _replace_child(
        self,
        parent: _DirectoryLease,
        source_name: str,
        destination_name: str,
        expected_source_identity: tuple[int, int],
    ) -> None:
        self._verify_directory_binding(parent)
        source = self._stat_child(parent, source_name)
        if (
            source is None
            or self._identity(source) != expected_source_identity
        ):
            raise WorkspaceIntegrityError(
                f"Snapshot temp entry changed: {parent.state.path / source_name}"
            )
        self._require_single_link(source, parent.state.path / source_name)
        if parent.fd is not None:
            os.replace(
                source_name,
                destination_name,
                src_dir_fd=parent.fd,
                dst_dir_fd=parent.fd,
            )
        else:
            os.replace(
                parent.state.path / source_name,
                parent.state.path / destination_name,
            )

    @staticmethod
    def _require_single_link(file_stat: os.stat_result, path: Path) -> None:
        if stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink != 1:
            raise ValueError(
                f"Governed regular file has multiple hardlinks: {path}"
            )

    def _require_snapshot_destination(
        self, path: Path, file_stat: os.stat_result | None
    ) -> None:
        if file_stat is None:
            return
        if (
            self._is_link_or_reparse(file_stat)
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
        ):
            raise ValueError(f"Snapshot destination alias is not accessible: {path}")

    def _open_file(
        self,
        parent: _DirectoryLease,
        name: str,
        path: Path,
        flags: int,
        *,
        mode: int = 0o777,
    ) -> int:
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if parent.fd is not None:
            return os.open(name, flags, mode, dir_fd=parent.fd)
        return os.open(path, flags, mode)

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        candidate = Path(run_id)
        if (
            not run_id
            or candidate.is_absolute()
            or len(candidate.parts) != 1
            or candidate.parts[0] in {".", ".."}
            or ":" in candidate.parts[0]
        ):
            raise ValueError("run_id must be one safe path component")
        return candidate.parts[0]

    @staticmethod
    def _validate_filename_component(value: str) -> str:
        if (
            not value
            or value in {".", ".."}
            or any(character in value for character in ("/", "\\", ":", "\0"))
        ):
            raise ValueError("Snapshot names must be one safe filename component")
        return value

    @staticmethod
    def _relative_parts(relative: str) -> tuple[str, ...]:
        candidate = Path(relative)
        if candidate.is_absolute():
            raise ValueError(f"Path escapes workspace: {relative}")
        parts = tuple(part for part in candidate.parts if part != ".")
        if not parts or any(part == ".." for part in parts):
            raise ValueError(f"Path escapes workspace: {relative}")
        if os.name == "nt" and any(":" in part for part in parts):
            raise ValueError(f"Path component contains colon: {relative}")
        return parts

    @staticmethod
    def _identity(file_stat: os.stat_result) -> tuple[int, int]:
        return file_stat.st_dev, file_stat.st_ino

    @classmethod
    def _direct_directory_stat(cls, path: Path, message: str) -> os.stat_result:
        file_stat = path.lstat()
        cls._require_direct_directory(file_stat, path, message)
        return file_stat

    @classmethod
    def _require_direct_directory(
        cls, file_stat: os.stat_result, path: Path, message: str | None = None
    ) -> None:
        if cls._is_link_or_reparse(file_stat) or not stat.S_ISDIR(file_stat.st_mode):
            raise ValueError(message or f"Protected path is not accessible: {path}")

    @staticmethod
    def _is_link_or_reparse(file_stat: os.stat_result) -> bool:
        attributes = getattr(file_stat, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & reparse)

    @staticmethod
    def _decode_text(value: bytes) -> str:
        with io.TextIOWrapper(io.BytesIO(value), encoding="utf-8") as reader:
            return reader.read()

    @staticmethod
    def _encode_text(value: str) -> bytes:
        buffer = io.BytesIO()
        writer = io.TextIOWrapper(buffer, encoding="utf-8")
        writer.write(value)
        writer.flush()
        encoded = buffer.getvalue()
        writer.detach()
        return encoded

    @staticmethod
    def _win_open_directory(path: Path) -> int:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            str(path),
            _WIN_GENERIC_READ,
            _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_WRITE,
            None,
            _WIN_OPEN_EXISTING,
            _WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    @staticmethod
    def _win_handle_identity(handle: int) -> tuple[int, int]:
        class _FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        class _FileInfo(ctypes.Structure):
            _fields_ = [
                ("attributes", ctypes.c_uint32),
                ("creation", _FileTime),
                ("access", _FileTime),
                ("write", _FileTime),
                ("volume", ctypes.c_uint32),
                ("size_high", ctypes.c_uint32),
                ("size_low", ctypes.c_uint32),
                ("links", ctypes.c_uint32),
                ("index_high", ctypes.c_uint32),
                ("index_low", ctypes.c_uint32),
            ]

        info = _FileInfo()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.GetFileInformationByHandle(
            ctypes.c_void_p(handle), ctypes.byref(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.volume, (info.index_high << 32) | info.index_low

    @staticmethod
    def _win_close_handle(handle: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.CloseHandle(ctypes.c_void_p(handle)):
            raise ctypes.WinError(ctypes.get_last_error())
