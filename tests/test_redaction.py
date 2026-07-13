from __future__ import annotations

import sqlite3

import pytest

from agentpermit.audit import AuditStore
import agentpermit.redaction as redaction_module
from agentpermit.redaction import (
    REDACTED,
    redact_durable,
    redact_text,
    summarize_content,
)


@pytest.mark.parametrize(
    ("source", "expected", "secret_fragments"),
    [
        (
            'ordinary {"password":"plain-secret"} retained',
            f'ordinary {{"password":"{REDACTED}"}} retained',
            ("plain-secret", "plain"),
        ),
        (
            "ordinary {'password':'plain-secret'} retained",
            f"ordinary {{'password':'{REDACTED}'}} retained",
            ("plain-secret", "plain"),
        ),
        (
            "ordinary Authorization: Basic dXNlcjpwYXNz retained",
            f"ordinary Authorization: Basic {REDACTED} retained",
            ("dXNlcjpwYXNz", "YXNz"),
        ),
        (
            "ordinary AUTHORIZATION: bEaReR header.payload.signature retained",
            f"ordinary AUTHORIZATION: bEaReR {REDACTED} retained",
            ("header.payload.signature", "payload"),
        ),
        (
            "ordinary https://user:pass@example.com/path retained",
            f"ordinary https://{REDACTED}@example.com/path retained",
            ("user:pass", "pass"),
        ),
        (
            "ordinary password='two word secret' retained",
            f"ordinary password='{REDACTED}' retained",
            ("two word secret", "word secret"),
        ),
        (
            'ordinary PASSWORD="two word secret" retained',
            f'ordinary PASSWORD="{REDACTED}" retained',
            ("two word secret", "word secret"),
        ),
        (
            "ordinary token=unquoted-secret retained",
            f"ordinary token={REDACTED} retained",
            ("unquoted-secret", "unquoted"),
        ),
        (
            "ordinary API_KEY:case-insensitive-secret retained",
            f"ordinary API_KEY:{REDACTED} retained",
            ("case-insensitive-secret", "insensitive"),
        ),
        (
            'ordinary {"clientSecret":"camel-secret"} retained',
            f'ordinary {{"clientSecret":"{REDACTED}"}} retained',
            ("camel-secret", "camel"),
        ),
        (
            'ordinary {"auth":"auth-secret"} retained',
            f'ordinary {{"auth":"{REDACTED}"}} retained',
            ("auth-secret", "auth-secret"),
        ),
        (
            'ordinary {"proxyAuthorization":"Bearer proxy-secret"} retained',
            f'ordinary {{"proxyAuthorization":"Bearer {REDACTED}"}} retained',
            ("proxy-secret", "proxy-secret"),
        ),
        (
            "ordinary client_auth=client-auth-secret retained",
            f"ordinary client_auth={REDACTED} retained",
            ("client-auth-secret", "client-auth-secret"),
        ),
        (
            'ordinary {"HTTPAuthorization":"Basic aHR0cC1zZWNyZXQ="} retained',
            f'ordinary {{"HTTPAuthorization":"Basic {REDACTED}"}} retained',
            ("aHR0cC1zZWNyZXQ=", "ZWNyZXQ"),
        ),
        (
            "ordinary clientAPIKey=client-api-secret retained",
            f"ordinary clientAPIKey={REDACTED} retained",
            ("client-api-secret", "client-api-secret"),
        ),
        (
            "ordinary credential=credential-secret retained",
            f"ordinary credential={REDACTED} retained",
            ("credential-secret", "credential-secret"),
        ),
        (
            "ordinary client_secret='two word client secret' retained",
            f"ordinary client_secret='{REDACTED}' retained",
            ("two word client secret", "word client"),
        ),
        (
            "ordinary access_token=access-token-secret retained",
            f"ordinary access_token={REDACTED} retained",
            ("access-token-secret", "access-token"),
        ),
        (
            'ordinary {"Authorization":"Bearer serialized-token"} retained',
            f'ordinary {{"Authorization":"Bearer {REDACTED}"}} retained',
            ("serialized-token", "serialized"),
        ),
        (
            "ordinary Authorization=Basic dXNlcjphc3NpZ25tZW50 retained",
            f"ordinary Authorization=Basic {REDACTED} retained",
            ("dXNlcjphc3NpZ25tZW50", "Z25tZW50"),
        ),
        (
            'ordinary {"password":123456} retained',
            f'ordinary {{"password":"{REDACTED}"}} retained',
            ("123456", "2345"),
        ),
        (
            "ordinary -----BEGIN ENCRYPTED PRIVATE KEY-----\n"
            "encrypted-private-material\n"
            "-----END ENCRYPTED PRIVATE KEY----- retained",
            f"ordinary {REDACTED} retained",
            ("encrypted-private-material", "private-material"),
        ),
        (
            "ordinary -----BEGIN DSA PRIVATE KEY-----\n"
            "dsa-private-material\n"
            "-----END DSA PRIVATE KEY----- retained",
            f"ordinary {REDACTED} retained",
            ("dsa-private-material", "private-material"),
        ),
    ],
)
def test_redact_text_replaces_complete_common_credential_values(
    source: str,
    expected: str,
    secret_fragments: tuple[str, ...],
):
    redacted = redact_text(source)

    assert redacted == expected
    assert all(fragment not in redacted for fragment in secret_fragments)


