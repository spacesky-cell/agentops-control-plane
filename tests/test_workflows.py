from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [
    ROOT / ".github" / "workflows" / name for name in ("ci.yml", "release.yml")
]
EXPECTED_ACTIONS = {
    "actions/checkout": "34e114876b0b11c390a56381ad16ebd13914f8d5",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
}


def _load(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_official_actions_are_pinned_to_reviewed_commits() -> None:
    found: set[str] = set()
    for workflow in WORKFLOWS:
        for match in re.finditer(
            r"^\s*-\s+uses:\s+(actions/[a-z-]+)@([^\s#]+)",
            workflow.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            action, revision = match.groups()
            found.add(action)
            assert re.fullmatch(r"[0-9a-f]{40}", revision)
            assert revision == EXPECTED_ACTIONS[action]
    assert found == set(EXPECTED_ACTIONS)


def test_run_blocks_do_not_interpolate_github_expressions() -> None:
    for workflow in WORKFLOWS:
        jobs = _load(workflow)["jobs"]
        for job in jobs.values():
            for step in job.get("steps", []):
                run = step.get("run")
                if run is not None:
                    assert "${{" not in run, f"{workflow.name}: expression in run block"


def test_release_validation_and_publication_boundaries() -> None:
    workflow = _load(ROOT / ".github" / "workflows" / "release.yml")
    jobs = workflow["jobs"]
    validate_steps = jobs["validate-build"]["steps"]
    install_index = next(
        i
        for i, step in enumerate(validate_steps)
        if "pip install" in step.get("run", "")
    )
    source_validation_index = next(
        i
        for i, step in enumerate(validate_steps)
        if "validate_release.py" in step.get("run", "")
        and "--npm-tgz" not in step.get("run", "")
    )
    first_expensive_index = next(
        i
        for i, step in enumerate(validate_steps)
        if any(
            command in step.get("run", "")
            for command in ("ruff ", "pytest", "python -m build")
        )
    )
    assert install_index < source_validation_index < first_expensive_index

    publish = jobs["publish-npm"]
    assert publish["environment"] == "npm"
    assert publish["permissions"] == {"id-token": "write"}
    assert any("publish_npm.mjs" in step.get("run", "") for step in publish["steps"])

    release = jobs["github-release"]
    assert publish["if"] == "github.repository == 'spacesky-cell/agentpermit'"
    assert release["if"] == "github.repository == 'spacesky-cell/agentpermit'"
    assert release["env"]["GH_REPO"] == "${{ github.repository }}"
    release_commands = "\n".join(step.get("run", "") for step in release["steps"])
    assert "gh release view" in release_commands
    assert "gh release upload" in release_commands
    assert "--clobber" in release_commands


def test_validate_build_checks_repository_before_expensive_steps() -> None:
    workflow = _load(ROOT / ".github" / "workflows" / "release.yml")
    job = workflow["jobs"]["validate-build"]
    assert job["env"]["EXPECTED_REPOSITORY"] == "spacesky-cell/agentpermit"
    assert job["env"]["ACTUAL_REPOSITORY"] == "${{ github.repository }}"
    steps = job["steps"]
    repository_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Validate release repository"
    )
    install_index = next(
        i for i, step in enumerate(steps) if "pip install" in step.get("run", "")
    )
    expensive_index = next(
        i
        for i, step in enumerate(steps)
        if any(
            command in step.get("run", "")
            for command in ("ruff ", "pytest", "python -m build")
        )
    )
    assert repository_index < install_index < expensive_index


def test_smoke_workflows_use_tarball_environment_fallback() -> None:
    smoke_source = (ROOT / "scripts" / "smoke_npm_artifact.mjs").read_text(
        encoding="utf-8"
    )
    assert "process.argv[2] ?? process.env.TARBALL" in smoke_source
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        if "smoke_npm_artifact.mjs" in text:
            assert "smoke_npm_artifact.mjs\n" in text


def test_windows_ci_runs_security_sensitive_python_suite() -> None:
    workflow = _load(ROOT / ".github" / "workflows" / "ci.yml")
    job = workflow["jobs"]["windows-python"]

    assert job["runs-on"] == "windows-latest"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "pip install" in commands
    assert "python -m pytest -q" in commands
