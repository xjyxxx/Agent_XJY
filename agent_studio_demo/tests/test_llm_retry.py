"""模型调用重试：只重试临时故障，4xx 立刻失败，流式已输出则不重试。"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from llm_client.deepseek import DeepSeekReplyModel, ModelUnavailable
from tests.fake_deepseek import FakeDeepSeek


def make_model(fake: FakeDeepSeek, **kwargs) -> DeepSeekReplyModel:
    delays = []
    model = DeepSeekReplyModel(
        api_key="sk-test",
        base_url=fake.base_url,
        model="deepseek-chat",
        retry_base_delay=0.01,
        **kwargs,
    )
    model._sleep = lambda seconds: delays.append(seconds)
    model.delays = delays  # type: ignore[attr-defined]
    return model


class NonStreamRetryTests(unittest.TestCase):
    def test_retries_5xx_then_succeeds(self) -> None:
        with FakeDeepSeek(reply="哈哈知道了", fail_plan=[500, 503]) as fake:
            model = make_model(fake)
            reply = model._chat(system="s", user="u")
        self.assertEqual(reply, "哈哈知道了")
        self.assertEqual(fake.request_count, 3)
        self.assertEqual(model.retries_used, 2)
        self.assertEqual(model.delays, [0.01, 0.02])  # 指数退避

    def test_gives_up_after_max_retries(self) -> None:
        with FakeDeepSeek(fail_plan=[500, 500, 500, 500]) as fake:
            model = make_model(fake, max_retries=2)
            with self.assertRaisesRegex(RuntimeError, "连续 3 次不可用"):
                model._chat(system="s", user="u")
        self.assertEqual(fake.request_count, 3)

    def test_4xx_is_not_retried(self) -> None:
        with FakeDeepSeek(fail_plan=[400]) as fake:
            model = make_model(fake)
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                model._chat(system="s", user="u")
        self.assertEqual(fake.request_count, 1)
        self.assertEqual(model.retries_used, 0)
        self.assertEqual(model.delays, [])

    def test_auth_error_is_not_retried_and_stays_readable(self) -> None:
        with FakeDeepSeek(expected_token="other") as fake:
            model = make_model(fake)
            with self.assertRaisesRegex(RuntimeError, "鉴权失败"):
                model._chat(system="s", user="u")
        self.assertEqual(fake.request_count, 1)

    def test_rate_limit_is_not_retried(self) -> None:
        with FakeDeepSeek(fail_plan=[429]) as fake:
            model = make_model(fake)
            with self.assertRaisesRegex(RuntimeError, "被限流"):
                model._chat(system="s", user="u")
        self.assertEqual(fake.request_count, 1)

    def test_timeout_is_retried(self) -> None:
        with FakeDeepSeek(reply="ok", delay_seconds=0.4) as fake:
            model = make_model(fake, timeout=0.05, max_retries=1)
            with self.assertRaisesRegex(RuntimeError, "连续 2 次不可用"):
                model._chat(system="s", user="u")
        self.assertEqual(fake.request_count, 2)

    def test_probe_does_not_retry(self) -> None:
        with FakeDeepSeek(fail_plan=[500]) as fake:
            model = make_model(fake)
            with self.assertRaises(RuntimeError):
                model.probe(timeout=2)
        self.assertEqual(fake.request_count, 1)


class StreamRetryTests(unittest.TestCase):
    def test_stream_retries_before_first_token(self) -> None:
        with FakeDeepSeek(reply="哈哈知道了", fail_plan=[500, 502]) as fake:
            model = make_model(fake)
            chunks = list(model._chat_stream(system="s", user="u"))
        self.assertEqual("".join(chunks), "哈哈知道了")
        self.assertEqual(fake.request_count, 3)
        self.assertEqual(model.retries_used, 2)

    def test_stream_does_not_retry_after_partial_output(self) -> None:
        with FakeDeepSeek(reply="哈哈知道了") as fake:
            model = make_model(fake)

            def broken(system, user, temperature=0.7):
                yield "哈"
                raise ModelUnavailable("连接被重置")

            with mock.patch.object(model, "_stream_once", broken):
                with self.assertRaisesRegex(RuntimeError, "已输出部分内容"):
                    list(model._chat_stream(system="s", user="u"))
        self.assertEqual(model.retries_used, 0)
        self.assertEqual(fake.request_count, 0)


class ConfigKnobTests(unittest.TestCase):
    def test_retry_settings_from_environment(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"LLM_MAX_RETRIES": "5", "LLM_RETRY_BASE_DELAY": "0.25"},
            clear=False,
        ):
            model = DeepSeekReplyModel(api_key="sk-test", base_url="http://127.0.0.1:1")
        self.assertEqual(model.max_retries, 5)
        self.assertAlmostEqual(model.retry_base_delay, 0.25)

    def test_bad_values_fall_back_to_defaults(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"LLM_MAX_RETRIES": "abc", "LLM_RETRY_BASE_DELAY": ""},
            clear=False,
        ):
            model = DeepSeekReplyModel(api_key="sk-test", base_url="http://127.0.0.1:1")
        self.assertEqual(model.max_retries, 2)
        self.assertAlmostEqual(model.retry_base_delay, 0.5)

    def test_backoff_is_capped(self) -> None:
        model = DeepSeekReplyModel(
            api_key="sk-test", base_url="http://127.0.0.1:1", retry_base_delay=10.0
        )
        self.assertLessEqual(model._retry_delay(3), 8.0)


if __name__ == "__main__":
    unittest.main()
