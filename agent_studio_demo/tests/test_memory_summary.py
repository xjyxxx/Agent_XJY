"""验收第 6 条：多轮之后不丢"正在聊的主题"。

窗口是 6 轮；第 7 轮起最旧的一轮被挤出去，压成摘要继续参与生成。
两个引擎都要满足（自研 / 官方 LangGraph）。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.engine import create_runtime, langgraph_available
from ingest import session_store
from memory.persona import ShortTermMemory, build_persona_card
from agent.nodes import draft_node
from tests.fakes import ScriptedModel
from tools.chat_parser import parse_chat_file

FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


class SummaryUnitTests(unittest.TestCase):
    def test_overflow_is_folded_into_summary(self) -> None:
        memory = ShortTermMemory(max_turns=2)
        memory.add_turn("第一轮问题", "第一轮回答")
        memory.add_turn("第二轮问题", "第二轮回答")
        self.assertEqual(memory.summary, "")

        memory.add_turn("第三轮问题", "第三轮回答")
        self.assertEqual(len(memory.turns), 2)
        self.assertIn("第一轮问题", memory.summary)
        self.assertIn("第一轮回答", memory.summary)

    def test_summary_is_capped_and_keeps_the_tail(self) -> None:
        memory = ShortTermMemory(max_turns=1)
        for index in range(30):
            memory.add_turn(f"问题{index}" + "很长的内容" * 5, f"回答{index}")
        self.assertLessEqual(len(memory.summary), ShortTermMemory.SUMMARY_LIMIT)
        # 最后一轮还在窗口里，所以被折进摘要的是倒数第二轮
        self.assertIn("问题28", memory.summary)
        self.assertNotIn("问题0很长的内容", memory.summary)  # 头部旧信息被裁掉

    def test_summary_round_trips_through_draft_node(self) -> None:
        chat = parse_chat_file(FIXTURE)
        persona = build_persona_card(chat, "小林").to_dict()
        model = ScriptedModel(["嗯"])
        state = {
            "user_input": "还在聊晚饭",
            "persona": persona,
            "retrieved": [],
            "short_memory": [
                {"user_input": f"第{i}轮", "assistant_reply": "嗯"} for i in range(6)
            ],
            "memory_summary": "早前聊到周六去面馆",
        }
        update = draft_node(state, model)
        self.assertIn("早前聊到周六去面馆", update["memory_summary"])
        self.assertEqual(len(update["short_memory"]), 6)


class MultiTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def _run(self, engine: str) -> object:
        runtime = create_runtime(":memory:", engine=engine)
        self.addCleanup(runtime.close)
        state = None
        for index in range(8):
            message = GOLDEN_QUERY if index == 0 else f"第{index}轮：那周六呢？"
            state = runtime.invoke(
                thread_id="mem",
                chat_path=FIXTURE,
                user_speaker="老王",
                target_speaker="小林",
                user_input=message,
            )
        return state

    def test_topic_survives_beyond_the_window(self) -> None:
        state = self._run("builtin")
        self.assertEqual(state.turn_count, 8)
        self.assertEqual(len(state.short_memory), 6)  # 窗口有界
        self.assertTrue(state.memory_summary.strip())  # 但早期的没丢
        self.assertIn("王哥今晚加班", state.memory_summary)

    @unittest.skipUnless(langgraph_available(), "未安装 langgraph")
    def test_langgraph_engine_also_summarises(self) -> None:
        state = self._run("langgraph")
        self.assertEqual(state.turn_count, 8)
        self.assertTrue(state.memory_summary.strip())
        self.assertIn("王哥今晚加班", state.memory_summary)


if __name__ == "__main__":
    unittest.main()
