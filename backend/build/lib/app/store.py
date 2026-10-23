from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain import SCENARIOS, redact

TERMINAL = {"closed", "rolled_back", "escalated", "rejected", "failed"}


def now() -> str:
    return datetime.now(UTC).isoformat()


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS incidents(
          id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
          scenario TEXT NOT NULL, title TEXT NOT NULL, service TEXT NOT NULL,
          severity TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, plan TEXT, error TEXT,
          provider TEXT NOT NULL, model TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL
          REFERENCES incidents(id), at TEXT NOT NULL, stage TEXT NOT NULL,
          actor TEXT NOT NULL, message TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback(
          incident_id TEXT PRIMARY KEY REFERENCES incidents(id),
          outcome TEXT NOT NULL, candidate TEXT NOT NULL, at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
          BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
          BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def close(self):
        self.db.close()

    def _event(self, incident_id: str, stage: str, actor: str, message: str, payload: dict):
        self.db.execute(
            "INSERT INTO events(incident_id,at,stage,actor,message,payload) VALUES(?,?,?,?,?,?)",
            (
                incident_id,
                now(),
                stage,
                actor,
                redact(message),
                json.dumps(sanitize(payload), ensure_ascii=True),
            ),
        )

    def event(self, incident_id: str, stage: str, message: str, payload: dict | None = None):
        with self.transaction():
            self._event(incident_id, stage, "agent", message, payload or {})

    def create(self, scenario: str, key: str, provider: str, model: str, capacity: int) -> tuple:
        with self.transaction():
            existing = self.db.execute(
                "SELECT * FROM incidents WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["scenario"] != scenario:
                    raise Conflict("Idempotency key already belongs to another scenario")
                return self.get(existing["id"]), False
            placeholders = ",".join("?" for _ in TERMINAL)
            count = self.db.execute(
                f"SELECT COUNT(*) FROM incidents WHERE status NOT IN ({placeholders})",
                tuple(TERMINAL),
            ).fetchone()[0]
            if count >= capacity:
                raise Conflict("Incident queue is full; retry after active incidents finish")
            fixture = SCENARIOS[scenario]
            incident_id = str(uuid.uuid4())
            stamp = now()
            self.db.execute(
                "INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    incident_id,
                    key,
                    scenario,
                    fixture["title"],
                    fixture["service"],
                    fixture["severity"],
                    "queued",
                    stamp,
                    stamp,
                    None,
                    None,
                    provider,
                    model,
                ),
            )
            self._event(incident_id, "incident", "operator", "Synthetic signal received.", {})
            return self.get(incident_id), True

    def get(self, incident_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if row is None:
                raise KeyError(incident_id)
            result = dict(row)
            result.pop("idempotency_key", None)
            if result["plan"]:
                result["plan"] = json.loads(result["plan"])
            return result

    def list(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self.get(row["id"]) for row in rows]

    def events(self, incident_id: str, after: int = 0) -> list[dict]:
        self.get(incident_id)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE incident_id=? AND seq>? ORDER BY seq LIMIT 500",
                (incident_id, after),
            ).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def transition(
        self,
        incident_id: str,
        expected: set[str],
        status: str,
        stage: str,
        message: str,
        *,
        actor: str = "agent",
        payload: dict | None = None,
        plan: dict | None = None,
        error: str | None = None,
    ):
        with self.transaction():
            current = self.get(incident_id)
            if current["status"] not in expected:
                raise Conflict(f"Cannot transition from {current['status']} to {status}")
            self.db.execute(
                "UPDATE incidents SET status=?,updated_at=?,plan=?,error=? WHERE id=?",
                (
                    status,
                    now(),
                    json.dumps(plan)
                    if plan
                    else json.dumps(current["plan"])
                    if current["plan"]
                    else None,
                    redact(error) if error else None,
                    incident_id,
                ),
            )
            self._event(incident_id, stage, actor, message, payload or {})

    def next_job(self) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM incidents WHERE status IN ('queued','approved') "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            return self.get(row["id"]) if row else None

    def recover(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,status FROM incidents "
                "WHERE status IN ('triaging','ready','executing','verifying')"
            ).fetchall()
        for row in rows:
            self.transition(
                row["id"],
                {row["status"]},
                "failed",
                "recovery",
                "Interrupted workflow requires operator review; no blind replay.",
                error="Process interrupted. Inspect audit trail before creating a new incident.",
            )

    def learn(self, incident_id: str, outcome: str):
        with self.transaction():
            self.db.execute(
                "INSERT INTO feedback VALUES(?,?,?,?)",
                (incident_id, outcome, "Review runbook candidate; never auto-promote.", now()),
            )
            self._event(
                incident_id,
                "learn",
                "agent",
                "Outcome recorded; runbook changes require offline review.",
                {"outcome": outcome},
            )

    def feedback(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT * FROM feedback ORDER BY at DESC LIMIT 100"
                ).fetchall()
            ]


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value
