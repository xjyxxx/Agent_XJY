import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.persona import PersonaStore, ShortTermMemory, build_persona_card
from tools.chat_parser import parse_chat_file


FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"


class MemoryTests(unittest.TestCase):
    def test_persona_card_is_explainable_and_storeable(self) -> None:
        card = build_persona_card(parse_chat_file(FIXTURE), "小林")
        self.assertEqual(card.speaker, "小林")
        self.assertEqual(card.message_count, 10)
        self.assertGreater(card.average_message_length, 0)
        self.assertTrue(card.example_replies)

        store = PersonaStore()
        store.put(card)
        self.assertEqual(store.get("小林").to_dict(), card.to_dict())

    def test_short_memory_is_bounded(self) -> None:
        memory = ShortTermMemory(max_turns=2)
        memory.add_turn("一", "甲")
        memory.add_turn("二", "乙")
        memory.add_turn("三", "丙")
        self.assertEqual([turn.user_input for turn in memory.turns], ["二", "三"])
        self.assertIn("用户：二", memory.format_context())


if __name__ == "__main__":
    unittest.main()
