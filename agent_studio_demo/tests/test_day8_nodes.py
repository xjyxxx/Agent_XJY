"""Day8：把单图拆成 Planner / Worker / Critic 三个职责。

这一组测试钉死节点契约：
- 节点返回增量，不原地改 state。
- Planner 只在导入时做一次，之后的轮次直接短路。
- Worker 的草稿在重写时"替换"而不是"追加"本轮记忆。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.nodes import draft_node, planner_node, retrieve_node
from ingest import session_store
from memory.persona import build_persona_card
from tests.fakes import ScriptedModel
from tools.chat_parser import parse_chat_file


FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


def persona_dict() -> dict:
    return build_persona_card(parse_chat_file(FIXTURE), "小林").to_dict()


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def test_planner_builds_persona_and_session_once(self) -> None:
        patch = planner_node({"chat_path": str(FIXTURE)})
        self.assertTrue(patch["persona_ready"])
        self.assertTrue(patch["chat_id"])
        self.assertEqual(patch["speakers"], ["老王", "小林"])
        # 未指定角色时，第一个说话人作为用户，第二个作为扮演对象。
        self.assertEqual(patch["user_speaker"], "老王")
        self.assertEqual(patch["target_speaker"], "小林")
        self.assertEqual(patch["persona"]["speaker"], "小林")

        ready = {**patch, "chat_path": str(FIXTURE)}
        self.assertEqual(planner_node(ready), {})

    def test_planner_requires_chat_path_on_first_turn(self) -> None:
        with self.assertRaisesRegex(ValueError, "chat_path"):
            planner_node({"user_input": "你好"})

    def test_planner_rejects_same_speaker_for_both_roles(self) -> None:
        with self.assertRaisesRegex(ValueError, "different"):
            planner_node(
                {
                    "chat_path": str(FIXTURE),
                    "user_speaker": "小林",
                    "target_speaker": "小林",
                }
            )


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def test_retrieve_can_bootstrap_without_chat_id(self) -> None:
        patch = retrieve_node(
            {
                "chat_path": str(FIXTURE),
                "user_speaker": "老王",
                "target_speaker": "小林",
                "user_input": GOLDEN_QUERY,
            }
        )
        self.assertTrue(patch["persona_ready"])
        self.assertTrue(patch["chat_id"])
        self.assertEqual(patch["retrieved"][0]["line_number"], 15)

    def test_draft_replaces_turn_on_rewrite(self) -> None:
        model = ScriptedModel(["第一次的草稿", "改写后的草稿"])
        base = {
            "user_input": GOLDEN_QUERY,
            "persona": persona_dict(),
            "retrieved": [],
            "short_memory": [],
        }

        first = draft_node(base, model)
        self.assertEqual(first["turn_count"], 1)
        self.assertEqual(len(first["short_memory"]), 1)

        second = draft_node(
            {
                **base,
                **first,
                "critique": {"verdict": "rewrite", "problems": ["too_long"]},
            },
            model,
        )
        self.assertIn("too_long", model.feedbacks[-1])
        # 重写是替换：轮数不涨，窗口里也不能出现两条相同的用户输入。
        self.assertEqual(second["turn_count"], 1)
        self.assertEqual(len(second["short_memory"]), 1)
        self.assertEqual(second["short_memory"][0]["user_input"], GOLDEN_QUERY)
        self.assertEqual(second["short_memory"][0]["assistant_reply"], second["reply"])

    def test_draft_without_feedback_has_empty_feedback(self) -> None:
        model = ScriptedModel(["嗯"])
        draft_node(
            {
                "user_input": "在吗",
                "persona": persona_dict(),
                "retrieved": [],
                "short_memory": [],
            },
            model,
        )
        self.assertEqual(model.feedbacks, [""])


if __name__ == "__main__":
    unittest.main()
