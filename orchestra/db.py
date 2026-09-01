"""Fresh Orchestra-next SQLite store. No migration or compatibility path exists."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from orchestra import paths

SCHEMA_VERSION = "v3"
GENERAL_GROUP_ID = "general"
RUN_ACTIVE = ("queued", "starting", "running", "waiting")
RUN_TERMINAL = ("completed", "failed", "timed_out", "stopped", "skipped")
TERMINAL_SQL = "(" + ",".join(f"'{status}'" for status in RUN_TERMINAL) + ")"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE run_groups (
  group_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
  name TEXT NOT NULL,
  default_cwd TEXT,
  max_concurrency INTEGER CHECK(max_concurrency IS NULL OR max_concurrency > 0),
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
  last_run_seq INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TRIGGER protect_general_group_delete BEFORE DELETE ON run_groups
WHEN OLD.group_id='general' BEGIN SELECT RAISE(ABORT,'the General group is permanent'); END;

CREATE TABLE profiles (
  profile_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
  name TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  effort TEXT,
  tier INTEGER NOT NULL CHECK(tier BETWEEN 1 AND 3),
  max_concurrency INTEGER CHECK(max_concurrency IS NULL OR max_concurrency > 0),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
  note TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE devices (
  device_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, last_seen_at TEXT, revoked_at TEXT
);
CREATE TABLE service_tokens (
  token_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  authorities_json TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT, revoked_at TEXT
);
CREATE TABLE pairing_codes (
  pairing_id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
  created_by_device_id TEXT REFERENCES devices(device_id), created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, used_at TEXT
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  request_id TEXT NOT NULL UNIQUE,
  group_id TEXT NOT NULL REFERENCES run_groups(group_id),
  group_seq INTEGER NOT NULL,
  profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
  root_run_id INTEGER REFERENCES runs(id),
  parent_run_id INTEGER REFERENCES runs(id),
  retry_of_run_id INTEGER REFERENCES runs(id),
  continuation_of_run_id INTEGER REFERENCES runs(id),
  title TEXT,
  objective TEXT NOT NULL,
  strategy TEXT NOT NULL CHECK(strategy IN ('goal','ralph')),
  permission_mode TEXT NOT NULL CHECK(permission_mode IN ('read-only','workspace-write','danger-full-access')),
  max_rounds INTEGER NOT NULL,
  active_seconds_limit INTEGER NOT NULL,
  active_seconds REAL NOT NULL DEFAULT 0,
  verify_json TEXT,
  verification_repairs INTEGER NOT NULL DEFAULT 0,
  max_children INTEGER,
  max_child_tier INTEGER,
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','starting','running','waiting','completed','failed','timed_out','stopped','skipped')),
  waiting_kind TEXT CHECK(waiting_kind IS NULL OR waiting_kind IN ('attention','children','permission','verification')),
  waiting_detail TEXT,
  cwd TEXT NOT NULL,
  workdir TEXT,
  branch TEXT,
  start_ref TEXT,
  end_ref TEXT,
  git_status TEXT,
  git_diff_stat TEXT,
  dsh_session_id TEXT,
  dsh_journal_path TEXT,
  dsh_pid INTEGER,
  resume_count INTEGER NOT NULL DEFAULT 0,
  goal_id TEXT,
  goal_state TEXT,
  goal_corrected INTEGER NOT NULL DEFAULT 0,
  rounds_started INTEGER NOT NULL DEFAULT 0,
  route_provider TEXT NOT NULL,
  route_model TEXT NOT NULL,
  route_effort TEXT,
  cache_epoch INTEGER NOT NULL DEFAULT 0,
  tokens_input INTEGER NOT NULL DEFAULT 0,
  tokens_output INTEGER NOT NULL DEFAULT 0,
  tokens_cache_read INTEGER NOT NULL DEFAULT 0,
  tokens_cache_write INTEGER NOT NULL DEFAULT 0,
  tokens_total INTEGER NOT NULL DEFAULT 0,
  run_token_hash TEXT,
  summary TEXT,
  error TEXT,
  profile_snapshot TEXT NOT NULL,
  request_snapshot TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  UNIQUE(group_id,group_seq)
);
CREATE INDEX idx_runs_status ON runs(status,created_at);
CREATE INDEX idx_runs_parent ON runs(parent_run_id);

CREATE TABLE run_dependencies (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  depends_on_run_id INTEGER NOT NULL REFERENCES runs(id),
  condition TEXT NOT NULL CHECK(condition IN ('success','terminal')),
  PRIMARY KEY(run_id,depends_on_run_id)
);
CREATE TABLE messages (
  message_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  sender TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
  delivered_at TEXT, acknowledged_at TEXT
);
CREATE TABLE attention_requests (
  attention_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  kind TEXT NOT NULL, prompt TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','answered','cancelled','expired')),
  blocking INTEGER NOT NULL DEFAULT 1 CHECK(blocking IN (0,1)),
  created_at TEXT NOT NULL, expires_at TEXT, closed_at TEXT
);
CREATE TABLE attention_responses (
  response_id TEXT PRIMARY KEY, attention_id TEXT NOT NULL REFERENCES attention_requests(attention_id),
  answer TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE attention_leases (
  attention_id TEXT PRIMARY KEY REFERENCES attention_requests(attention_id),
  lease_id TEXT NOT NULL UNIQUE, holder TEXT NOT NULL, acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, released_at TEXT
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  session_id TEXT, source_seq INTEGER, type TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(run_id,session_id,source_seq)
);
CREATE TABLE usage_events (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  session_id TEXT NOT NULL, source_seq INTEGER NOT NULL, event_type TEXT NOT NULL,
  provider TEXT, model TEXT, descendant_session_id TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0, cache_epoch INTEGER NOT NULL,
  observed_at TEXT NOT NULL, raw_json TEXT NOT NULL,
  UNIQUE(run_id,session_id,source_seq)
);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), name TEXT NOT NULL,
  relative_path TEXT NOT NULL, stored_path TEXT NOT NULL, source_path TEXT NOT NULL,
  mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL, pruned_at TEXT, UNIQUE(run_id,relative_path)
);
CREATE TABLE evidence_pins (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id), reason TEXT,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE prune_plans (
  plan_id TEXT PRIMARY KEY, criteria_json TEXT NOT NULL, items_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, applied_by TEXT,
  applied_at TEXT, result_json TEXT
);
CREATE TABLE control_events (
  id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
  target_type TEXT, target_id TEXT, request_id TEXT, detail TEXT,
  outcome TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_file=None) -> sqlite3.Connection:
    target = Path(db_file) if db_file is not None else paths.db_path()
    if str(target) != ":memory:":
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(str(target), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if not tables:
        stamp = now()
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO meta(key,value) VALUES(?,?)", (("schema_version", SCHEMA_VERSION), ("instance_id", str(uuid.uuid4())), ("board_revision", "0")))
        con.execute("INSERT INTO run_groups(group_id,slug,name,created_at,updated_at) VALUES('general','general','General',?,?)", (stamp, stamp))
        con.commit()
    else:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            con.close()
            raise RuntimeError(f"orchestra-next: {target} is not a fresh {SCHEMA_VERSION} database")
    return con


def meta_get(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(con, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def instance_id(con) -> str:
    return meta_get(con, "instance_id") or ""


def board_revision(con) -> int:
    return int(meta_get(con, "board_revision") or 0)


def bump_board_revision(con) -> None:
    con.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='board_revision'")


def record_control(con, *, actor: str, action: str, outcome: str,
                   target_type=None, target_id=None, request_id=None, detail=None) -> int:
    encoded = None if detail is None else json.dumps(detail, ensure_ascii=False, default=str)[:8000]
    cur = con.execute("INSERT INTO control_events(actor,action,target_type,target_id,request_id,detail,outcome,created_at) VALUES(?,?,?,?,?,?,?,?)", (actor, action, target_type, None if target_id is None else str(target_id), request_id, encoded, outcome, now()))
    bump_board_revision(con)
    return int(cur.lastrowid)


def append_event(con, run_id: int, event_type: str, payload: dict, *, session_id=None, source_seq=None) -> bool:
    try:
        con.execute("INSERT INTO events(run_id,session_id,source_seq,type,payload_json,created_at) VALUES(?,?,?,?,?,?)", (run_id, session_id, source_seq, event_type, json.dumps(payload, ensure_ascii=False, default=str), now()))
    except sqlite3.IntegrityError:
        return False
    bump_board_revision(con)
    return True
