import json
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts.validate_release import ReleaseValidationError, validate_release


ROOT = Path(__file__).resolve().parents[1]


def _write_npm_tgz(path: Path, version: str) -> None:
    manifest = json.dumps({"name": "agentpermit", "version": version}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(manifest)
        archive.addfile(info, BytesIO(manifest))


def _write_wheel(path: Path, version: str, filename_version: str | None = None) -> None:
    metadata = (
        f"Metadata-Version: 2.1\nName: agentpermit\nVersion: {version}\n".encode()
    )
    name = filename_version or version
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"agentpermit-{name}.dist-info/METADATA", metadata)


def _write_sdist(path: Path, version: str, filename_version: str | None = None) -> None:
    metadata = (
        f"Metadata-Version: 2.1\nName: agentpermit\nVersion: {version}\n".encode()
    )
    name = filename_version or version
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(f"agentpermit-{name}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, BytesIO(metadata))


def test_current_source_release_matches_v020() -> None:
    evidence = validate_release(ROOT, "v0.2.0")
    assert evidence["version"] == "0.2.0"


def test_release_validator_accepts_matching_artifacts(tmp_path: Path) -> None:
    npm = tmp_path / "agentpermit-0.2.0.tgz"
    wheel = tmp_path / "agentpermit-0.2.0-py3-none-any.whl"
    sdist = tmp_path / "agentpermit-0.2.0.tar.gz"
    _write_npm_tgz(npm, "0.2.0")
    _write_wheel(wheel, "0.2.0")
    _write_sdist(sdist, "0.2.0")

    result = validate_release(ROOT, "v0.2.0", npm_tgz=npm, wheel=wheel, sdist=sdist)
    assert result["artifacts"] == {"npm": "0.2.0", "wheel": "0.2.0", "sdist": "0.2.0"}


@pytest.mark.parametrize("tag", ["0.2.0", "v0.2", "v0.2.0-rc.1"])
def test_release_validator_rejects_non_release_tags(tag: str) -> None:
    with pytest.raises(ReleaseValidationError, match="tag must match"):
        validate_release(ROOT, tag)


def test_release_validator_rejects_mismatched_npm_metadata(tmp_path: Path) -> None:
    npm = tmp_path / "agentpermit-0.2.0.tgz"
    _write_npm_tgz(npm, "0.2.1")
    with pytest.raises(ReleaseValidationError, match="npm metadata version"):
        validate_release(ROOT, "v0.2.0", npm_tgz=npm)


def test_release_validator_rejects_mismatched_npm_filename(tmp_path: Path) -> None:
    npm = tmp_path / "agentpermit-0.2.1.tgz"
    _write_npm_tgz(npm, "0.2.0")
    with pytest.raises(ReleaseValidationError, match="npm filename version"):
        validate_release(ROOT, "v0.2.0", npm_tgz=npm)


def test_release_validator_rejects_mismatched_python_filename(tmp_path: Path) -> None:
    wheel = tmp_path / "agentpermit-0.2.1-py3-none-any.whl"
    _write_wheel(wheel, "0.2.0", filename_version="0.2.1")
    with pytest.raises(ReleaseValidationError, match="wheel filename version"):
        validate_release(ROOT, "v0.2.0", wheel=wheel)
