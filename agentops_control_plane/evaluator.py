from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agents import ScriptedAgent
from .gateway import RuntimeGateway


def run_eval(
    gateway: RuntimeGateway,
    tasks_path: str | Path,
    auto_approve: bool = True,
) -> dict[str, Any]:
    tasks = [
        json.loads(line)
        for line in Path(tasks_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results = []
    for task in tasks:
        agent = ScriptedAgent.from_file(task["plan"])
        run_id = agent.run(
            gateway,
            task=task["task"],
            source=task.get("source"),
            auto_approve=auto_approve,
        )
        run = gateway.audit_store.get_run(run_id)
        passed = bool(run and run["status"] == task.get("expected_status", "success"))
        results.append(
            {
                "name": task.get("name", task["task"]),
                "run_id": run_id,
                "passed": passed,
                "status": run["status"] if run else "missing",
            }
        )
    passed_count = sum(1 for item in results if item["passed"])
    return {
        "total": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "results": results,
    }

