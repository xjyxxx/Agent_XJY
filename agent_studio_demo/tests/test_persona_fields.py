"""Day13：人设卡的语义字段（称呼 / 关系 / 口头禅 / 禁用词）。

原来的字段只有统计量（句长、单字频次、标点），这次补的是"读不出但必须知道"的：
- 他怎么称呼对方（"王哥"）
- 两人什么关系
- 词级口头禅（"哈哈""随便"，不是单字"哈"）
- 他绝不会说的话（助手腔 + 角色特有禁忌）
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.nodes import critic_node
from ingest import session_store
from ingest.session_store import get_or_build
from llm_client.deepseek import DeepSeekReplyModel
from memory.persona import (
    DEFAULT_FORBIDDEN,
    PersonaCard,
    build_persona_card,
    build_persona_card_llm,
)
from tests.fake_deepseek import FakeDeepSeek
from tools.chat_parser import parse_chat_file

FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
CHAT = parse_chat_file(FIXTURE)


class HeuristicFieldTests(unittest.TestCase):
    def test_addressing_is_detected_from_position(self) -> None:
        card = build_persona_card(CHAT, "小林", "老王")
        self.assertIn("王哥", card.addressing)

    def test_addressing_needs_the_user_name(self) -> None:
        card = build_persona_card(CHAT, "小林")
        self.assertEqual(card.addressing, [])

    def test_catchphrases_are_words_not_single_chars(self) -> None:
        card = build_persona_card(CHAT, "小林", "老王")
        self.assertTrue(card.catchphrases)
        for phrase in card.catchphrases:
            self.assertGreaterEqual(len(phrase), 2, phrase)
            self.assertNotIn(phrase, card.addressing)

    def test_forbidden_defaults_to_assistant_tone(self) -> None:
        card = build_persona_card(CHAT, "小林", "老王")
        self.assertEqual(list(card.forbidden), list(DEFAULT_FORBIDDEN))
        self.assertEqual(card.persona_source, "heuristic")

    def test_round_trip_keeps_new_fields(self) -> None:
        card = build_persona_card(CHAT, "小林", "老王")
        restored = PersonaCard.from_dict(card.to_dict())
        self.assertEqual(restored.addressing, card.addressing)
        self.assertEqual(restored.catchphrases, card.catchphrases)
        self.assertEqual(restored.relationship, card.relationship)


class LlmFieldTests(unittest.TestCase):
    def test_llm_merges_semantic_fields(self) -> None:
        payload = '{"addressing": ["王哥"], "relationship": "朋友", ' \
                  '"catchphrases": ["随便", "哈哈"], "forbidden": ["哎呀"]}'
        card = build_persona_card_llm(
            CHAT, "小林", "老王", call=lambda system, user: payload
        )
        self.assertEqual(card.addressing, ["王哥"])
        self.assertEqual(card.relationship, "朋友")
        self.assertIn("随便", card.catchphrases)
        self.assertIn("哎呀", card.forbidden)
        self.assertIn("作为ai", card.forbidden)  # 默认黑名单仍然保留
        self.assertEqual(card.persona_source, "heuristic+llm")

    def test_llm_failure_falls_back_to_heuristic(self) -> None:
        def broken(system, user):
            raise RuntimeError("model down")

        card = build_persona_card_llm(CHAT, "小林", "老王", call=broken)
        self.assertEqual(card.persona_source, "heuristic")

    def test_session_uses_llm_when_enabled(self) -> None:
        payload = '{"addressing": ["王哥"], "relationship": "朋友", ' \
                  '"catchphrases": ["随便"], "forbidden": []}'
        session_store.clear()
        with FakeDeepSeek(reply=payload) as fake:
            with mock.patch.dict(
                os.environ,
                {
                    "DEEPSEEK_API_KEY": "sk-test",
                    "DEEPSEEK_BASE_URL": fake.base_url,
                    "PERSONA_LLM": "1",
                },
                clear=False,
            ):
                session = get_or_build(FIXTURE, "老王", "小林")
        self.assertEqual(session.persona.persona_source, "heuristic+llm")
        self.assertEqual(session.persona.relationship, "朋友")
        self.assertEqual(fake.requests and 1, 1)

    def test_session_defaults_to_offline_extraction(self) -> None:
        session_store.clear()
        with mock.patch.dict(os.environ, {"PERSONA_LLM": "0"}, clear=False):
            session = get_or_build(FIXTURE, "老王", "小林")
        self.assertEqual(session.persona.persona_source, "heuristic")


class PromptAndCriticTests(unittest.TestCase):
    def test_system_prompt_includes_new_fields(self) -> None:
        card = PersonaCard(
            speaker="小林",
            message_count=10,
            average_message_length=4.2,
            common_terms=["哈"],
            punctuation_style=[","],
            addressing=["王哥"],
            relationship="朋友",
            catchphrases=["随便", "哈哈"],
            forbidden=["哎呀"],
        )
        prompt = DeepSeekReplyModel(
            api_key="sk-test", base_url="http://127.0.0.1:1"
        )._build_system_prompt(card)
        self.assertIn("王哥", prompt)
        self.assertIn("朋友", prompt)
        self.assertIn("随便", prompt)
        self.assertIn("哎呀", prompt)

    def test_critic_flags_custom_forbidden_phrase(self) -> None:
        card = build_persona_card(CHAT, "小林", "老王").to_dict()
        card["forbidden"] = [*card["forbidden"], "哎呀"]
        result = critic_node(
            {
                "reply": "哎呀知道了",
                "persona": card,
                "retrieved": [],
                "short_memory": [],
            }
        )
        self.assertIn("forbidden_phrase", result["critique"]["problems"])

    def test_critic_ignores_default_forbidden_as_separate_problem(self) -> None:
        card = build_persona_card(CHAT, "小林", "老王").to_dict()
        result = critic_node(
            {
                "reply": "作为AI，我建议你早点休息",
                "persona": card,
                "retrieved": [],
                "short_memory": [],
            }
        )
        self.assertIn("assistant_tone", result["critique"]["problems"])
        self.assertNotIn("forbidden_phrase", result["critique"]["problems"])


if __name__ == "__main__":
    unittest.main()
