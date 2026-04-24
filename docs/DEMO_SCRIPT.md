# Demo Script

Use this flow in interviews or portfolio recordings.

## 1. Show The Source Bug

```powershell
Get-Content examples\sample_repo\math_utils.py
python -m unittest -q
```

Run the unittest command from `examples\sample_repo` to show the failure.

## 2. Run With Automatic Approval

```powershell
python -m agentops_control_plane run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --task "Fix sample CI failure" `
  --auto-approve
```

Explain that the source directory is copied into an isolated workspace and the
original file is left unchanged.

## 3. Run With Human Approval

```powershell
python -m agentops_control_plane run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --task "Approval gate demo"

python -m agentops_control_plane approvals --run-id <run_id>
python -m agentops_control_plane approve <approval_id> --approver reviewer
python -m agentops_control_plane resume-script <run_id> `
  --plan examples\scripted_fix_agent.json `
  --approver reviewer
```

Point out that `patch_text` pauses because patch operations require approval.

## 4. Export Evidence

```powershell
python -m agentops_control_plane export <run_id> --format html --out report.html
python -m agentops_control_plane serve --port 8765
```

Open the dashboard at `http://127.0.0.1:8765`.

