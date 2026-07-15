# Deterministic demo and evaluation

The scripted agent is a repeatable demo/evaluation tool. Standard MCP is the supported public integration.

## Automatic local demo

From the repository root:

```powershell
python -m agentpermit --home .demo run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --task "Fix sample test failure" `
  --auto-approve
python -m agentpermit --home .demo runs
python -m agentpermit --home .demo serve --port 8765
```

Open <http://127.0.0.1:8765>. The run modifies only its copied workspace and records the auto-approved patch, command result, and snapshot evidence.

## Human approval demo

```powershell
python -m agentpermit --home .demo run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --task "Approval gate demo"
python -m agentpermit --home .demo approvals --run-id <run_id>
python -m agentpermit --home .demo approve <approval_id> `
  --approver reviewer `
  --reason "Patch reviewed"
python -m agentpermit --home .demo resume-script <run_id> `
  --plan examples\scripted_fix_agent.json `
  --approver reviewer
```

The patch pauses once, the approved request is consumed once, and the deterministic plan resumes from that step.

## Export and evaluation

```powershell
python -m agentpermit --home .demo export <run_id> --format html --out report.html
python -m agentpermit --home .demo export <run_id> --format json --out report.json
python -m agentpermit --home .demo eval --tasks examples\tasks.jsonl --auto-approve
```

The demo runs local code with the current user's OS permissions. The copied workspace is not a security sandbox; see [../SECURITY.md](../SECURITY.md).

