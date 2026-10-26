"""可独立计算的客观指标（Day12 L1）。

这些函数**不读 Critic 的结论**，而是从回复本身重新算一遍。原因：
如果评估器直接复用被评估者的判据，那测出来的只是"自己跟自己一致"，
不是"回复本身好不好"。规则会有重叠，但计算路径是独立的。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence

_NUMBER = re.compile(r"\d+")
_PUNCT = "，。！？、；：,.!?;:~～…"


def message_length_score(reply: str, average_length: float) -> float:
    """1.0 表示句长和真实分布一致，越偏越小。"""

    if not reply:
        return 0.0
    if average_length <= 0:
        return 1.0
    shorter = min(len(reply), average_length)
    longer = max(len(reply), average_length)
    return round(shorter / longer, 4)


def term_hit_score(reply: str, common_terms: Sequence[str], top: int = 3) -> float:
    """回复里是否出现对方的标志性用词。"""

    if not common_terms:
        return 1.0
    return 1.0 if any(term in reply for term in common_terms[:top]) else 0.0


def punctuation_score(reply: str, punctuation_style: Sequence[str]) -> float:
    """标点习惯吻合度：回复用了对方常用的标点得 1，用了非常用标点得 0。"""

    used = [char for char in reply if char in _PUNCT]
    if not used:
        # 对方本来就不怎么用标点，那"没用标点"也是像的。
        return 1.0 if not punctuation_style else 0.5
    if not punctuation_style:
        return 0.0
    hits = sum(1 for char in used if char in punctuation_style)
    return round(hits / len(used), 4)


def style_similarity(reply: str, persona: dict) -> float:
    """句长 0.5 + 用词 0.3 + 标点 0.2 的加权和（0–1）。"""

    if not reply:
        return 0.0
    length = message_length_score(reply, float(persona.get("average_message_length", 0)))
    terms = term_hit_score(reply, list(persona.get("common_terms", [])))
    punct = punctuation_score(reply, list(persona.get("punctuation_style", [])))
    return round(0.5 * length + 0.3 * terms + 0.2 * punct, 4)


def unsupported_numbers(reply: str, evidence_texts: Iterable[str]) -> List[str]:
    """回复里的数字有多少不在证据里（编造事实的最廉价信号）。"""

    blob = " ".join(evidence_texts)
    return [number for number in _NUMBER.findall(reply) if number not in blob]


def assistant_tone_hits(reply: str, blacklist: Sequence[str]) -> List[str]:
    lowered = reply.lower()
    return [token for token in blacklist if token in lowered]
