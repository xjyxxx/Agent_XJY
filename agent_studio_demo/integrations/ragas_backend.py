"""RAGAS 可选后端：装了就调用它算 faithfulness，没装就用内置实现。

**内置实现是默认**（`evaluator/faithfulness.py`），原因：
- RAGAS 会拉 datasets/langchain/embeddings 一整套依赖，对 4k 行的项目太重；
- 我们的回复大多是 2–5 字的语气句，RAGAS 的 claim 拆分同样会落到"没有事实断言"，
  两者结论一致，没必要为此装几百 MB。

这个适配器的价值是"需要和论文/榜单对齐时，能一键切换"，不是替代内置实现。

⚠️ 本适配器按 RAGAS 0.2+ 的 `EvaluationDataset / SingleTurnSample / Faithfulness`
接口编写，但**本机没有安装 ragas，未能实机验证**；接口不匹配时会抛
`RagasBackendError` 并提示改用内置实现，不会静默返回错误数字。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from integrations.optional import DependencyMissing, module_available


class RagasBackendError(RuntimeError):
    """装了 ragas 但接口对不上（版本差异），或调用失败。"""


def ragas_available() -> bool:
    return module_available("ragas")


class RagasFaithfulness:
    name = "ragas"

    def __init__(self, llm: Any = None, embeddings: Any = None):
        if not ragas_available():
            raise DependencyMissing(
                "缺少可选依赖 ragas。安装：pip install ragas\n"
                "（不装也能用：内置 evaluator.faithfulness 实现了同样的定义）"
            )
        self.llm = llm
        self.embeddings = embeddings

    def score(self, samples: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """samples: [{"user_input": ..., "response": ..., "retrieved_contexts": [...]}]"""

        try:
            from ragas import EvaluationDataset, SingleTurnSample, evaluate
            from ragas.metrics import Faithfulness
        except Exception as exc:  # noqa: BLE001
            raise RagasBackendError(
                f"ragas 接口与预期不符（{exc}）；请改用内置 evaluator.faithfulness"
            ) from exc

        rows = []
        for sample in samples:
            rows.append(
                SingleTurnSample(
                    user_input=str(sample.get("user_input", "")),
                    response=str(sample.get("response", "")),
                    retrieved_contexts=list(sample.get("retrieved_contexts") or []),
                )
            )
        if not rows:
            return {"faithfulness": None, "samples": 0}

        try:
            metric = Faithfulness(llm=self.llm) if self.llm is not None else Faithfulness()
            result = evaluate(EvaluationDataset(samples=rows), metrics=[metric])
            value = float(result["faithfulness"])
        except Exception as exc:  # noqa: BLE001
            raise RagasBackendError(
                f"ragas 评估失败（{exc}）；请改用内置 evaluator.faithfulness"
            ) from exc
        return {"faithfulness": round(value, 4), "samples": len(rows)}


def score_with_backend(
    samples: List[Dict[str, Any]], backend: str = "native"
) -> Dict[str, Any]:
    """统一入口：native（默认）/ ragas。"""

    if backend == "native":
        from evaluator.faithfulness import aggregate, score

        rows = [
            score(
                str(sample.get("response", "")),
                sample.get("retrieved_contexts") or [],
            )
            for sample in samples
        ]
        return {"backend": "native", **aggregate(rows)}

    if backend == "ragas":
        return {"backend": "ragas", **RagasFaithfulness().score(samples)}

    raise ValueError(f"unknown backend: {backend!r} (native|ragas)")


__all__ = [
    "RagasBackendError",
    "RagasFaithfulness",
    "ragas_available",
    "score_with_backend",
]
