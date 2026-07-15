# AgentPermit v0.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release AgentPermit v0.3.0 as a trustworthy, local, npm-distributed standard MCP control plane.

**Architecture:** Keep the Python standard-library core as the single runtime implementation, expose it through a dependency-free Node launcher, and route every agent tool call through one gateway-owned policy/approval/audit state machine. Keep the server local-only and make all security limitations explicit.

**Tech Stack:** Python 3.10+, SQLite, standard-library HTTP/stdio, Node.js 18+, npm, pytest, Ruff, mypy, GitHub Actions.

---

### Task 1: Clean Rebrand And npm Launcher

**Files:** Rename `agentops_control_plane/` to `agentpermit/`; update all imports/tests; create `package.json`, `bin/agentpermit.js`, and launcher tests.

- [ ] Write Node subprocess tests that require interpreter precedence, Python 3.10 validation, argument/stdio/exit propagation, and a helpful missing-Python error; run them and confirm RED.
- [ ] Rename the Python package and CLI/runtime branding without compatibility aliases; update `pyproject.toml` and package entry points.
- [ ] Implement the zero-dependency launcher and restrict npm `files` to the launcher, Python source, README, and LICENSE.
- [ ] Add a version-consistency check for npm and Python metadata.
- [ ] Run launcher tests, Python tests, `npm pack --dry-run`, and a clean tarball install smoke test; commit `feat: rebrand project as AgentPermit`.

### Task 2: Atomic Approval And Durable Redaction

**Files:** Modify audit schema/gateway/workspace/policy; add focused approval and security tests.

- [ ] Add failing tests for MCP/client self-approval rejection, duplicate pending request reuse, concurrent approval consumption, rejected terminal behavior, auto-approval consumption, protected source exclusion, snapshot exclusion, and secret-free events.
- [ ] Migrate SQLite schema to v2 with `request_fingerprint`, separate policy/reviewer reasons, an active-approval uniqueness rule, and indexes on run/event/approval lookup paths.
- [ ] Replace find-then-consume behavior with a conditional atomic consume whose row count gates execution; converge concurrent creates on the active approval.
- [ ] Make protected globs apply to copy/list/snapshot/file tools and introduce one recursive audit redactor used by every adapter.
- [ ] Run focused concurrency/security tests and the complete suite; commit `fix: enforce atomic approvals and protected audit data`.

### Task 3: Structured And Bounded Command Execution

**Files:** Modify tool schema, policy config/engine, executor, examples, and command tests.

- [ ] Add failing tests for `{program,args}`, exact argv allow rules, legacy command rejection, quoted arguments, stripped credential environment, bounded output, timeout, and child-process termination.
- [ ] Replace command strings and shell-token rules with argv-prefix allow/deny configuration and validation.
- [ ] Execute with `shell=False`, a minimal environment, a draining bounded-output reader, and platform-specific process-group cleanup.
- [ ] Update example policy/plans and audit summaries to the structured shape.
- [ ] Run command tests on the current platform and the complete suite; commit `feat: bound structured command execution`.

### Task 4: Standard MCP-Only Integration

**Files:** Replace stdio session lifecycle and CLI surface; remove Claude plan/local plan adapters and their tests; add subprocess protocol tests.

- [ ] Add failing tests proving standard initialize -> initialized -> tools/list -> tools/call works without private methods, client `auto_approve` is ignored/rejected, pending ids are stable, and an approved identical retry executes once.
- [ ] Introduce `agentpermit mcp --source --task [--auto-approve]`; make the session lazily start one governed run.
- [ ] Remove `run.start`, `tool.call`, `run.finish`, `run-claude-code-plan`, `run-mcp-plan`, and their adapter-only resume paths.
- [ ] Finalize a running session on clean EOF, preserve waiting runs, and mark transport failures failed.
- [ ] Run MCP unit/subprocess tests and the complete suite; commit `feat: expose a standard governed MCP server`.

### Task 5: Local Dashboard Evidence And Request Safety

**Files:** Split HTTP routing from rendering where needed; add snapshot diff helper and web tests.

- [ ] Add failing tests for non-loopback refusal, missing/invalid CSRF, reviewer reason persistence, non-duplicated diff rows, real created/modified/deleted snapshot diffs, binary/large-file bounds, and correct 404 responses.
- [ ] Add per-process CSRF tokens and loopback-only binding; keep approval mutations POST-only.
- [ ] Render bounded actual snapshot diffs, summary metrics, human-readable approvals, and collapsible/filterable events.
- [ ] Verify desktop and 390px mobile layouts, approval interaction, overflow, and console output with Playwright.
- [ ] Run web tests and full suite; commit `feat: improve local approval and evidence dashboard`.

### Task 6: Public Documentation And Repository Health

**Files:** Rewrite `README.md` and `README_CN.md`; update architecture/MCP docs; add `SECURITY.md`, `CONTRIBUTING.md`, issue and PR templates, and visual assets.

- [ ] Replace portfolio/resume language with user problem, npm install, three-minute quick start, standard Claude Code/Codex MCP configs, architecture, and explicit local-backend security limits.
- [ ] Keep English and Chinese feature/command coverage equivalent and remove stale version/examples.
- [ ] Capture a real run/approval screenshot or short GIF and verify every documented command against the packed npm artifact.
- [ ] Add disclosure, contribution, issue, and PR workflows; commit `docs: prepare AgentPermit for public adoption`.

### Task 7: Quality And Release Automation

**Files:** Update CI/project config; add npm publish workflow and release checks.

- [ ] Fix current mypy errors and formatting drift, then configure Ruff, mypy, pytest, and coverage >= 90% in project metadata.
- [ ] Test Python 3.10-3.14, Node 18/20/22, Windows/Linux launcher behavior, package build, deterministic eval, and clean npm tarball execution.
- [ ] Add tag-gated npm/GitHub release workflow with minimal permissions, provenance, artifact checksums, and protected `npm` environment secret for the first publication.
- [ ] Add release validation that tag, npm version, Python version, changelog, and artifact versions are identical.
- [ ] Run all local quality/build/package checks; commit `ci: add npm and GitHub release gates`.

### Task 8: PR, Rename, And v0.3.0 Release

- [ ] Push `codex/agentpermit-v0.3`, open a ready PR, and wait for every required CI job to pass.
- [ ] Squash merge to `main`, pull the merge, rename the GitHub repository to `spacesky-cell/agentpermit`, update `origin`, and verify redirects.
- [ ] Revoke the exposed GitHub/npm credentials; place a newly issued short-lived npm token only in the protected GitHub environment.
- [ ] Update versions and `CHANGELOG.md`, run the complete verification matrix, commit `chore: release v0.3.0`, tag, and push main/tag.
- [ ] Monitor publication, verify `npm view agentpermit@0.3.0`, install/run it from a clean directory, verify GitHub Release artifacts/checksums, revoke the bootstrap npm token, and configure Trusted Publishing for future tags.
