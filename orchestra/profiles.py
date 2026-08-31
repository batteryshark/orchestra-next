"""Durable routes validated against DSH's ACP catalog."""
from __future__ import annotations

import sqlite3
import uuid

from orchestra import db, paths


def find(con, selector: str):
    return con.execute("SELECT * FROM profiles WHERE profile_id=? OR slug=?", (selector, selector)).fetchone()


def all_profiles(con, *, include_archived=False):
    where = "" if include_archived else " WHERE archived=0"
    return con.execute("SELECT * FROM profiles" + where + " ORDER BY lower(name)").fetchall()


def _validate_route(provider: str, model: str, effort: str | None, catalog) -> None:
    choices = catalog if catalog is not None else __import__("orchestra.dsh", fromlist=["catalog"]).catalog(str(paths.state_dir()))
    efforts = choices.get((provider, model))
    if efforts is None:
        raise ValueError(f"DSH does not advertise {provider}/{model}")
    if effort is not None and effort not in efforts:
        allowed = ", ".join(sorted(efforts)) or "no reasoning effort"
        raise ValueError(f"DSH does not advertise effort {effort!r} for {provider}/{model}; available: {allowed}")


def create(con: sqlite3.Connection, *, name: str, provider: str, model: str,
           effort: str | None = None, slug: str | None = None, tier: int = 1,
           max_concurrency: int | None = None, note: str | None = None,
           catalog=None, actor="operator"):
    name, provider, model = (str(value or "").strip() for value in (name, provider, model))
    if not all((name, provider, model)):
        raise ValueError("name, provider, and model are required")
    if tier not in (1, 2, 3):
        raise ValueError("tier must be 1, 2, or 3")
    if max_concurrency is not None and (isinstance(max_concurrency, bool) or int(max_concurrency) < 1):
        raise ValueError("max_concurrency must be positive")
    _validate_route(provider, model, effort, catalog)
    profile_id, stamp = str(uuid.uuid4()), db.now()
    chosen = paths.kebab(slug or name)
    with con:
        con.execute("INSERT INTO profiles(profile_id,slug,name,provider,model,effort,tier,max_concurrency,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (profile_id, chosen, name, provider, model, effort, tier, max_concurrency, note, stamp, stamp))
        db.record_control(con, actor=actor, action="profile.create", outcome="ok", target_type="profile", target_id=profile_id, detail={"provider": provider, "model": model, "effort": effort})
    return find(con, profile_id)


def update(con, selector: str, *, expected_revision: int, catalog=None, actor="operator", **changes):
    row = find(con, selector)
    if row is None:
        raise LookupError(f"no profile matches {selector!r}")
    allowed = {"name", "provider", "model", "effort", "tier", "max_concurrency", "enabled", "archived", "note"}
    if set(changes) - allowed:
        raise ValueError("unknown profile fields: " + ", ".join(sorted(set(changes) - allowed)))
    values = dict(row)
    values.update(changes)
    if not all(str(values[key] or "").strip() for key in ("name", "provider", "model")):
        raise ValueError("name, provider, and model are required")
    if values["tier"] not in (1, 2, 3):
        raise ValueError("tier must be 1, 2, or 3")
    if values["max_concurrency"] is not None and (
            isinstance(values["max_concurrency"], bool) or int(values["max_concurrency"]) < 1):
        raise ValueError("max_concurrency must be positive")
    for key in ("enabled", "archived"):
        if key in changes and not isinstance(changes[key], bool):
            raise ValueError(f"{key} must be a boolean")
    if any(key in changes for key in ("provider", "model", "effort")):
        _validate_route(values["provider"], values["model"], values["effort"], catalog)
    assignments = ",".join(f"{key}=?" for key in changes)
    if not assignments:
        return row
    with con:
        changed = con.execute(f"UPDATE profiles SET {assignments},revision=revision+1,updated_at=? WHERE profile_id=? AND revision=?", (*changes.values(), db.now(), row["profile_id"], expected_revision))
        if changed.rowcount != 1:
            raise RuntimeError("profile changed since it was read")
        db.record_control(con, actor=actor, action="profile.update", outcome="ok", target_type="profile", target_id=row["profile_id"], detail=changes)
    return find(con, row["profile_id"])


def payload(row) -> dict:
    value = {key: row[key] for key in ("slug", "name", "provider", "model", "effort", "tier", "max_concurrency", "enabled", "archived", "note", "revision", "created_at", "updated_at")}
    value["enabled"] = bool(value["enabled"])
    value["archived"] = bool(value["archived"])
    return {"id": row["profile_id"], **value}
