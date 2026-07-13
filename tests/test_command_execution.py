from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentpermit.config import PolicyConfig
import agentpermit.tools as tools_module
from agentpermit.tools import ToolExecutor, list_tool_definitions
from agentpermit.workspace import WorkspaceManager


def make_executor(tmp_path: Path, **config_overrides):
    config = PolicyConfig(**config_overrides)
    manager = WorkspaceManager(tmp_path / ".agentpermit", config)
    workspace = manager.create("run_command_test")
    return ToolExecutor(manager, config), manager, workspace


def run_python(executor: ToolExecutor, workspace: Path, code: str, *args: str):
    return executor.execute(
        workspace,
        "run_command",
        {"program": sys.executable, "args": ["-c", code, *args]},
    )


def test_run_command_schema_is_structured_and_rejects_legacy_command(tmp_path):
    definition = next(
        item for item in list_tool_definitions() if item["name"] == "run_command"
    )
    schema = definition["inputSchema"]
    executor, _manager, workspace = make_executor(tmp_path)

    assert schema["required"] == ["program", "args"]
    assert schema["properties"] == {
        "program": {"type": "string", "description": "Executable name or path."},
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Arguments passed as exact argv elements.",
        },
    }
    with pytest.raises(ValueError, match="program.*args"):
        executor.execute(
            workspace,
            "run_command",
            {"command": f'{sys.executable} -c "print(1)"'},
        )


def test_run_command_preserves_quoted_argument_elements(tmp_path):
    executor, _manager, workspace = make_executor(tmp_path)
    expected = ["two words", '"already quoted"', "a&b", "semi;colon"]

    result = run_python(
        executor,
        workspace,
        "import json,sys; print(json.dumps(sys.argv[1:]))",
        *expected,
    )

    assert result["program"] == sys.executable
    assert result["args"][-4:] == expected
    assert json.loads(result["output"]) == expected
    assert result["exit_code"] == 0
    assert result["timed_out"] is False


def test_run_command_uses_minimal_environment_without_credentials(tmp_path, monkeypatch):
    executor, _manager, workspace = make_executor(tmp_path)
    monkeypatch.setenv("AGENTPERMIT_TEST_TOKEN", "credential-must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "credential-must-not-leak")

    result = run_python(
        executor,
        workspace,
        "import json,os; print(json.dumps(dict(os.environ), sort_keys=True))",
    )
    environment = json.loads(result["output"])

    assert "AGENTPERMIT_TEST_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["HOME"] == str(workspace)
    assert environment["USERPROFILE"] == str(workspace)
    assert "PATH" in environment


def test_run_command_continuously_drains_and_bounds_both_output_streams(tmp_path):
    executor, _manager, workspace = make_executor(tmp_path, max_output_chars=256)

    result = run_python(
        executor,
        workspace,
        "import sys; sys.stdout.write('o' * 200000); sys.stdout.flush(); "
        "sys.stderr.write('e' * 200000); sys.stderr.flush()",
    )

    assert result["exit_code"] == 0
    assert result["output_truncated"] is True
    assert len(result["output"]) <= 256
    assert result["output"].endswith("\n[output truncated]")


def test_owned_read_fd_cannot_close_an_unrelated_reused_descriptor():
    original_read, original_write = os.pipe()
    owner = tools_module._OwnedReadFd(original_read)
    owner.close()
    os.close(original_write)
    replacement_read, replacement_write = os.pipe()
    if replacement_read != original_read:
        os.dup2(replacement_read, original_read)
        os.close(replacement_read)
        replacement_read = original_read

    try:
        owner.close()
        os.write(replacement_write, b"x")
        assert os.read(replacement_read, 1) == b"x"
    finally:
        os.close(replacement_read)
        os.close(replacement_write)


def test_popen_start_failure_closes_all_executor_owned_pipe_fds(
    tmp_path, monkeypatch
):
    executor, _manager, workspace = make_executor(tmp_path)
    created_fds = []
    real_pipe = os.pipe

    def recording_pipe():
        pair = real_pipe()
        created_fds.extend(pair)
        return pair

    def fail_popen(*args, **kwargs):
        raise OSError("synthetic Popen failure")

    monkeypatch.setattr(os, "pipe", recording_pipe)
    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with pytest.raises(OSError, match="synthetic Popen failure"):
        run_python(executor, workspace, "print('never')")

    assert len(created_fds) == 4
    for fd in created_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job cleanup")
@pytest.mark.parametrize("failure_on", [1, 2])
def test_pipe_allocation_failure_closes_partial_fds_and_job_once(
    tmp_path, monkeypatch, failure_on
):
    executor, _manager, workspace = make_executor(tmp_path)
    created_fds = []
    real_pipe = os.pipe
    calls = 0
    jobs = []

    class FakeJob:
        assigned = False

        def __init__(self):
            self.close_count = 0
            jobs.append(self)

        def close(self):
            self.close_count += 1

    def failing_pipe():
        nonlocal calls
        calls += 1
        if calls == failure_on:
            raise OSError("synthetic pipe allocation failure")
        pair = real_pipe()
        created_fds.extend(pair)
        return pair

    monkeypatch.setattr(tools_module, "_WindowsJob", FakeJob)
    monkeypatch.setattr(os, "pipe", failing_pipe)

    with pytest.raises(OSError, match="synthetic pipe allocation failure"):
        run_python(executor, workspace, "print('never')")

    assert len(jobs) == 1
    assert jobs[0].close_count == 1
    for fd in created_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_run_command_timeout_terminates_descendant_process(tmp_path):
    executor, _manager, workspace = make_executor(tmp_path, max_command_seconds=3)
    pid_file = workspace / "child.pid"
    child_code = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(300)"
    )
    parent_code = "\n".join(
        [
            "import pathlib,subprocess,sys,threading,time",
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])",
            "target=pathlib.Path(sys.argv[2])",
            "deadline=time.monotonic()+10",
            "event=threading.Event()",
            "while not target.exists() and time.monotonic()<deadline:",
            "    event.wait(.01)",
            "child.wait()",
        ]
    )

    result = run_python(
        executor,
        workspace,
        parent_code,
        child_code,
        str(pid_file),
    )

    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert pid_file.exists()
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 15
    while process_is_alive(child_pid) and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert not process_is_alive(child_pid)


