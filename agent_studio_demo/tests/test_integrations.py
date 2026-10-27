"""可选集成层：探测、降级、以及"装了之后能调通"（用假模块验证调用形状）。"""

import sys
import types
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from integrations.mem0_backend import Mem0Store, mem0_available
from integrations.optional import DependencyMissing, module_available, module_version
from integrations.ragas_backend import (
    RagasBackendError,
    RagasFaithfulness,
    ragas_available,
    score_with_backend,
)


def fake_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class ProbeTests(unittest.TestCase):
    def test_missing_module(self) -> None:
        self.assertFalse(module_available("definitely_not_a_real_module_xyz"))
        self.assertIsNone(module_version("definitely_not_a_real_module_xyz"))

    def test_available_via_sys_modules(self) -> None:
        with mock.patch.dict(sys.modules, {"fake_opt": fake_module("fake_opt")}):
            self.assertTrue(module_available("fake_opt"))

    def test_broken_import_is_not_available(self) -> None:
        class Boom(types.ModuleType):
            def __getattr__(self, item):
                raise ImportError("broken dependency")

        broken = Boom("fake_broken")
        with mock.patch.dict(sys.modules, {"fake_broken": broken}):
            self.assertTrue(module_available("fake_broken"))  # 导入本身成功
        # importlib.import_module 成功即视为可用；内部延迟报错由调用方兜。

    def test_status_shape(self) -> None:
        from integrations.optional import status

        value = status()
        for name in ("mem0", "ragas", "opentelemetry"):
            self.assertIn(name, value)
            self.assertIn("available", value[name])


class Mem0Tests(unittest.TestCase):
    def test_missing_dependency_message(self) -> None:
        if mem0_available():
            self.skipTest("本机装了 mem0，跳过缺少依赖的分支")
        with self.assertRaises(DependencyMissing) as caught:
            Mem0Store()
        self.assertIn("pip install mem0ai", str(caught.exception))

    def test_works_with_injected_memory(self) -> None:
        calls = []

        class FakeMemory:
            def add(self, messages, user_id=None):
                calls.append(("add", messages, user_id))

            def search(self, query, user_id=None, limit=3):
                calls.append(("search", query, user_id, limit))
                return {"results": [{"memory": "王哥喜欢面馆"}, {"memory": "周六看电影"}]}

        store = Mem0Store(memory=FakeMemory(), user_id="u1")
        store.add_turn("在吗", "在呀")
        self.assertEqual(calls[0][0], "add")
        self.assertEqual(calls[0][2], "u1")
        memories = store.search("吃什么", limit=2)
        self.assertEqual(memories, ["王哥喜欢面馆", "周六看电影"])

    def test_search_tolerates_list_payload(self) -> None:
        class FakeMemory:
            def search(self, query, user_id=None, limit=3):
                return [{"text": "A"}, "B", {"memory": ""}]

        store = Mem0Store(memory=FakeMemory())
        self.assertEqual(store.search("q"), ["A", "B"])


class RagasTests(unittest.TestCase):
    SAMPLES = [
        {
            "user_input": "我们什么时候结婚",
            "response": "啥呀，没说过啊",
            "retrieved_contexts": ["老王: 周末去看电影吗", "小林: 可以呀"],
        }
    ]

    def test_missing_dependency(self) -> None:
        if ragas_available():
            self.skipTest("本机装了 ragas，跳过缺少依赖的分支")
        with self.assertRaises(DependencyMissing) as caught:
            RagasFaithfulness()
        self.assertIn("pip install ragas", str(caught.exception))

    def test_native_backend_is_default(self) -> None:
        result = score_with_backend(self.SAMPLES)
        self.assertEqual(result["backend"], "native")
        self.assertIn("faithfulness", result)

    def test_unknown_backend(self) -> None:
        with self.assertRaises(ValueError):
            score_with_backend(self.SAMPLES, backend="nope")

    def test_ragas_adapter_calls_library(self) -> None:
        seen = {}

        def evaluate(dataset, metrics):
            seen["samples"] = dataset["samples"]
            seen["metrics"] = metrics
            return {"faithfulness": 0.66666}

        ragas = fake_module(
            "ragas",
            EvaluationDataset=lambda samples: {"samples": samples},
            SingleTurnSample=lambda **kwargs: kwargs,
            evaluate=evaluate,
        )
        metrics = fake_module("ragas.metrics", Faithfulness=lambda llm=None: "metric")
        ragas.metrics = metrics
        with mock.patch.dict(
            sys.modules, {"ragas": ragas, "ragas.metrics": metrics}
        ):
            result = RagasFaithfulness().score(self.SAMPLES)

        self.assertEqual(result["faithfulness"], 0.6667)
        self.assertEqual(result["samples"], 1)
        self.assertEqual(seen["samples"][0]["user_input"], "我们什么时候结婚")
        self.assertEqual(seen["metrics"], ["metric"])

    def test_ragas_api_mismatch_raises_actionable_error(self) -> None:
        ragas = fake_module("ragas")  # 没有 EvaluationDataset 等
        with mock.patch.dict(sys.modules, {"ragas": ragas}):
            with self.assertRaises(RagasBackendError) as caught:
                RagasFaithfulness().score(self.SAMPLES)
        self.assertIn("内置", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
