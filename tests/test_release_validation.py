import json
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts.validate_release import ReleaseValidationError, validate_release


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))[
    "version"
]
CURRENT_TAG = f"v{CURRENT_VERSION}"
MISMATCH_VERSION = "9.9.9"


def _write_npm_tgz(path: Path, version: str) -> None:
    manifest = json.dumps({"name": "agentpermit", "version": version}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(manifest)
        archive.addfile(info, BytesIO(manifest))


def _write_tgz_member(path: Path, name: str, content: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        archive.addfile(info, BytesIO(content))


def _write_wheel(
    path: Path,
    version: str,
    filename_version: str | None = None,
    metadata: bytes | None = None,
) -> None:
    metadata = metadata or (
        f"Metadata-Version: 2.1\nName: agentpermit\nVersion: {version}\n".encode()
    )
    name = filename_version or version
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"agentpermit-{name}.dist-info/METADATA", metadata)


def _write_sdist(
    path: Path,
    version: str,
    filename_version: str | None = None,
    metadata: bytes | None = None,
) -> None:
    metadata = metadata or (
        f"Metadata-Version: 2.1\nName: agentpermit\nVersion: {version}\n".encode()
    )
    name = filename_version or version
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(f"agentpermit-{name}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, BytesIO(metadata))


def test_current_source_release_matches_manifest_version() -> None:
    evidence = validate_release(ROOT, CURRENT_TAG)
    assert evidence["version"] == CURRENT_VERSION


def test_release_validator_accepts_matching_artifacts(tmp_path: Path) -> None:
    npm = tmp_path / f"agentpermit-{CURRENT_VERSION}.tgz"
    wheel = tmp_path / f"agentpermit-{CURRENT_VERSION}-py3-none-any.whl"
    sdist = tmp_path / f"agentpermit-{CURRENT_VERSION}.tar.gz"
    _write_npm_tgz(npm, CURRENT_VERSION)
    _write_wheel(wheel, CURRENT_VERSION)
    _write_sdist(sdist, CURRENT_VERSION)

    result = validate_release(ROOT, CURRENT_TAG, npm_tgz=npm, wheel=wheel, sdist=sdist)
    assert result["artifacts"] == {
        "npm": CURRENT_VERSION,
        "wheel": CURRENT_VERSION,
        "sdist": CURRENT_VERSION,
    }


@pytest.mark.parametrize("tag", ["0.2.0", "v0.2", "v0.2.0-rc.1"])
def test_release_validator_rejects_non_release_tags(tag: str) -> None:
    with pytest.raises(ReleaseValidationError, match="tag must match"):
        validate_release(ROOT, tag)


def test_release_validator_rejects_mismatched_npm_metadata(tmp_path: Path) -> None:
    npm = tmp_path / f"agentpermit-{CURRENT_VERSION}.tgz"
    _write_npm_tgz(npm, MISMATCH_VERSION)
    with pytest.raises(ReleaseValidationError, match="npm metadata version"):
        validate_release(ROOT, CURRENT_TAG, npm_tgz=npm)


def test_release_validator_rejects_mismatched_npm_filename(tmp_path: Path) -> None:
    npm = tmp_path / f"agentpermit-{MISMATCH_VERSION}.tgz"
    _write_npm_tgz(npm, CURRENT_VERSION)
    with pytest.raises(ReleaseValidationError, match="npm filename version"):
        validate_release(ROOT, CURRENT_TAG, npm_tgz=npm)


def test_release_validator_rejects_mismatched_python_filename(tmp_path: Path) -> None:
    wheel = tmp_path / f"agentpermit-{MISMATCH_VERSION}-py3-none-any.whl"
    _write_wheel(wheel, CURRENT_VERSION, filename_version=MISMATCH_VERSION)
    with pytest.raises(ReleaseValidationError, match="wheel filename version"):
        validate_release(ROOT, CURRENT_TAG, wheel=wheel)


def test_release_validator_normalizes_missing_npm_manifest(tmp_path: Path) -> None:
    npm = tmp_path / f"agentpermit-{CURRENT_VERSION}.tgz"
    _write_tgz_member(npm, "package/README.md", b"missing manifest")
    with pytest.raises(ReleaseValidationError, match="missing package/package.json"):
        validate_release(ROOT, CURRENT_TAG, npm_tgz=npm)


def test_release_validator_normalizes_non_object_npm_manifest(tmp_path: Path) -> None:
    npm = tmp_path / f"agentpermit-{CURRENT_VERSION}.tgz"
    _write_tgz_member(npm, "package/package.json", b"[]")
    with pytest.raises(ReleaseValidationError, match="must contain a JSON object"):
        validate_release(ROOT, CURRENT_TAG, npm_tgz=npm)


def test_release_validator_normalizes_invalid_utf8_npm_json(tmp_path: Path) -> None:
    npm = tmp_path / f"agentpermit-{CURRENT_VERSION}.tgz"
    _write_tgz_member(npm, "package/package.json", b"{\xff")
    with pytest.raises(ReleaseValidationError, match="cannot read npm tarball"):
        validate_release(ROOT, CURRENT_TAG, npm_tgz=npm)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_release_validator_normalizes_invalid_utf8_python_metadata(
    tmp_path: Path, kind: str
) -> None:
    metadata = b"Metadata-Version: 2.1\nVersion: \xff\n"
    if kind == "wheel":
        artifact = tmp_path / f"agentpermit-{CURRENT_VERSION}-py3-none-any.whl"
        _write_wheel(artifact, CURRENT_VERSION, metadata=metadata)
        keyword = {"wheel": artifact}
    else:
        artifact = tmp_path / f"agentpermit-{CURRENT_VERSION}.tar.gz"
        _write_sdist(artifact, CURRENT_VERSION, metadata=metadata)
        keyword = {"sdist": artifact}
    with pytest.raises(ReleaseValidationError, match="invalid UTF-8"):
        validate_release(ROOT, CURRENT_TAG, **keyword)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_release_validator_normalizes_corrupt_python_artifacts(
    tmp_path: Path, kind: str
) -> None:
    if kind == "wheel":
        artifact = tmp_path / f"agentpermit-{CURRENT_VERSION}-py3-none-any.whl"
        keyword = {"wheel": artifact}
    else:
        artifact = tmp_path / f"agentpermit-{CURRENT_VERSION}.tar.gz"
        keyword = {"sdist": artifact}
    artifact.write_bytes(b"not an archive")
    with pytest.raises(ReleaseValidationError, match=f"cannot read {kind}"):
        validate_release(ROOT, CURRENT_TAG, **keyword)
