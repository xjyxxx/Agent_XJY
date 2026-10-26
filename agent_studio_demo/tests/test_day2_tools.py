import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.react import ReActAgent
from tools.history_tools import default_tool_registry


FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"


class ToolAndReActTests(unittest.TestCase):
    def test_read_only_tool_registry_parses_and_searches(self) -> None:
        registry = default_tool_registry()
        parsed = json.loads(registry.invoke("parse_chat", json.dumps({"path": str(FIXTURE)})))
        self.assertEqual(parsed["message_count"], 20)
        self.assertEqual(parsed["speakers"], ["老王", "小林"])

        result = json.loads(
            registry.invoke(
                "search_history",
                json.dumps(
                    {
                        "path": str(FIXTURE),
                        "query": "周五晚上吃饭",
                        "user_speaker": "老王",
                        "target_speaker": "小林",
                    },
                    ensure_ascii=False,
                ),
            )
        )
        self.assertEqual(result["target_speaker"], "小林")
        self.assertTrue(result["hits"])

    def test_send_tool_is_not_allowed(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowed"):
            default_tool_registry().invoke("send_message", "{}")

    def test_react_loop_records_three_read_only_actions(self) -> None:
        result = ReActAgent().run(
            question="周五晚上吃饭吗？",
            chat_path=FIXTURE,
            user_speaker="老王",
            target_speaker="小林",
        )
        self.assertEqual(
            [step.action for step in result.steps],
            ["parse_chat", "search_history", "get_persona"],
        )
        self.assertIn("历史参考", result.answer)


if __name__ == "__main__":
    unittest.main()
