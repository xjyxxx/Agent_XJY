"""Critic 的模型复核：只补"无数字编造"，且失败不影响规则结论。"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.nodes import critic_node
from llm_client.verifier import critic_llm_enabled, make_llm_check
from memory.persona import build_persona_card
from tools.chat_parser import parse_chat_file

FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
PERSONA = build_persona_card(parse_chat_file(FIXTURE), "小林", "老王").to_dict()


def state(reply: str, evidence: str = "那次是真加班") -> dict:
    return {
        "reply": reply,
        "persona": PERSONA,
        "retrieved": [{"content": evidence, "context": ""}],
        "short_memory": [],
        "rewrite_count": 0,
    }


class CriticHookTests(unittest.TestCase):
    def test_no_hook_keeps_rules_only(self) -> None:
        result = critic_node(state("王哥搬去上海了"))
        self.assertNotIn("unsupported_fact_llm", result["critique"]["problems"])

    def test_hook_flags_unsupported_claim(self) -> None:
        calls = []

        def check(reply, evidence):
            calls.append((reply, evidence))
            return False, "证据里没有搬家"

        result = critic_node(state("王哥搬去上海了"), llm_check=check)
        self.assertIn("unsupported_fact_llm", result["critique"]["problems"])
        self.assertEqual(result["critique"]["verdict"], "rewrite")
        self.assertEqual(len(calls), 1)

    def test_hook_passes_supported_claim(self) -> None:
        result = critic_node(state("那次是真加班"), llm_check=lambda r, e: (True, ""))
        self.assertEqual(result["critique"]["problems"], [])

    def test_hook_skipped_for_phatic_reply(self) -> None:
        calls = []
        result = critic_node(
            state("哈哈知道了"), llm_check=lambda r, e: calls.append(1) or (False, "")
        )
        self.assertNotIn("unsupported_fact_llm", result["critique"]["problems"])
        self.assertEqual(calls, [])

    def test_hook_skipped_when_rules_already_failed(self) -> None:
        calls = []
        result = critic_node(
            state("作为AI，我建议你早点休息"),
            llm_check=lambda r, e: calls.append(1) or (False, ""),
        )
        self.assertIn("assistant_tone", result["critique"]["problems"])
        self.assertEqual(calls, [])

    def test_hook_exception_is_swallowed(self) -> None:
        def boom(reply, evidence):
            raise RuntimeError("model down")

        result = critic_node(state("王哥搬去上海了"), llm_check=boom)
        self.assertNotIn("unsupported_fact_llm", result["critique"]["problems"])
        self.assertEqual(result["critique"]["verdict"], "pass")


class VerifierFactoryTests(unittest.TestCase):
    def test_enabled_flag(self) -> None:
        with mock.patch.dict(os.environ, {"CRITIC_LLM": "1"}):
            self.assertTrue(critic_llm_enabled())
        with mock.patch.dict(os.environ, {"CRITIC_LLM": "0"}):
            self.assertFalse(critic_llm_enabled())

    def test_parses_json(self) -> None:
        check = make_llm_check(lambda system, user: '{"supported": false, "reason": "编造"}')
        supported, reason = check("王哥搬去上海了", "无")
        self.assertFalse(supported)
        self.assertEqual(reason, "编造")

    def test_bad_json_means_pass(self) -> None:
        check = make_llm_check(lambda system, user: "我不知道")
        supported, _ = check("随便说说", "无")
        self.assertTrue(supported)

    def test_prompt_contains_evidence_and_reply(self) -> None:
        seen = {}

        def call(system, user):
            seen["system"] = system
            seen["user"] = user
            return '{"supported": true}'

        make_llm_check(call)("在吗", "老王: 在吗")
        self.assertIn("事实核查员", seen["system"])
        self.assertIn("老王: 在吗", seen["user"])
        self.assertIn("在吗", seen["user"])


if __name__ == "__main__":
    unittest.main()
