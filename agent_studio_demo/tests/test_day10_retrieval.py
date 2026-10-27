"""Day10：检索策略与向量后端。

钉住三件事：
1. 哈希向量是确定性的（跨进程一致，不能用内置 hash()）。
2. 查询扩写只在命中同义词时改动 query。
3. 融合（RRF）不会凭空造出文档，也不会丢掉词法已经召回的正确答案。
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

from rag import ab_test
from rag.embedding import HashingEmbedder, build_embedder, cosine
from rag.history_index import HistoryIndex
from rag.retriever import (
    ExpandedRetriever,
    HybridRetriever,
    LexicalRetriever,
    VectorRetriever,
    build_retrievers,
    expand_query,
)
from tools.chat_parser import parse_chat_file


FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


def build_index() -> HistoryIndex:
    return HistoryIndex.from_chat(parse_chat_file(FIXTURE), "小林", "老王")


class EmbedderTests(unittest.TestCase):
    def test_hashing_embedder_is_deterministic(self) -> None:
        embedder = HashingEmbedder(dim=64)
        first = embedder.embed("晚饭你自己先吃")
        second = HashingEmbedder(dim=64).embed("晚饭你自己先吃")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertTrue(any(value != 0.0 for value in first))

    def test_similar_text_scores_higher_than_unrelated(self) -> None:
        embedder = HashingEmbedder(dim=256)
        base = embedder.embed("今晚加班的话晚饭你自己先吃")
        near = embedder.embed("晚饭你自己先吃")
        far = embedder.embed("帮我写一份季度财报")
        self.assertGreater(cosine(base, near), cosine(base, far))

    def test_http_embedder_requires_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "EMBEDDING_BASE_URL"):
            build_embedder("http")

    def test_unknown_embedder_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown embedder"):
            build_embedder("magic")


class RetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = build_index()

    def test_expand_query_adds_synonyms_only_on_hit(self) -> None:
        expanded = expand_query("晚饭吃啥")
        self.assertIn("吃饭", expanded)
        self.assertEqual(expand_query("完全无关的一句话"), "完全无关的一句话")

    def test_lexical_and_vector_agree_on_the_golden_query(self) -> None:
        lexical = LexicalRetriever(self.index).search(GOLDEN_QUERY, limit=3)
        vector = VectorRetriever(self.index, HashingEmbedder()).search(
            GOLDEN_QUERY, limit=3
        )
        self.assertEqual(lexical[0].line_number, 15)
        self.assertEqual(vector[0].line_number, 15)
        self.assertGreaterEqual(vector[0].score, 0.0)
        self.assertLessEqual(vector[0].score, 1.0)

    def test_expanded_retriever_is_superset_free_but_keeps_top_hit(self) -> None:
        expanded = ExpandedRetriever(self.index).search(GOLDEN_QUERY, limit=3)
        self.assertEqual(expanded[0].line_number, 15)

    def test_hybrid_fuses_by_rank_and_keeps_lexical_top(self) -> None:
        lexical = LexicalRetriever(self.index)
        vector = VectorRetriever(self.index, HashingEmbedder())
        hybrid = HybridRetriever([lexical, vector])
        fused = hybrid.search(GOLDEN_QUERY, limit=3)
        self.assertTrue(fused)
        self.assertEqual(fused[0].line_number, 15)
        scores = [hit.score for hit in fused]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_build_retrievers_without_embedder_has_two_backends(self) -> None:
        names = [retriever.name for retriever in build_retrievers(self.index, None)]
        self.assertEqual(names, ["lexical", "lexical+expand"])


class BenchmarkTests(unittest.TestCase):
    def test_lexical_hits_the_annotated_line_for_same_situation(self) -> None:
        result = ab_test.score_backend(
            LexicalRetriever(build_index()),
            [
                {
                    "query": "今晚加班的话晚饭你自己先吃",
                    "expected_line": 15,
                    "group": "same_situation",
                }
            ],
        )
        self.assertEqual(result["overall"]["hit@1"], 1.0)
        self.assertEqual(result["overall"]["false_positive_rate"], 0.0)

    def test_missing_line_is_reported_as_miss(self) -> None:
        result = ab_test.score_backend(
            LexicalRetriever(build_index()),
            [
                {
                    "query": "今晚加班的话晚饭你自己先吃",
                    "expected_line": 2,
                    "group": "same_situation",
                }
            ],
        )
        self.assertEqual(result["overall"]["hit@1"], 0.0)

    def test_cli_runs_and_emits_json(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = ab_test.main(["--embedder", "none", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn("lexical", payload)
        self.assertIn("hit@1", payload["lexical"]["overall"])

    def test_cli_runs_with_hashing_embedder(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = ab_test.main(["--embedder", "hashing"])
        self.assertEqual(code, 0)
        self.assertIn("vector:hashing-256", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