def test_single_deadline_covers_descendant_that_holds_inherited_pipes(tmp_path):
    executor, _manager, workspace = make_executor(
        tmp_path, max_command_seconds=1
    )
    pid_file = workspace / "pipe-child.pid"
    child_code = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(4)"
    )
    parent_code = "\n".join(
        [
            "import pathlib,subprocess,sys,threading,time",
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])",
            "target=pathlib.Path(sys.argv[2])",
            "deadline=time.monotonic()+10",
            "event=threading.Event()",
            "while not target.exists() and time.monotonic()<deadline:",
            "    event.wait(.01)",
        ]
    )

    started = time.monotonic()
    result = run_python(
        executor, workspace, parent_code, child_code, str(pid_file)
    )
    elapsed = time.monotonic() - started

    assert result["timed_out"] is True
    assert elapsed < 2.5
    assert pid_file.exists()
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 15
    while process_is_alive(child_pid) and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert not process_is_alive(child_pid)


def test_command_cwd_lease_prevents_root_reopen_race(tmp_path, monkeypatch):
    executor, _manager, workspace = make_executor(tmp_path)
    (workspace / "identity.txt").write_text("trusted", encoding="utf-8")
    original = workspace.with_name(f"{workspace.name}-original")
    replacement = workspace.with_name(f"{workspace.name}-replacement")
    replacement.mkdir()
    (replacement / "identity.txt").write_text("replacement", encoding="utf-8")
    real_popen = subprocess.Popen
    replacement_attempted = False

    def replacing_popen(*args, **kwargs):
        nonlocal replacement_attempted
        replacement_attempted = True
        if os.name == "nt":
            with pytest.raises(PermissionError):
                os.replace(workspace, original)
        else:
            os.replace(workspace, original)
            os.replace(replacement, workspace)
        assert kwargs["env"]["HOME"] == kwargs["cwd"]
        assert kwargs["env"]["USERPROFILE"] == kwargs["cwd"]
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", replacing_popen)
    result = run_python(
        executor,
        workspace,
        "import os; from pathlib import Path; "
        "print(Path('identity.txt').read_text()); "
        "print(Path(os.environ['HOME']).samefile(Path.cwd()))",
    )

    assert replacement_attempted
    assert result["output"].splitlines() == ["trusted", "True"]


