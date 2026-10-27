"""本地配置文件（INI）：优先级、来源标记、以及"密钥不入库"的硬约束。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import loader

EXAMPLE = PROJECT_ROOT / "config" / "settings.ini.example"

SAMPLE_INI = """\
[model]
api_key = sk-from-ini-000000000000
base_url = https://example.invalid
model = test-model

[server]
provider = heuristic
port = 9999

[features]
stream = 1
"""


class ReadSettingsTests(unittest.TestCase):
    def _write(self, directory: str, text: str = SAMPLE_INI) -> Path:
        path = Path(directory) / "settings.ini"
        path.write_text(text, encoding="utf-8")
        return path

    def test_maps_sections_to_env_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = loader.read_settings(self._write(directory))
        self.assertEqual(values["DEEPSEEK_API_KEY"], "sk-from-ini-000000000000")
        self.assertEqual(values["DEEPSEEK_MODEL"], "test-model")
        self.assertEqual(values["PROVIDER"], "heuristic")
        self.assertEqual(values["PORT"], "9999")
        self.assertEqual(values["STREAM"], "1")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(loader.read_settings(Path("does-not-exist.ini")), {})

    def test_empty_values_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "[model]\napi_key =\nmodel = deepseek-chat\n",
            )
            values = loader.read_settings(path)
        self.assertNotIn("DEEPSEEK_API_KEY", values)
        self.assertEqual(values["DEEPSEEK_MODEL"], "deepseek-chat")

    def test_comments_and_unknown_sections_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "# 注释\n[model]\nmodel = m\n[unknown]\nfoo = bar\n",
            )
            values = loader.read_settings(path)
        self.assertEqual(values, {"DEEPSEEK_MODEL": "m"})


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        loader._ORIGIN.clear()
        self._saved = dict(os.environ)
        for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "PROVIDER", "PORT", "STREAM"):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        loader._ORIGIN.clear()

    def test_environment_wins_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            path.write_text(SAMPLE_INI, encoding="utf-8")
            os.environ["DEEPSEEK_MODEL"] = "from-env"
            result = loader.bootstrap(path)
        self.assertEqual(os.environ["DEEPSEEK_MODEL"], "from-env")
        self.assertNotIn("DEEPSEEK_MODEL", result["applied_from_settings"])
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-from-ini-000000000000")

    def test_second_call_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            path.write_text(SAMPLE_INI, encoding="utf-8")
            loader.bootstrap(path)
            again = loader.bootstrap(path)
        self.assertEqual(again["applied_from_settings"], [])
        self.assertEqual(again["applied_from_dotenv"], [])

    def test_origin_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            path.write_text(SAMPLE_INI, encoding="utf-8")
            loader.bootstrap(path)
            with mock.patch.object(loader, "SETTINGS_PATH", path), \
                 mock.patch.object(loader, "LEGACY_ENV_PATH", Path(directory) / "nope"):
                info = loader.describe()
        self.assertEqual(info["key_configured_by"], str(path))
        self.assertTrue(info["key_configured"])


class NoSecretTests(unittest.TestCase):
    """这些断言是"密钥不入库"的最后一道闸。"""

    def test_example_template_has_no_secret(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"sk-[A-Za-z0-9]{20,}")
        self.assertIn("api_key =", text)

    def test_describe_never_contains_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            path.write_text(SAMPLE_INI, encoding="utf-8")
            saved = dict(os.environ)
            for key in ("DEEPSEEK_API_KEY",):
                os.environ.pop(key, None)
            loader.bootstrap(path)
            try:
                payload = json.dumps(loader.describe(), ensure_ascii=False)
            finally:
                os.environ.clear()
                os.environ.update(saved)
        self.assertNotIn("sk-from-ini", payload)
        self.assertNotIn("from-ini", payload)

    def test_settings_ini_is_gitignored(self) -> None:
        rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("config/settings.ini", rules)

    def test_status_line_is_actionable_without_key(self) -> None:
        saved = dict(os.environ)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            with mock.patch.object(loader, "SETTINGS_PATH", Path("nope.ini")), \
                 mock.patch.object(loader, "LEGACY_ENV_PATH", Path("nope.env")), \
                 mock.patch.dict(loader._ORIGIN, {}, clear=True):
                line = loader.status_line()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertIn("settings.ini", line)
        self.assertIn("离线", line)


if __name__ == "__main__":
    unittest.main()
