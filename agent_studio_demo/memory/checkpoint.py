"""SQLite checkpoint storage for graph state.

Day14：API 会在线程池里并发处理请求，而 `sqlite3.Connection` 默认不允许跨线程
使用。这里显式 `check_same_thread=False` 并用一把锁串行化写入——checkpoint 的
写入量很小（每 thread 一条记录），锁不会是瓶颈；同一 thread 的并发写必须串行，
否则后写的会把先写的冲掉。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class SQLiteCheckpointer:
    """Persist one JSON state document per thread_id."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def save(self, thread_id: str, state: dict) -> None:
        if not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO checkpoints(thread_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (thread_id, payload, now),
            )
            self.connection.commit()

    def load(self, thread_id: str) -> Optional[dict]:
        with self._lock:
            row = self.connection.execute(
                "SELECT state_json FROM checkpoints WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_threads(self) -> list[dict]:
        """Return (thread_id, updated_at) pairs, newest first."""

        with self._lock:
            rows = self.connection.execute(
                "SELECT thread_id, updated_at FROM checkpoints ORDER BY updated_at DESC"
            ).fetchall()
        return [{"thread_id": row[0], "updated_at": row[1]} for row in rows]

    def delete(self, thread_id: str) -> None:
        with self._lock:
            self.connection.execute(
                "DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)
            )
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()