@pytest.mark.skipif(os.name != "nt", reason="Windows batch execution regression")
@pytest.mark.parametrize("requested_suffix", ["", " ", "."])
def test_windows_rejects_batch_arguments_that_could_trigger_shell_injection(
    tmp_path, requested_suffix
):
    executor, _manager, workspace = make_executor(tmp_path)
    batch = workspace / "unsafe.cmd"
    injected = workspace / "AGENTPERMIT_INJECTED"
    batch.write_text(
        f"@echo off\r\necho injected > {injected}\r\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="batch|script"):
        executor.execute(
            workspace,
            "run_command",
            {
                "program": f"{batch}{requested_suffix}",
                "args": ["safe & echo AGENTPERMIT_INJECTED"],
            },
        )
    assert not injected.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization")
@pytest.mark.parametrize("suffix", [".bat", ".ps1"])
def test_windows_rejects_other_script_suffixes(tmp_path, suffix):
    executor, _manager, workspace = make_executor(tmp_path)
    script = workspace / f"unsafe{suffix}"
    script.write_text("unsafe", encoding="utf-8")

    with pytest.raises(ValueError, match="batch|script"):
        executor.execute(
            workspace,
            "run_command",
            {"program": str(script), "args": []},
        )


def test_relative_path_search_resolves_inspected_program_to_strict_absolute_path(
    tmp_path, monkeypatch
):
    executor, _manager, workspace = make_executor(tmp_path)
    candidate = workspace / ("relative.exe" if os.name == "nt" else "relative")
    candidate.write_bytes(b"inspected executable")
    seen_paths = []

    def fake_which(program, path=None):
        seen_paths.append(path)
        return str(Path(path.split(os.pathsep)[0]) / candidate.name)

    monkeypatch.setattr(shutil, "which", fake_which)

    resolved, resolved_args = executor._resolve_program(
        candidate.name,
        ["arg"],
        {"PATH": ".", "PATHEXT": ".EXE"},
        str(workspace),
    )

    assert seen_paths == [str(workspace.resolve())]
    assert Path(resolved).is_absolute()
    assert Path(resolved).samefile(candidate)
    assert resolved_args == ["arg"]


@pytest.mark.skipif(os.name != "nt", reason="Windows npm adapter")
@pytest.mark.parametrize(
    ("program", "relative_cli"),
    [
        ("npm", Path("node_modules/npm/bin/npm-cli.js")),
        ("pnpm", Path("node_modules/pnpm/bin/pnpm.cjs")),
    ],
)
def test_windows_npm_adapters_use_node_and_known_cli_without_reading_shim(
    tmp_path, monkeypatch, program, relative_cli
):
    executor, _manager, workspace = make_executor(tmp_path)
    install = tmp_path / "node-install"
    install.mkdir()
    shim = install / f"{program}.cmd"
    shim.write_text("@echo MALICIOUS-SHIM-CONTENT", encoding="utf-8")
    node = install / "node.exe"
    node.write_bytes(b"")
    cli = install / relative_cli
    cli.parent.mkdir(parents=True)
    cli.write_text("// known CLI", encoding="utf-8")

    def fake_which(candidate, path=None):
        return str(shim if candidate == program else node if candidate == "node" else "")

    monkeypatch.setattr("shutil.which", fake_which)

    resolved, resolved_args = executor._resolve_program(
        program, ["test", "a&b"], {"PATH": str(install), "PATHEXT": ".EXE;.CMD"}
    )

    assert resolved == str(node)
    assert resolved_args == [str(cli), "test", "a&b"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_command_seconds", True),
        ("max_command_seconds", 1.5),
        ("max_command_seconds", 0),
        ("max_output_chars", False),
        ("max_output_chars", 64.0),
        ("max_output_chars", -1),
    ],
)
def test_command_limits_require_real_positive_integers(field, value):
    with pytest.raises(ValueError, match=field):
        PolicyConfig(**{field: value})


def test_command_output_decodes_utf8_and_replaces_invalid_bytes(tmp_path):
    executor, _manager, workspace = make_executor(tmp_path)

    result = run_python(
        executor, workspace, "import sys; sys.stdout.buffer.write(b'valid\\xff')"
    )

    assert result["output"] == "valid\ufffd"


def test_posix_timeout_escalates_for_descendants_after_parent_exits(
    tmp_path, monkeypatch
):
    executor, _manager, _workspace = make_executor(tmp_path)
    signals = []

    class ExitedParent:
        pid = 12345

        def wait(self, timeout=None):
            return -signal.SIGTERM

        def poll(self):
            return -signal.SIGTERM

    import signal

    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        os, "killpg", lambda pid, sent: signals.append((pid, sent)), raising=False
    )

    executor._terminate_posix_process_tree(ExitedParent())

    assert signals == [
        (ExitedParent.pid, signal.SIGTERM),
        (ExitedParent.pid, signal.SIGKILL),
    ]


def process_is_alive(pid: int) -> bool:
    if os.name != "nt":
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            fields = stat_path.read_text(encoding="utf-8").split()
            return len(fields) > 2 and fields[2] != "Z"
    try:
        os.kill(pid, 0)
    except (OSError, SystemError):
        return False
    return True
