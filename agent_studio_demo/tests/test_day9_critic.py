"""Day9：Critic 与条件边。

这一组测试钉死三件事：
1. Critic 先规则后模型；助手腔、超长、串台、编造数字都能被规则拦住。
2. 重写是回边，最多两次，之后 **降级** 而不是抛异常。
3. 步数预算耗尽也是降级（budget_exhausted），不返回 500。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import PersonaReplayRuntime
from agent.nodes import critic_node, route_after_critic
from ingest import session_store
from memory.persona import build_persona_card
from tests.fakes import ScriptedModel
from tools.chat_parser import parse_chat_file


FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


def persona_dict() -> dict:
    return build_persona_card(parse_chat_file(FIXTURE), "小林").to_dict()


def base_state(reply: str, retrieved=None) -> dict:
    return {
        "reply": reply,
        "persona": persona_dict(),
        "retrieved": retrieved or [],
        "short_memory": [],
    }


class CriticRuleTests(unittest.TestCase):
    def test_assistant_tone_is_caught(self) -> None:
        result = critic_node(base_state("作为AI，我建议你早点休息"))
        self.assertIn("assistant_tone", result["critique"]["problems"])
        self.assertEqual(result["critique"]["verdict"], "rewrite")
        self.assertEqual(result["rewrite_count"], 1)

    def test_overlong_reply_is_caught(self) -> None:
        result = critic_node(
            base_state("这个事情我觉得吧，要看你怎么想，要不我们先这样，回头再细说")
        )
        self.assertIn("too_long", result["critique"]["problems"])

    def test_copied_other_speaker_text_is_crosstalk(self) -> None:
        other = ["另外，周末的电影票我已经买好了，别临时放我鸽子。"]
        result = critic_node(base_state("周末的电影票我已经买好"), other)
        self.assertIn("crosstalk", result["critique"]["problems"])

    def test_short_reply_is_not_crosstalk(self) -> None:
        other = ["另外，周末的电影票我已经买好了，别临时放我鸽子。"]
        result = critic_node(base_state("哈哈知道了"), other)
        self.assertNotIn("crosstalk", result["critique"]["problems"])

    def test_invented_numbers_are_flagged(self) -> None:
        result = critic_node(base_state("密码是123456"))
        self.assertIn("unsupported_fact", result["critique"]["problems"])

    def test_numbers_from_evidence_are_allowed(self) -> None:
        evidence = [
            {"content": "六点半到", "context": "18:30 见面", "speaker": "小林", "line_number": 9}
        ]
        result = critic_node(base_state("18点30分到", retrieved=evidence))
        self.assertNotIn("unsupported_fact", result["critique"]["problems"])

    def test_repeated_reply_is_flagged(self) -> None:
        state = base_state("随便呀")
        state["short_memory"] = [
            {"user_input": "上一轮", "assistant_reply": "随便呀"},
            {"user_input": "这一轮", "assistant_reply": "随便呀"},
        ]
        result = critic_node(state)
        self.assertIn("repeated", result["critique"]["problems"])

    def test_clean_reply_passes(self) -> None:
        result = critic_node(base_state("哈哈知道了"))
        self.assertEqual(result["critique"]["verdict"], "pass")
        self.assertEqual(result["critique"]["problems"], [])
        self.assertEqual(result["rewrite_count"], 0)

    def test_router_maps_verdict_to_key(self) -> None:
        self.assertEqual(route_after_critic({"critique": {"verdict": "pass"}}), "pass")
        self.assertEqual(route_after_critic({"critique": {"verdict": "review"}}), "review")
        self.assertEqual(route_after_critic({}), "pass")


class RewriteLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def _runtime(self, model, max_steps: int = 12) -> PersonaReplayRuntime:
        return PersonaReplayRuntime(":memory:", model=model, max_steps=max_steps)

    def test_two_failed_rewrites_pause_for_human(self) -> None:
        model = ScriptedModel(["作为AI，我建议你早点休息"])
        runtime = self._runtime(model)
        try:
            state = runtime.invoke(
                thread_id="t",
                chat_path=str(FIXTURE),
                user_speaker="老王",
                target_speaker="小林",
                user_input=GOLDEN_QUERY,
            )
        finally:
            runtime.close()

        # Day11 起不再静默降级：中断，等人裁决。
        self.assertEqual(state.status, "interrupted")
        self.assertEqual(state.rewrite_count, 2)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(
            state.steps,
            ["planner", "retrieve", "draft", "critic", "draft", "critic",
             "draft", "critic", "human_review"],
        )
        pending = state.pending or {}
        self.assertEqual(pending.get("reason"), "rewrite_budget")
        self.assertEqual(pending.get("resume_at"), "__end__")
        self.assertEqual(state.critique["verdict"], "review")
        self.assertIn("assistant_tone", state.critique["problems"])

    def test_rewrite_recovers_when_model_is_fixed(self) -> None:
        model = ScriptedModel(["作为AI，我建议你早点休息", "哈哈知道了"])
        runtime = self._runtime(model)
        try:
            state = runtime.invoke(
                thread_id="t",
                chat_path=str(FIXTURE),
                user_speaker="老王",
                target_speaker="小林",
                user_input=GOLDEN_QUERY,
            )
        finally:
            runtime.close()

        self.assertEqual(state.status, "ok")
        self.assertEqual(state.reply, "哈哈知道了")
        self.assertEqual(state.rewrite_count, 1)
        self.assertIn("assistant_tone", model.feedbacks[1])
        # 重写不该让记忆里留下两条相同的用户输入。
        self.assertEqual(len(state.short_memory), 1)
        self.assertEqual(state.turn_count, 1)

    def test_interrupt_does_not_leak_into_next_turn(self) -> None:
        # 前三次调用都不合格（走完两次重写后中断），第四次才给合格回复。
        model = ScriptedModel(["作为AI，我建议你早点休息"] * 3 + ["哈哈知道了"])
        runtime = self._runtime(model)
        try:
            first = runtime.invoke(
                thread_id="t",
                chat_path=str(FIXTURE),
                user_speaker="老王",
                target_speaker="小林",
                user_input=GOLDEN_QUERY,
            )
            runtime.resume("t", {"action": "reject"})
            second = runtime.invoke(thread_id="t", user_input="那周六呢？")
        finally:
            runtime.close()

        self.assertEqual(first.status, "interrupted")
        self.assertEqual(first.rewrite_count, 2)
        # 上一轮用满了两次重写，这一轮必须重新计数，不能立刻再停一次。
        self.assertEqual(second.status, "ok")
        self.assertEqual(second.rewrite_count, 0)
        self.assertEqual(second.reply, "哈哈知道了")

    def test_step_budget_degrades_instead_of_raising(self) -> None:
        model = ScriptedModel(["作为AI，我建议你早点休息"])
        runtime = self._runtime(model, max_steps=4)
        try:
            state = runtime.invoke(
                thread_id="t",
                chat_path=str(FIXTURE),
                user_speaker="老王",
                target_speaker="小林",
                user_input=GOLDEN_QUERY,
            )
        finally:
            runtime.close()

        self.assertEqual(state.status, "budget_exhausted")
        self.assertEqual(state.steps, ["planner", "retrieve", "draft", "critic"])
        self.assertTrue(state.reply)


if __name__ == "__main__":
    unittest.main()
