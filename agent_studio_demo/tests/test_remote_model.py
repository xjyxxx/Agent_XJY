"""远程模型（DeepSeek / OpenAI 兼容）：协议、预检、真流式。

没有真实 key 也能验证的部分全部在这里：
- 请求体与鉴权头
- SSE 增量解析
- 401 / 402 / 429 错误映射
- 预检 CLI 的退出码
- 开启 `STREAM=1` 后正文先于判定出现的顺序

本地假服务实现了同协议（`tests/fake_deepseek.py`），所以这段代码换成真 key 即可用。
"""

import json
import os
import sys
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.service import SessionService
from ingest import session_store
from llm_client import check as llm_check
from llm_client.deepseek import DeepSeekReplyModel
from llm_client.scripted import ScriptedModel
from memory.persona import ShortTermMemory, build_persona_card
from tests.fake_deepseek import FakeDeepSeek
from tests.test_transport import UrllibClient
from tools.chat_parser import parse_chat_file
from transport.events import EventBus, NullSink
from transport.handlers import ApiHandlers
from transport.http.stdlib_adapter import StdlibHttpServer

FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"
STREAM_PATH = "?message=" + urllib.request.quote(GOLDEN_QUERY)


def persona():
    return build_persona_card(parse_chat_file(FIXTURE), "小林")


def env_for(fake: FakeDeepSeek, **extra) -> dict:
    return {
        "DEEPSEEK_API_KEY": "sk-test",
        "DEEPSEEK_BASE_URL": fake.base_url,
        "DEEPSEEK_MODEL": "deepseek-flash",
        **extra,
    }


@contextmanager
def stdlib_server(service):
    handlers = ApiHandlers(service, EventBus([NullSink()]))
    server = StdlibHttpServer(handlers, host="127.0.0.1", port=0).start()
    try:
        yield UrllibClient(f"http://{server.host}:{server.port}")
    finally:
        server.shutdown()


def parse_sse(body: str):
    events, current = [], None
    for line in body.splitlines():
        if line.startswith("event: "):
            current = line[len("event: "):]
        elif line.startswith("data: ") and current:
            events.append((current, json.loads(line[len("data: "):])))
            current = None
    return events


class ConfigTests(unittest.TestCase):
    def test_missing_key_is_actionable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                DeepSeekReplyModel()

    def test_config_summary_masks_the_key(self) -> None:
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "sk-abcdefghijklmn"}, clear=False
        ):
            summary = DeepSeekReplyModel.config_summary()
        self.assertTrue(summary["key_present"])
        self.assertIn("***", summary["key_hint"])
        self.assertNotIn("abcdefghijklmn", summary["key_hint"])

    def test_v1_suffix_is_normalised(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_BASE_URL": "https://api.x.com/v1"},
            clear=False,
        ):
            model = DeepSeekReplyModel()
        self.assertEqual(model.base_url, "https://api.x.com")


class RemoteCallTests(unittest.TestCase):
    def test_probe_reports_latency(self) -> None:
        with FakeDeepSeek(reply="OK") as fake:
            with mock.patch.dict(os.environ, env_for(fake), clear=False):
                result = DeepSeekReplyModel().probe()
        self.assertTrue(result["ok"])
        self.assertGreater(result["latency_ms"], 0.0)
        self.assertEqual(result["reply"], "OK")

    def test_generate_stream_yields_multiple_deltas(self) -> None:
        with FakeDeepSeek(reply="哈哈知道了", chunk_size=2) as fake:
            with mock.patch.dict(os.environ, env_for(fake), clear=False):
                model = DeepSeekReplyModel()
                pieces = list(
                    model.generate_stream(
                        user_input="在吗",
                        persona=persona(),
                        retrieved=[],
                        memory=ShortTermMemory(),
                    )
                )
        self.assertEqual("".join(pieces), "哈哈知道了")
        self.assertGreater(len(pieces), 1)
        self.assertEqual(fake.stream_requests, 1)

    def test_non_stream_call_is_not_flagged_as_stream(self) -> None:
        with FakeDeepSeek(reply="好的") as fake:
            with mock.patch.dict(os.environ, env_for(fake), clear=False):
                model = DeepSeekReplyModel()
                text = model._chat("s", "u")
        self.assertEqual(text, "好的")
        self.assertEqual(fake.stream_requests, 0)
        self.assertFalse(fake.requests[0]["stream"])

    def test_auth_failure_is_mapped(self) -> None:
        with FakeDeepSeek() as fake:
            with mock.patch.dict(
                os.environ, {**env_for(fake), "DEEPSEEK_API_KEY": "sk-wrong"}, clear=False
            ):
                model = DeepSeekReplyModel()
                with self.assertRaisesRegex(RuntimeError, "鉴权失败"):
                    model._chat("s", "u")

    def test_balance_failure_is_mapped(self) -> None:
        with FakeDeepSeek(fail_status=402) as fake:
            with mock.patch.dict(os.environ, env_for(fake), clear=False):
                model = DeepSeekReplyModel()
                with self.assertRaisesRegex(RuntimeError, "余额不足"):
                    model._chat("s", "u")

    def test_rate_limit_is_mapped(self) -> None:
        with FakeDeepSeek(fail_status=429) as fake:
            with mock.patch.dict(os.environ, env_for(fake), clear=False):
                model = DeepSeekReplyModel()
                with self.assertRaisesRegex(RuntimeError, "限流"):
                    model._chat("s", "u")


class FullTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def test_turn_runs_through_the_remote_endpoint(self) -> None:
        with FakeDeepSeek(reply="哈哈知道了") as fake:
            with mock.patch.dict(os.environ, env_for(fake), clear=False):
                service = SessionService(":memory:", model=DeepSeekReplyModel())
                try:
                    state = service.create_session(FIXTURE, "老王", "小林")
                    turn = service.send(state.thread_id, GOLDEN_QUERY)
                finally:
                    service.close()
        self.assertEqual(turn.reply, "哈哈知道了")
        self.assertEqual(turn.status, "ok")
        self.assertEqual((turn.critique or {}).get("verdict"), "pass")
        # 请求体里必须带人设与证据，而不是空 prompt
        body = json.dumps(fake.requests[-1], ensure_ascii=False)
        self.assertIn("你在扮演聊天记录里的「小林」", body)
        self.assertIn("哈哈知道了", body)


class StreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        session_store.clear()

    def test_stream_flag_puts_deltas_before_critique(self) -> None:
        with FakeDeepSeek(reply="哈哈知道了", chunk_size=2) as fake:
            with mock.patch.dict(
                os.environ, {**env_for(fake), "STREAM": "1"}, clear=False
            ):
                service = SessionService(":memory:", model=DeepSeekReplyModel())
                try:
                    with stdlib_server(service) as client:
                        session_id = client.post(
                            "/sessions",
                            {
                                "path": FIXTURE,
                                "user_speaker": "老王",
                                "target_speaker": "小林",
                            },
                        ).json()["session_id"]
                        body = client.get(f"/sessions/{session_id}/stream{STREAM_PATH}").text
                finally:
                    service.close()

        events = parse_sse(body)
        names = [name for name, _ in events]
        self.assertGreaterEqual(names.count("reply_delta"), 2)
        self.assertLess(names.index("reply_delta"), names.index("critique"))
        joined = "".join(
            payload["delta"] for name, payload in events if name == "reply_delta"
        )
        self.assertEqual(joined, "哈哈知道了")
        done = [payload for name, payload in events if name == "done"][0]
        self.assertEqual(done["reply"], "哈哈知道了")
        self.assertEqual(fake.stream_requests, 1)

    def test_rewrite_emits_reset_before_the_second_draft(self) -> None:
        with mock.patch.dict(os.environ, {"STREAM": "1"}, clear=False):
            service = SessionService(
                ":memory:",
                model=ScriptedModel(["作为AI，我建议你早点休息", "哈哈知道了"]),
            )
            try:
                with stdlib_server(service) as client:
                    session_id = client.post(
                        "/sessions",
                        {
                            "path": FIXTURE,
                            "user_speaker": "老王",
                            "target_speaker": "小林",
                        },
                    ).json()["session_id"]
                    body = client.get(f"/sessions/{session_id}/stream{STREAM_PATH}").text
            finally:
                service.close()

        events = parse_sse(body)
        names = [name for name, _ in events]
        self.assertIn("reply_reset", names)
        first_delta = names.index("reply_delta")
        reset_at = names.index("reply_reset")
        last_delta = max(index for index, name in enumerate(names) if name == "reply_delta")
        # reset 必须出现在第二版正文之前、第一版正文之后
        self.assertLess(first_delta, reset_at)
        self.assertGreater(last_delta, reset_at)
        done = [payload for name, payload in events if name == "done"][0]
        self.assertEqual(done["reply"], "哈哈知道了")


class PreflightCliTests(unittest.TestCase):
    def _run(self, argv) -> int:
        from contextlib import redirect_stdout
        from io import StringIO

        with redirect_stdout(StringIO()):
            return llm_check.main(argv)

    def test_exit_2_when_key_missing(self) -> None:
        # 用空字符串而不是 pop：空值仍在 os.environ 里，load_dotenv 不会用 .env 覆盖它，
        # 这样即使开发者本机配了 key，这个用例仍然是"没配 key"的场景。
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            code = self._run(["--json"])
        self.assertEqual(code, llm_check.EXIT_NO_KEY)

    def test_exit_0_when_endpoint_answers(self) -> None:
        with FakeDeepSeek(reply="OK") as fake:
            with mock.patch.dict(os.environ, env_for(fake), clear=False):
                code = self._run(["--json"])
        self.assertEqual(code, llm_check.EXIT_OK)

    def test_exit_1_when_endpoint_rejects_auth(self) -> None:
        with FakeDeepSeek() as fake:
            with mock.patch.dict(
                os.environ, {**env_for(fake), "DEEPSEEK_API_KEY": "sk-wrong"}, clear=False
            ):
                code = self._run(["--json"])
        self.assertEqual(code, llm_check.EXIT_FAIL)


if __name__ == "__main__":
    unittest.main()
