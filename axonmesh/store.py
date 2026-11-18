"""SQLite store: the single source of truth (decisions 2, 9, 10)."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
create table if not exists workspaces (
  id integer primary key, repo_root text unique, ignore_case integer);
create table if not exists runs (
  id text primary key, workspace_id integer, spec text, mode text, status text,
  started_at real, finished_at real, total_tokens integer default 0, cost real default 0);
create table if not exists agents (
  id text primary key, workspace_id integer, run_id text, name text, origin text, harness text,
  model text, rules_yaml text, commands text, mode_override text, approval text,
  program_path text, parent_process text, worktree text, heartbeat_at real, last_activity_at real,
  reported_status text, last_seen_event_id integer default 0, client_pid integer);
create table if not exists tasks (
  id text primary key, workspace_id integer, run_id text, kind text, title text,
  owner_agent_id text, status text, version integer, attempts integer default 0, depends_on text);
create table if not exists claims (
  id integer primary key, workspace_id integer, run_id text, task_id text, agent_id text,
  path_norm text, lock_type text, expires_at real, released integer default 0);
create table if not exists messages (
  id integer primary key, workspace_id integer, run_id text, task_id text, from_agent text,
  to_agent text, body text, urgent integer default 0, idempotency_key text, reply_to integer,
  state text, deadline real, created_at real, unique (from_agent, idempotency_key));
create table if not exists handoffs (
  id integer primary key, task_id text, from_agent text, to_agent text, state text, created_at real);
create table if not exists approvals (
  id integer primary key, run_id text, agent_id text, task_id text, request text, state text,
  created_at real, decided_at real);
create table if not exists contracts (
  id integer primary key, workspace_id integer, run_id text, name text, version integer,
  schema text, published_by text, created_at real);
create table if not exists evidence (
  id integer primary key, task_id text, item text, source text, created_at real);
create table if not exists task_notes (
  id integer primary key, task_id text, agent_id text, kind text, body text, created_at real);
create table if not exists events (
  id integer primary key autoincrement, workspace_id integer, run_id text, agent_id text,
  type text, payload text, created_at real);
create table if not exists token_ledger (
  id integer primary key, run_id text, task_id text, agent_id text, model text,
  input integer, output integer, cache_read integer default 0, cache_write integer default 0, cost real);
create index if not exists events_ws on events (workspace_id, id);
create index if not exists claims_path on claims (workspace_id, released, path_norm);
"""


MIGRATIONS = [("agents", "client_pid", "integer")]


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we manage transactions ourselves with BEGIN IMMEDIATE (fix #14)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma busy_timeout=30000")
        self.db.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:  # columns added after a database was first created
            if col not in {r[1] for r in self.db.execute(f"pragma table_info({table})")}:
                self.db.execute(f"alter table {table} add column {col} {decl}")
        self.lock = threading.RLock()  # one connection shared by harness threads

    @contextmanager
    def tx(self):
        """Write transaction. Takes the write lock up front, so it can't fail with 'database is locked' midway."""
        with self.lock:
            if self.db.in_transaction:  # nested: join the outer transaction
                yield self.db
                return
            self.db.execute("begin immediate")
            try:
                yield self.db
                self.db.execute("commit")
            except BaseException:
                self.db.execute("rollback")
                raise

    def q(self, sql: str, *params) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def one(self, sql: str, *params):
        rows = self.q(sql, *params)
        return rows[0] if rows else None

    def event(self, workspace_id, run_id, agent_id, type_, payload: dict):
        with self.tx() as db:
            db.execute(
                "insert into events (workspace_id, run_id, agent_id, type, payload, created_at) values (?,?,?,?,?,?)",
                (workspace_id, run_id, agent_id, type_, json.dumps(payload, default=str), time.time()),
            )
