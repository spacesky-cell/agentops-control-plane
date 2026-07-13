from __future__ import annotations

import sqlite3

import pytest

from agentpermit.audit import AuditStore
from agentpermit.redaction import REDACTED, redact_durable, redact_text, summarize_content


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
