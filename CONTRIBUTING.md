# Contributing to AgentPermit

AgentPermit accepts focused bug fixes, documentation improvements, tests, and product-boundary-aligned features.

## Before opening a change

Search existing issues and describe the user problem, expected behavior, and security impact. For significant architecture, policy, approval, persistence, or public protocol changes, open a feature issue before implementation.

Do not include secrets, private repository content, `.agentpermit` runtime data, generated reports, or personal paths in commits or screenshots. Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md).

## Development setup

Requirements: Python 3.10+, Node.js 18+, and npm.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
npm install --ignore-scripts
```

AgentPermit has no runtime Python dependencies and the npm package has no JavaScript runtime dependencies or install scripts.

## Verification

Run the checks relevant to your change, with the full baseline before requesting review:

```powershell
ruff format --check agentpermit tests scripts
ruff check agentpermit tests scripts
mypy --no-incremental agentpermit
python -m pytest --cov=agentpermit --cov-report=term-missing --cov-fail-under=90
python -m build
npm test
npm pack --dry-run
python -m agentpermit --home .eval eval --tasks examples/tasks.jsonl --auto-approve
python scripts/validate_release.py --tag v0.2.0
git diff --check
```

For dashboard changes, run the local server and verify desktop/mobile layout, interaction, console errors, and screenshots with Playwright. For distribution or CLI changes, install the `npm pack` tarball in a fresh directory and run the documented commands through that installed artifact. For MCP changes, verify `initialize`, `notifications/initialized`, and `tools/list` at minimum.

## Pull requests

- Keep one problem per pull request.
- Add or update tests for behavior changes.
- Update English and Chinese README coverage together when public workflows change.
- Update architecture, MCP, security, or contribution docs when their contracts change.
- Do not edit generated artifacts as source.
- Complete the pull request template with exact commands and results.

By contributing, you agree that your contribution is licensed under the repository's MIT license.
