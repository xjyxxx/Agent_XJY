import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.chat_parser import parse_chat_file, parse_chat_text


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "chat_laowang_xiaolin.txt"
)


class ChatParserTests(unittest.TestCase):
    def test_parse_golden_fixture_identifies_two_speakers(self) -> None:
        parsed = parse_chat_file(FIXTURE)

        self.assertEqual(parsed.require_two_speakers(), ("老王", "小林"))
        self.assertEqual(len(parsed.messages), 20)
        self.assertEqual(parsed.speaker_counts, {"老王": 10, "小林": 10})

    def test_parse_timestamp_header_and_continuation_line(self) -> None:
        parsed = parse_chat_text(
            "2026-10-03 10:00:00 老王\n第一行\n第二行\n"
            "[2026-10-03 10:01] 小林: 哈哈"
        )

        self.assertEqual(parsed.messages[0].timestamp, "2026-10-03 10:00:00")
        self.assertEqual(parsed.messages[0].content, "第一行\n第二行")
        self.assertEqual(parsed.messages[1].timestamp, "2026-10-03 10:01")

    def test_parse_supports_colon_variants(self) -> None:
        parsed = parse_chat_text("老王：你好\n小林: 哈哈")

        self.assertEqual(parsed.speakers, ("老王", "小林"))
        self.assertEqual(
            [message.content for message in parsed.messages], ["你好", "哈哈"]
        )

    def test_require_two_speakers_rejects_group_chat(self) -> None:
        parsed = parse_chat_text("甲: 你好\n乙: 嗨\n丙: 在吗")

        with self.assertRaisesRegex(ValueError, "exactly two speakers"):
            parsed.require_two_speakers()

    def test_unparseable_text_has_a_line_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "line 1"):
            parse_chat_text("这不是一条带说话人的消息")


if __name__ == "__main__":
    unittest.main()