def test_redact_text_preserves_non_secret_password_language():
    source = "ordinary password policy retained"

    assert redact_text(source) == source


def test_redact_durable_uses_exact_secret_field_semantics():
    value = {
        "clientSecret": "client-secret-value",
        "auth": "auth-value",
        "proxyAuthorization": "proxy-value",
        "client_auth": "client-auth-value",
        "HTTPAuthorization": "http-auth-value",
        "clientAPIKey": "client-api-value",
        "credential": "credential-value",
        "client_secret": "client-secret-snake",
        "access_token": "access-token-value",
        "token_count": 7,
        "authentication_method": "oauth",
        "secretary": "Alice",
    }

    assert redact_durable(value) == {
        "clientSecret": REDACTED,
        "auth": REDACTED,
        "proxyAuthorization": REDACTED,
        "client_auth": REDACTED,
        "HTTPAuthorization": REDACTED,
        "clientAPIKey": REDACTED,
        "credential": REDACTED,
        "client_secret": REDACTED,
        "access_token": REDACTED,
        "token_count": 7,
        "authentication_method": "oauth",
        "secretary": "Alice",
    }


def test_redact_durable_normalizes_content_field_case_and_style():
    assert redact_durable(
        {
            "Content": "upper-content",
            "rawContent": "camel-content",
            "STDERR": "upper-stderr",
            "stdOut": "camel-stdout",
            "std-out": "hyphen-stdout",
            "stdErr": "camel-stderr",
            "std-err": "hyphen-stderr",
            "content_type": "text/plain",
        }
    ) == {
        "Content": summarize_content("upper-content"),
        "rawContent": summarize_content("camel-content"),
        "STDERR": summarize_content("upper-stderr"),
        "stdOut": summarize_content("camel-stdout"),
        "std-out": summarize_content("hyphen-stdout"),
        "stdErr": summarize_content("camel-stderr"),
        "std-err": summarize_content("hyphen-stderr"),
        "content_type": "text/plain",
    }


