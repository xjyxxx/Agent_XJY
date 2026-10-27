"""checkpoint 并发安全：WAL、写锁探测、被占用时的可读报错。

真实现场：两个实例都指 `runtime/api.sqlite3` → 后启动的那个每个请求都 500，
而错误信息只有 "database is locked"，从 HTTP 层完全看不出原因。
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.checkpoint import (
    CheckpointLocked,
    SQLiteCheckpointer,
    ensure_writable,
)


class EnsureWritableTests(unittest.TestCase):
    def test_fresh_file_is_writable_and_wal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "c.sqlite3"
            info = ensure_writable(db, busy_timeout_ms=200)
        self.assertEqual(info["journal_mode"], "wal")
        self.assertEqual(info["busy_timeout_ms"], 200)

    def test_memory_is_always_ok(self) -> None:
        info = ensure_writable(":memory:")
        self.assertEqual(info["journal_mode"], "memory")

    def test_detects_other_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "c.sqlite3"
            holder = SQLiteCheckpointer(db)
            holder.connection.execute("BEGIN IMMEDIATE")  # 占住写锁
            try:
                with self.assertRaises(CheckpointLocked) as caught:
                    ensure_writable(db, busy_timeout_ms=100)
            finally:
                holder.connection.execute("ROLLBACK")
                holder.close()
        message = str(caught.exception)
        self.assertIn("CHECKPOINT_PATH", message)
        self.assertIn("另一个进程", message)

    def test_recovers_after_lock_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "c.sqlite3"
            holder = SQLiteCheckpointer(db)
            holder.connection.execute("BEGIN IMMEDIATE")
            holder.connection.execute("ROLLBACK")
            holder.close()
            info = ensure_writable(db, busy_timeout_ms=100)
        self.assertEqual(info["journal_mode"], "wal")


class CheckpointerInfoTests(unittest.TestCase):
    def test_reports_wal_and_busy_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "c.sqlite3"
            checkpointer = SQLiteCheckpointer(db, busy_timeout_ms=1234)
            try:
                info = checkpointer.info()
                self.assertEqual(info["journal_mode"], "wal")
                self.assertEqual(info["busy_timeout_ms"], 1234)
                checkpointer.probe()  # 没被占用时不应抛
            finally:
                checkpointer.close()

    def test_memory_checkpointer_probe(self) -> None:
        checkpointer = SQLiteCheckpointer()
        try:
            self.assertEqual(checkpointer.probe()["journal_mode"], "memory")
        finally:
            checkpointer.close()

    def test_locked_write_raises_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "c.sqlite3"
            first = SQLiteCheckpointer(db)
            second = SQLiteCheckpointer(db, busy_timeout_ms=100)
            first.connection.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaises(CheckpointLocked) as caught:
                    second.save("t1", {"status": "ok"})
            finally:
                first.connection.execute("ROLLBACK")
            first.close()
            second.close()
        self.assertIn("写锁被占用", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
