"""用现成的对话模型做"语义重排"，不额外接 embedding 供应商。

为什么这样做：真正的语义检索要 embedding 服务，而 DeepSeek 没有公开的
embeddings 接口，你又不想下载本地模型。折中办法是**先词法召回候选，再让模型
判断候选与这句话是否在说同一件事**——一次调用给一批候选打分，成本可控
（21 条 query × 1 次调用 ≈ ¥0.01）。

边界：这不是 embedding，召回集合仍由词法决定；模型只能重排已经召回的候选。
所以它救不了"压根没召回"的情况——这一点在实验结论里必须写清楚。
"""

from __future__ import annotations

import json
import re
from typing import Callable, List, Optional

from rag.retriever import BaseRetriever, HistoryHit

RERANK_SYSTEM = """你在判断"用户这句话"和候选句子是否在说同一件事（同一情境/同一话题）。
只输出 JSON 数组，长度必须等于候选数，每个元素是 0-100 的整数，越大越相关。
不要解释。"""

RERANK_USER = """用户这句话：{query}

候选：
{candidates}
"""

_NUMBER_RE = re.compile(r"\d+")


def parse_scores(raw: str, expected: int) -> List[int]:
    """宽松解析：先把 JSON 数组抠出来，失败再用正则扫数字。"""

    start, end = raw.find("["), raw.rfind("]")
    values: List[int] = []
    if 0 <= start < end:
        try:
            items = json.loads(raw[start : end + 1])
            values = [int(float(item)) for item in items if isinstance(item, (int, float, str))]
        except (json.JSONDecodeError, ValueError, TypeError):
            values = []
    if not values:
        values = [int(item) for item in _NUMBER_RE.findall(raw)]

    if len(values) != expected:
        # 数量对不上就当作"没有判断"，避免把错位的分数套到别的句子上
        return []
    return [max(0, min(100, value)) for value in values]


class LlmReranker:
    name = "llm-rerank"

    def __init__(
        self,
        call: Optional[Callable[[str, str], str]] = None,
        depth: int = 8,
    ):
        if call is None:
            from llm_client.deepseek import chat_once

            call = chat_once
        self._call = call
        self.depth = depth
        self.failures = 0

    def rerank(self, query: str, hits: List[HistoryHit]) -> List[HistoryHit]:
        if len(hits) <= 1:
            return hits

        numbered = "\n".join(
            f"{index + 1}. {hit.content}"
            + (f"（上文：{hit.context}）" if hit.context else "")
            for index, hit in enumerate(hits)
        )
        try:
            raw = self._call(
                RERANK_SYSTEM, RERANK_USER.format(query=query, candidates=numbered)
            )
            scores = parse_scores(raw, len(hits))
        except Exception:  # noqa: BLE001 - 重排失败就用原顺序
            self.failures += 1
            return hits

        if not scores:
            self.failures += 1
            return hits

        order = sorted(range(len(hits)), key=lambda index: -scores[index])
        return [
            HistoryHit(
                content=hits[index].content,
                context=hits[index].context,
                speaker=hits[index].speaker,
                line_number=hits[index].line_number,
                score=round(scores[index] / 100.0, 4),
                context_score=hits[index].context_score,
                content_score=hits[index].content_score,
            )
            for index in order
        ]


class RerankedRetriever(BaseRetriever):
    """先取较深的候选，再重排，只返回前 limit 条。"""

    name = "lexical+llm-rerank"

    def __init__(self, base: BaseRetriever, reranker: LlmReranker):
        self.base = base
        self.reranker = reranker

    def search(self, query: str, limit: int = 5) -> List[HistoryHit]:
        depth = max(self.reranker.depth, limit)
        candidates = self.base.search(query, limit=depth)
        return self.reranker.rerank(query, candidates)[:limit]


__all__ = ["LlmReranker", "RerankedRetriever", "parse_scores"]
