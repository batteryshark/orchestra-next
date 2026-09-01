"""Strict public request values."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

RUN_STATES = ("queued", "starting", "running", "waiting", "completed", "failed", "timed_out", "stopped", "skipped")
TERMINAL_STATES = frozenset(RUN_STATES[-5:])
WAITING_KINDS = frozenset(("attention", "children", "permission", "verification"))
DEPENDENCY_CONDITIONS = frozenset(("success", "terminal"))
STRATEGIES = frozenset(("goal", "ralph"))
PERMISSION_MODES = frozenset(("read-only", "workspace-write", "danger-full-access"))

GOAL_ROUNDS_DEFAULT = 32
GOAL_ROUNDS_MAX = 128
RALPH_ROUNDS_DEFAULT = 8
RALPH_ROUNDS_MAX = 32
ACTIVE_SECONDS_DEFAULT = 7200
ACTIVE_SECONDS_MAX = 28800
PERMISSION_WAIT_SECONDS = 600
VERIFICATION_REPAIRS = 2


class ContractError(ValueError):
    pass


def _text(value: Any, name: str, *, required=False, maximum=None) -> str | None:
    if value is None:
        if required:
            raise ContractError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ContractError(f"{name} must not be empty")
    if maximum is not None and len(value) > maximum:
        raise ContractError(f"{name} must be at most {maximum} characters")
    return value or None


def _integer(value: Any, name: str, low: int, high: int, default=None) -> int:
    if value is None:
        if default is None:
            raise ContractError(f"{name} is required")
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ContractError(f"{name} must be an integer between {low} and {high}")
    return value


@dataclass(frozen=True, slots=True)
class Dependency:
    run_id: int
    condition: str = "success"

    @classmethod
    def from_value(cls, value: Any) -> "Dependency":
        if not isinstance(value, Mapping) or set(value) - {"run_id", "condition"}:
            raise ContractError("each after entry must contain run_id and optional condition")
        condition = value.get("condition", "success")
        if condition not in DEPENDENCY_CONDITIONS:
            raise ContractError("after.condition must be success or terminal")
        return cls(_integer(value.get("run_id"), "after.run_id", 1, 2**63 - 1), condition)

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "condition": self.condition}


@dataclass(frozen=True, slots=True)
class Verify:
    argv: tuple[str, ...]
    timeout_seconds: int = 600

    @classmethod
    def from_value(cls, value: Any) -> "Verify | None":
        if value is None:
            return None
        if not isinstance(value, Mapping) or set(value) - {"argv", "timeout_seconds"}:
            raise ContractError("verify must contain argv and optional timeout_seconds")
        argv = value.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item or "\0" in item for item in argv):
            raise ContractError("verify.argv must be a non-empty string array")
        return cls(tuple(argv), _integer(value.get("timeout_seconds"), "verify.timeout_seconds", 1, 3600, 600))

    def as_dict(self) -> dict:
        return {"argv": list(self.argv), "timeout_seconds": self.timeout_seconds}


@dataclass(frozen=True, slots=True)
class RunRequest:
    request_id: str
    profile: str
    objective: str
    group: str = "general"
    strategy: str = "goal"
    permission_mode: str = "workspace-write"
    title: str | None = None
    cwd: str | None = None
    ref: str | None = None
    after: tuple[Dependency, ...] = field(default_factory=tuple)
    requested_by: str = "operator"
    max_rounds: int = GOAL_ROUNDS_DEFAULT
    active_seconds: int = ACTIVE_SECONDS_DEFAULT
    verify: Verify | None = None
    max_children: int | None = None
    max_child_tier: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunRequest":
        if not isinstance(value, Mapping):
            raise ContractError("request body must be an object")
        accepted = {"request_id", "profile", "objective", "group", "strategy", "permission_mode", "title", "cwd", "ref", "after", "requested_by", "limits", "verify", "max_children", "max_child_tier"}
        unknown = set(value) - accepted
        if unknown:
            raise ContractError("unknown run request fields: " + ", ".join(sorted(unknown)))
        strategy = value.get("strategy", "goal")
        if strategy not in STRATEGIES:
            raise ContractError("strategy must be goal or ralph")
        permission = value.get("permission_mode", "workspace-write")
        if permission not in PERMISSION_MODES:
            raise ContractError("permission_mode must be read-only, workspace-write, or danger-full-access")
        limits = value.get("limits") or {}
        if not isinstance(limits, Mapping) or set(limits) - {"max_rounds", "active_seconds"}:
            raise ContractError("limits may contain max_rounds and active_seconds")
        round_default, round_max = (GOAL_ROUNDS_DEFAULT, GOAL_ROUNDS_MAX) if strategy == "goal" else (RALPH_ROUNDS_DEFAULT, RALPH_ROUNDS_MAX)
        raw_after = value.get("after") or []
        if not isinstance(raw_after, list):
            raise ContractError("after must be an array")
        after = tuple(Dependency.from_value(item) for item in raw_after)
        if len({item.run_id for item in after}) != len(after):
            raise ContractError("after must not repeat a run")
        return cls(
            request_id=_text(value.get("request_id"), "request_id", required=True, maximum=200) or "",
            profile=_text(value.get("profile"), "profile", required=True, maximum=128) or "",
            objective=_text(value.get("objective"), "objective", required=True) or "",
            group=_text(value.get("group", "general"), "group", required=True, maximum=128) or "general",
            strategy=strategy, permission_mode=permission,
            title=_text(value.get("title"), "title", maximum=200), cwd=_text(value.get("cwd"), "cwd"), ref=_text(value.get("ref"), "ref", maximum=500),
            after=after, requested_by=_text(value.get("requested_by", "operator"), "requested_by", required=True, maximum=128) or "operator",
            max_rounds=_integer(limits.get("max_rounds"), "limits.max_rounds", 1, round_max, round_default),
            active_seconds=_integer(limits.get("active_seconds"), "limits.active_seconds", 1, ACTIVE_SECONDS_MAX, ACTIVE_SECONDS_DEFAULT),
            verify=Verify.from_value(value.get("verify")),
            max_children=None if value.get("max_children") is None else _integer(value["max_children"], "max_children", 1, 100),
            max_child_tier=None if value.get("max_child_tier") is None else _integer(value["max_child_tier"], "max_child_tier", 1, 3),
        )

    def as_dict(self) -> dict:
        return {"request_id": self.request_id, "profile": self.profile, "objective": self.objective, "group": self.group, "strategy": self.strategy, "permission_mode": self.permission_mode, "title": self.title, "cwd": self.cwd, "ref": self.ref, "after": [item.as_dict() for item in self.after], "requested_by": self.requested_by, "limits": {"max_rounds": self.max_rounds, "active_seconds": self.active_seconds}, "verify": self.verify.as_dict() if self.verify else None, "max_children": self.max_children, "max_child_tier": self.max_child_tier}


def child_tier_allowed(parent_tier: int, child_tier: int, ceiling: int | None = None) -> bool:
    return parent_tier in (1, 2, 3) and child_tier in (1, 2, 3) and child_tier <= (ceiling or parent_tier)
