"""Validate that release tags and all package metadata identify one version."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


VERSION_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
RUNTIME_VERSION_RE = re.compile(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
CHANGELOG_HEADING = "## {version}"


class ReleaseValidationError(ValueError):
    """Raised when a release input does not agree with the requested tag."""


def _read_npm_version(path: Path) -> str:
    try:
        with tarfile.open(path, "r:gz") as archive:
            try:
                member_info = archive.getmember("package/package.json")
            except KeyError as exc:
                raise ReleaseValidationError(
                    "npm tarball is missing package/package.json"
                ) from exc
            member = archive.extractfile(member_info)
            if member is None:
                raise ReleaseValidationError(
                    "npm tarball is missing package/package.json"
                )
            manifest = json.load(member)
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"cannot read npm tarball {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ReleaseValidationError("npm package.json must contain a JSON object")
    version = manifest.get("version")
    if not isinstance(version, str):
        raise ReleaseValidationError("npm package.json has no string version")
    return version


def _metadata_version(data: bytes, source: Path) -> str:
    match = re.search(rb"^Version:\s*([^\r\n]+)", data, re.MULTILINE)
    if not match:
        raise ReleaseValidationError(f"{source} is missing a Version metadata field")
    return match.group(1).decode("utf-8").strip()


def _read_wheel_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = next(
                (
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")
                ),
                None,
            )
            if metadata is None:
                raise ReleaseValidationError("wheel is missing dist-info/METADATA")
            return _metadata_version(archive.read(metadata), path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseValidationError(f"cannot read wheel {path}: {exc}") from exc


def _read_sdist_version(path: Path) -> str:
    try:
        with tarfile.open(path, "r:gz") as archive:
            member = next(
                (
                    item
                    for item in archive.getmembers()
                    if item.name.endswith("/PKG-INFO")
                ),
                None,
            )
            if member is None:
                raise ReleaseValidationError("sdist is missing PKG-INFO")
            data = archive.extractfile(member)
            if data is None:
                raise ReleaseValidationError("sdist PKG-INFO cannot be read")
            return _metadata_version(data.read(), path)
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseValidationError(f"cannot read sdist {path}: {exc}") from exc


def _filename_version(path: Path, kind: str) -> str:
    if kind == "npm":
        match = re.match(r"^agentpermit-(\d+\.\d+\.\d+)\.tgz$", path.name)
    elif kind == "wheel":
        match = re.match(r"^agentpermit-(\d+\.\d+\.\d+)-.+\.whl$", path.name)
    else:
        match = re.match(r"^agentpermit-(\d+\.\d+\.\d+)\.tar\.gz$", path.name)
    if not match:
        raise ReleaseValidationError(
            f"{kind} filename does not contain agentpermit version: {path.name}"
        )
    return match.group(1)


def validate_release(
    root: str | Path,
    tag: str,
    *,
    npm_tgz: str | Path | None = None,
    wheel: str | Path | None = None,
    sdist: str | Path | None = None,
) -> dict[str, Any]:
    """Validate source metadata and optional built artifacts, returning evidence."""

    root_path = Path(root)
    tag_match = VERSION_RE.fullmatch(tag)
    if tag_match is None:
        raise ReleaseValidationError(f"tag must match v<major>.<minor>.<patch>: {tag}")
    expected = tag_match.group("version")

    package = json.loads((root_path / "package.json").read_text(encoding="utf-8"))
    npm_version = package.get("version")
    pyproject = tomllib.loads(
        (root_path / "pyproject.toml").read_text(encoding="utf-8")
    )
    python_version = pyproject.get("project", {}).get("version")
    runtime_text = (root_path / "agentpermit" / "__init__.py").read_text(
        encoding="utf-8"
    )
    runtime_match = RUNTIME_VERSION_RE.search(runtime_text)
    runtime_version = runtime_match.group(1) if runtime_match else None
    changelog = (root_path / "CHANGELOG.md").read_text(encoding="utf-8")

    values: dict[str, str | None] = {
        "tag": expected,
        "npm": npm_version if isinstance(npm_version, str) else None,
        "pyproject": python_version if isinstance(python_version, str) else None,
        "runtime": runtime_version,
    }
    for name, value in values.items():
        if value != expected:
            raise ReleaseValidationError(
                f"{name} version {value!r} does not match tag {expected}"
            )
    heading = CHANGELOG_HEADING.format(version=expected)
    if re.search(rf"^{re.escape(heading)}(?:\s|$)", changelog, re.MULTILINE) is None:
        raise ReleaseValidationError(
            f"CHANGELOG.md is missing heading {CHANGELOG_HEADING.format(version=expected)!r}"
        )

    artifacts: dict[str, str] = {}
    for kind, artifact, reader in (
        ("npm", npm_tgz, _read_npm_version),
        ("wheel", wheel, _read_wheel_version),
        ("sdist", sdist, _read_sdist_version),
    ):
        if artifact is None:
            continue
        artifact_path = Path(artifact)
        metadata_version = reader(artifact_path)
        if _filename_version(artifact_path, kind) != expected:
            raise ReleaseValidationError(
                f"{kind} filename version does not match tag {expected}"
            )
        if metadata_version != expected:
            raise ReleaseValidationError(
                f"{kind} metadata version {metadata_version!r} does not match tag {expected}"
            )
        artifacts[kind] = metadata_version

    return {"version": expected, "source": values, "artifacts": artifacts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--npm-tgz")
    parser.add_argument("--wheel")
    parser.add_argument("--sdist")
    args = parser.parse_args(argv)
    try:
        result = validate_release(
            args.root,
            args.tag,
            npm_tgz=args.npm_tgz,
            wheel=args.wheel,
            sdist=args.sdist,
        )
    except (
        OSError,
        ReleaseValidationError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
