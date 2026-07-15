from __future__ import annotations

import difflib
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiffLimits:
    max_files: int = 200
    max_total_bytes: int = 8 * 1024 * 1024
    max_text_bytes: int = 128 * 1024
    max_diff_lines: int = 400
    max_diff_chars: int = 64 * 1024


@dataclass(frozen=True)
class DiffEntry:
    path: str
    status: str
    display: str
    before_size: int | None
    after_size: int | None
    diff: str | None


@dataclass(frozen=True)
class SnapshotDiff:
    available: bool
    reason: str | None
    counts: dict[str, int]
    entries: tuple[DiffEntry, ...]


def compare_snapshot_archives(
    before: str | Path,
    after: str | Path,
    *,
    limits: DiffLimits | None = None,
) -> SnapshotDiff:
    effective_limits = limits or DiffLimits()
    before_path = Path(before)
    after_path = Path(after)
    if not before_path.is_file() or not after_path.is_file():
        return _unavailable("snapshot_missing")
    try:
        before_files = _read_archive(before_path, effective_limits)
        after_files = _read_archive(after_path, effective_limits)
    except _SnapshotLimitError:
        return _unavailable("snapshot_limits_exceeded")
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return _unavailable("snapshot_invalid")

    all_paths = sorted(set(before_files) | set(after_files))
    if len(all_paths) > effective_limits.max_files:
        return _unavailable("snapshot_limits_exceeded")

    counts = {"created": 0, "modified": 0, "deleted": 0, "unchanged": 0}
    entries: list[DiffEntry] = []
    for path in all_paths:
        before_content = before_files.get(path)
        after_content = after_files.get(path)
        if before_content is None:
            status = "created"
        elif after_content is None:
            status = "deleted"
        elif before_content == after_content:
            status = "unchanged"
        else:
            status = "modified"
        counts[status] += 1
        if status == "unchanged":
            continue
        entries.append(
            _make_entry(
                path,
                status,
                before_content,
                after_content,
                effective_limits,
            )
        )
    return SnapshotDiff(True, None, counts, tuple(entries))


class _SnapshotLimitError(ValueError):
    pass


def _unavailable(reason: str) -> SnapshotDiff:
    return SnapshotDiff(False, reason, {}, ())


def _read_archive(path: Path, limits: DiffLimits) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) > limits.max_files:
            raise _SnapshotLimitError
        total_size = sum(member.file_size for member in members)
        if total_size > limits.max_total_bytes:
            raise _SnapshotLimitError
        files: dict[str, bytes] = {}
        for member in members:
            normalized = Path(member.filename.replace("\\", "/"))
            if (
                normalized.is_absolute()
                or not normalized.parts
                or any(part in {"", ".", ".."} for part in normalized.parts)
            ):
                raise ValueError("Unsafe snapshot member path")
            name = normalized.as_posix()
            if name in files:
                raise ValueError("Duplicate snapshot member path")
            content = archive.read(member)
            if len(content) != member.file_size:
                raise ValueError("Snapshot member size mismatch")
            files[name] = content
        return files


def _make_entry(
    path: str,
    status: str,
    before: bytes | None,
    after: bytes | None,
    limits: DiffLimits,
) -> DiffEntry:
    before_size = len(before) if before is not None else None
    after_size = len(after) if after is not None else None
    contents = [content for content in (before, after) if content is not None]
    if any(len(content) > limits.max_text_bytes for content in contents):
        return DiffEntry(path, status, "oversized", before_size, after_size, None)
    decoded: list[str | None] = []
    for content in (before, after):
        if content is None:
            decoded.append(None)
            continue
        if b"\0" in content:
            return DiffEntry(path, status, "binary", before_size, after_size, None)
        try:
            decoded.append(content.decode("utf-8"))
        except UnicodeDecodeError:
            return DiffEntry(path, status, "binary", before_size, after_size, None)

    diff_lines = difflib.unified_diff(
        (decoded[0] or "").splitlines(),
        (decoded[1] or "").splitlines(),
        fromfile=f"before/{path}",
        tofile=f"after/{path}",
        lineterm="",
    )
    rendered: list[str] = []
    rendered_chars = 0
    truncated = False
    for line in diff_lines:
        addition = len(line) + (1 if rendered else 0)
        if (
            len(rendered) >= limits.max_diff_lines
            or rendered_chars + addition > limits.max_diff_chars
        ):
            truncated = True
            break
        rendered.append(line)
        rendered_chars += addition
    if truncated:
        marker = "... diff truncated by dashboard limits ..."
        if rendered_chars + len(marker) + 1 <= limits.max_diff_chars:
            rendered.append(marker)
    if not rendered and before != after:
        before_style = _newline_style(before)
        after_style = _newline_style(after)
        rendered = [
            f"--- before/{path}",
            f"+++ after/{path}",
            "@@ newline metadata @@",
            f"- newline style: {before_style}",
            f"+ newline style: {after_style}",
        ]
    return DiffEntry(path, status, "text", before_size, after_size, "\n".join(rendered))


def _newline_style(content: bytes | None) -> str:
    if content is None:
        return "not present"
    has_crlf = b"\r\n" in content
    normalized = content.replace(b"\r\n", b"")
    has_lf = b"\n" in normalized
    has_cr = b"\r" in content.replace(b"\r\n", b"")
    styles = []
    if has_crlf:
        styles.append("CRLF")
    if has_lf:
        styles.append("LF")
    if has_cr:
        styles.append("CR")
    label = "/".join(styles) if styles else "none"
    final = "present" if content.endswith((b"\n", b"\r")) else "absent"
    return f"{label}, final newline={final}"
