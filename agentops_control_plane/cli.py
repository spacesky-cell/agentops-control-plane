from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agents import ScriptedAgent
from .audit import AuditStore
from .config import load_policy, write_default_policy
from .evaluator import run_eval
from .exporter import export_html, export_json
from .gateway import RuntimeGateway
from .mcp_adapter import McpPlanAdapter
from .mcp_stdio import serve_json_lines
from .web import serve


def build_gateway(args: argparse.Namespace) -> RuntimeGateway:
    policy = load_policy(getattr(args, "policy", None))
    return RuntimeGateway.from_home(get_home(args), policy)


def get_home(args: argparse.Namespace) -> Path:
    home = getattr(args, "home", None)
    return Path(home).resolve() if home else Path.cwd()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentops", description="AgentOps Control Plane")
    parser.add_argument("--policy", help="Path to policy JSON file")
    parser.add_argument(
        "--home",
        help="Project home where .agentops runtime data is stored. Defaults to the current directory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_script = sub.add_parser("run-script", help="Run a deterministic scripted agent")
    run_script.add_argument("--plan", required=True, help="Path to scripted agent JSON plan")
    run_script.add_argument("--source", help="Source directory copied into an isolated workspace")
    run_script.add_argument("--task", default="Run scripted agent task")
    run_script.add_argument("--auto-approve", action="store_true")

    run_mcp_plan = sub.add_parser("run-mcp-plan", help="Run a local MCP-style tool-call plan")
    run_mcp_plan.add_argument("--plan", required=True, help="Path to MCP-style tool-call JSON plan")
    run_mcp_plan.add_argument("--source", help="Source directory copied into an isolated workspace")
    run_mcp_plan.add_argument("--task", default="Run MCP-style tool-call plan")
    run_mcp_plan.add_argument("--auto-approve", action="store_true")

    resume_script = sub.add_parser("resume-script", help="Resume a waiting scripted agent run")
    resume_script.add_argument("run_id")
    resume_script.add_argument("--plan", required=True, help="Path to scripted agent JSON plan")
    resume_script.add_argument("--approver", default="human")
    resume_script.add_argument("--auto-approve-remaining", action="store_true")

    resume_mcp_plan = sub.add_parser("resume-mcp-plan", help="Resume a waiting MCP-style plan run")
    resume_mcp_plan.add_argument("run_id")
    resume_mcp_plan.add_argument("--plan", required=True, help="Path to MCP-style tool-call JSON plan")
    resume_mcp_plan.add_argument("--approver", default="human")
    resume_mcp_plan.add_argument("--auto-approve-remaining", action="store_true")

    sub.add_parser("runs", help="List runs")

    show = sub.add_parser("show", help="Show a run trace")
    show.add_argument("run_id")

    approvals = sub.add_parser("approvals", help="List approvals")
    approvals.add_argument("--run-id")

    approve = sub.add_parser("approve", help="Mark an approval as approved")
    approve.add_argument("approval_id", type=int)
    approve.add_argument("--approver", default="human")
    approve.add_argument("--reason", default="")

    reject = sub.add_parser("reject", help="Mark an approval as rejected")
    reject.add_argument("approval_id", type=int)
    reject.add_argument("--approver", default="human")
    reject.add_argument("--reason", default="")

    export = sub.add_parser("export", help="Export a run report")
    export.add_argument("run_id")
    export.add_argument("--format", choices=["json", "html"], default="html")
    export.add_argument("--out", required=True)

    serve_cmd = sub.add_parser("serve", help="Serve the local dashboard")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)

    sub.add_parser("serve-mcp-stdio", help="Serve the local MCP-style JSON-lines stdio transport")

    init_policy = sub.add_parser("init-policy", help="Write a default policy JSON")
    init_policy.add_argument("--out", default="examples/policy.json")

    eval_cmd = sub.add_parser("eval", help="Run deterministic eval tasks")
    eval_cmd.add_argument("--tasks", required=True)
    eval_cmd.add_argument("--auto-approve", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "init-policy":
        output = get_home(args) / args.out
        write_default_policy(output)
        print(f"Wrote {output}")
        return

    gateway = build_gateway(args)
    store: AuditStore = gateway.audit_store

    if args.command == "run-script":
        agent = ScriptedAgent.from_file(args.plan)
        run_id = agent.run(
            gateway,
            task=args.task,
            source=args.source,
            auto_approve=args.auto_approve,
        )
        run = store.get_run(run_id)
        print(json.dumps({"run_id": run_id, "status": run["status"]}, indent=2))
        return

    if args.command == "run-mcp-plan":
        adapter = McpPlanAdapter.from_file(args.plan)
        run_id = adapter.run(
            gateway,
            task=args.task,
            source=args.source,
            auto_approve=args.auto_approve,
        )
        run = store.get_run(run_id)
        print(json.dumps({"run_id": run_id, "status": run["status"]}, indent=2))
        return

    if args.command == "resume-script":
        agent = ScriptedAgent.from_file(args.plan)
        run_id = agent.resume(
            gateway,
            args.run_id,
            approver=args.approver,
            auto_approve_remaining=args.auto_approve_remaining,
        )
        run = store.get_run(run_id)
        print(json.dumps({"run_id": run_id, "status": run["status"]}, indent=2))
        return

    if args.command == "resume-mcp-plan":
        adapter = McpPlanAdapter.from_file(args.plan)
        run_id = adapter.resume(
            gateway,
            args.run_id,
            approver=args.approver,
            auto_approve_remaining=args.auto_approve_remaining,
        )
        run = store.get_run(run_id)
        print(json.dumps({"run_id": run_id, "status": run["status"]}, indent=2))
        return

    if args.command == "runs":
        for run in store.list_runs():
            print(f"{run['id']}  {run['status']:<20}  {run['agent_name']:<20}  {run['task']}")
        return

    if args.command == "show":
        run = store.get_run(args.run_id)
        if not run:
            raise SystemExit(f"Run not found: {args.run_id}")
        print(json.dumps(run, indent=2, ensure_ascii=False))
        for event in store.get_events(args.run_id):
            print(json.dumps(event, indent=2, ensure_ascii=False))
        return

    if args.command == "approvals":
        print(json.dumps(store.list_approvals(args.run_id), indent=2, ensure_ascii=False))
        return

    if args.command == "approve":
        store.decide_approval(args.approval_id, "approved", args.approver, args.reason)
        print(f"Approved {args.approval_id}")
        return

    if args.command == "reject":
        store.decide_approval(args.approval_id, "rejected", args.approver, args.reason)
        print(f"Rejected {args.approval_id}")
        return

    if args.command == "export":
        if args.format == "json":
            output = export_json(store, args.run_id, args.out)
        else:
            output = export_html(store, args.run_id, args.out)
        print(f"Wrote {output}")
        return

    if args.command == "serve":
        print(f"Serving http://{args.host}:{args.port}")
        serve(store, args.host, args.port)
        return

    if args.command == "serve-mcp-stdio":
        serve_json_lines(gateway, sys.stdin, sys.stdout)
        return

    if args.command == "eval":
        report = run_eval(gateway, args.tasks, auto_approve=args.auto_approve)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if report["failed"]:
            raise SystemExit(1)
        return
