from agentpermit.audit import AuditStore
from agentpermit.exporter import export_html


def test_export_html_renders_escaped_policy_and_reviewer_reasons(tmp_path):
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("export reasons", "test-agent", tmp_path / "workspace")
    approval_id = store.create_approval(
        run_id,
        "write_file",
        {"args": {"path": "safe.txt"}},
        "Policy <requires> review",
    )
    store.decide_approval(
        approval_id,
        "approved",
        "reviewer",
        "Reviewer says <safe> & approved",
    )

    output = export_html(store, run_id, tmp_path / "run.html")
    rendered = output.read_text(encoding="utf-8")

    assert "Policy reason: Policy &lt;requires&gt; review" in rendered
    assert "Reviewer reason: Reviewer says &lt;safe&gt; &amp; approved" in rendered
    assert "<requires>" not in rendered
    assert "<safe>" not in rendered
