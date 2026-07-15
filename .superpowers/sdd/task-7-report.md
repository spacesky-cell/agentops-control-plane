# Task 7 Report: Quality And Release Automation

## RED Baseline

- Python suite: 227 passed, 10 expected Windows privilege skips.
- Statement coverage: 84% (2,882 statements, 465 missed).
- Mypy: 19 errors in six source modules.
- Ruff format: 26 files would be reformatted; Ruff lint was otherwise clean.
- CI covered only Python 3.10-3.12, had no Node matrix, no clean installed-artifact MCP smoke, and no release validation.

## GREEN Evidence

- Full Python suite: **271 passed, 10 skipped**.
- Coverage gate: **90.01%** (2,612/2,902 statements; 290 missed), with `precision = 2` and `fail_under = 90`.
- Ruff: `30 files already formatted`; `All checks passed!`.
- Mypy: `Success: no issues found in 17 source files`.
- npm tests: **13/13 passed**.
- Deterministic eval: `total=1, passed=1, failed=0`.
- Python build: wheel and sdist built as `agentpermit-0.2.0`.
- npm pack dry-run: `agentpermit-0.2.0.tgz`, 21 files, no install hooks/dependencies.
- Fresh tarball install and MCP smoke: `clean npm artifact MCP smoke passed` using `npx --no-install` from the installed package.
- Release validator against real v0.2.0 source, npm tgz, wheel, and sdist: all versions `0.2.0`.
- Validator mismatch tests cover malformed tags, source metadata, npm metadata/filename, wheel filename, and artifact agreement failures.
- Workflow YAML parsed successfully with PyYAML. `actionlint` was unavailable in the local environment.
- `git diff --check`: no whitespace errors.

## Coverage Table

| Area | Statements | Missed | Coverage |
| --- | ---: | ---: | ---: |
| All `agentpermit` modules | 2,902 | 290 | 90.01% |
| CLI | 129 | 2 | 98.45% |
| MCP stdio | 284 | 39 | 86.27% |
| Dashboard web | 406 | 32 | 92.12% |
| Workspace/process platform code | 647 | 98 | 84.85% |

The threshold is met with meaningful tests for CLI workflows, policy boundaries, MCP protocol errors, dashboard request parsing, release metadata, and startup cleanup. No runtime modules were excluded.

## Changed Files

- `pyproject.toml`: Python 3.10 target, Ruff, mypy, pytest, coverage 90% gate, and development tools.
- `.github/workflows/ci.yml`: quality, Python 3.10-3.14, Node 18/20/22, Windows/Linux launcher, package/build/eval/smoke jobs.
- `.github/workflows/release.yml`: tag-gated validation, artifact build/checksum, protected npm publication, provenance, and post-publish GitHub Release.
- `scripts/validate_release.py`: source and artifact version agreement validator.
- `scripts/smoke_npm_artifact.mjs`: clean npm install and installed-bin MCP lifecycle smoke.
- `agentpermit/*.py`: Ruff formatting and narrow source typing fixes; no version change.
- `tests/*.py`: focused coverage and release-validator tests.
- `README.md`, `README_CN.md`, `CONTRIBUTING.md`: development quality/build commands; release-state install text unchanged.

## CI And Release Matrix

| Workflow area | Matrix / behavior |
| --- | --- |
| Python tests | Ubuntu, Python 3.10, 3.11, 3.12, 3.13, 3.14 |
| Node tests | Ubuntu, Node 18, 20, 22 |
| Launcher smoke | Ubuntu and Windows, Node 20, Python 3.10 |
| Quality gate | Ruff format/check, mypy, full pytest with coverage fail-under |
| Package gate | Python build, npm pack dry-run, deterministic eval, validator, clean tarball MCP smoke |
| Release trigger | Only `v*.*.*` tags |

## Permissions And Secret Boundary

- Workflow default permission is `contents: read`.
- npm publication is isolated to the protected `npm` environment and receives only `contents: read` plus `id-token: write`.
- First publication uses `secrets.NPM_TOKEN` through `NODE_AUTH_TOKEN`; no credential is stored in source or artifacts.
- npm publish uses `--provenance --access public`.
- GitHub Release runs only after npm publication succeeds and receives `contents: write`.
- The workflow is structured so Task 8 can replace the short-lived `NPM_TOKEN` path with npm Trusted Publishing while retaining provenance.

## Exact Commands

```text
ruff format --check agentpermit tests scripts
ruff check agentpermit tests scripts
mypy --no-incremental agentpermit
python -m pytest --cov=agentpermit --cov-report=term-missing --cov-fail-under=90 -q
npm test
python -m build
npm pack --dry-run
python -m agentpermit --home .eval eval --tasks examples/tasks.jsonl --auto-approve
npm pack --silent
python scripts/validate_release.py --tag v0.2.0 --npm-tgz agentpermit-0.2.0.tgz --wheel dist/agentpermit-0.2.0-py3-none-any.whl --sdist dist/agentpermit-0.2.0.tar.gz
node scripts/smoke_npm_artifact.mjs agentpermit-0.2.0.tgz
python -c "import yaml; [yaml.safe_load(open(p, encoding='utf-8')) for p in ['.github/workflows/ci.yml','.github/workflows/release.yml']]"
git diff --check
```

## Remaining Concerns

- Local Windows cannot exercise ten POSIX/symlink privilege tests; CI retains Linux coverage for those paths.
- `actionlint` was not installed locally; YAML parsing succeeded with PyYAML, but hosted workflow execution remains the authoritative check.
- No tag, publish, push, release, or version bump was performed. Task 8 must handle token migration and first-publication release-state changes.
