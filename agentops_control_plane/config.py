from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class PolicyConfig:
    command_allow_prefixes: list[str] = field(
        default_factory=lambda: [
            "python -m unittest",
            "python -m pytest",
            "pytest",
            "git diff",
            "git status",
            "npm test",
            "pnpm test",
            "pnpm run test",
        ]
    )
    command_deny_contains: list[str] = field(
        default_factory=lambda: [
            "rm -rf",
            "remove-item",
            " del ",
            "format ",
            "shutdown",
            "git push",
            "git reset --hard",
            "curl ",
            "wget ",
            "invoke-webrequest",
        ]
    )
    command_deny_shell_tokens: list[str] = field(
        default_factory=lambda: [
            "&&",
            "&",
            "||",
            ";",
            "|",
            ">",
            "<",
            "`",
            "$(",
            "\n",
            "\r",
        ]
    )
    protected_globs: list[str] = field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            "**/.env",
            "**/.env.*",
            "**/*secret*",
            "**/*token*",
            "**/id_rsa",
            ".git/**",
        ]
    )
    write_requires_approval: bool = True
    patch_requires_approval: bool = True
    unknown_command_requires_approval: bool = True
    max_command_seconds: int = 30
    max_output_chars: int = 8000


def load_policy(path: str | Path | None = None) -> PolicyConfig:
    if path is None:
        return PolicyConfig()
    policy_path = Path(path)
    if not policy_path.exists():
        raise FileNotFoundError(f"Policy file not found: {policy_path}")
    data = json.loads(policy_path.read_text(encoding="utf-8"))
    defaults = asdict(PolicyConfig())
    defaults.update(data)
    return PolicyConfig(**defaults)


def write_default_policy(path: str | Path) -> None:
    policy_path = Path(path)
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(asdict(PolicyConfig()), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

