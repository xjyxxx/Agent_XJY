"""会话生命周期：ChatSession 缓存与 API 会话都要能被 TTL/容量清理。

两条独立的清理线：
1. `ingest.session_store`：解析结果（人设卡 + 索引）很占内存，按 LRU + TTL 淘汰；
   但 chat_id 的"重建参数"要留着，否则 checkpoint 里的引用会悬空。
2. `api.SessionService`：会话本身（thread）按最后访问时间淘汰，并**同步删 checkpoint**。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.engine import create_runtime
from api.service import SessionNotFound, SessionService
from ingest import session_store

CHAT = """老王: 在吗
小林: 在
老王: 晚饭吃啥
小林: 随便
"""

CHAT_TWO = """老王: 周末去爬山吗
小林: 看情况
老王: 那就定周六
小林: 行吧
"""


def write_chat(directory: str, name: str, text: str = CHAT) -> Path:
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path


class ChatCacheLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()
        self._saved_env = dict(os.environ)

    def tearDown(self) -> None:
        session_store.clear()
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_ttl_evicts_but_can_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_chat(directory, "a.txt")
            clock = [1000.0]
            with mock.patch.object(session_store, "_now", lambda: clock[0]), \
                 mock.patch.dict(os.environ, {"SESSION_TTL": "10", "SESSION_MAX": "10"}):
                session = session_store.get_or_build(path, "老王", "小林")
                clock[0] += 11
                removed = session_store.evict_now()
                self.assertEqual(removed, [session.chat_id])
                self.assertEqual(session_store.stats()["cached"], 0)
                # checkpoint 里存的 chat_id 仍然可用：按原参数重建
                rebuilt = session_store.get_session(session.chat_id)
                self.assertIsNotNone(rebuilt)
                self.assertEqual(rebuilt.speakers, ["老王", "小林"])

    def test_capacity_keeps_most_recent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = write_chat(directory, "a.txt", CHAT)
            second = write_chat(directory, "b.txt", CHAT_TWO)
            clock = [1000.0]
            with mock.patch.object(session_store, "_now", lambda: clock[0]), \
                 mock.patch.dict(os.environ, {"SESSION_TTL": "0", "SESSION_MAX": "1"}):
                session_store.get_or_build(first, "老王", "小林")
                clock[0] += 1
                second_session = session_store.get_or_build(second, "老王", "小林")
                self.assertEqual(session_store.stats()["cached"], 1)
                kept = session_store.get_session(second_session.chat_id)
                self.assertEqual(kept.speakers, ["老王", "小林"])

    def test_missing_chat_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_chat(directory, "a.txt")
            session = session_store.get_or_build(path, "老王", "小林")
            path.unlink()
            # 缓存还在时仍可用（数据本身没坏）；淘汰后重建才会发现文件没了
            session_store.evict_now(now=session.last_used + 10_000)
            self.assertIsNone(session_store.get_session(session.chat_id))


class SessionServiceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()
        self._saved_env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = write_chat(self.tmp.name, "a.txt")

    def tearDown(self) -> None:
        session_store.clear()
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _service(self, **kwargs) -> SessionService:
        return SessionService(":memory:", engine="builtin", **kwargs)

    def test_sweep_deletes_expired_checkpoints(self) -> None:
        service = self._service(session_ttl=10, session_max=10)
        clock = [1000.0]
        service._clock = lambda: clock[0]
        try:
            first = service.create_session(str(self.chat), "老王", "小林")
            second = service.create_session(str(self.chat), "老王", "小林")
            clock[0] += 11
            removed = service.sweep()
            self.assertCountEqual(removed, [first.thread_id, second.thread_id])
            self.assertIsNone(service.runtime.load_state(first.thread_id))
            self.assertIsNone(service.runtime.load_state(second.thread_id))
            self.assertEqual(service.stats()["sessions"], 0)
        finally:
            service.close()

    def test_fresh_session_survives_sweep(self) -> None:
        service = self._service(session_ttl=10, session_max=10)
        clock = [1000.0]
        service._clock = lambda: clock[0]
        try:
            session = service.create_session(str(self.chat), "老王", "小林")
            clock[0] += 3
            self.assertEqual(service.sweep(), [])
            self.assertIsNotNone(service.runtime.load_state(session.thread_id))
        finally:
            service.close()

    def test_capacity_evicts_least_recently_used(self) -> None:
        service = self._service(session_ttl=0, session_max=2)
        clock = [1000.0]
        service._clock = lambda: clock[0]
        try:
            sessions = []
            for _ in range(3):
                sessions.append(service.create_session(str(self.chat), "老王", "小林"))
                clock[0] += 1
            removed = service.sweep()
            self.assertEqual(len(removed), 1)
            self.assertIn(removed[0], [s.thread_id for s in sessions])
            self.assertLessEqual(service.stats()["sessions"], 2)
        finally:
            service.close()

    def test_send_after_expiry_raises_not_found(self) -> None:
        service = self._service(session_ttl=5, session_max=10)
        clock = [1000.0]
        service._clock = lambda: clock[0]
        try:
            session = service.create_session(str(self.chat), "老王", "小林")
            clock[0] += 10
            with self.assertRaises(SessionNotFound):
                service.send(session.thread_id, "在吗")
        finally:
            service.close()

    def test_touch_extends_life(self) -> None:
        service = self._service(session_ttl=10, session_max=10)
        clock = [1000.0]
        service._clock = lambda: clock[0]
        try:
            session = service.create_session(str(self.chat), "老王", "小林")
            clock[0] += 6
            service.get(session.thread_id)  # 访问即续命
            clock[0] += 6  # 距创建 12s，但距上次访问 6s
            self.assertEqual(service.sweep(), [])
        finally:
            service.close()

    def test_delete_forgets_session(self) -> None:
        service = self._service()
        try:
            session = service.create_session(str(self.chat), "老王", "小林")
            service.delete(session.thread_id)
            self.assertEqual(service.stats()["sessions"], 0)
            self.assertIsNone(service.runtime.load_state(session.thread_id))
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
