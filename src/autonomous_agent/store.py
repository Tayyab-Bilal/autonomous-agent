"""SQLite store. Every claim that must be exclusive is a conditional UPDATE, a unique INSERT, or a
BEGIN IMMEDIATE transaction around a check-then-write, so two workers on one database cannot both win.

A running run is owned through a fencing token: the `seq` the claim returned. Heartbeats, step
writes and ledger writes only succeed while the row is still 'running' at that seq, so a worker
that was reaped or replaced is cut off instead of racing its successor."""
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from .clock import Clock, utcnow

REUSE_WINDOW = timedelta(minutes=10)
LOCK_TTL = timedelta(minutes=5)

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, tenant TEXT, user TEXT, goal TEXT, goal_hash TEXT,
  plan_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, plan_id TEXT, tenant TEXT, status TEXT, reason TEXT DEFAULT '',
  context TEXT, writes INTEGER DEFAULT 0, seq INTEGER DEFAULT 0, heartbeat TEXT, created_at TEXT,
  turns_used INTEGER DEFAULT 0, resume_count INTEGER DEFAULT 0, trigger TEXT DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS step_states (
  run_id TEXT, step_id TEXT, status TEXT, detail TEXT DEFAULT '', instruction TEXT,
  PRIMARY KEY (run_id, step_id));
CREATE TABLE IF NOT EXISTS write_ledger (
  run_id TEXT, idem_key TEXT, result TEXT, step_id TEXT, tool TEXT, args TEXT,
  PRIMARY KEY (run_id, idem_key));
