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


class CheckpointLocked(RuntimeError):
    """另一个进程正持有这个 SQLite 文件的写锁。

    典型场景：同一台机器起了两个实例，都指向 `runtime/api.sqlite3`。
    SQLite 同一时刻只允许一个写者，后到的会拿到 "database is locked"，
    在 HTTP 层就变成莫名其妙的 500。
    """


LOCK_HINT = (
    "给每个实例单独的数据库即可：\n"
    "    $env:CHECKPOINT_PATH = 'runtime/api-<端口>.sqlite3'\n"
    "或者先停掉占用它的旧进程（Windows: Get-NetTCPConnection -LocalPort <端口> 找 PID）。"
)


def ensure_writable(path: str | Path, busy_timeout_ms: int = 1500) -> dict:
    """启动前的写锁探测：拒绝在"别人正写着"的库上启动，并给出可操作的提示。

    只做一次 `BEGIN IMMEDIATE; ROLLBACK`，不写任何数据。WAL 模式让读写可以并发，
    所以"能拿到写锁"就是"这个库现在归我用"。
    """

    if str(path) == ":memory:":
        return {"path": ":memory:", "journal_mode": "memory", "busy_timeout_ms": 0}

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000.0)
    except sqlite3.OperationalError as exc:  # pragma: no cover - 极少见
        raise CheckpointLocked(f"打不开 checkpoint {db_path}：{exc}") from exc

    try:
        mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            raise CheckpointLocked(
                f"checkpoint 被另一个进程占用：{db_path}\n" + LOCK_HINT
            ) from exc
        raise
    finally:
        connection.close()

    return {
        "path": str(db_path),
        "journal_mode": mode,
        "busy_timeout_ms": busy_timeout_ms,
    }


class SQLiteCheckpointer:
    """Persist one JSON state document per thread_id."""

    def __init__(self, path: str | Path = ":memory:", busy_timeout_ms: int = 5000):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.busy_timeout_ms = busy_timeout_ms
        self.connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=busy_timeout_ms / 1000.0
        )
        if self.path != ":memory:":
            # WAL：读写可并发，多个实例各自读不会互相阻塞；写仍串行。
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self.connection.execute("PRAGMA synchronous=NORMAL")
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

    def _execute(self, sql: str, params: tuple = ()):
        """把 "database is locked" 翻译成能照着做的错误，而不是裸 500。"""

        try:
            return self.connection.execute(sql, params)
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                raise CheckpointLocked(
                    f"checkpoint 写锁被占用（{self.path}）：{exc}\n" + LOCK_HINT
                ) from exc
            raise

    def info(self) -> dict:
        mode = (
            self.connection.execute("PRAGMA journal_mode").fetchone()[0]
            if self.path != ":memory:"
            else "memory"
        )
        return {
            "path": self.path,
            "journal_mode": str(mode),
            "busy_timeout_ms": self.busy_timeout_ms,
        }

    def save(self, thread_id: str, state: dict) -> None:
        if not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._execute(
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
            row = self._execute(
                "SELECT state_json FROM checkpoints WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_threads(self) -> list[dict]:
        """Return (thread_id, updated_at) pairs, newest first."""

        with self._lock:
            rows = self._execute(
                "SELECT thread_id, updated_at FROM checkpoints ORDER BY updated_at DESC"
            ).fetchall()
        return [{"thread_id": row[0], "updated_at": row[1]} for row in rows]

    def delete(self, thread_id: str) -> None:
        with self._lock:
            self._execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
            self.connection.commit()

    def probe(self) -> dict:
        """现在能不能拿到写锁（运行期自检用）。"""

        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute("ROLLBACK")
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    raise CheckpointLocked(
                        f"checkpoint 被另一个进程占用：{self.path}\n" + LOCK_HINT
                    ) from exc
                raise
        return self.info()

    def close(self) -> None:
        with self._lock:
            self.connection.close()
