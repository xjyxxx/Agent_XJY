"""Day12：评估器契约。

钉住三件事：
1. 规则指标可独立计算（不读 Critic 的结论）。
2. 指标必须分"干净轮次"和"对抗轮次"——混在一起会把"成功拦截"算成"失败"。
3. 门禁只做回归，不做效果宣称。
"""

import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluator import run as eval_run
from evaluator.llm_judge import (
    FIXTURE,
    LLMJudge,
    RoundRecord,
    RuleJudge,
    TurnMetrics,
    aggregate,
    evaluate,
    gate,
    load_rounds,
)
from evaluator.rules import (
    punctuation_score,
    style_similarity,
    unsupported_numbers,
)
from ingest import session_store


def persona() -> dict:
    return {
        "speaker": "小林",
        "message_count": 10,
        "average_message_length": 4.2,
        "common_terms": ["哈", "王", "哥", "随", "便", "呀", "嗯", "好"],
        "punctuation_style": ["，"],
        "uses_emoji": False,
        "example_replies": ["随便呀", "嗯嗯都行", "哈哈可以"],
    }


def record(round_id: str, expect_human: bool, metrics: TurnMetrics, status="ok",
           problems=None) -> RoundRecord:
    return RoundRecord(
        round_id=round_id,
        user_input="x",
        reply="y",
        status=status,
        verdict="pass" if status == "ok" else "review",
        problems=problems or [],
        tags=[],
        metrics=metrics,
        expectation_ok=True,
        expect_human=expect_human,
    )


class RuleMetricTests(unittest.TestCase):
    def test_style_similarity_of_a_clean_short_reply(self) -> None:
        score = style_similarity("哈哈知道了", persona())
        self.assertGreater(score, 0.7)
        self.assertLessEqual(score, 1.0)

    def test_assistant_tone_scores_lower(self) -> None:
        clean = style_similarity("哈哈知道了", persona())
        verbose = style_similarity("作为AI，我建议你早点休息，注意身体", persona())
        self.assertLess(verbose, clean)

    def test_punctuation_handles_absent_punctuation(self) -> None:
        self.assertEqual(punctuation_score("哈哈知道了", ["，"]), 0.5)
        self.assertEqual(punctuation_score("好呀，", ["，"]), 1.0)
        self.assertEqual(punctuation_score("好呀！", ["，"]), 0.0)

    def test_unsupported_numbers(self) -> None:
        self.assertEqual(unsupported_numbers("密码是123456", []), ["123456"])
        # 证据里出现过的数字（18:30）算有依据。
        self.assertEqual(unsupported_numbers("18点30分到", ["18:30 见面"]), [])
        # 证据里没有的数字才算编造。
        self.assertEqual(unsupported_numbers("18点30分到", ["六点半到"]), ["18", "30"])
        # 已知边界：中文数字不参与比对（"六点半"里的"六"不是阿拉伯数字）。
        self.assertEqual(unsupported_numbers("六点半到", ["六点半到"]), [])

    def test_rule_judge_scores_a_state(self) -> None:
        metrics = RuleJudge().assess(
            {
                "reply": "哈哈知道了",
                "persona": persona(),
                "retrieved": [
                    {"content": "哈哈知道了", "context": "今晚加班的话晚饭你自己先吃"}
                ],
                "user_input": "王哥今晚加班",
            }
        )
        self.assertFalse(metrics.crosstalk)
        self.assertFalse(metrics.hallucination)
        self.assertEqual(metrics.evidence_hits, 1)
        self.assertGreater(metrics.style_similarity, 0.7)

    def test_llm_judge_requires_a_call(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "call"):
            LLMJudge().assess({"reply": "x", "persona": persona(), "retrieved": []})

    def test_llm_judge_parses_json(self) -> None:
        judge = LLMJudge(
            call=lambda system, user: '{"like_original": true, "crosstalk": false, '
            '"hallucination": false}'
        )
        metrics = judge.assess({"reply": "哈哈", "persona": persona(), "retrieved": []})
        self.assertEqual(metrics.style_similarity, 1.0)
        self.assertFalse(metrics.crosstalk)


class AggregateTests(unittest.TestCase):
    def test_rates_split_clean_and_adversarial(self) -> None:
        records = [
            record("c1", False, TurnMetrics(style_similarity=0.8)),
            record("c2", False, TurnMetrics(style_similarity=0.4)),
            record(
                "a1",
                True,
                TurnMetrics(style_similarity=0.2),
                status="interrupted",
                problems=["assistant_tone"],
            ),
        ]
        metrics = aggregate(records)
        self.assertEqual(metrics["clean_rounds"], 2)
        self.assertEqual(metrics["adversarial_rounds"], 1)
        self.assertEqual(metrics["style_similarity"], 0.6)  # 只算干净轮次
        self.assertEqual(metrics["catch_rate"], 1.0)
        self.assertEqual(metrics["crosstalk_rate"], 0.0)

    def test_catch_rate_drops_when_detection_misses(self) -> None:
        records = [
            record("a1", True, TurnMetrics(), status="ok", problems=[]),
        ]
        self.assertEqual(aggregate(records)["catch_rate"], 0.0)

    def test_gate_passes_on_healthy_metrics(self) -> None:
        metrics = {
            "expectation_pass_rate": 1.0,
            "catch_rate": 1.0,
            "crosstalk_rate": 0.0,
            "hallucination_rate": 0.0,
            "assistant_tone_rate": 0.0,
            "style_similarity": 0.9,
        }
        self.assertEqual(gate(metrics), [])

    def test_gate_reports_each_failed_check(self) -> None:
        metrics = {
            "expectation_pass_rate": 0.8,
            "catch_rate": 1.0,
            "crosstalk_rate": 0.1,
            "hallucination_rate": 0.0,
            "assistant_tone_rate": 0.0,
            "style_similarity": 0.3,
        }
        failures = gate(metrics)
        self.assertEqual(len(failures), 3)
        self.assertTrue(any("expectation_pass_rate" in item for item in failures))
        self.assertTrue(any("crosstalk_rate" in item for item in failures))
        self.assertTrue(any("style_similarity" in item for item in failures))


class EndToEndEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def test_rounds_file_has_ten_rounds_with_both_groups(self) -> None:
        rounds = load_rounds()
        self.assertEqual(len(rounds), 10)
        self.assertTrue(any(round_["expect_human"] for round_ in rounds))
        self.assertTrue(any(not round_["expect_human"] for round_ in rounds))

    def test_two_rounds_produce_records(self) -> None:
        rounds = load_rounds()[:2]
        records = evaluate(rounds, RuleJudge(), chat_path=FIXTURE)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item.expectation_ok for item in records))

    def test_full_run_passes_the_gate(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = eval_run.main(["--gate"])
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertIn("gate: PASS", buffer.getvalue())

    def test_json_output_is_machine_readable(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = eval_run.main(["--json", "--limit", "2"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["judge"], "rule")
        self.assertEqual(len(payload["rounds"]), 2)
        self.assertIn("style_similarity", payload["metrics"])


if __name__ == "__main__":
    unittest.main()
