"""Day7：把单轮扮演跑稳。

这一组测试钉死三件事：
1. 黄金样例的一句话，必须召回"同一情境下对方真实回过的那句"（line 15），
   而不是被虚词撞出来的无关发言。
2. 打分归一化到 [0, 1]，阈值才有意义（0.4）。
3. 解析 / 人设 / 索引导入时只做一次；后续轮次不再需要 chat_path。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import PersonaReplayRuntime
from agent.nodes import retrieve_node
from ingest import session_store
from rag.history_index import HistoryHit, HistoryIndex, tokenize
from tools.chat_parser import parse_chat_file


FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


class RetrievalQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()
        self.chat = parse_chat_file(FIXTURE)
        self.index = HistoryIndex.from_chat(self.chat, "小林", "老王")

    def test_chinese_is_tokenized_as_bigrams(self) -> None:
        tokens = tokenize("晚饭你自己先吃")
        self.assertIn("晚饭", tokens)
        self.assertIn("先吃", tokens)
        # 单字不再直接进入索引：虚词重叠正是 Day6 之前召不准的原因。
        self.assertNotIn("饭", tokens)

    def test_golden_query_hits_the_same_situation_reply(self) -> None:
        hits = self.index.search(GOLDEN_QUERY, limit=3)
        top = hits[0]
        self.assertEqual(top.line_number, 15)
        self.assertEqual(top.content, "哈哈知道了")
        self.assertGreaterEqual(top.score, 0.4)
        self.assertLessEqual(top.score, 1.0)
        # 用户这句话对应历史里 A 的那句话，所以上下文相似度必须高于回复本身。
        self.assertGreater(top.context_score, top.content_score)
        self.assertLess(hits[1].score, top.score)

    def test_unrelated_query_scores_below_evidence_threshold(self) -> None:
        hits = self.index.search("帮我写一份季度财报分析", limit=1)
        self.assertLess(hits[0].score, 0.4)

    def test_hit_round_trips_through_json_safe_dict(self) -> None:
        hit = self.index.search(GOLDEN_QUERY, limit=1)[0]
        restored = HistoryHit.from_dict(hit.to_dict())
        self.assertEqual(restored, hit)


class SingleTurnStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def test_golden_turn_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = PersonaReplayRuntime(str(Path(directory) / "state.db"))
            try:
                state = runtime.invoke(
                    thread_id="golden",
                    chat_path=str(FIXTURE),
                    user_speaker="老王",
                    target_speaker="小林",
                    user_input=GOLDEN_QUERY,
                )
                self.assertEqual(state.reply, "哈哈知道了")
                self.assertEqual(state.status, "ok")
                self.assertEqual((state.critique or {}).get("verdict"), "pass")
                self.assertEqual(state.rewrite_count, 0)
                self.assertEqual(state.retrieved[0]["line_number"], 15)
            finally:
                runtime.close()

    def test_session_is_parsed_once_per_chat(self) -> None:
        session_store.clear()
        real_parser = session_store.import_chat
        calls = {"n": 0}

        def counting_parser(path):
            calls["n"] += 1
            return real_parser(path)

        with mock.patch.object(session_store, "import_chat", counting_parser):
            with tempfile.TemporaryDirectory() as directory:
                runtime = PersonaReplayRuntime(str(Path(directory) / "state.db"))
                try:
                    first = runtime.invoke(
                        thread_id="t1",
                        chat_path=str(FIXTURE),
                        user_speaker="老王",
                        target_speaker="小林",
                        user_input=GOLDEN_QUERY,
                    )
                    second = runtime.invoke(thread_id="t1", user_input="那周六呢？")
                    self.assertEqual(first.turn_count, 1)
                    self.assertEqual(second.turn_count, 2)
                    # 第二轮的 planner 直接短路，不再解析文件。
                    self.assertEqual(calls["n"], 1)
                finally:
                    runtime.close()

    def test_second_turn_does_not_need_chat_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = PersonaReplayRuntime(str(Path(directory) / "state.db"))
            try:
                runtime.invoke(
                    thread_id="t2",
                    chat_path=str(FIXTURE),
                    user_speaker="老王",
                    target_speaker="小林",
                    user_input=GOLDEN_QUERY,
                )
                state = runtime.invoke(thread_id="t2", user_input="那周六呢？")
                self.assertTrue(state.chat_id)
                self.assertTrue(state.persona_ready)
                # "那周六呢？" 在历史里没有对应情境，噪声证据必须被阈值挡掉，
                # 而不是拿 0.0009 分的命中冒充"依据"。
                self.assertEqual(state.retrieved, [])
                self.assertTrue(state.reply)
            finally:
                runtime.close()

    def test_low_score_evidence_is_filtered_out(self) -> None:
        session_store.clear()
        patch = retrieve_node(
            {
                "chat_path": str(FIXTURE),
                "user_speaker": "老王",
                "target_speaker": "小林",
                "user_input": "那周六呢？",
            }
        )
        self.assertEqual(patch["retrieved"], [])

        golden = retrieve_node(
            {
                "chat_path": str(FIXTURE),
                "user_speaker": "老王",
                "target_speaker": "小林",
                "user_input": GOLDEN_QUERY,
            }
        )
        self.assertEqual(golden["retrieved"][0]["line_number"], 15)


if __name__ == "__main__":
    unittest.main()
