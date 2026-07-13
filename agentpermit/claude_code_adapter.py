from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .agents import AgentAdapter
from .gateway import RuntimeGateway
from .mcp_adapter import McpPlanAdapter, McpToolCall
from .tools import list_tool_definitions

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ClaudeCodePlan:
    name: str
    tool_calls: list[McpToolCall]
    raw_response: str


class ClaudeCodePlanner:
    def __init__(
        self,
        command: str = "claude",
        timeout_seconds: int = 120,
        runner: Runner = subprocess.run,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    def plan(self, task: str, workspace: str | Path) -> ClaudeCodePlan:
        workspace_path = Path(workspace)
        prompt = self._build_prompt(task)
        args = [
            self._resolve_command(),
            "--safe-mode",
            "-p",
            "--tools=",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            "plan",
            prompt,
        ]
        try:
            completed = self.runner(
                args,
                cwd=workspace_path,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude Code timed out after {self.timeout_seconds} seconds.") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Claude Code command not found: {self.command}. "
                "Install Claude Code or pass --claude-command."
            ) from exc

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            detail = self._trim(stderr or stdout or "no output")
            raise RuntimeError(f"Claude Code exited with {completed.returncode}: {detail}")
        return self._parse_plan(stdout)

    def _resolve_command(self) -> str:
        resolved = shutil.which(self.command)
        return resolved or self.command

    def _build_prompt(self, task: str) -> str:
        tool_schema = json.dumps(list_tool_definitions(), ensure_ascii=False, indent=2)
        return "\n".join(
            [
                "You are generating a governed AgentPermit tool plan.",
                "Do not edit files or run commands yourself.",
                "Return only JSON with this exact shape:",
                '{"name":"claude-code-plan","tool_calls":[{"name":"list_files","arguments":{}}]}',
                "Available tools and input schemas:",
                tool_schema,
                "Task:",
                task,
            ]
        )

    def _parse_plan(self, stdout: str) -> ClaudeCodePlan:
        payload = self._loads_json(stdout.strip(), "Claude Code output")
        if isinstance(payload, dict) and isinstance(payload.get("result"), str):
            raw_response = payload["result"]
            plan_payload = self._loads_json(raw_response.strip(), "Claude Code result")
        else:
            raw_response = stdout
            plan_payload = payload
        if not isinstance(plan_payload, dict):
            raise ValueError("Claude Code plan must be a JSON object.")

        name = plan_payload.get("name", "claude-code-plan")
        if not isinstance(name, str) or not name:
            raise ValueError("Claude Code plan name must be a non-empty string.")
        tool_calls = plan_payload.get("tool_calls")
        if not isinstance(tool_calls, list):
            raise ValueError("Claude Code plan tool_calls must be a list.")

        parsed_calls: list[McpToolCall] = []
        for index, item in enumerate(tool_calls):
            if not isinstance(item, dict):
                raise ValueError(f"Claude Code plan tool_calls[{index}] must be an object.")
            call_name = item.get("name")
            if not isinstance(call_name, str) or not call_name:
                raise ValueError(f"Claude Code plan tool_calls[{index}].name is required.")
            arguments = item.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError(f"Claude Code plan tool_calls[{index}].arguments must be an object.")
            parsed_calls.append(McpToolCall(name=call_name, arguments=arguments))
        return ClaudeCodePlan(name=name, tool_calls=parsed_calls, raw_response=raw_response)

    def _loads_json(self, text: str, label: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} was not valid JSON: {self._trim(text)}") from exc

    def _trim(self, text: str) -> str:
        return text.strip()[:1000]


@dataclass
class ClaudeCodePlanAdapter(AgentAdapter):
    planner: ClaudeCodePlanner
    name: str = "claude-code-plan"

    def run(
        self,
        gateway: RuntimeGateway,
        task: str,
        source: str | Path | None = None,
        auto_approve: bool = False,
    ) -> str:
        run_id, workspace = gateway.start_run(task, self.name, source)
        gateway.audit_store.set_run_metadata(
            run_id,
            {
                "adapter": "claude-code-plan",
                "source": str(Path(source).resolve()) if source is not None else "",
                "task": task,
                "claude_command": self.planner.command,
            },
        )
        try:
            plan = self.planner.plan(task, workspace)
        except Exception as exc:
            gateway.audit_store.add_event(
                run_id,
                "claude_code_plan_failed",
                str(exc),
                {"adapter": "claude-code-plan"},
            )
            gateway.finish_run(run_id, workspace, "failed")
            return run_id

        gateway.audit_store.add_event(
            run_id,
            "claude_code_plan_generated",
            f"Claude Code generated {len(plan.tool_calls)} tool call(s).",
            {
                "plan_name": plan.name,
                "tool_calls": [
                    {"name": call.name, "arguments": gateway._redact_args(call.arguments)}
                    for call in plan.tool_calls
                ],
            },
        )

        plan_adapter = McpPlanAdapter(name=plan.name, tool_calls=plan.tool_calls)
        return plan_adapter.run_existing(
            gateway,
            run_id,
            workspace,
            task=task,
            source=source,
            auto_approve=auto_approve,
            metadata={
                "adapter": "claude-code-plan",
                "source": str(Path(source).resolve()) if source is not None else "",
                "task": task,
                "claude_command": self.planner.command,
                "plan_name": plan.name,
            },
        )

    def resume(
        self,
        gateway: RuntimeGateway,
        run_id: str,
        approver: str = "human",
        auto_approve_remaining: bool = False,
    ) -> str:
        raise NotImplementedError(
            "Claude Code generated plans are not persisted for resume. "
            "Use run-mcp-plan with a saved plan when a resumable generated plan is required."
        )
