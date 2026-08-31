"""Small, strict value objects at Orchestra's v2 boundary.

The daemon accepts neutral requests. These objects deliberately know nothing
about caller-specific workflow, routing, acceptance, or delivery policy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping


RUN_STATES = (
    "queued",
    "starting",
    "running",
    "waiting",
    "completed",
    "failed",
    "timed_out",
    "stopped",
    "skipped",
)
TERMINAL_STATES = frozenset(RUN_STATES[-5:])
WAITING_KINDS = frozenset(("input", "children"))
DEPENDENCY_CONDITIONS = frozenset(("success", "terminal"))


class ContractError(ValueError):
    """A public v2 value is malformed."""


def _text(value: Any, field_name: str, *, required: bool = False,
          maximum: int | None = None) -> str | None:
    if value is None:
        if required:
            raise ContractError(f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise ContractError(f"{field_name} must be a string")
    value = value.strip()
    if required and not value:
        raise ContractError(f"{field_name} must not be empty")
    if maximum is not None and len(value) > maximum:
        raise ContractError(f"{field_name} must be at most {maximum} characters")
    return value or None


def _id(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be a positive run id")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be a positive run id") from exc
    if result < 1:
        raise ContractError(f"{field_name} must be a positive run id")
    return result


def _bounded(value: Any, field_name: str, low: int, high: int) -> int | None:
    """An optional integer ceiling the operator chose, or None for the default."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be an integer") from exc
    if not low <= result <= high:
        raise ContractError(f"{field_name} must be between {low} and {high}")
    return result


@dataclass(frozen=True, slots=True)
class Dependency:
    run_id: int
    condition: str = "success"

    @classmethod
    def from_value(cls, value: Any) -> "Dependency":
        if not isinstance(value, Mapping):
            raise ContractError("each after entry must be an object")
        unknown = set(value) - {"run_id", "condition"}
        if unknown:
            raise ContractError(
                "unknown after field(s): " + ", ".join(sorted(unknown)))
        condition = value.get("condition", "success")
        if condition not in DEPENDENCY_CONDITIONS:
            allowed = ", ".join(sorted(DEPENDENCY_CONDITIONS))
            raise ContractError(f"after.condition must be one of: {allowed}")
        return cls(_id(value.get("run_id"), "after.run_id"), condition)

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "condition": self.condition}


@dataclass(frozen=True, slots=True)
class RunRequest:
    request_id: str
    profile: str
    context: str
    group: str = "general"
    title: str | None = None
    cwd: str | None = None
    ref: str | None = None
    after: tuple[Dependency, ...] = field(default_factory=tuple)
    requested_by: str = "operator"
    observer: str = "inherit"
    parent_run_id: int | None = None
    max_children: int | None = None
    max_child_tier: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *,
                     allow_parent: bool = True) -> "RunRequest":
        if not isinstance(value, Mapping):
            raise ContractError("request body must be an object")
        if not allow_parent and "parent_run_id" in value:
            raise ContractError(
                "parent_run_id is internal; delegate through /runs/{id}/children")
        accepted = {
            "request_id", "profile", "context", "group", "title", "cwd",
            "ref", "after", "requested_by",
            "observer", "parent_run_id", "max_children", "max_child_tier",
        }
        unknown = set(value) - accepted
        if unknown:
            raise ContractError("unknown run request field(s): " +
                                ", ".join(sorted(unknown)))

        raw_after = value.get("after", [])
        if not isinstance(raw_after, list):
            raise ContractError("after must be an array")
        after = tuple(Dependency.from_value(item) for item in raw_after)
        if len({dep.run_id for dep in after}) != len(after):
            raise ContractError("after must not name the same run twice")

        observer = _text(value.get("observer", "inherit"), "observer",
                         required=True, maximum=128)
        parent = value.get("parent_run_id")
        return cls(
            request_id=_text(value.get("request_id"), "request_id", required=True,
                             maximum=200) or "",
            profile=_text(value.get("profile"), "profile", required=True,
                          maximum=128) or "",
            context=_text(value.get("context"), "context", required=True) or "",
            group=_text(value.get("group", "general"), "group", required=True,
                        maximum=128) or "general",
            title=_text(value.get("title"), "title", maximum=200),
            cwd=_text(value.get("cwd"), "cwd"),
            ref=_text(value.get("ref"), "ref", maximum=500),
            after=after,
            requested_by=_text(value.get("requested_by", "operator"),
                               "requested_by", required=True, maximum=128)
                         or "operator",
            observer=observer or "inherit",
            parent_run_id=None if parent is None else _id(parent, "parent_run_id"),
            max_children=_bounded(value.get("max_children"),
                                  "max_children", 1, 100),
            max_child_tier=_bounded(value.get("max_child_tier"),
                                    "max_child_tier", 1, 3),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "profile": self.profile,
            "context": self.context,
            "group": self.group,
            "title": self.title,
            "cwd": self.cwd,
            "ref": self.ref,
            "after": [dependency.as_dict() for dependency in self.after],
            "requested_by": self.requested_by,
            "observer": self.observer,
            "parent_run_id": self.parent_run_id,
            "max_children": self.max_children,
            "max_child_tier": self.max_child_tier,
        }


def child_tier_allowed(parent_tier: int, child_tier: int,
                       ceiling: int | None = None) -> bool:
    """Children may use the parent's capability tier or a cheaper one, unless
    the operator raised the ceiling for this run when dispatching it."""
    top = ceiling if ceiling in (1, 2, 3) else parent_tier
    return parent_tier in (1, 2, 3) and child_tier in (1, 2, 3) \
        and child_tier <= top


def delegation_overrides(request_snapshot: str | None) -> dict[str, int]:
    """The per-run delegation ceilings chosen at dispatch, if any.

    They ride in ``request_snapshot`` rather than their own run columns: the
    snapshot already freezes the whole request, and nothing queries on them.
    """
    try:
        data = json.loads(request_snapshot or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in ("max_children", "max_child_tier")
            if isinstance(data.get(key), int)
            and not isinstance(data.get(key), bool)}
