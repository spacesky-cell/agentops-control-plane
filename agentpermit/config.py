from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath


class ResourceLimitError(ValueError):
    def __init__(self, limit: str, max_bytes: int, actual_bytes: int) -> None:
        self.limit = limit
        self.max_bytes = max_bytes
        self.actual_bytes = actual_bytes
        super().__init__(
            f"{limit} exceeded: {actual_bytes} bytes is greater than {max_bytes} bytes."
        )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "limit": self.limit,
            "max_bytes": self.max_bytes,
            "actual_bytes": self.actual_bytes,
        }


@dataclass
class PolicyConfig:
    command_allow_prefixes: list[list[str]] = field(
        default_factory=lambda: [
            ["python", "-m", "unittest"],
            ["python", "-m", "pytest"],
            ["pytest"],
            ["git", "diff"],
            ["git", "status"],
            ["npm", "test"],
            ["pnpm", "test"],
            ["pnpm", "run", "test"],
        ]
    )
    command_deny_prefixes: list[list[str]] = field(
        default_factory=lambda: [
            ["rm", "-rf"],
            ["Remove-Item"],
            ["del"],
            ["format"],
            ["shutdown"],
            ["git", "push"],
            ["git", "reset", "--hard"],
            ["curl"],
            ["wget"],
            ["Invoke-WebRequest"],
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
    max_mcp_frame_bytes: int = 1_048_576
    max_tool_argument_bytes: int = 262_144
    max_file_bytes: int = 1_048_576
    max_source_bytes: int = 16_777_216

    def __post_init__(self) -> None:
        self._validate_command_rules(
            "command_allow_prefixes", self.command_allow_prefixes
        )
        self._validate_command_rules(
            "command_deny_prefixes", self.command_deny_prefixes
        )
        self._validate_positive_int("max_command_seconds", self.max_command_seconds)
        self._validate_positive_int("max_output_chars", self.max_output_chars)
        self._validate_positive_int("max_mcp_frame_bytes", self.max_mcp_frame_bytes)
        self._validate_positive_int(
            "max_tool_argument_bytes", self.max_tool_argument_bytes
        )
        self._validate_positive_int("max_file_bytes", self.max_file_bytes)
        self._validate_positive_int("max_source_bytes", self.max_source_bytes)

    @staticmethod
    def _validate_command_rules(name: str, rules: list[list[str]]) -> None:
        if not isinstance(rules, list):
            raise ValueError(f"{name} must be a list of argv-prefix arrays.")
        for index, rule in enumerate(rules):
            if (
                not isinstance(rule, list)
                or not rule
                or any(not isinstance(element, str) or not element for element in rule)
            ):
                raise ValueError(
                    f"{name}[{index}] must be a non-empty array of non-empty strings."
                )

    @staticmethod
    def _validate_positive_int(name: str, value: object) -> None:
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer.")


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
