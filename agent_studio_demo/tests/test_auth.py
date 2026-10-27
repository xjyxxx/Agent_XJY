"""鉴权与限流：token 校验、公开路径、令牌桶、以及"对外暴露必须配 token"。"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from transport.handlers import ApiHandlers, _bearer_token
from transport.security import InsecureBind, ensure_bind_is_safe, is_loopback


class FakeService:
    model_name = "FakeModel"


def handler(**kwargs) -> ApiHandlers:
    return ApiHandlers(FakeService(), **kwargs)


class BearerTokenTests(unittest.TestCase):
    def test_extracts_bearer(self) -> None:
        self.assertEqual(_bearer_token({"Authorization": "Bearer abc"}), "abc")

    def test_case_insensitive_header(self) -> None:
        self.assertEqual(_bearer_token({"authorization": "bearer abc"}), "abc")

    def test_raw_token_allowed(self) -> None:
        self.assertEqual(_bearer_token({"Authorization": "abc"}), "abc")

    def test_missing(self) -> None:
        self.assertEqual(_bearer_token({}), "")


class AuthTests(unittest.TestCase):
    def test_open_when_no_token(self) -> None:
        api = handler(token="")
        self.assertIsNone(api.guard({}, "1.2.3.4", "/sessions"))

    def test_requires_token(self) -> None:
        api = handler(token="s3cret")
        status, payload = api.guard({}, "1.2.3.4", "/sessions")
        self.assertEqual(status, 401)
        self.assertIn("Authorization", payload["detail"])

    def test_wrong_token(self) -> None:
        api = handler(token="s3cret")
        status, _ = api.guard({"Authorization": "Bearer nope"}, "1.2.3.4", "/sessions")
        self.assertEqual(status, 401)

    def test_correct_token_passes(self) -> None:
        api = handler(token="s3cret")
        self.assertIsNone(api.guard({"Authorization": "Bearer s3cret"}, "1.2.3.4", "/sessions"))

    def test_public_paths_are_open(self) -> None:
        api = handler(token="s3cret")
        for path in ("/health", "/", "/app.js", "/style.css", "/favicon.ico"):
            with self.subTest(path=path):
                self.assertIsNone(api.guard({}, "1.2.3.4", path))

    def test_health_reports_auth_required(self) -> None:
        status, payload = handler(token="s3cret").health()
        self.assertEqual(status, 200)
        self.assertTrue(payload["auth_required"])


class RateLimitTests(unittest.TestCase):
    def test_bucket_exhausts_then_refills(self) -> None:
        now = [1000.0]
        api = handler(token="", rate_limit_per_minute=3, clock=lambda: now[0])
        headers = {}
        for _ in range(3):
            self.assertIsNone(api.guard(headers, "1.1.1.1", "/sessions"))
        status, payload = api.guard(headers, "1.1.1.1", "/sessions")
        self.assertEqual(status, 429)
        self.assertGreaterEqual(payload["retry_after_seconds"], 1)

        now[0] += 20  # 20 秒回填 1 个令牌（3/min → 0.05/s）
        self.assertIsNone(api.guard(headers, "1.1.1.1", "/sessions"))

    def test_clients_are_isolated(self) -> None:
        api = handler(token="", rate_limit_per_minute=1)
        self.assertIsNone(api.guard({}, "1.1.1.1", "/sessions"))
        self.assertEqual(api.guard({}, "1.1.1.1", "/sessions")[0], 429)
        self.assertIsNone(api.guard({}, "2.2.2.2", "/sessions"))

    def test_rate_limit_counts_only_protected_paths(self) -> None:
        api = handler(token="", rate_limit_per_minute=2)
        for _ in range(5):
            self.assertIsNone(api.guard({}, "1.1.1.1", "/health"))
        self.assertIsNone(api.guard({}, "1.1.1.1", "/sessions"))
        self.assertIsNone(api.guard({}, "1.1.1.1", "/sessions"))
        self.assertEqual(api.guard({}, "1.1.1.1", "/sessions")[0], 429)

    def test_security_summary(self) -> None:
        api = handler(token="x", rate_limit_per_minute=5)
        api.guard({}, "1.1.1.1", "/sessions")
        summary = api.security_summary()
        self.assertEqual(summary["auth"], "bearer")
        self.assertEqual(summary["rate_limit_per_minute"], 5)
        self.assertEqual(summary["rejected_auth"], 1)


class BindGuardTests(unittest.TestCase):
    def test_loopback_detection(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
            self.assertTrue(is_loopback(host), host)
        for host in ("0.0.0.0", "192.168.1.10", "", None):
            self.assertFalse(is_loopback(host), host)

    def test_public_bind_without_token_is_refused(self) -> None:
        with self.assertRaises(InsecureBind) as caught:
            ensure_bind_is_safe("0.0.0.0", "")
        message = str(caught.exception)
        self.assertIn("API_TOKEN", message)
        self.assertIn("rate_limit_per_minute", message)

    def test_public_bind_with_token_is_allowed(self) -> None:
        ensure_bind_is_safe("0.0.0.0", "s3cret")

    def test_loopback_without_token_is_allowed(self) -> None:
        ensure_bind_is_safe("127.0.0.1", "")


if __name__ == "__main__":
    unittest.main()
