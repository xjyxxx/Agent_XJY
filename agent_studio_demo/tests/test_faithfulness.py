"""faithfulness：借 RAGAS 的定义，验我们自己实现的判定边界。"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluator.faithfulness import aggregate, is_factual, score, split_claims, support_native

EVIDENCE = [
    "周六下午吧，三点那场还有票",
    "那次是真加班",
    "我十二点下去排队，你直接过来",
]


class SplitAndClassifyTests(unittest.TestCase):
    def test_split_on_punctuation(self) -> None:
        self.assertEqual(split_claims("好的。那就这样！行吗？"), ["好的", "那就这样", "行吗"])

    def test_phatic_reply_is_not_factual(self) -> None:
        for text in ("哈哈", "随便", "在呀", "嗯嗯知道了"):
            with self.subTest(text=text):
                self.assertFalse(is_factual(text))

    def test_numbers_and_fact_words_are_factual(self) -> None:
        self.assertTrue(is_factual("我三点到"))
        self.assertTrue(is_factual("王哥搬去上海了"))
        self.assertTrue(is_factual("你密码是多少"))


class SupportTests(unittest.TestCase):
    def test_number_must_appear_in_evidence(self) -> None:
        ok, reason = support_native("三点那场", EVIDENCE)
        self.assertTrue(ok, reason)
        ok, reason = support_native("五点那场", EVIDENCE)
        self.assertFalse(ok)
        self.assertIn("数字不在证据里", reason)

    def test_text_overlap(self) -> None:
        ok, _ = support_native("那次是真加班", EVIDENCE)
        self.assertTrue(ok)
        ok, _ = support_native("王哥搬去上海了", EVIDENCE)
        self.assertFalse(ok)

    def test_no_evidence(self) -> None:
        ok, reason = support_native("三点到", [])
        self.assertFalse(ok)
        self.assertIn("没有证据", reason)


class ScoreTests(unittest.TestCase):
    def test_no_factual_claims_returns_none(self) -> None:
        result = score("哈哈知道了", EVIDENCE)
        self.assertIsNone(result["faithfulness"])
        self.assertEqual(result["reason"], "no_factual_claims")

    def test_all_supported(self) -> None:
        result = score("三点那场还有票", EVIDENCE)
        self.assertEqual(result["faithfulness"], 1.0)
        self.assertEqual(result["unsupported"], [])

    def test_mixed_claims(self) -> None:
        result = score("三点那场还有票，我五点开会", EVIDENCE)
        self.assertEqual(result["factual"], 2)
        self.assertEqual(result["supported"], 1)
        self.assertEqual(result["faithfulness"], 0.5)
        self.assertEqual(result["unsupported"][0]["claim"], "我五点开会")

    def test_judge_can_rescue_rule_false_negative(self) -> None:
        calls = []

        def judge(claim, evidence):
            calls.append(claim)
            return True

        result = score("王哥搬去上海了", EVIDENCE, judge=judge)
        self.assertEqual(result["faithfulness"], 1.0)
        self.assertEqual(len(calls), 1)

    def test_judge_failure_falls_back_to_rule(self) -> None:
        def judge(claim, evidence):
            raise RuntimeError("model down")

        result = score("王哥搬去上海了", EVIDENCE, judge=judge)
        self.assertEqual(result["faithfulness"], 0.0)


class AggregateTests(unittest.TestCase):
    def test_skips_not_applicable(self) -> None:
        rows = [
            {"faithfulness": 1.0},
            {"faithfulness": None},
            {"faithfulness": 0.5},
        ]
        result = aggregate(rows)
        self.assertEqual(result["faithfulness"], 0.75)
        self.assertEqual(result["applicable"], 2)
        self.assertEqual(result["skipped"], 1)

    def test_all_skipped(self) -> None:
        result = aggregate([{"faithfulness": None}, {"faithfulness": None}])
        self.assertIsNone(result["faithfulness"])
        self.assertEqual(result["skipped"], 2)


if __name__ == "__main__":
    unittest.main()