CREATE TABLE IF NOT EXISTS step_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, step_id TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS scratchpad (
  run_id TEXT, key TEXT, value TEXT, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS approvals (
  run_id TEXT, idem_key TEXT, status TEXT, reason TEXT, call TEXT, PRIMARY KEY (run_id, idem_key));
CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, owner TEXT, expires_at TEXT);
"""


class LockBusy(Exception):
    pass


class Fenced(Exception):
    """This worker no longer owns the run (reaped, or claimed by another worker)."""


def ts(dt: datetime) -> str:
    """Fixed-width UTC text so string comparison in SQL is time comparison."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Store:
    def __init__(self, path: str = ":memory:", clock: Clock = utcnow) -> None:
        self.clock = clock
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def now(self) -> str:
        return ts(self.clock())

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # -- plans ---------------------------------------------------------------------------
    @contextmanager
    def plan_lock(self, tenant: str, request_hash: str, ttl: timedelta = LOCK_TTL):
        """Portable advisory lock: INSERT-or-fail with an expiry so a dead holder cannot wedge it.
        The TTL must comfortably exceed the slowest plan request (LLM latency), because an expired
        lock can be taken over; release is owner-checked so the old holder never frees the new
        holder's lock. Simplification: a lock table, not a database advisory lock; use
        pg_try_advisory_lock on Postgres (see README)."""
        name, owner = f"plan:{tenant}:{request_hash}", uuid.uuid4().hex
        with self.tx():
            self.db.execute("DELETE FROM locks WHERE name=? AND expires_at<=?", (name, self.now()))
            try:
                self.db.execute("INSERT INTO locks VALUES (?,?,?)", (name, owner, ts(self.clock() + ttl)))
            except sqlite3.IntegrityError:
                raise LockBusy(name) from None
        try:
            yield
        finally:
            self.db.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    def find_recent_plan(self, tenant: str, goal_hash: str, window: timedelta = REUSE_WINDOW):
        return self.db.execute(
            "SELECT * FROM plans WHERE tenant=? AND goal_hash=? AND created_at>=? "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant, goal_hash, ts(self.clock() - window))).fetchone()

    def save_plan(self, tenant: str, user: str, goal: str, goal_hash: str, plan_json: str) -> str:
        plan_id = f"plan-{uuid.uuid4().hex[:8]}"
        self.db.execute("INSERT INTO plans VALUES (?,?,?,?,?,?,?)",
                        (plan_id, tenant, user, goal, goal_hash, plan_json, self.now()))
        return plan_id

    def get_plan(self, plan_id: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()

    def plans_for_tenant(self, tenant: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM plans WHERE tenant=?", (tenant,)).fetchall()

    # -- runs ----------------------------------------------------------------------------
    def create_run(self, plan_id: str, tenant: str, context: dict, trigger: str = "manual") -> str:
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.db.execute(
            "INSERT INTO runs (id, plan_id, tenant, status, context, heartbeat, created_at, trigger) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, plan_id, tenant, "pending", json.dumps(context), self.now(), self.now(), trigger))
        return run_id

    def get_run(self, run_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def update_status(self, run_id: str, allowed_from: tuple[str, ...], to: str, reason: str, *,
                      seq: int | None = None, stale_before: datetime | None = None) -> int | None:
        """The atomic claim: succeeds only if the run is still in an allowed state (and, when given,
        still at this fencing token / still stale). Returns the new seq, or None if it lost."""
        marks = ",".join("?" * len(allowed_from))
        # resume_count counts only resumes of parked failures (parked -> running)
        sql = (f"UPDATE runs SET status=?, reason=?, heartbeat=?, seq=seq+1, "
               f"resume_count=resume_count + (status='parked') "
               f"WHERE id=? AND status IN ({marks})")
        args: list = [to, reason, self.now(), run_id, *allowed_from]
        if seq is not None:
            sql, args = sql + " AND seq=?", [*args, seq]
        if stale_before is not None:
            sql, args = sql + " AND heartbeat<?", [*args, ts(stale_before)]
        rows = self.db.execute(sql + " RETURNING seq", args).fetchall()
        return rows[0]["seq"] if rows else None

    def holds(self, run_id: str, token: int) -> bool:
        return self.db.execute("SELECT 1 FROM runs WHERE id=? AND status='running' AND seq=?",
                               (run_id, token)).fetchone() is not None

    def check_fence(self, run_id: str, token: int) -> None:
        if not self.holds(run_id, token):
            raise Fenced(run_id)

    def tick(self, run_id: str, token: int) -> None:
        """Heartbeat plus turn counter in one fenced write."""
        cur = self.db.execute(
            "UPDATE runs SET heartbeat=?, turns_used=turns_used+1 "
            "WHERE id=? AND status='running' AND seq=?", (self.now(), run_id, token))
        if cur.rowcount != 1:
            raise Fenced(run_id)

    def active_runs(self, tenant: str, trigger: str | None = None) -> int:
        """Unfinished runs for a tenant, optionally only those started by one kind of trigger."""
        return self.db.execute(
            "SELECT COUNT(*) FROM runs WHERE tenant=? AND (? IS NULL OR trigger=?) "
            "AND status IN ('pending','running','awaiting_approval')", (tenant, trigger, trigger)).fetchone()[0]

    def failed_runs(self, plan_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM runs WHERE plan_id=? AND status='failed'",
                               (plan_id,)).fetchone()[0]

    def running_older_than(self, cutoff: datetime) -> list[str]:
        rows = self.db.execute("SELECT id FROM runs WHERE status='running' AND heartbeat<?",
                               (ts(cutoff),)).fetchall()
        return [r["id"] for r in rows]

    # -- step states and write ledger ----------------------------------------------------
    def init_steps(self, run_id: str, step_ids: list[str]) -> None:
        self.db.executemany("INSERT OR IGNORE INTO step_states (run_id, step_id, status) "
                            "VALUES (?,?,'pending')", [(run_id, s) for s in step_ids])

    def set_step(self, run_id: str, step_id: str, status: str, detail: str, token: int) -> bool:
        """pending -> anything; complete -> blocked|failed (a gate verdict beats the agent's claim,
        whatever order they arrive in). Nothing else moves, so an agent cannot talk a blocked or
        failed step into 'complete'. False = nothing changed."""
        cur = self.db.execute(
            "UPDATE step_states SET status=?, detail=? WHERE run_id=? AND step_id=? "
            "AND (status='pending' OR (status='complete' AND ? IN ('blocked','failed'))) "
            "AND EXISTS (SELECT 1 FROM runs WHERE id=? AND status='running' AND seq=?)",
            (status, detail, run_id, step_id, status, run_id, token))
        if cur.rowcount != 1:
            self.check_fence(run_id, token)  # raises Fenced if we lost the run
            return False
        return True

    def instructions(self, run_id: str) -> dict[str, str]:
        """Per-run rewrites of step instructions (the shared plan itself is never edited)."""
        rows = self.db.execute("SELECT step_id, instruction FROM step_states WHERE run_id=? "
                               "AND instruction IS NOT NULL", (run_id,)).fetchall()
        return {r["step_id"]: r["instruction"] for r in rows}

    def set_instruction(self, run_id: str, step_id: str, text: str, token: int) -> bool:
        cur = self.db.execute(
            "UPDATE step_states SET instruction=? WHERE run_id=? AND step_id=? AND status='pending' "
            "AND EXISTS (SELECT 1 FROM runs WHERE id=? AND status='running' AND seq=?)",
            (text, run_id, step_id, run_id, token))
        if cur.rowcount != 1:
            self.check_fence(run_id, token)
            return False
        return True

    def step_states(self, run_id: str) -> dict[str, tuple[str, str]]:
        rows = self.db.execute("SELECT * FROM step_states WHERE run_id=? ORDER BY step_id",
                               (run_id,)).fetchall()
        return {r["step_id"]: (r["status"], r["detail"]) for r in rows}

    def ledger_get(self, run_id: str, key: str):
        row = self.db.execute("SELECT result FROM write_ledger WHERE run_id=? AND idem_key=?",
                              (run_id, key)).fetchone()
        return json.loads(row["result"]) if row else None

    def record_write(self, run_id: str, key: str, result, token: int, *, step_id: str | None = None,
                     tool: str = "", args: dict | None = None) -> None:
        with self.tx():
            cur = self.db.execute("UPDATE runs SET writes=writes+1 WHERE id=? AND status='running' AND seq=?",
                                  (run_id, token))
            if cur.rowcount != 1:
                raise Fenced(run_id)
            self.db.execute("INSERT INTO write_ledger VALUES (?,?,?,?,?,?)",
                            (run_id, key, json.dumps(result), step_id, tool, json.dumps(args or {})))

    def writes_by_step(self, run_id: str) -> dict[str, list[dict]]:
        """What really executed for each step: the plan view's 'already executed' marker."""
        rows = self.db.execute("SELECT * FROM write_ledger WHERE run_id=? AND step_id IS NOT NULL "
                               "ORDER BY rowid", (run_id,)).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["step_id"], []).append(
                {"tool": r["tool"], "args": json.loads(r["args"]), "result": json.loads(r["result"])})
        return out

    def writes_used(self, run_id: str) -> int:
        return self.get_run(run_id)["writes"]

    # -- inbound events ------------------------------------------------------------------
    def claim_event(self, event_id: str) -> bool:
        try:
            self.db.execute("INSERT INTO processed_events VALUES (?)", (event_id,))
            return True
        except sqlite3.IntegrityError:
            return False

    # -- notes, scratchpad, log, approvals -----------------------------------------------
    def add_note(self, run_id: str, step_id: str, note: str, token: int) -> None:
        self.check_fence(run_id, token)
        self.db.execute("INSERT INTO step_notes (run_id, step_id, note) VALUES (?,?,?)",
                        (run_id, step_id, note))

    def notes(self, run_id: str) -> dict[str, list[str]]:
        rows = self.db.execute("SELECT step_id, note FROM step_notes WHERE run_id=? ORDER BY id",
                               (run_id,)).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["step_id"], []).append(r["note"])
        return out

    def scratch_set(self, run_id: str, key: str, value: str, token: int) -> None:
        self.check_fence(run_id, token)
        self.db.execute("INSERT OR REPLACE INTO scratchpad VALUES (?,?,?)", (run_id, key, value))

    def scratchpad(self, run_id: str) -> dict[str, str]:
        rows = self.db.execute("SELECT key, value FROM scratchpad WHERE run_id=? ORDER BY key",
                               (run_id,)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def log(self, run_id: str, kind: str, detail: str) -> None:
        self.db.execute("INSERT INTO run_log (run_id, kind, detail) VALUES (?,?,?)", (run_id, kind, detail))

    def log_entries(self, run_id: str, kind: str) -> list[str]:
        rows = self.db.execute("SELECT detail FROM run_log WHERE run_id=? AND kind=? ORDER BY id",
                               (run_id, kind)).fetchall()
        return [r["detail"] for r in rows]

    def hold_write(self, run_id: str, key: str, reason: str, call: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO approvals VALUES (?,?,?,?,?)",
                        (run_id, key, "held", reason, json.dumps(call)))

    def approval(self, run_id: str, key: str) -> str | None:
        row = self.db.execute("SELECT status FROM approvals WHERE run_id=? AND idem_key=?",
                              (run_id, key)).fetchone()
        return row["status"] if row else None

    def held_writes(self, run_id: str) -> list[dict]:
        rows = self.db.execute("SELECT reason, call FROM approvals WHERE run_id=? AND status='held'",
                               (run_id,)).fetchall()
        return [{"reason": r["reason"], "call": json.loads(r["call"])} for r in rows]

    def decide_held(self, run_id: str, approved: bool) -> int:
        cur = self.db.execute("UPDATE approvals SET status=? WHERE run_id=? AND status='held'",
                              ("approved" if approved else "rejected", run_id))
        return cur.rowcount
