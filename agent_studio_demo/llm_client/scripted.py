"""脚本化的确定性回复模型。

放在 `llm_client` 而不是 `tests` 里，因为评估器（生产代码路径）也要用它：
评估集里那些"故意不合格"的轮次，需要一个能稳定复现的坏模型。
"""

from __future__ import annotations

from typing import List, Sequence

from memory.persona import PersonaCard, ShortTermMemory
from rag.history_index import HistoryHit


class ScriptedModel:
    """按脚本顺序返回回复；调用次数、反馈内容都可断言。

    最后一个回复会被重复使用，方便构造"重写后依然不合格"的场景。
    """

    def __init__(self, replies: Sequence[str]):
        if not replies:
            raise ValueError("ScriptedModel requires at least one reply")
        self.replies: List[str] = list(replies)
        self.calls: List[str] = []
        self.feedbacks: List[str] = []

    def generate(
        self,
        user_input: str,
        persona: PersonaCard,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str = "",
    ) -> str:
        self.feedbacks.append(feedback)
        index = min(len(self.calls), len(self.replies) - 1)
        self.calls.append(user_input)
        return self.replies[index]

    def generate_stream(
        self,
        user_input: str,
        persona: PersonaCard,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str = "",
    ):
        text = self.generate(user_input, persona, retrieved, memory, feedback)
        for index in range(0, len(text), 2):
            yield text[index : index + 2]
