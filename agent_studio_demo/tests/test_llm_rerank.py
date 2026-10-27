"""语义重排：解析边界 + 用假模型验证调用形状。"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rag.history_index import HistoryHit
from rag.llm_rerank import LlmReranker, RerankedRetriever, parse_scores


def hit(line: int, text: str) -> HistoryHit:
    return HistoryHit(content=text, context="", speaker="小林", line_number=line, score=0.5)


class ParseTests(unittest.TestCase):
    def test_json_array(self) -> None:
        self.assertEqual(parse_scores("[90, 10, 55]", 3), [90, 10, 55])

    def test_json_with_prose(self) -> None:
        self.assertEqual(parse_scores('结果：[100, 0] 完毕', 2), [100, 0])

    def test_falls_back_to_numbers(self) -> None:
        self.assertEqual(parse_scores("分数 30 70", 2), [30, 70])

    def test_clamps(self) -> None:
        self.assertEqual(parse_scores("[150, -5]", 2), [100, 0])

    def test_wrong_count_returns_empty(self) -> None:
        self.assertEqual(parse_scores("[90, 10]", 3), [])

    def test_garbage_returns_empty(self) -> None:
        self.assertEqual(parse_scores("不知道", 2), [])


class RerankTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hits = [hit(1, "可以呀"), hit(2, "那次是真加班"), hit(3, "随便")]

    def test_reorders_by_score(self) -> None:
        reranker = LlmReranker(call=lambda s, u: "[10, 95, 40]")
        result = reranker.rerank("上次你又放我鸽子", self.hits)
        self.assertEqual([item.line_number for item in result], [2, 3, 1])
        self.assertEqual(result[0].score, 0.95)

    def test_prompt_lists_candidates(self) -> None:
        seen = {}

        def call(system, user):
            seen["user"] = user
            return "[1,2,3]"

        LlmReranker(call=call).rerank("在吗", self.hits)
        self.assertIn("1. 可以呀", seen["user"])
        self.assertIn("3. 随便", seen["user"])

    def test_failure_keeps_original_order(self) -> None:
        def boom(system, user):
            raise RuntimeError("model down")

        reranker = LlmReranker(call=boom)
        result = reranker.rerank("在吗", self.hits)
        self.assertEqual([item.line_number for item in result], [1, 2, 3])
        self.assertEqual(reranker.failures, 1)

    def test_single_hit_skips_call(self) -> None:
        calls = []
        reranker = LlmReranker(call=lambda s, u: calls.append(1) or "[5]")
        result = reranker.rerank("在吗", [hit(1, "在呀")])
        self.assertEqual(len(result), 1)
        self.assertEqual(calls, [])


class RerankedRetrieverTests(unittest.TestCase):
    def test_uses_deeper_candidates(self) -> None:
        class FakeBase:
            def search(self, query, limit=5):
                self.called_with = limit
                return [hit(index, f"c{index}") for index in range(1, limit + 1)]

        base = FakeBase()
        reranker = LlmReranker(call=lambda s, u: "[0, 0, 0, 0, 0, 0, 100, 0]", depth=8)
        retriever = RerankedRetriever(base, reranker)
        result = retriever.search("q", limit=3)
        self.assertEqual(base.called_with, 8)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0].line_number, 7)


class RetrieveNodeWiringTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        from agent import nodes

        with mock.patch.dict(os.environ, {"RERANK_LLM": "0"}):
            self.assertFalse(nodes._rerank_enabled())

    def test_env_enables(self) -> None:
        from agent import nodes

        with mock.patch.dict(os.environ, {"RERANK_LLM": "1"}):
            self.assertTrue(nodes._rerank_enabled())

    def test_uses_cached_reranker(self) -> None:
        from agent import nodes

        class Fake:
            def rerank(self, query, hits):
                return list(reversed(hits))

        with mock.patch.object(nodes, "_RERANKER", Fake()):
            result = nodes._rerank("q", [hit(1, "a"), hit(2, "b")], limit=1)
        self.assertEqual(result[0].line_number, 2)

    def test_unavailable_reranker_falls_back(self) -> None:
        from agent import nodes

        hits = [hit(1, "a"), hit(2, "b")]
        with mock.patch.object(nodes, "_RERANKER", False):
            self.assertEqual(nodes._rerank("q", hits, limit=2), hits)


if __name__ == "__main__":
    unittest.main()
