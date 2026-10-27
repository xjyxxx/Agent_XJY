"""官方 LangGraph 引擎：与自研引擎的行为一致性。

方案文档要求「编排只用 LangGraph」。这一组测试用来证明**两个引擎跑同一份业务图，
行为一致**——否则"换成 LangGraph"就只是换了个名字。

没有安装 langgraph 时整组跳过（核心链路仍然可用）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.engine import create_runtime, langgraph_available
from api.service import SessionService
from ingest import session_store
from llm_client.scripted import ScriptedModel

FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"
SKIP = "未安装 langgraph（可选依赖）"


def make_runtime(engine: str, checkpoint: str = ":memory:", model=None):
    return create_runtime(checkpoint, model=model, engine=engine)


@unittest.skipUnless(langgraph_available(), SKIP)
class EngineSelectionTests(unittest.TestCase):
    def test_explicit_engines_resolve_to_different_classes(self) -> None:
        builtin = make_runtime("builtin")
        graph = make_runtime("langgraph")
        try:
            self.assertEqual(type(builtin).__name__, "PersonaReplayRuntime")
            self.assertEqual(type(graph).__name__, "LangGraphRuntime")
        finally:
            builtin.close()
            graph.close()

    def test_auto_prefers_langgraph_when_available(self) -> None:
        runtime = make_runtime("auto")
        try:
            self.assertEqual(type(runtime).__name__, "LangGraphRuntime")
        finally:
            runtime.close()

    def test_unknown_engine_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown GRAPH_ENGINE"):
            create_runtime(engine="tensorflow")


@unittest.skipUnless(langgraph_available(), SKIP)
class ParityTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def _run_turn(self, runtime, thread_id: str, message: str):
        return runtime.invoke(
            thread_id=thread_id,
            chat_path=FIXTURE,
            user_speaker="老王",
            target_speaker="小林",
            user_input=message,
        )

    def test_same_reply_steps_and_critique(self) -> None:
        messages = [GOLDEN_QUERY, "那周六呢？", "谢谢王哥"]
        outputs = {}
        for engine in ("builtin", "langgraph"):
            runtime = make_runtime(engine)
            try:
                results = [
                    self._run_turn(runtime, "parity", message) for message in messages
                ]
            finally:
                runtime.close()
            outputs[engine] = [(r.reply, r.steps, (r.critique or {}).get("verdict")) for r in results]

        self.assertEqual(outputs["builtin"], outputs["langgraph"])

    def test_langgraph_persists_turns_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "lg.sqlite3")
            runtime = make_runtime("langgraph", path)
            try:
                first = self._run_turn(runtime, "lg-1", GOLDEN_QUERY)
                self.assertEqual(first.turn_count, 1)
            finally:
                runtime.close()

            reopened = make_runtime("langgraph", path)
            try:
                loaded = reopened.load_state("lg-1")
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.turn_count, 1)
                second = reopened.invoke(thread_id="lg-1", user_input="那周六呢？")
                self.assertEqual(second.turn_count, 2)
                self.assertEqual(len(second.short_memory), 2)
            finally:
                reopened.close()


@unittest.skipUnless(langgraph_available(), SKIP)
class HitlParityTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def test_sensitive_reply_interrupts_and_edit_resumes(self) -> None:
        model = ScriptedModel(["我银行卡密码是123456"])
        runtime = make_runtime("langgraph", model=model)
        try:
            state = runtime.invoke(
                thread_id="hitl",
                chat_path=FIXTURE,
                user_speaker="老王",
                target_speaker="小林",
                user_input=GOLDEN_QUERY,
            )
            self.assertEqual(state.status, "interrupted")
            self.assertEqual((state.pending or {}).get("reason"), "sensitive")
            self.assertEqual((state.pending or {}).get("categories"), ["credential"])
            self.assertEqual(len(model.calls), 1)  # 敏感内容不重写

            resumed = runtime.resume(
                "hitl", {"action": "edit", "reply": "哈哈这个不说"}
            )
            self.assertEqual(resumed.status, "ok")
            self.assertEqual(resumed.reply, "哈哈这个不说")
            self.assertEqual(resumed.human_decision, "edit")
            self.assertEqual(
                resumed.short_memory[-1]["assistant_reply"], "哈哈这个不说"
            )
        finally:
            runtime.close()

    def test_rewrite_budget_then_approve(self) -> None:
        model = ScriptedModel(["作为AI，我建议你早点休息"] * 3)
        runtime = make_runtime("langgraph", model=model)
        try:
            state = runtime.invoke(
                thread_id="hitl2",
                chat_path=FIXTURE,
                user_speaker="老王",
                target_speaker="小林",
                user_input=GOLDEN_QUERY,
            )
            self.assertEqual(state.status, "interrupted")
            self.assertEqual(state.rewrite_count, 2)
            self.assertEqual((state.pending or {}).get("reason"), "rewrite_budget")
            self.assertEqual(len(model.calls), 3)

            approved = runtime.resume("hitl2", {"action": "approve"})
            self.assertEqual(approved.status, "ok")
            self.assertEqual(approved.human_decision, "approve")
        finally:
            runtime.close()

    def test_service_can_run_on_langgraph(self) -> None:
        service = SessionService(":memory:", engine="langgraph")
        try:
            self.assertEqual(type(service.runtime).__name__, "LangGraphRuntime")
            created = service.create_session(FIXTURE, "老王", "小林")
            turn = service.send(created.thread_id, GOLDEN_QUERY)
            self.assertEqual(turn.reply, "哈哈知道了")
            self.assertEqual(turn.status, "ok")
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
