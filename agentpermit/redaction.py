from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable


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
        f"{authorization.group('scheme')} {REDACTED}" if authorization else REDACTED
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


_CREDENTIAL_REPLACEMENTS: list[
    tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]
] = [
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
        re.compile(r"(?P<prefix>\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
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
_COMMAND_REQUEST_FIELDS = frozenset({"program", "args"})
_COMMAND_RESULT_FIELDS = frozenset(
    {
        "program",
        "args",
        "exit_code",
        "output",
        "output_truncated",
        "timed_out",
    }
)


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


def _is_content_summary(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "content_chars",
        "content_sha256",
    }:
        return False
    content_chars = value["content_chars"]
    content_sha256 = value["content_sha256"]
    return (
        isinstance(content_chars, int)
        and not isinstance(content_chars, bool)
        and content_chars >= 0
        and isinstance(content_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", content_sha256) is not None
    )


def _has_command_shape(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and all(isinstance(key, str) for key in value)
        and frozenset(value) in {_COMMAND_REQUEST_FIELDS, _COMMAND_RESULT_FIELDS}
    )


def _is_command_mapping(value: Any) -> bool:
    if not _has_command_shape(value):
        return False
    fields = frozenset(value)
    if not isinstance(value.get("program"), str):
        return False
    args = value.get("args")
    if not isinstance(args, list) or any(
        not isinstance(argument, str) for argument in args
    ):
        return False
    if fields == _COMMAND_REQUEST_FIELDS:
        return True
    exit_code = value.get("exit_code")
    return (
        (
            exit_code is None
            or (isinstance(exit_code, int) and not isinstance(exit_code, bool))
        )
        and (
            isinstance(value.get("output"), str)
            or _is_content_summary(value.get("output"))
        )
        and isinstance(value.get("output_truncated"), bool)
        and isinstance(value.get("timed_out"), bool)
    )


def _is_secret_flag(flag: str) -> bool:
    return _is_secret_field(flag.lstrip("-/"))


def _is_value_free_flag(argument: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:--[A-Za-z][A-Za-z0-9_-]*|/[A-Za-z][A-Za-z0-9_-]*|-[A-Za-z])",
            argument,
        )
    )


def _split_inline_flag(argument: str) -> tuple[str, str, str] | None:
    if not argument.startswith(("--", "/")):
        return None
    separators = [
        (position, separator)
        for separator in ("=", ":")
        if (position := argument.find(separator)) > 0
    ]
    if not separators:
        return None
    position, separator = min(separators)
    flag = argument[:position]
    if not _is_value_free_flag(flag):
        return None
    return flag, separator, argument[position + 1 :]


def _redact_command_args(args: list[Any]) -> list[Any]:
    durable: list[Any] = []
    redact_next = False
    end_of_options = False
    for argument in args:
        if not isinstance(argument, str):
            durable.append(redact_durable(argument))
            redact_next = False
            continue
        if redact_next:
            durable.append(REDACTED)
            redact_next = False
            continue
        if end_of_options:
            durable.append(summarize_content(argument))
            continue
        if argument == "--":
            durable.append(argument)
            end_of_options = True
            continue
        inline = _split_inline_flag(argument)
        if inline is not None:
            flag, separator, inline_value = inline
            if _is_secret_flag(flag):
                durable.append(f"{flag}{separator}{REDACTED}")
            else:
                durable.append(
                    {
                        "flag": flag,
                        "separator": separator,
                        "value": summarize_content(inline_value),
                    }
                )
            continue
        if _is_value_free_flag(argument):
            durable.append(redact_text(argument))
            redact_next = _is_secret_flag(argument)
            continue
        durable.append(summarize_content(argument))
    return durable


def _redact_command_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    durable: dict[str, Any] = {
        "program": redact_text(value["program"]),
        "args": _redact_command_args(value["args"]),
    }
    for key, item in value.items():
        field = str(key)
        if field not in _COMMAND_REQUEST_FIELDS:
            durable[field] = redact_durable(item, _field=field)
    return durable


def _summarize_unsafe(value: Any) -> Any:
    if isinstance(value, str):
        return summarize_content(value)
    if isinstance(value, (bytes, bytearray)):
        return summarize_content(bytes(value))
    if isinstance(value, Mapping):
        return {
            redact_text(str(key)): _summarize_unsafe(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_summarize_unsafe(item) for item in value]
    return summarize_content(str(value))


def _redact_malformed_command_args(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _summarize_unsafe(value)
    return {
        redact_text(str(key)): _summarize_unsafe(item) for key, item in value.items()
    }


def redact_tool_args(tool_name: str, args: Any) -> Any:
    if tool_name != "run_command":
        return redact_durable(args)
    if _is_command_mapping(args):
        return _redact_command_mapping(args)
    return _redact_malformed_command_args(args)


def redact_tool_payload(tool_name: str | None, payload: Any) -> Any:
    if tool_name != "run_command":
        return redact_durable(payload)
    if not isinstance(payload, Mapping):
        return _redact_malformed_command_args(payload)

    def visit(value: Any) -> Any:
        if isinstance(value, Mapping):
            if "program" in value or "command" in value:
                return redact_tool_args("run_command", value)
            return {
                redact_text(str(key)): (
                    redact_tool_args("run_command", item)
                    if str(key) == "args"
                    else visit(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return [visit(item) for item in value]
        return redact_durable(value)

    return visit(payload)


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
        if _is_command_mapping(value):
            return _redact_command_mapping(value)
        if _has_command_shape(value):
            return _redact_malformed_command_args(value)
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
