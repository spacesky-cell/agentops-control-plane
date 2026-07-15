from __future__ import annotations

import ctypes
import codecs
import os
import select
import shutil
import signal
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import PolicyConfig
from .models import structured_command_argv
from .workspace import WorkspaceManager


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": "List files in the isolated run workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Optional glob pattern. Defaults to **/*.",
                },
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
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                },
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
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                },
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
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                },
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
                "program": {
                    "type": "string",
                    "description": "Executable name or path.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments passed as exact argv elements.",
                },
            },
            "required": ["program", "args"],
            "additionalProperties": False,
        },
    },
]


def list_tool_definitions() -> list[dict[str, Any]]:
    return deepcopy(TOOL_DEFINITIONS)


class ToolExecutor:
    _KILL_GRACE_SECONDS = 1.0
    _TERM_GRACE_SECONDS = 0.1
    _WINDOWS_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".ps1"})

    def __init__(
        self, workspace_manager: WorkspaceManager, config: PolicyConfig
    ) -> None:
        self.workspace_manager = workspace_manager
        self.config = config

    def execute(self, workspace: Path, tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "list_files":
            return self.list_files(workspace, args.get("pattern"))
        if tool_name == "read_file":
            return self.read_file(workspace, str(args["path"]))
        if tool_name == "write_file":
            return self.write_file(
                workspace, str(args["path"]), str(args.get("content", ""))
            )
        if tool_name == "patch_text":
            return self.patch_text(
                workspace,
                str(args["path"]),
                str(args.get("old", "")),
                str(args.get("new", "")),
            )
        if tool_name == "run_command":
            argv = structured_command_argv(args)
            return self.run_command(workspace, argv[0], argv[1:])
        raise ValueError(f"Unknown tool: {tool_name}")

    def list_files(self, workspace: Path, pattern: str | None = None) -> list[str]:
        return self.workspace_manager.list_files(workspace, pattern)

    def read_file(self, workspace: Path, relative: str) -> str:
        return self.workspace_manager.read_text(workspace, relative)

    def write_file(
        self, workspace: Path, relative: str, content: str
    ) -> dict[str, Any]:
        before = self.workspace_manager.write_text(workspace, relative, content)
        return {
            "path": relative,
            "created": before is None,
            "before_chars": len(before or ""),
            "after_chars": len(content),
        }

    def patch_text(
        self, workspace: Path, relative: str, old: str, new: str
    ) -> dict[str, Any]:
        text, updated = self.workspace_manager.patch_text(workspace, relative, old, new)
        return {
            "path": relative,
            "before_chars": len(text),
            "after_chars": len(updated),
            "replacements": 1,
        }

    def run_command(
        self, workspace: Path, program: str, args: list[str]
    ) -> dict[str, Any]:
        output = _BoundedOutput(self.config.max_output_chars)
        timed_out = False
        with self.workspace_manager.command_cwd_lease(workspace) as cwd_lease:
            deadline = time.monotonic() + self.config.max_command_seconds
            environment = self._minimal_environment(cwd_lease.cwd)
            executable, executable_args = self._resolve_program(
                program, args, environment, cwd_lease.cwd
            )
            popen_kwargs: dict[str, Any] = {
                "cwd": cwd_lease.cwd,
                "env": environment,
                "shell": False,
                "stdin": subprocess.DEVNULL,
            }
            job: _WindowsJob | None = None
            if os.name == "nt":
                job = _WindowsJob()
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED
                )
            else:
                popen_kwargs["start_new_session"] = True
                popen_kwargs["pass_fds"] = cwd_lease.pass_fds
            process: subprocess.Popen[str] | None = None
            readers: list[tuple[threading.Thread, _OwnedReadFd]] = []
            output_owners: list[_OwnedReadFd] = []
            write_fds: list[int] = []
            try:
                output_owners, write_fds = self._create_output_pipes()
                popen_kwargs["stdout"] = write_fds[0]
                popen_kwargs["stderr"] = write_fds[1]
                try:
                    process = subprocess.Popen(
                        [executable, *executable_args], **popen_kwargs
                    )
                finally:
                    self._close_fds(write_fds)
                if job is not None:
                    job.assign_and_resume(process)
                readers = self._start_readers(output_owners, output)
                if not self._wait_process_until(process, deadline):
                    timed_out = True
                elif not self._join_readers_until(readers, deadline):
                    timed_out = True
                if timed_out:
                    kill_deadline = time.monotonic() + self._KILL_GRACE_SECONDS
                    self._terminate_process_tree(process, job, kill_deadline)
                    self._close_reader_pipes(readers)
                    self._join_readers_until(readers, kill_deadline)
            except BaseException:
                if process is not None:
                    kill_deadline = time.monotonic() + self._KILL_GRACE_SECONDS
                    self._terminate_process_tree(process, job, kill_deadline)
                    self._close_reader_pipes(readers)
                    self._join_readers_until(readers, kill_deadline)
                raise
            finally:
                self._close_reader_pipes(readers)
                for owner in output_owners:
                    owner.close()
                self._close_fds(write_fds)
                if job is not None:
                    job.close()
            if process is None:
                raise RuntimeError("Command process did not start.")
        return {
            "program": program,
            "args": list(args),
            "exit_code": None if timed_out else process.returncode,
            "output": output.value(),
            "output_truncated": output.truncated,
            "timed_out": timed_out,
        }

    def _minimal_environment(self, leased_cwd: str) -> dict[str, str]:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": leased_cwd,
            "USERPROFILE": leased_cwd,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            environment.update(
                {
                    "SystemRoot": system_root,
                    "WINDIR": system_root,
                    "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
                    "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                    "TEMP": leased_cwd,
                    "TMP": leased_cwd,
                }
            )
        return environment

    def _resolve_program(
        self,
        program: str,
        args: list[str],
        environment: dict[str, str],
        cwd: str | None = None,
    ) -> tuple[str, list[str]]:
        search_path = self._absolute_search_path(
            environment.get("PATH", os.defpath), cwd or os.getcwd()
        )
        requested = Path(program)
        if os.name == "nt":
            self._validate_windows_program_path(program)
            if requested.suffix.lower() in self._WINDOWS_SCRIPT_SUFFIXES:
                raise ValueError(
                    "Windows batch and script programs are not executable tools."
                )
        if os.name == "nt" and program in {"npm", "pnpm"}:
            return self._resolve_windows_node_adapter(program, args, search_path)
        if requested.is_absolute() or requested.parent != Path("."):
            candidate = (
                requested if requested.is_absolute() else Path(cwd or ".") / requested
            )
            resolved = self._canonical_file(candidate, "Command program")
        else:
            located = shutil.which(program, path=search_path)
            if not located:
                raise FileNotFoundError(f"Command program not found: {program}")
            resolved = self._canonical_file(Path(located), "Command program")
        if (
            os.name == "nt"
            and Path(resolved).suffix.lower() in self._WINDOWS_SCRIPT_SUFFIXES
        ):
            raise ValueError(
                "Windows batch and script programs are not executable tools."
            )
        return resolved, list(args)

    def _resolve_windows_node_adapter(
        self, program: str, args: list[str], search_path: str
    ) -> tuple[str, list[str]]:
        shim = shutil.which(program, path=search_path)
        node = shutil.which("node", path=search_path)
        if not shim or not node:
            raise FileNotFoundError(f"Safe Windows {program} adapter is unavailable.")
        self._validate_windows_program_path(shim)
        self._validate_windows_program_path(node)
        shim_path = Path(self._canonical_file(Path(shim), f"Windows {program} shim"))
        install_dir = shim_path.parent.resolve(strict=True)
        node_path = Path(self._canonical_file(Path(node), "Windows node executable"))
        if node_path.suffix.lower() != ".exe":
            raise ValueError("Safe Windows node adapter must use node.exe.")
        relative_cli = (
            Path("node_modules/npm/bin/npm-cli.js")
            if program == "npm"
            else Path("node_modules/pnpm/bin/pnpm.cjs")
        )
        candidates = [
            install_dir / relative_cli,
            node_path.parent / relative_cli,
        ]
        cli = next(
            (
                Path(self._canonical_file(candidate, f"Windows {program} CLI"))
                for candidate in candidates
                if candidate.is_file()
            ),
            None,
        )
        if cli is None:
            raise FileNotFoundError(
                f"Safe Windows {program} CLI entrypoint was not found."
            )
        return str(node_path), [str(cli), *args]

    def _absolute_search_path(self, search_path: str, cwd: str) -> str:
        base = Path(cwd)
        entries: list[str] = []
        for raw_entry in search_path.split(os.pathsep):
            entry = Path(raw_entry or ".")
            candidate = entry if entry.is_absolute() else base / entry
            entries.append(str(candidate.resolve(strict=False)))
        return os.pathsep.join(entries)

    def _canonical_file(self, path: Path, label: str) -> str:
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise FileNotFoundError(f"{label} not found: {path}") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"{label} is not a file: {resolved}")
        if os.name == "nt":
            self._validate_windows_program_path(str(resolved))
        return str(resolved)

    def _validate_windows_program_path(self, program: str) -> None:
        normalized = program.replace("/", "\\")
        lowered = normalized.lower()
        if lowered.startswith(("\\\\.\\", "\\\\?\\")):
            raise ValueError("Windows device program paths are not allowed.")
        _drive, tail = os.path.splitdrive(normalized)
        reserved = {"con", "prn", "aux", "nul"}
        for component in (part for part in tail.split("\\") if part):
            if component.endswith((" ", ".")):
                raise ValueError(
                    "Windows batch or script program paths cannot end in space or dot."
                )
            if ":" in component:
                raise ValueError(
                    "Windows alternate data stream programs are not allowed."
                )
            stem = component.split(".", 1)[0].lower()
            if (
                stem in reserved
                or stem.startswith("com")
                and stem[3:].isdigit()
                or stem.startswith("lpt")
                and stem[3:].isdigit()
            ):
                raise ValueError("Windows device program names are not allowed.")

    def _create_output_pipes(self) -> tuple[list["_OwnedReadFd"], list[int]]:
        owners: list[_OwnedReadFd] = []
        write_fds: list[int] = []
        try:
            for _stream in range(2):
                read_fd, write_fd = os.pipe()
                owners.append(_OwnedReadFd(read_fd))
                write_fds.append(write_fd)
        except BaseException:
            for owner in owners:
                owner.close()
            self._close_fds(write_fds)
            raise
        return owners, write_fds

    def _close_fds(self, fds: list[int]) -> None:
        while fds:
            fd = fds.pop()
            try:
                os.close(fd)
            except OSError:
                pass

    def _start_readers(
        self, owners: list["_OwnedReadFd"], output: "_BoundedOutput"
    ) -> list[tuple[threading.Thread, "_OwnedReadFd"]]:
        readers: list[tuple[threading.Thread, _OwnedReadFd]] = []
        for owner in owners:
            reader = threading.Thread(
                target=self._drain_fd, args=(owner, output), daemon=True
            )
            reader.start()
            readers.append((reader, owner))
        return readers

    def _wait_process_until(
        self, process: subprocess.Popen[str], deadline: float
    ) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return process.poll() is not None
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _join_readers_until(
        self,
        readers: list[tuple[threading.Thread, "_OwnedReadFd"]],
        deadline: float,
    ) -> bool:
        for reader, _owner in readers:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                reader.join(remaining)
        return all(not reader.is_alive() for reader, _owner in readers)

    def _close_reader_pipes(
        self, readers: list[tuple[threading.Thread, "_OwnedReadFd"]]
    ) -> None:
        for _reader, owner in readers:
            owner.close()

    def _drain_fd(self, owner: "_OwnedReadFd", output: "_BoundedOutput") -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = owner.read(4096)
                if not chunk:
                    break
                decoded = decoder.decode(chunk)
                if decoded:
                    output.add(decoded)
        except OSError:
            pass
        finally:
            decoded = decoder.decode(b"", final=True)
            if decoded:
                output.add(decoded)
            owner.close()

    def _terminate_process_tree(
        self,
        process: subprocess.Popen[str],
        job: "_WindowsJob | None" = None,
        deadline: float | None = None,
    ) -> None:
        deadline = deadline or (time.monotonic() + self._KILL_GRACE_SECONDS)
        if os.name == "nt":
            try:
                if job is None or not job.assigned:
                    raise OSError("Command process was not assigned to its Job Object.")
                job.terminate()
            except OSError:
                self._taskkill_fallback(process.pid)
            if process.poll() is None and not self._wait_process_until(
                process, deadline
            ):
                process.kill()
                self._wait_process_until(process, deadline)
            return
        self._terminate_posix_process_tree(process, deadline)

    def _taskkill_fallback(self, pid: int) -> None:
        taskkill = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "taskkill.exe"
        )
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=self._KILL_GRACE_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _terminate_posix_process_tree(
        self, process: subprocess.Popen[str], deadline: float | None = None
    ) -> None:
        deadline = deadline or (time.monotonic() + self._KILL_GRACE_SECONDS)
        killpg = getattr(os, "killpg", None)
        sigterm = getattr(signal, "SIGTERM", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if killpg is None or sigterm is None or sigkill is None:
            self._wait_process_until(process, deadline)
            return
        try:
            killpg(process.pid, sigterm)
        except ProcessLookupError:
            pass
        threading.Event().wait(
            max(0.0, min(self._TERM_GRACE_SECONDS, deadline - time.monotonic()))
        )
        try:
            killpg(process.pid, sigkill)
        except ProcessLookupError:
            pass
        self._wait_process_until(process, deadline)


class _OwnedReadFd:
    _POLL_SECONDS = 0.01

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def read(self, size: int) -> bytes:
        while True:
            with self._lock:
                fd = self._fd
                if fd is None:
                    return b""
                if os.name == "nt":
                    available = self._windows_available(fd)
                    if available is not None:
                        if available == 0:
                            return b""
                        return os.read(fd, min(size, available))
                elif select.select([fd], [], [], 0)[0]:
                    return os.read(fd, size)
            self._closed.wait(self._POLL_SECONDS)

    def close(self) -> None:
        with self._lock:
            fd = self._fd
            self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self._closed.set()

    def _windows_available(self, fd: int) -> int | None:
        import msvcrt

        available = ctypes.c_uint32()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.PeekNamedPipe.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        kernel32.PeekNamedPipe.restype = ctypes.c_int
        if kernel32.PeekNamedPipe(
            ctypes.c_void_p(msvcrt.get_osfhandle(fd)),
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        ):
            return available.value or None
        error = ctypes.get_last_error()
        if error in {109, 232}:
            return 0
        raise ctypes.WinError(error)


_WINDOWS_CREATE_SUSPENDED = 0x00000004


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operations", ctypes.c_ulonglong),
        ("write_operations", ctypes.c_ulonglong),
        ("other_operations", ctypes.c_ulonglong),
        ("read_bytes", ctypes.c_ulonglong),
        ("write_bytes", ctypes.c_ulonglong),
        ("other_bytes", ctypes.c_ulonglong),
    ]


class _WindowsBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_longlong),
        ("per_job_user_time", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _WindowsExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic", _WindowsBasicLimitInformation),
        ("io", _WindowsIoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _WindowsJob:
    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_SUSPEND_RESUME = 0x0800

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable on this platform.")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._configure_functions()
        self.handle = self._kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.assigned = False
        try:
            limits = _WindowsExtendedLimitInformation()
            limits.basic.limit_flags = self._KILL_ON_JOB_CLOSE
            if not self._kernel32.SetInformationJobObject(
                self.handle,
                self._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def _configure_functions(self) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
        ]
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._kernel32.SetInformationJobObject.restype = ctypes.c_int
        self._kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        self._kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        self._kernel32.TerminateJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._kernel32.TerminateJobObject.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self._ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        self._ntdll.NtResumeProcess.restype = ctypes.c_long

    def assign_and_resume(self, process: subprocess.Popen[str]) -> None:
        process_handle = self._kernel32.OpenProcess(
            self._PROCESS_TERMINATE
            | self._PROCESS_SET_QUOTA
            | self._PROCESS_SUSPEND_RESUME,
            False,
            process.pid,
        )
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self.handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.assigned = True
            status = self._ntdll.NtResumeProcess(process_handle)
            if status < 0:
                raise OSError(
                    f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08X}"
                )
        finally:
            self._kernel32.CloseHandle(process_handle)

    def terminate(self) -> None:
        if self.handle and not self._kernel32.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle = getattr(self, "handle", None)
        self.handle = None
        if handle:
            self._kernel32.CloseHandle(handle)


class _BoundedOutput:
    _TRUNCATION_MARKER = "\n[output truncated]"

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self._chunks: list[str] = []
        self._retained_chars = 0
        self.truncated = False
        self._lock = threading.Lock()

    def add(self, chunk: str) -> None:
        with self._lock:
            remaining = self.max_chars - self._retained_chars
            if remaining > 0:
                retained = chunk[:remaining]
                self._chunks.append(retained)
                self._retained_chars += len(retained)
            if len(chunk) > max(remaining, 0):
                self.truncated = True

    def value(self) -> str:
        with self._lock:
            value = "".join(self._chunks)
            if not self.truncated:
                return value
            marker = self._TRUNCATION_MARKER[: self.max_chars]
            return value[: self.max_chars - len(marker)] + marker
