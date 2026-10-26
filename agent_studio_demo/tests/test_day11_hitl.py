"""Day11：HITL —— 中断、落盘、人工裁决、从恢复点继续。

钉住四件事：
1. 敏感内容直接中断（不自动重写），因为"要不要答应借钱"不是语气问题。
2. 中断状态必须落盘，进程重启后仍能 resume。
3. resume 从 `pending.resume_at` 继续，不重跑整轮。
4. 人工改写后，短期记忆必须跟着改（否则下一轮会看到没改过的版本）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import PersonaReplayRuntime
from agent.nodes import DEGRADED_REPLY, sensitive_categories
from ingest import session_store
from tests.fakes import ScriptedModel


FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


class SensitiveDetectionTests(unittest.TestCase):
    def test_categories_are_detected(self) -> None:
        self.assertEqual(sensitive_categories("我银行卡密码是123456"), ["credential"])
        self.assertIn("money", sensitive_categories("那我先借你两千块"))
        self.assertIn("commitment", sensitive_categories("我保证周六一定去"))
        self.assertEqual(sensitive_categories("哈哈知道了"), [])


class HumanInTheLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def _runtime(self, model, path: str = ":memory:") -> PersonaReplayRuntime:
        return PersonaReplayRuntime(path, model=model)

    def _interrupted_turn(self, model, directory: str = ""):
        path = str(Path(directory) / "state.db") if directory else ":memory:"
        runtime = self._runtime(model, path)
        state = runtime.invoke(
            thread_id="hitl",
            chat_path=str(FIXTURE),
            user_speaker="老王",
            target_speaker="小林",
            user_input=GOLDEN_QUERY,
        )
        return runtime, state

    def test_sensitive_reply_interrupts_without_rewriting(self) -> None:
        model = ScriptedModel(["我银行卡密码是123456"])
        runtime, state = self._interrupted_turn(model)
        try:
            self.assertEqual(state.status, "interrupted")
            # 敏感内容不做自动重写：只调了一次模型。
            self.assertEqual(len(model.calls), 1)
            pending = state.pending or {}
            self.assertEqual(pending.get("reason"), "sensitive")
            self.assertEqual(pending.get("categories"), ["credential"])
            self.assertIn("sensitive", state.critique["problems"])
            self.assertEqual(state.critique["verdict"], "review")
            self.assertIn("approve", pending.get("allowed_actions", []))
        finally:
            runtime.close()

    def test_interrupt_is_persisted_for_a_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, state = self._interrupted_turn(
                ScriptedModel(["我保证周六一定去"]), directory
            )
            self.assertEqual(state.status, "interrupted")
            runtime.close()

            # 模拟另一个进程：新的 runtime 连同一个 SQLite 文件。
            reopened = PersonaReplayRuntime(str(Path(directory) / "state.db"))
            try:
                loaded = reopened.load_state("hitl")
                self.assertEqual(loaded.status, "interrupted")
                self.assertEqual(loaded.pending["reason"], "sensitive")
                resumed = reopened.resume("hitl", {"action": "reject"})
                self.assertEqual(resumed.status, "rejected")
            finally:
                reopened.close()

    def test_approve_keeps_the_reply(self) -> None:
        model = ScriptedModel(["我保证周六一定去"])
        runtime, state = self._interrupted_turn(model)
        try:
            draft = state.reply
            resumed = runtime.resume("hitl", {"action": "approve"})
            self.assertEqual(resumed.status, "ok")
            self.assertEqual(resumed.reply, draft)
            self.assertEqual(resumed.human_decision, "approve")
            self.assertEqual(resumed.critique.get("human"), "approve")
            self.assertIsNone(resumed.pending)
            # resume 从 human_review 的后继（END）继续，不重跑整轮。
            self.assertEqual(resumed.steps, state.steps)
        finally:
            runtime.close()

    def test_edit_replaces_reply_and_syncs_memory(self) -> None:
        model = ScriptedModel(["我保证周六一定去"])
        runtime, _ = self._interrupted_turn(model)
        try:
            resumed = runtime.resume(
                "hitl", {"action": "edit", "reply": "哈哈看情况吧"}
            )
            self.assertEqual(resumed.status, "ok")
            self.assertEqual(resumed.reply, "哈哈看情况吧")
            self.assertEqual(resumed.human_decision, "edit")
            self.assertEqual(resumed.short_memory[-1]["assistant_reply"], "哈哈看情况吧")
        finally:
            runtime.close()

    def test_reject_falls_back_to_a_neutral_reply(self) -> None:
        model = ScriptedModel(["我保证周六一定去"])
        runtime, _ = self._interrupted_turn(model)
        try:
            resumed = runtime.resume("hitl", {"action": "reject"})
            self.assertEqual(resumed.status, "rejected")
            self.assertEqual(resumed.reply, DEGRADED_REPLY)
            self.assertEqual(resumed.short_memory[-1]["assistant_reply"], DEGRADED_REPLY)
        finally:
            runtime.close()

    def test_resume_requires_an_interrupted_thread(self) -> None:
        runtime = PersonaReplayRuntime(":memory:")
        try:
            with self.assertRaisesRegex(ValueError, "unknown thread_id"):
                runtime.resume("missing", {"action": "approve"})

            runtime.invoke(
                thread_id="ok",
                chat_path=str(FIXTURE),
                user_speaker="老王",
                target_speaker="小林",
                user_input=GOLDEN_QUERY,
            )
            with self.assertRaisesRegex(ValueError, "not interrupted"):
                runtime.resume("ok", {"action": "approve"})
        finally:
            runtime.close()

    def test_invalid_decision_is_rejected(self) -> None:
        model = ScriptedModel(["我保证周六一定去"])
        runtime, _ = self._interrupted_turn(model)
        try:
            with self.assertRaisesRegex(ValueError, "action must be"):
                runtime.resume("hitl", {"action": "maybe"})
            with self.assertRaisesRegex(ValueError, "non-empty reply"):
                runtime.resume("hitl", {"action": "edit", "reply": "  "})
        finally:
            runtime.close()

    def test_prepare_then_turn_without_chat_path(self) -> None:
        """Day14 的会话创建路径：先 prepare，再只用 session_id 发消息。"""

        runtime = PersonaReplayRuntime(":memory:")
        try:
            prepared = runtime.prepare(
                thread_id="api-session",
                chat_path=str(FIXTURE),
                user_speaker="老王",
                target_speaker="小林",
            )
            self.assertTrue(prepared.chat_id)
            self.assertEqual(prepared.persona["speaker"], "小林")

            turn = runtime.invoke(thread_id="api-session", user_input=GOLDEN_QUERY)
            self.assertEqual(turn.status, "ok")
            self.assertEqual(turn.reply, "哈哈知道了")
            self.assertEqual(turn.turn_count, 1)
            # prepare 之后 planner 不再需要 chat_path。
            self.assertEqual(turn.steps[-3:], ["retrieve", "draft", "critic"])
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
