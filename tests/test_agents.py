from pathlib import Path

from agentpermit.agents import AgentAdapter, ScriptedAgent
from agentpermit.gateway import RuntimeGateway


def make_sample_repo(root: Path) -> Path:
    source = root / "sample_repo"
    source.mkdir()
    (source / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    return source


def test_scripted_agent_satisfies_agent_adapter_protocol(tmp_path):
    source = make_sample_repo(tmp_path)
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    agent: AgentAdapter = ScriptedAgent(
        name="scripted",
        steps=[{"tool": "read_file", "args": {"path": "math_utils.py"}}],
    )

    run_id = agent.run(gateway, "read sample repo", source=source)
    run = gateway.audit_store.get_run(run_id)

    assert run["agent_name"] == "scripted"
    assert run["status"] == "success"
