# Task 6 Report: Public Documentation And Repository Health

## Outcome

Prepared AgentPermit's public adoption and repository health layer without publishing, changing the `0.2.0` version, or starting release work. Standard MCP is the primary public integration. The scripted adapter is documented only as a deterministic demo/evaluation tool.

## Changed files

- `README.md`: rewritten English product, install, npm quick start, MCP client configuration, approval/dashboard, architecture, security, development, and documentation coverage.
- `README_CN.md`: equivalent Chinese coverage and commands.
- `docs/ARCHITECTURE.md`: current owner boundaries, standard MCP lifecycle, approval state machine, workspace/execution model, evidence, and supported product boundary.
- `docs/MCP_STDIO.md`: verified Claude Code/Codex configuration syntax, standard lifecycle, example exchange, schemas, and approval retry.
- `docs/DEMO_SCRIPT.md`: deterministic demo/eval scope and current commands; removed interview/portfolio framing.
- `SECURITY.md`: private disclosure workflow, local single-user security model, explicit limitations, and operational guidance.
- `CONTRIBUTING.md`: setup, verification, review, documentation, security, and artifact expectations.
- `.github/ISSUE_TEMPLATE/bug_report.yml`: reproducible and sanitized bug intake.
- `.github/ISSUE_TEMPLATE/feature_request.yml`: problem/acceptance/owner/security feature intake.
- `.github/ISSUE_TEMPLATE/config.yml`: disables blank issues and links private vulnerability reporting.
- `.github/PULL_REQUEST_TEMPLATE.md`: scope, verification, security/data, and documentation checklist.
- `docs/assets/dashboard-completed-run.png`: real completed-run dashboard screenshot with created, modified, and deleted snapshot evidence.

## Command and configuration verification

- `codex mcp add --help`: confirmed `codex mcp add <NAME> -- <COMMAND>...`.
- `claude mcp add --help`: confirmed `claude mcp add --scope project <name> -- <command>...`; `project` is a supported scope.
- `npx --version`: prerequisite present (`10.9.4`).
- Verified `--home` semantics against CLI source and runtime: it names the project home, so documented project configuration uses `--home .` and stores runtime data in `./.agentpermit`.
- No client configuration was mutated during verification; exact client grammar was checked with each CLI's help and the configured server command was exercised through the installed artifact.

## Packed npm artifact smoke

Final smoke root: `D:\AgentPermitFinalSmoke` (generic disposable path, outside the repository).

1. `npm pack --pack-destination D:\AgentPermitFinalSmoke\pack` produced `agentpermit-0.2.0.tgz`, 21 files, approximately 48.3 kB packed / 225.5 kB unpacked.
2. A fresh `npm init -y` directory installed the tarball with `npm install --ignore-scripts`; npm reported one package, zero vulnerabilities.
3. `npx --no-install agentpermit --help` resolved the installed launcher and listed the current CLI surface.
4. The documented deterministic quick-start shape ran through `npx --no-install agentpermit` using repository examples and completed with status `success`; `runs` listed the completed run.
5. The installed artifact processed newline-delimited standard MCP `initialize`, `notifications/initialized`, and `tools/list`. It returned two responses (notifications correctly returned none), selected protocol `2025-06-18`, identified server `agentpermit` version `0.2.0`, and exposed five governed tools.
6. The installed artifact's dashboard command returned HTTP 200 with title `AgentPermit` on loopback.

No npm publish, PyPI publish, tag, version bump, global install, or real MCP client configuration change occurred.

## Dashboard visual evidence

- Generated a real completed run at generic path `D:\AgentPermitDemo` with before/after snapshots.
- Evidence counts in the rendered page: Created 1 (`created.txt`), Modified 1 (`settings.txt`), Deleted 1 (`obsolete.txt`), Unchanged 1.
- `write_file` and `patch_text` crossed the gateway, were server-side auto-approved for the trusted demo process, were consumed, and were audited. The deleted fixture was removed inside the copied workspace before the final snapshot because the public tool set intentionally has no delete tool; the snapshot evidence reflects the actual workspace change.
- Playwright desktop: `1440x900`, successful run page loaded, semantic snapshot contained all evidence and approval sections, zero browser console errors or warnings.
- Playwright mobile: `390x844`, responsive summary/evidence layout rendered, horizontal table regions remained scrollable, no incoherent overlap observed, zero browser console errors or warnings.
- Final committed screenshot: `docs/assets/dashboard-completed-run.png`. It was opened and visually inspected after capture. It contains only the generic `D:\AgentPermitDemo` workspace path and no user profile, token, secret, or private repository path.

## Test and build verification

- `python -m pytest -q`: **227 passed, 10 skipped** in 27.18s.
- `npm test`: **13 passed, 0 failed** after preserving the runtime-only npm file manifest.
- `python -m build`: built `agentpermit-0.2.0.tar.gz` and `agentpermit-0.2.0-py3-none-any.whl` successfully.
- `npm pack --dry-run --json`: succeeded; runtime-only 21-file manifest retained.
- Relative Markdown link scan: all local targets resolved.
- Public stale-term scan: no portfolio, resume, recruiter, interview, removed lifecycle method, old package/import, or private lifecycle wording remains.
- README coverage review: English and Chinese each contain equivalent feature, install, quick-start, MCP configuration, approval/dashboard, architecture, security, development, and documentation sections.
- `git diff --check`: run after this report and before commit.

