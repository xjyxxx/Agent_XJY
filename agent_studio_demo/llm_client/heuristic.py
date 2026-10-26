"""A deterministic reply model for local verification.

It is deliberately not presented as an LLM. The graph accepts this model via
the `ReplyModel` protocol so DeepSeek / Ollama can be swapped in without
touching graph or checkpoint code.

Day7 调整：检索分数归一化到 [0, 1] 之后，`EVIDENCE_THRESHOLD` 才有意义——
超过阈值就把历史原句直接当作回复（这个场景里"重复对方说过的话"通常就是
最像的答案）；低于阈值才走候选池。

Day9 调整：**候选池** 取代"命中一个分支就返回"。Critic 说"重复了"时，
同一个分支会给出同一句模板，于是三次重写都撞同一句话，最后被迫降级。
改成按优先级收集多个候选，取第一个与上一轮不同的。
"""

from __future__ import annotations

from typing import List, Protocol

from memory.persona import PersonaCard, ShortTermMemory
from rag.history_index import HistoryHit


# 证据分数到这个程度，就直接复用历史原句，而不是自己编。
EVIDENCE_THRESHOLD = 0.4

QUESTION_MARKERS = ("要不要", "吗", "呢", "么", "可以")
THANKS_MARKERS = ("谢谢", "感谢")
BUSY_MARKERS = ("忙", "晚点")

FALLBACKS = ("嗯嗯，知道啦～", "哈哈好", "嗯嗯")


class ReplyModel(Protocol):
    def generate(
        self,
        user_input: str,
        persona: PersonaCard,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str = "",
    ) -> str:
        ...


class HeuristicReplyModel:
    """Generate a safe, short, deterministic reply from local evidence."""

    def generate(
        self,
        user_input: str,
        persona: PersonaCard,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str = "",
    ) -> str:
        if "too_long" in feedback:
            return self._first_new(["嗯嗯", "哈哈好"], memory)
        if "assistant_tone" in feedback:
            return self._first_new(["哈哈好", "嗯嗯"], memory)

        text = user_input.strip()
        candidates: List[str] = []

        if retrieved and retrieved[0].score >= EVIDENCE_THRESHOLD:
            candidates.append(retrieved[0].content)

        if any(word in text for word in QUESTION_MARKERS):
            candidates.append("可以呀，你定时间～")
        if any(word in text for word in THANKS_MARKERS):
            candidates.append("不客气呀")
        if any(word in text for word in BUSY_MARKERS):
            candidates.append("没事，你先忙～")

        limit = max(12, int(persona.average_message_length * 1.5))
        candidates.extend(
            example for example in persona.example_replies if len(example) <= limit
        )
        candidates.extend(FALLBACKS)
        return self._first_new(candidates, memory)

    def generate_stream(
        self,
        user_input: str,
        persona: PersonaCard,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str = "",
    ):
        """离线模型的"流式"：把整句按 2 字切开，保证上层流式路径可被测试。"""

        text = self.generate(user_input, persona, retrieved, memory, feedback)
        for index in range(0, len(text), 2):
            yield text[index : index + 2]

    @staticmethod
    def _first_new(candidates: List[str], memory: ShortTermMemory) -> str:
        """Pick the first candidate that differs from the previous reply."""

        seen: List[str] = []
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.append(candidate)

        last = HeuristicReplyModel._last_reply(memory)
        for candidate in seen:
            if candidate != last:
                return candidate
        return seen[0] if seen else "嗯"

    @staticmethod
    def _last_reply(memory: ShortTermMemory) -> str:
        turns = memory.turns
        return turns[-1].assistant_reply if turns else ""
