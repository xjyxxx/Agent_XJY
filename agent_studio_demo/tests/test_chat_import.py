"""多格式导入：csv / html / jsonl / txt，以及主链路是否真的吃到了新格式。"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ingest import session_store
from ingest.session_store import get_or_build
from tools.chat_import import (
    UnsupportedFormat,
    import_chat,
    parse_csv,
    parse_html,
    parse_jsonl,
    supported_formats,
)

FIXTURES = PROJECT_ROOT / "fixtures"
CSV_FILE = FIXTURES / "chat_wechat_like.csv"
HTML_FILE = FIXTURES / "chat_export.html"
JSONL_FILE = FIXTURES / "chat_export.jsonl"
TXT_FILE = FIXTURES / "chat_laowang_xiaolin.txt"


class FormatTests(unittest.TestCase):
    def test_csv_with_chinese_header(self) -> None:
        chat = parse_csv(CSV_FILE)
        self.assertEqual(chat.speakers, ("老王", "小林"))
        self.assertEqual(len(chat.messages), 6)
        self.assertEqual(chat.messages[0].timestamp, "2026-10-02 09:00:00")
        self.assertEqual(chat.messages[1].content, "哈哈知道了")

    def test_csv_without_header_falls_back_to_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "no_header.csv"
            path.write_text("老王,在吗\n小林,在\n", encoding="utf-8")
            chat = parse_csv(path)
        self.assertEqual(chat.speakers, ("老王", "小林"))
        self.assertEqual(chat.messages[0].content, "在吗")

    def test_csv_with_tab_delimiter_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tab.csv"
            path.write_text("\ufeff时间\t发送者\t内容\n09:00\t老王\t早\n09:01\t小林\t嗯\n", encoding="utf-8")
            chat = parse_csv(path)
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(chat.messages[1].speaker, "小林")

    def test_html_name_content_blocks(self) -> None:
        chat = parse_html(HTML_FILE)
        self.assertEqual(chat.speakers, ("老王", "小林"))
        self.assertEqual(len(chat.messages), 4)
        self.assertIn("哈哈知道了", chat.messages[1].content)

    def test_html_falls_back_to_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plain.html"
            path.write_text(
                "<html><body><p>老王: 在吗</p><p>小林: 在</p></body></html>",
                encoding="utf-8",
            )
            chat = parse_html(path)
        self.assertEqual(chat.speakers, ("老王", "小林"))

    def test_jsonl(self) -> None:
        chat = parse_jsonl(JSONL_FILE)
        self.assertEqual(len(chat.messages), 4)
        self.assertEqual(chat.messages[-1].content, "随便，王哥决定")

    def test_dispatch_and_unsupported(self) -> None:
        for path, expected in ((CSV_FILE, 6), (HTML_FILE, 4), (JSONL_FILE, 4), (TXT_FILE, 20)):
            with self.subTest(path=path.name):
                self.assertEqual(len(import_chat(path).messages), expected)

        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "chat.pdf"
            bad.write_text("x", encoding="utf-8")
            with self.assertRaises(UnsupportedFormat):
                import_chat(bad)

    def test_supported_formats_is_documented(self) -> None:
        formats = dict(supported_formats())
        self.assertIn(".csv", formats)
        self.assertIn(".html", formats)


class PipelineIntegrationTests(unittest.TestCase):
    """导入格式必须被主链路吃到，而不只是解析器自娱自乐。"""

    def setUp(self) -> None:
        session_store.clear()

    def test_csv_fixture_runs_a_full_turn(self) -> None:
        session = get_or_build(CSV_FILE, "老王", "小林")
        self.assertEqual(session.speakers, ["老王", "小林"])
        self.assertTrue(session.index.search("晚饭你自己先吃", limit=1))

        from agent.engine import create_runtime

        runtime = create_runtime(":memory:", engine="builtin")
        try:
            state = runtime.invoke(
                thread_id="csv",
                chat_path=str(CSV_FILE),
                user_speaker="老王",
                target_speaker="小林",
                user_input="今晚加班的话晚饭你自己先吃？",
            )
            self.assertEqual(state.status, "ok")
            self.assertTrue(state.reply)
            self.assertTrue(state.retrieved)
        finally:
            runtime.close()

    def test_html_fixture_runs_a_full_turn(self) -> None:
        from agent.engine import create_runtime

        runtime = create_runtime(":memory:", engine="builtin")
        try:
            state = runtime.invoke(
                thread_id="html",
                chat_path=str(HTML_FILE),
                user_speaker="老王",
                target_speaker="小林",
                user_input="你回复得也太短了吧",
            )
            self.assertEqual(state.status, "ok")
            self.assertTrue(state.reply)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
