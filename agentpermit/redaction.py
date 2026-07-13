from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any


REDACTED = "[redacted]"

_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATOR = re.compile(r"[-\s]+")
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "secret",
        "token",
    }
)
_SECRET_FIELD_SUFFIXES = tuple(
    f"_{name}"
    for name in (
        "api_key",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "token",
    )
)
_FIELD = (
    r"(?P<key_quote>[\"']?)(?P<key>[A-Za-z][A-Za-z0-9_-]*)"
    r"(?P=key_quote)"
)


def _normalize_field_name(field: str) -> str:
    separated = _ACRONYM_BOUNDARY.sub("_", field.strip())
    separated = _CAMEL_BOUNDARY.sub("_", separated)
    return _SEPARATOR.sub("_", separated).lower()


def _is_secret_field(field: str) -> bool:
    normalized = _normalize_field_name(field)
    return normalized in _SECRET_FIELD_NAMES or normalized.endswith(
        _SECRET_FIELD_SUFFIXES
    )


def _redact_quoted_assignment(match: re.Match[str]) -> str:
    if not _is_secret_field(match.group("key")):
        return match.group(0)
    value = match.group("value")
    authorization = re.fullmatch(
        r"(?P<scheme>Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+",
        value,
        re.IGNORECASE,
    )
    replacement = (
        f"{authorization.group('scheme')} {REDACTED}"
        if authorization
        else REDACTED
    )
    quote = match.group("value_quote")
    return f"{match.group('prefix')}{quote}{replacement}{quote}"


def _redact_unquoted_assignment(match: re.Match[str]) -> str:
    if not _is_secret_field(match.group("key")):
        return match.group(0)
    scheme = match.group("scheme")
    replacement = f"{scheme} {REDACTED}" if scheme else REDACTED
    if match.group("key_quote") and match.group("delimiter") == ":":
        replacement = f'"{replacement}"'
    return f"{match.group('prefix')}{replacement}"


_CREDENTIAL_REPLACEMENTS = [
    (
        re.compile(r"(?P<prefix>\bhttps?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE),
        rf"\g<prefix>{REDACTED}@",
    ),
    (
        re.compile(
            rf"(?P<prefix>{_FIELD}\s*[:=]\s*)"
            r"(?P<value_quote>[\"'])"
            r"(?P<value>(?:\\.|(?!(?P=value_quote)).)*)"
            r"(?P=value_quote)"
        ),
        _redact_quoted_assignment,
    ),
    (
        re.compile(
            rf"(?P<prefix>{_FIELD}\s*(?P<delimiter>[:=])\s*)"
            r"(?:(?P<scheme>Basic|Bearer)\s+)?"
            r"(?P<value>[^\s,;&|)\[\]}\"']+)",
            re.IGNORECASE,
        ),
        _redact_unquoted_assignment,
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        REDACTED,
    ),
    (
        re.compile(
            r"(?P<prefix>\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE
        ),
        rf"\g<prefix>{REDACTED}",
    ),
    (
        re.compile(
            r"-----BEGIN (?P<pem_label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----[\s\S]*?"
            r"-----END (?P=pem_label)-----"
        ),
        REDACTED,
    ),
]
_CONTENT_FIELDS = {
    "content",
    "output",
    "stdout",
    "std_out",
    "stderr",
    "std_err",
    "raw_content",
}


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in _CREDENTIAL_REPLACEMENTS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def summarize_content(value: str | bytes) -> dict[str, Any]:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return {
        "content_chars": len(value),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def redact_durable(value: Any, *, _field: str | None = None) -> Any:
    if _field and _is_secret_field(_field):
        return REDACTED
    if (
        _field
        and _normalize_field_name(_field) in _CONTENT_FIELDS
        and isinstance(value, (str, bytes))
    ):
        return summarize_content(value)
    if isinstance(value, Mapping):
        return {
            str(key): redact_durable(item, _field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_durable(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return summarize_content(value)
    return value
