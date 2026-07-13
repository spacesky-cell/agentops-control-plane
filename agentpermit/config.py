from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath


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
            "*secret*",
            "**/*secret*",
            "*token*",
            "**/*token*",
            "*credential*",
            "**/*credential*",
            "id_rsa",
            "**/id_rsa",
            "id_ed25519",
            "**/id_ed25519",
            "id_ecdsa",
            "**/id_ecdsa",
            "id_dsa",
            "**/id_dsa",
            ".npmrc",
            "**/.npmrc",
            ".pypirc",
            "**/.pypirc",
            ".netrc",
            "**/.netrc",
            "*.pem",
            "**/*.pem",
            "*.key",
            "**/*.key",
            ".ssh",
            ".ssh/**",
            "**/.ssh",
            "**/.ssh/**",
            ".git",
            ".git/**",
            "**/.git",
            "**/.git/**",
            ".agentpermit",
            ".agentpermit/**",
            "**/.agentpermit",
            "**/.agentpermit/**",
            ".pytest_cache",
            ".pytest_cache/**",
            "**/.pytest_cache",
            "**/.pytest_cache/**",
            "__pycache__",
            "__pycache__/**",
            "**/__pycache__",
            "**/__pycache__/**",
            "node_modules",
            "node_modules/**",
            "**/node_modules",
            "**/node_modules/**",
            "dist",
            "dist/**",
            "build",
            "build/**",
            "coverage",
            "coverage/**",
            ".venv",
            ".venv/**",
        ]
    )
    write_requires_approval: bool = True
    patch_requires_approval: bool = True
    unknown_command_requires_approval: bool = True
    max_command_seconds: int = 30
    max_output_chars: int = 8000


def is_protected_path(path: str | Path, protected_globs: list[str]) -> bool:
    normalized = str(path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lower()
    candidate = PurePosixPath(normalized)
    for pattern in protected_globs:
        normalized_pattern = pattern.replace("\\", "/").lower()
        if candidate.match(normalized_pattern):
            return True
        if normalized_pattern.endswith("/**"):
            directory = normalized_pattern[:-3].rstrip("/")
            if directory.startswith("**/"):
                directory = directory[3:]
            if normalized == directory or f"/{directory}/" in f"/{normalized}/":
                return True
    return False


def load_policy(path: str | Path | None = None) -> PolicyConfig:
    if path is None:
        return PolicyConfig()
    policy_path = Path(path)
    if not policy_path.exists():
        raise FileNotFoundError(f"Policy file not found: {policy_path}")
    data = json.loads(policy_path.read_text(encoding="utf-8-sig"))
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

