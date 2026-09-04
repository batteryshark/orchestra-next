"""Durable routes validated against DSH's ACP catalog."""
from __future__ import annotations

import json
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


# V2 (~/.orchestra/v2/orchestra.db) stored runtime-specific model names; DSH routes are provider/model.
V2_ROUTES = {
    ("claude", "claude-opus-5"): ("claude-subscription", "opus"),
    ("claude", "claude-sonnet-5"): ("claude-subscription", "sonnet"),
    ("claude", "claude-haiku-4-5"): ("claude-subscription", "haiku"),
    ("claude", "claude-fable-5"): ("claude-subscription", "fable"),
    ("codex", "gpt-5.6-sol"): ("openai-codex", "gpt-5.6-sol"),
    ("codex", "gpt-5.6-luna"): ("openai-codex", "gpt-5.6-luna"),
    ("reasonix", "deepseek/deepseek-v4-flash"): ("deepseek-official", "deepseek-v4-flash"),
    ("reasonix", "deepseek/deepseek-v4-pro"): ("deepseek-official", "deepseek-v4-pro"),
    ("opencode", "zai/glm-5.3"): ("zai", "glm-5.3"),
    ("opencode", "zai/glm-5.3-flash"): ("zai", "glm-5.3-flash"),
    ("pi", "zai/glm-5.3"): ("zai", "glm-5.3"),
    ("pi", "zai/glm-5.3-flash"): ("zai", "glm-5.3-flash"),
}
# DSH efforts differ per provider; a V2 effort DSH does not advertise falls to the nearest one it does.
EFFORT_FALLBACK = {"medium": ("high", "low"), "max": ("high",), "high": ("medium",), "low": ("medium",)}


def import_v2(con, v2_path, *, catalog, apply=False, actor="operator") -> list[dict]:
    """Plan (and with apply=True, create) profiles from a V2 database. One row per V2 profile."""
    v2 = sqlite3.connect(f"file:{v2_path}?mode=ro", uri=True)
    v2.row_factory = sqlite3.Row
    rows = v2.execute("SELECT p.*, r.slug AS runtime FROM profiles p JOIN runtimes r USING(runtime_id) "
                      "WHERE p.archived=0 ORDER BY p.tier DESC, p.priority DESC").fetchall()
    report = []
    for row in rows:
        entry = {"slug": row["slug"], "v2": f'{row["runtime"]}/{row["model"]}', "effort": row["effort"]}
        route = V2_ROUTES.get((row["runtime"], row["model"]))
        config = json.loads(row["config_json"] or "{}")
        if not row["enabled"]:
            entry.update(outcome="skipped", reason="disabled in V2")
        elif route is None or route not in catalog:
            entry.update(outcome="skipped", reason="no DSH route")
        elif find(con, row["slug"]) is not None:
            entry.update(outcome="exists", route="/".join(route))
        else:
            effort = row["effort"] or config.get("variant")  # OpenCode GLM variants were efforts by another name
            if effort and effort not in catalog[route]:
                effort = next((e for e in EFFORT_FALLBACK.get(effort, ()) if e in catalog[route]), None)
            note = "; ".join(part for part in (
                config.get("role"), "spawn: " + ", ".join(config["spawn_profiles"]) if config.get("spawn_profiles") else None,
                f"imported from V2 {row['runtime']}/{row['model']} {row['effort'] or ''}".rstrip()) if part)
            entry.update(outcome="create" if not apply else "created", route="/".join(route), effort=effort, tier=row["tier"])
            if apply:
                create(con, name=row["name"], slug=row["slug"], provider=route[0], model=route[1], effort=effort,
                       tier=row["tier"], max_concurrency=row["max_concurrency"], note=note, catalog=catalog, actor=actor)
        report.append(entry)
    v2.close()
    return report