## Concerns and residual risk

- Pytest emitted an environment-level `pytest_asyncio` deprecation warning about an unset default fixture loop scope; the repository has no asynchronous tests affected by this documentation task.
- The README screenshot is a repository asset and is not added to the deliberately runtime-only npm tarball manifest. The npm package README remains usable as text; the repository view contains the visual.
- The documented install path uses a locally packed `0.2.0` tarball until the separate release task publishes npm. The docs explicitly avoid claiming publication.

## Fix Review

Review fixes were verified against a fresh packed artifact at `D:\AgentPermitReviewSmoke`.

### Documentation corrections

- Replaced bare post-install CLI invocations in both READMEs and `docs/MCP_STDIO.md` with `npx --no-install agentpermit`.
- Claude Code project registration now launches the resolvable npx command.
- Replaced the incorrectly labeled Codex CLI registration with an exact project `.codex/config.toml` `[mcp_servers.agentpermit]` entry using `command = "npx"` and an explicit argument array.
- Approval, rejection, show, and export commands now carry the same `--home` as their run: `.demo` in the README workflow and `.` in the MCP project workflow.
- Both READMEs now use the final repository raw screenshot URL while retaining `docs/assets/dashboard-completed-run.png` as the tracked source asset.
- The private reporting contact link now targets the current `spacesky-cell/agentops-control-plane` repository and can redirect after the rename.
- Removed premature `v0.3` wording from the architecture product boundary.

### Fresh tarball workflow matrix

The final `agentpermit-0.2.0.tgz` installed into a new npm project with zero vulnerabilities. The package remained the runtime-only 21-file allowlist; no version, allowlist, dependency, install hook, or publish state changed.

- `npx --no-install agentpermit --help`: resolved the local npm binary and listed the complete CLI.
- `init-policy --home .policy --out policy.json`: wrote `.policy/policy.json`; it correctly did not create runtime database state.
- `run-script --home .demo --auto-approve`: run `run_71fe6814183f` completed with `success`.
- `runs --home .demo`: listed the completed run.
- `show --home .demo <run_id>`: returned the run and `run_finished` evidence.
- `approvals --home .review --run-id <run_id>`: returned one pending approval for run `run_3e0a67e47090`.
- `approve --home .review <approval_id>` followed by `resume-script --home .review`: resumed the same run to `success`; the approval status became `consumed`.
- Separate `run-script --home .reject` created pending run `run_d9dc678ffbb9`; `reject --home .reject <approval_id>` changed its approval status to `rejected`.
- `export --home .demo` produced an 8,103-byte HTML report and a 9,741-byte JSON report.
- `eval --home .eval --tasks examples/tasks.jsonl --auto-approve`: one passed, zero failed.
- `serve --home .demo --port 18765`: returned HTTP 200 with title `AgentPermit`; the listener was stopped afterward.
- Standard MCP through `npx --no-install agentpermit --home . mcp --source .`: `initialize`, `notifications/initialized`, and `tools/list` returned protocol `2025-06-18` and five tools. The project-root `.agentpermit` and `node_modules` directories were excluded from source copying by protected globs.
- Runtime databases were observed at `.demo/.agentpermit`, `.review/.agentpermit`, `.reject/.agentpermit`, `.eval/.agentpermit`, and project `./.agentpermit`, confirming documented home semantics.

### Final verification

- `python -m pytest -q`: **227 passed, 10 skipped** in 25.43s.
- `npm test`: **13 passed, 0 failed**.
- `python -m build`: wheel and sdist built successfully at version `0.2.0`.
- `npm pack --dry-run --json`: succeeded with 21 files, 48,457 bytes packed, and 225,976 bytes unpacked.
- Scoped stale-term scan: no portfolio, hiring, removed lifecycle, old package, or premature `v0.3` wording.
- README parity scan: 9/9 public sections and 22/22 command/config lines.
- Scoped case-sensitive invocation and home scan: no bare installed-package commands, mislabeled Codex CLI registration, or stateful commands missing `--home`.
- Relative Markdown links: all targets resolved across 23 files.
- Screenshot check: both READMEs contain the same final raw URL and the tracked PNG remains present.
- `git diff --check`: run after this report and before commit.

### Review verification notes

Three PowerShell harness defects were isolated from product results: an argument-swallowing helper, a `$file:` interpolation parse error, and a case-insensitive scan that matched product-name prose. The final workflow commands were rerun literally without the helper, and the scans were narrowed and made case-sensitive. No AgentPermit workflow failed.

Task 8 must verify the raw screenshot URL after the repository is renamed to `spacesky-cell/agentpermit`; the final-repository URL is intentionally not expected to resolve before that rename.