def test_audit_store_never_persists_styled_content_fields(tmp_path):
    markers = {
        "stdOut": "persisted-camel-stdout",
        "std-out": "persisted-hyphen-stdout",
        "stdErr": "persisted-camel-stderr",
        "std-err": "persisted-hyphen-stderr",
    }
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("content styles", "test-agent", tmp_path / "workspace")

    store.add_event(run_id, "content_styles", "ordinary", markers)

    with sqlite3.connect(store.db_path) as conn:
        payload_json = conn.execute(
            "SELECT payload_json FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    assert all(marker not in payload_json for marker in markers.values())
    assert payload_json.count("content_sha256") == len(markers)


def test_audit_store_never_persists_common_credential_fragments(tmp_path):
    credentials = (
        '{"password":"json-secret"}',
        "{'password':'single-secret'}",
        "Authorization: Basic dXNlcjpiYXNpYw==",
        "authorization: Bearer bearer-secret-value",
        "https://url-user:url-pass@example.com/path",
        "password='two word secret'",
        'PASSWORD="other two word secret"',
        "token=unquoted-token-secret",
        "Api_Key=unquoted-api-secret",
        '{"clientSecret":"persisted-client-secret"}',
        '{"proxyAuthorization":"Bearer persisted-proxy-secret"}',
        "client_auth=persisted-client-auth",
        '{"HTTPAuthorization":"Basic cGVyc2lzdGVkLWh0dHA="}',
        "clientAPIKey=persisted-client-api-key",
        "credential=persisted-credential-secret",
        "client_secret='persisted two word secret'",
        "access_token=persisted-access-token",
        '{"Authorization":"Bearer persisted-auth-token"}',
        "Authorization=Basic cGVyc2lzdGVkLWJhc2lj",
        '{"password":987654}',
        (
            "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
            "persisted-encrypted-key\n"
            "-----END ENCRYPTED PRIVATE KEY-----"
        ),
    )
    secret_fragments = (
        "json-secret",
        "single-secret",
        "dXNlcjpiYXNpYw==",
        "bearer-secret-value",
        "url-user",
        "url-pass",
        "two word secret",
        "other two word secret",
        "unquoted-token-secret",
        "unquoted-api-secret",
        "persisted-client-secret",
        "persisted-proxy-secret",
        "persisted-client-auth",
        "cGVyc2lzdGVkLWh0dHA=",
        "persisted-client-api-key",
        "persisted-credential-secret",
        "persisted two word secret",
        "persisted-access-token",
        "persisted-auth-token",
        "cGVyc2lzdGVkLWJhc2lj",
        "987654",
        "persisted-encrypted-key",
    )
    ordinary_prefix = "ordinary audit text"
    task = f"{ordinary_prefix} | " + " | ".join(credentials)
    store = AuditStore(tmp_path / "runs.sqlite")

    run_id = store.start_run(task, "ordinary-agent", tmp_path / "workspace")
    store.add_event(run_id, "redaction_check", task)

    with sqlite3.connect(store.db_path) as conn:
        persisted = "\n".join(
            row[0]
            for row in conn.execute(
                "SELECT task FROM runs UNION ALL SELECT agent_name FROM runs "
                "UNION ALL SELECT message FROM events"
            )
        )

    assert ordinary_prefix in persisted
    assert "ordinary-agent" in persisted
    assert persisted.count(REDACTED) == 2 * len(credentials)
    assert all(fragment not in persisted for fragment in secret_fragments)


def test_redact_durable_summarizes_command_values_and_redacts_secret_flags():
    command = {
        "program": "https://user:pass@example.com/tool",
        "args": [
            "--password",
            "plain-secret",
            "--user",
            "alice:secret",
            "--token=plain",
            "positional-secret",
            "--verbose",
            "-pplain-secret",
            "--token:colon-secret",
            "--",
            "-dash-secret",
            "-1",
        ],
    }

    durable = redact_durable(command)

    assert durable == {
        "program": "https://[redacted]@example.com/tool",
        "args": [
            "--password",
            REDACTED,
            "--user",
            summarize_content("alice:secret"),
            "--token=[redacted]",
            summarize_content("positional-secret"),
            "--verbose",
            summarize_content("-pplain-secret"),
            "--token:[redacted]",
            "--",
            summarize_content("-dash-secret"),
            summarize_content("-1"),
        ],
    }


def test_command_values_never_reach_raw_sqlite_payloads(tmp_path):
    secrets = (
        "plain-secret",
        "alice:secret",
        "plain",
        "positional-secret",
        "url-user:url-pass",
        "attached-secret",
        "colon-secret",
        "dash-secret",
    )
    command = {
        "program": f"https://{secrets[4]}@example.com/tool",
        "args": [
            "--password",
            secrets[0],
            "--user",
            secrets[1],
            f"--token={secrets[2]}",
            secrets[3],
            f"-p{secrets[5]}",
            f"--token:{secrets[6]}",
            "--",
            f"-{secrets[7]}",
        ],
    }
    result = {
        **command,
        "exit_code": 0,
        "output": "ok",
        "output_truncated": False,
        "timed_out": False,
    }
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("command redaction", "test-agent", tmp_path / "workspace")
    store.add_event(run_id, "command", "ordinary", {"request": command, "result": result})
    store.create_approval(run_id, "run_command", {"args": command}, "review")

    with sqlite3.connect(store.db_path) as conn:
        persisted = "\n".join(
            row[0]
            for row in conn.execute(
                "SELECT payload_json FROM events UNION ALL "
                "SELECT payload_json FROM approvals"
            )
        )

    assert all(secret not in persisted for secret in secrets)
    assert "--password" in persisted
    assert "--user" in persisted
    assert "--token=[redacted]" in persisted
    assert "--token:[redacted]" in persisted
    assert "https://[redacted]@example.com/tool" in persisted


@pytest.mark.parametrize(
    ("malformed", "secrets"),
    [
        ({"command": "python legacy-secret-value"}, ("legacy-secret-value",)),
        (
            {
                "program": "tool",
                "args": ["ordinary-secret-value"],
                "extra": "extra-secret-value",
            },
            ("ordinary-secret-value", "extra-secret-value"),
        ),
        ({"program": "missing-args-secret"}, ("missing-args-secret",)),
        (
            {"program": ["wrong-program-secret"], "args": "wrong-args-secret"},
            ("wrong-program-secret", "wrong-args-secret"),
        ),
        (
            {
                "program": "tool",
                "args": [],
                "status": "plain-status-secret",
                "ordinary_extra": {"nested-ghp_abcdefghijklmnopqrstuvwxyz": "nested-secret"},
                "token-ghp_abcdefghijklmnopqrstuvwxyz": "key-secret",
            },
            (
                "plain-status-secret",
                "ghp_abcdefghijklmnopqrstuvwxyz",
                "nested-secret",
                "key-secret",
            ),
        ),
    ],
)
def test_tool_aware_redaction_fails_safe_for_malformed_command_args(
    malformed, secrets
):
    durable = redaction_module.redact_tool_args("run_command", malformed)
    serialized = str(durable)

    assert all(secret not in serialized for secret in secrets)
    assert "ordinary_extra" in durable or "ordinary_extra" not in malformed


def test_tool_aware_redaction_rejects_nested_non_string_command_args():
    malformed = {
        "program": "tool",
        "args": [{"ordinary": "wrong-type-secret"}],
    }

    durable = redaction_module.redact_tool_args("run_command", malformed)

    assert "wrong-type-secret" not in str(durable)
    assert durable["program"] == summarize_content("tool")
    assert durable["args"][0]["ordinary"] == summarize_content(
        "wrong-type-secret"
    )


def test_redact_durable_routes_malformed_command_shape_to_fail_safe_redaction():
    malformed = {
        "program": "tool",
        "args": [{"ordinary": "direct-wrong-type-secret"}],
    }

    durable = redact_durable(malformed)

    assert "direct-wrong-type-secret" not in str(durable)
    assert durable["program"] == summarize_content("tool")


@pytest.mark.parametrize(
    ("field", "invalid", "secret"),
    [
        ("exit_code", "exit-code-secret", "exit-code-secret"),
        (
            "output",
            {
                "content_chars": "output-length-secret",
                "content_sha256": "output-hash-secret",
            },
            "output-hash-secret",
        ),
        ("output_truncated", "truncation-secret", "truncation-secret"),
        ("timed_out", "timeout-secret", "timeout-secret"),
    ],
)
def test_tool_aware_redaction_rejects_malformed_command_result_fields(
    field, invalid, secret
):
    malformed = {
        "program": "tool",
        "args": ["--version"],
        "exit_code": 0,
        "output": "ok",
        "output_truncated": False,
        "timed_out": False,
    }
    malformed[field] = invalid

    durable = redaction_module.redact_tool_payload("run_command", malformed)

    assert secret not in str(durable)
    assert durable["program"] == summarize_content("tool")


def test_tool_aware_redaction_accepts_valid_safe_command_result_summary():
    safe_output = summarize_content("ok")
    result = {
        "program": "tool",
        "args": ["--version"],
        "exit_code": None,
        "output": safe_output,
        "output_truncated": False,
        "timed_out": True,
    }

    durable = redaction_module.redact_tool_payload("run_command", result)

    assert durable == {
        **result,
        "args": ["--version"],
    }


@pytest.mark.parametrize(
    "malformed",
    [
        ["non-object-secret"],
        "non-object-secret",
        42,
        None,
    ],
)
def test_tool_aware_redaction_handles_non_object_command_args(malformed):
    durable = redaction_module.redact_tool_args("run_command", malformed)

    assert "non-object-secret" not in str(durable)


def test_malformed_command_denials_and_approvals_never_reach_raw_sqlite(tmp_path):
    from agentpermit.gateway import RuntimeGateway
    from agentpermit.models import ToolRequest

    source = tmp_path / "source"
    source.mkdir()
    gateway = RuntimeGateway.from_home(tmp_path / "project")
    run_id, workspace = gateway.start_run("malformed redaction", "test-agent", source)
    malformed_requests = [
        {"command": "python denied-legacy-secret"},
        {
            "program": "tool",
            "args": ["denied-arg-secret"],
            "extra": "denied-extra-secret",
        },
        {"program": "denied-missing-secret"},
        {"program": ["denied-program-type"], "args": "denied-args-type"},
        {
            "program": "tool",
            "args": [],
            "status": "plain-status-secret",
            "ordinary_extra": "ordinary-extra-secret",
            "key-ghp_abcdefghijklmnopqrstuvwxyz": "key-value-secret",
        },
        {
            "program": "tool",
            "args": [{"ordinary": "denied-nested-type-secret"}],
        },
    ]
    secrets = tuple(
        value
        for request in malformed_requests
        for value in (
            "denied-legacy-secret",
            "denied-arg-secret",
            "denied-extra-secret",
            "denied-missing-secret",
            "denied-program-type",
            "denied-args-type",
            "plain-status-secret",
            "ordinary-extra-secret",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            "key-value-secret",
            "denied-nested-type-secret",
        )
        if value in str(request)
    )
    for request in malformed_requests:
        result = gateway.execute_tool(
            run_id, workspace, ToolRequest("run_command", request)
        )
        assert result.status.value == "denied"
        gateway.audit_store.create_approval(
            run_id,
            "run_command",
            {"args": request, "status": "denied"},
            "malformed request",
        )

    with sqlite3.connect(gateway.audit_store.db_path) as conn:
        persisted = "\n".join(
            row[0]
            for row in conn.execute(
                "SELECT payload_json FROM events UNION ALL "
                "SELECT payload_json FROM approvals"
            )
        )

    assert all(secret not in persisted for secret in secrets)
    assert '"ordinary_extra"' in persisted
    assert "key-[redacted]" in persisted


def test_malformed_command_results_never_reach_raw_sqlite(tmp_path):
    result = {
        "program": "tool",
        "args": ["--version"],
        "exit_code": "sqlite-exit-secret",
        "output": {
            "content_chars": "sqlite-length-secret",
            "content_sha256": "sqlite-hash-secret",
        },
        "output_truncated": "sqlite-truncation-secret",
        "timed_out": "sqlite-timeout-secret",
    }
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("malformed result", "test-agent", tmp_path / "workspace")

    store.add_event(
        run_id,
        "tool_failed",
        "malformed result",
        {"output": result, "args": ["sqlite-non-object-secret"]},
        tool_name="run_command",
        decision="deny",
    )

    with sqlite3.connect(store.db_path) as conn:
        persisted = conn.execute("SELECT payload_json FROM events").fetchone()[0]

    assert all(
        secret not in persisted
        for secret in (
            "sqlite-exit-secret",
            "sqlite-length-secret",
            "sqlite-hash-secret",
            "sqlite-truncation-secret",
            "sqlite-timeout-secret",
            "sqlite-non-object-secret",
        )
    )


def test_known_token_signature_inside_value_free_flag_is_redacted_in_sqlite(tmp_path):
    token = "ghp_abcdefghijklmnopqrstuvwxyz"
    command = {"program": "tool", "args": [f"--{token}"]}

    durable = redaction_module.redact_tool_args("run_command", command)
    store = AuditStore(tmp_path / "runs.sqlite")
    run_id = store.start_run("flag token", "test-agent", tmp_path / "workspace")
    store.add_event(
        run_id,
        "policy_decision",
        "denied",
        {"args": command},
        tool_name="run_command",
        decision="deny",
    )

    with sqlite3.connect(store.db_path) as conn:
        persisted = conn.execute("SELECT payload_json FROM events").fetchone()[0]

    assert durable["args"] == ["--[redacted]"]
    assert token not in persisted
    assert "--[redacted]" in persisted
