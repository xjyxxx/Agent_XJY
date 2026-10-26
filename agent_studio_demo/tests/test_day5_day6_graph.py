import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import PersonaReplayRuntime


FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"


class GraphAndCheckpointTests(unittest.TestCase):
    def test_retrieve_draft_graph_and_thread_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoints.sqlite3"
            runtime = PersonaReplayRuntime(str(checkpoint))
            try:
                first = runtime.invoke(
                    thread_id="thread-1",
                    chat_path=str(FIXTURE),
                    user_speaker="老王",
                    target_speaker="小林",
                    user_input="周五晚上吃饭吗？",
                )
                # Day8 之后主图是 planner → retrieve → draft → critic
                self.assertEqual(
                    first.steps, ["planner", "retrieve", "draft", "critic"]
                )
                self.assertEqual(first.speakers, ["老王", "小林"])
                self.assertTrue(first.retrieved)
                self.assertTrue(first.reply)
                self.assertEqual(first.turn_count, 1)
                self.assertEqual(first.status, "ok")

                second = runtime.invoke(
                    thread_id="thread-1",
                    user_input="那周六呢？",
                )
                self.assertEqual(second.thread_id, "thread-1")
                self.assertEqual(second.turn_count, 2)
                self.assertEqual(len(second.short_memory), 2)
                self.assertEqual(second.steps[-3:], ["retrieve", "draft", "critic"])
                self.assertEqual(runtime.load_state("thread-1").turn_count, 2)
            finally:
                runtime.close()

    def test_thread_id_is_required_by_compiled_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = PersonaReplayRuntime(str(Path(directory) / "state.db"))
            try:
                with self.assertRaisesRegex(ValueError, "thread_id"):
                    runtime.graph.invoke({"user_input": "测试"})
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
