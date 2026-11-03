from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import DATA


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA / "relay.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS robots(
                  id INTEGER PRIMARY KEY, name TEXT NOT NULL, webhook TEXT NOT NULL,
                  secret TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                  created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(
                  id INTEGER PRIMARY KEY, name TEXT NOT NULL, chat_id INTEGER NOT NULL,
                  chat_title TEXT NOT NULL, topic_id INTEGER NOT NULL DEFAULT 0,
                  topic_title TEXT NOT NULL DEFAULT '', robot_id INTEGER NOT NULL REFERENCES robots(id),
                  enabled INTEGER NOT NULL DEFAULT 1, edits INTEGER NOT NULL DEFAULT 1,
                  include_words TEXT NOT NULL DEFAULT '', exclude_words TEXT NOT NULL DEFAULT '',
                  stale_minutes INTEGER NOT NULL DEFAULT 5, catch_up INTEGER NOT NULL DEFAULT 1,
                  enabled_since REAL NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
                  last_scan REAL NOT NULL, created_at REAL NOT NULL,
                  UNIQUE(chat_id, topic_id, robot_id));
                CREATE TABLE IF NOT EXISTS seen(
                  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                  message_id INTEGER NOT NULL, version TEXT NOT NULL,
                  PRIMARY KEY(task_id, message_id, version));
                CREATE TABLE IF NOT EXISTS deliveries(
                  id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                  robot_id INTEGER NOT NULL REFERENCES robots(id), source TEXT NOT NULL,
                  message_id INTEGER NOT NULL DEFAULT 0, sender_name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
                  body TEXT NOT NULL, link TEXT NOT NULL DEFAULT '',
                  original_at REAL NOT NULL, event_at REAL NOT NULL, received_at REAL NOT NULL,
                  part INTEGER NOT NULL DEFAULT 1, parts INTEGER NOT NULL DEFAULT 1,
                  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt REAL NOT NULL DEFAULT 0, sent_at REAL,
                  error TEXT NOT NULL DEFAULT '', latency_ms INTEGER,
                  stale_minutes INTEGER NOT NULL DEFAULT 5);
                CREATE INDEX IF NOT EXISTS deliveries_queue ON deliveries(status, next_attempt, id);
                CREATE TABLE IF NOT EXISTS system_logs(
                  id INTEGER PRIMARY KEY, level TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            if "sender_name" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN sender_name TEXT NOT NULL DEFAULT ''")

    def mark_inflight_uncertain(self):
        self.execute("UPDATE deliveries SET status='uncertain',error='程序关闭时请求结果未知。请先在飞书群核对，避免重复通知。' WHERE status='sending'")

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def all(self, sql, params=()):
        with self.db() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.db() as db:
            cursor = db.execute(sql, params)
            return cursor.lastrowid

    def log(self, message, level="info"):
        self.execute("INSERT INTO system_logs(level,message,created_at) VALUES(?,?,?)", (level, message, time.time()))

    def enqueue(self, task, message_id, version, chunks, *, kind, source, sender_name="", link, original_at, event_at, received_at):
        with self.db() as db:
            inserted = db.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?)", (task["id"], message_id, version))
            if not inserted.rowcount:
                return 0
            for index, body in enumerate(chunks, 1):
                db.execute("""INSERT INTO deliveries(task_id,robot_id,source,message_id,sender_name,kind,body,link,
                  original_at,event_at,received_at,part,parts,stale_minutes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (task["id"], task["robot_id"], source, message_id, sender_name, kind, body, link, original_at, event_at,
                   received_at, index, len(chunks), task["stale_minutes"]))
            return len(chunks)

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT d.*,r.webhook,r.secret,r.name AS robot_name FROM deliveries d
                JOIN robots r ON r.id=d.robot_id LEFT JOIN tasks t ON t.id=d.task_id
                WHERE d.status IN ('pending','retry') AND d.next_attempt<=? AND r.enabled=1
                  AND (d.task_id IS NULL OR t.enabled=1)
                  AND NOT EXISTS(SELECT 1 FROM deliveries prev WHERE prev.id<d.id
                    AND prev.robot_id=d.robot_id AND prev.task_id IS d.task_id
                    AND prev.message_id=d.message_id AND prev.status IN ('pending','retry','sending','uncertain','failed'))
                ORDER BY d.id LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE deliveries SET status='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            result = dict(row)
            result["attempts"] += 1
            return result

    def stats(self):
        now = time.time()
        with self.db() as db:
            counts = dict(db.execute("SELECT status, COUNT(*) FROM deliveries GROUP BY status").fetchall())
            today = db.execute("SELECT COUNT(*),AVG(latency_ms) FROM deliveries WHERE status='sent' AND sent_at>?", (now - 86400,)).fetchone()
            return {"sent_24h": today[0], "avg_latency_ms": round(today[1]) if today[1] is not None else None,
                    "pending": sum(counts.get(k, 0) for k in ("pending", "sending", "retry")),
                    "uncertain": counts.get("uncertain", 0),
                    "failed": counts.get("failed", 0), "active_tasks": db.execute("SELECT COUNT(*) FROM tasks WHERE enabled=1").fetchone()[0]}

    def export(self):
        # An intentionally credential-free diagnostic, not a configuration backup.
        return json.dumps({"stats": self.stats(), "logs": self.all("SELECT * FROM system_logs ORDER BY id DESC LIMIT 100")}, ensure_ascii=False)
