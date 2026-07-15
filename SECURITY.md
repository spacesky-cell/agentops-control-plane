# Security Policy

## Supported versions

Security fixes are applied to the current development branch and the latest published release once releases exist. This repository is currently preparing the next release; npm publication is not yet claimed.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private vulnerability reporting for this repository. Include the affected version or commit, operating system, reproduction steps, impact, and any proposed mitigation. Avoid including real credentials, tokens, or private repository content.

Maintainers will acknowledge a complete report, reproduce it, determine severity, and coordinate a fix and disclosure. If private reporting is unavailable, open a public issue containing no exploit details and ask for a private contact channel.

## Security model

AgentPermit supports a loopback-only, local, single-user deployment. The dashboard refuses non-loopback binds. Approval forms use CSRF protection and require reviewer attribution and a reason. Tool requests cross a policy and audit gateway; commands use structured argv, `shell=False`, a minimal environment, output limits, and timeouts.

Protected globs exclude configured files from source copy, workspace listing, snapshots, and file tools. Durable event payloads redact secret-shaped keys and known credential formats. These controls reduce accidental exposure but are defense in depth, not data loss prevention.

Policy limits are enforced before MCP parsing, direct gateway execution, and file content loading. Defaults are 1,048,576 bytes per MCP frame, 262,144 bytes per tool argument object, 1,048,576 bytes per file, and 16,777,216 aggregate bytes copied from a source tree. Configure these with `max_mcp_frame_bytes`, `max_tool_argument_bytes`, `max_file_bytes`, and `max_source_bytes`; each must remain a positive integer. Limits bound AgentPermit processing, but they do not constrain host access by an allowed command.

## Explicit limitations

- A copied workspace is not a container, virtual machine, or operating-system sandbox.
- Same-user processes can read or modify AgentPermit's local runtime state and SQLite database.
- An allowed command can access host files and the network according to its OS permissions.
- Redaction can miss unknown secret formats; protected globs depend on correct configuration.
- Local dashboard users share the authority of the operating-system account running AgentPermit.
- AgentPermit does not provide multi-user authentication, remote isolation, or hostile-code containment.

Run untrusted code only inside a separate OS account, container, VM, or dedicated sandbox with appropriately restricted filesystem, network, and credentials.

## Operational guidance

- Keep the dashboard on its default loopback host.
- Do not place credentials in the source tree or runtime home.
- Review command allowlists and protected globs for each repository.
- Avoid `--auto-approve` except for a controlled local demo or deterministic evaluation.
- Store `.agentpermit` on a trusted local filesystem with account-appropriate permissions.
- Rotate any credential that appears in an event, snapshot, report, terminal log, or issue attachment.
