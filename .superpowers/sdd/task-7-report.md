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
- Workflow YAML parsed successfully with PyYAML. The initial binary lookup was unavailable, then the reproducible `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.9` check passed.
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
- Hosted workflow execution remains the authoritative check; local actionlint was verified reproducibly with `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.9`.
- No tag, publish, push, release, or version bump was performed. Task 8 must handle token migration and first-publication release-state changes.

## Fix Review

### RED Findings

- `tests/test_release_validation.py` reproduced uncaught `KeyError` for a missing npm manifest and `AttributeError` for a non-object `package.json`.
- `tests/test_workflows.py` initially rejected mutable action tags, direct GitHub expressions in `run` blocks, late source validation, and the extra `contents` permission on npm publication.
- `tests/publish.test.mjs` initially failed because no registry-integrity reconciliation implementation existed.
- `tests/version.test.js` initially failed because npm repository/homepage/bugs metadata was absent.

### GREEN Fixes

- `package.json` now declares the final public repository URL, homepage, and issues URL without changing version `0.2.0`.
- All `run` blocks consume quoted environment variables; no `${{ ... }}` expression remains in shell source. A static test forbids regressions.
- `publish-npm` has exactly `id-token: write`, retains protected environment `npm`, and uses the bootstrap `NPM_TOKEN` only as `NODE_AUTH_TOKEN`.
- `scripts/publish_npm.mjs` computes local SHA-512 SRI, queries the exact registry version, skips only an exact integrity match, and fails on mismatches or non-404 lookup errors. Publication remains `--provenance --access public`.
- GitHub Release creation uses explicit `GH_REPO`, creates only when absent, and always uploads all artifacts/checksums with `--clobber` for retry recovery.
- Source tag/changelog validation runs immediately after dev installation; artifact validation runs again after build/package creation.
- All official actions use reviewed immutable commits:
  - `actions/checkout` `34e114876b0b11c390a56381ad16ebd13914f8d5` (`v4.3.1`)
  - `actions/setup-python` `a26af69be951a213d495a4c3e4e4022e16d87065` (`v5.6.0`)
  - `actions/setup-node` `49933ea5288caeca8642d1e84afbd3f7d6820020` (`v4.4.0`)
  - `actions/upload-artifact` `ea165f8d65b6e75b540449e92b4886f43607fa02` (`v4.6.2`)
  - `actions/download-artifact` `d3f86a106a0bac45b974a628896c90dbdf5c8093` (`v4.3.0`)
- `validate_release.py` now converts missing, corrupt, and malformed npm/wheel/sdist inputs into `ReleaseValidationError` without tracebacks.
- `smoke_npm_artifact.mjs` accepts either an explicit tarball argument or the workflow-provided `TARBALL` environment variable; a static test protects the no-argv workflow contract.
- Actionlint v1.7.9 passed both workflows via the reproducible command `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.9`; PyYAML parsing and static workflow tests also pass.
- Release publication and GitHub Release jobs are guarded by the final repository identity; validate-build fails before installation if the actual repository differs.
- Invalid UTF-8 in npm JSON and wheel/sdist metadata is covered and normalized to `ReleaseValidationError`.

### Fix Verification

- Full Python suite: **283 passed, 10 expected skips**.
- Precision coverage: **90.01%** with `--cov-fail-under=90`.
- npm suite: **16/16 passed**.
- Ruff format/check and mypy: clean.
- Python build, deterministic eval, npm pack dry-run, real v0.2.0 release validator, Windows-local `npx --no-install` MCP smoke, actionlint, YAML parse, and `git diff --check`: passed.
- No tarballs, `dist`, build, eval, or coverage artifacts remain in the worktree.
