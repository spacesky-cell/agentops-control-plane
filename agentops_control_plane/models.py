from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Decision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ToolStatus(str, Enum):
    OK = "ok"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"
    FAILED = "failed"


@dataclass(frozen=True)
class ToolRequest:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    requested_by: str = "agent"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    risk: Risk
    reason: str


@dataclass
class ToolResult:
    status: ToolStatus
    output: Any = None
    error: str | None = None
    decision: PolicyDecision | None = None
    approval_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == ToolStatus.OK

