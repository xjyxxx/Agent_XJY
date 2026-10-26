"""Dependency-free lexical history index for the Persona Replay demo.

Day7 changes（把单轮扮演跑稳）:
- 中文按 **char-bigram** 切分，而不是单字。单字重叠会让「了/知/道」这种
  虚词把不相关的历史顶到第一名。
- 打分归一化到 [0, 1]，这样阈值（"这条够不够像证据"）才有意义。
- **上下文优先**：用户这句话对应的是历史里 A 的那句话，B 的回复是随证据
  一起返回的答案。所以 sim(query, context) 权重 0.7，sim(query, content) 0.3。
- token 向量在构造时预计算，避免每次 search 重新分词。

The production plan can replace this implementation with Chroma or Qdrant.
Keeping the index local and lexical makes the learning path runnable without
network access or an embedding service.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from tools.chat_parser import ChatMessage, ParsedChat


_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 打分权重：用户的话对应历史里 A 的话，所以上下文权重更高。
CONTEXT_WEIGHT = 0.7
CONTENT_WEIGHT = 0.3
EXACT_BONUS = 0.15
RECENCY_EPSILON = 0.0001


def tokenize(text: str) -> List[str]:
    """Tokenize mixed Chinese/English text into ascii words + CJK bigrams."""

    lowered = text.lower()
    tokens: List[str] = _ASCII_WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _cosine(left: Set[str], right: Set[str]) -> float:
    """Binary cosine similarity in [0, 1]."""

    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


@dataclass(frozen=True)
class HistoryEntry:
    """One target-speaker reply plus the preceding user message as context."""

    speaker: str
    content: str
    context: str
    line_number: int


@dataclass(frozen=True)
class HistoryHit:
    """A retrieved historical reply and its explainable evidence score."""

    content: str
    context: str
    speaker: str
    line_number: int
    score: float
    context_score: float = 0.0
    content_score: float = 0.0
    matched_tokens: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "HistoryHit":
        return cls(
            content=str(value["content"]),
            context=str(value.get("context", "")),
            speaker=str(value["speaker"]),
            line_number=int(value["line_number"]),
            score=float(value.get("score", 0.0)),
            context_score=float(value.get("context_score", 0.0)),
            content_score=float(value.get("content_score", 0.0)),
            matched_tokens=[str(token) for token in value.get("matched_tokens", [])],
        )


class HistoryIndex:
    """Small in-memory index over target replies and A→B conversation pairs."""

    def __init__(self, entries: Iterable[HistoryEntry]):
        self.entries: List[HistoryEntry] = list(entries)
        if not self.entries:
            raise ValueError("history index requires at least one target reply")
        # 预计算：分词是 search 里最贵的一步，每个 entry 只做一次。
        self._content_tokens: List[Set[str]] = [
            set(tokenize(entry.content)) for entry in self.entries
        ]
        self._context_tokens: List[Set[str]] = [
            set(tokenize(entry.context)) for entry in self.entries
        ]

    @classmethod
    def from_chat(
        cls,
        chat: ParsedChat,
        target_speaker: str,
        user_speaker: Optional[str] = None,
    ) -> "HistoryIndex":
        entries: List[HistoryEntry] = []
        previous: Optional[ChatMessage] = None
        for message in chat.messages:
            if message.speaker == target_speaker:
                context = ""
                if previous is not None and (
                    user_speaker is None or previous.speaker == user_speaker
                ):
                    context = previous.content
                entries.append(
                    HistoryEntry(
                        speaker=message.speaker,
                        content=message.content,
                        context=context,
                        line_number=message.line_number,
                    )
                )
            previous = message
        return cls(entries)

    def search(self, query: str, limit: int = 3) -> List[HistoryHit]:
        """Return the most relevant target replies with normalized scores."""

        if limit < 1:
            raise ValueError("limit must be at least 1")

        query_text = query.strip().lower()
        query_tokens = set(tokenize(query_text))
        scored: List[HistoryHit] = []

        for position, entry in enumerate(self.entries):
            context_tokens = self._context_tokens[position]
            content_tokens = self._content_tokens[position]
            context_score = _cosine(query_tokens, context_tokens)
            content_score = _cosine(query_tokens, content_tokens)

            exact = 0.0
            if len(query_text) >= 4 and (
                query_text in entry.content.lower()
                or (entry.context and query_text in entry.context.lower())
            ):
                exact = 1.0

            score = (
                CONTEXT_WEIGHT * context_score
                + CONTENT_WEIGHT * content_score
                + EXACT_BONUS * exact
                + position * RECENCY_EPSILON
            )
            scored.append(
                HistoryHit(
                    content=entry.content,
                    context=entry.context,
                    speaker=entry.speaker,
                    line_number=entry.line_number,
                    score=round(min(score, 0.9999), 4),
                    context_score=round(context_score, 4),
                    content_score=round(content_score, 4),
                    matched_tokens=sorted(query_tokens & (context_tokens | content_tokens)),
                )
            )

        scored.sort(key=lambda hit: (-hit.score, hit.line_number))
        return scored[:limit]


def best_score(hits: Iterable[HistoryHit]) -> float:
    """Convenience helper for calling code that only needs the top score."""

    values = [hit.score for hit in hits]
    return max(values) if values else 0.0


def score_breakdown(hit: HistoryHit) -> Dict[str, float]:
    """Explainable score parts, used by the CLI and by tests."""

    return {
        "context": hit.context_score,
        "content": hit.content_score,
        "total": hit.score,
    }
