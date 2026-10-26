"""Dependency-free persona extraction and conversation memory.

This is intentionally heuristic for Day4. A later day can replace extraction
with an LLM and replace storage with mem0 without changing graph state shape.
"""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional

from tools.chat_parser import ChatMessage, ParsedChat


_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
# 语气词/口头禅常见形态：叠字（哈哈/嗯嗯）、"随便/还行"这类两字口语
_REPEAT_RE = re.compile(r"([\u4e00-\u9fff])\1+")

# 默认禁用词：与 agent/nodes.py 的助手腔黑名单对齐（那边从人设卡读，避免循环依赖）
DEFAULT_FORBIDDEN = (
    "作为ai",
    "作为 ai",
    "作为一个",
    "以下是",
    "建议你",
    "我可以帮你",
    "希望对你",
    "总结一下",
    "很高兴为您",
    "首先，",
)


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class PersonaCard:
    """Stable, explainable style information extracted from target messages.

    Day13 增补了四个**语义字段**（原来的统计字段看不出来）：
    - `addressing`  他怎么称呼对方（"王哥"），不是统计出来的高频字
    - `relationship` 关系（朋友/同事…）——启发式给不出，靠 LLM 抽取
    - `catchphrases` 词级口头禅（"哈哈""随便"），不是单字
    - `forbidden`   这个角色不该说的话（默认是助手腔；LLM 可补充）
    """

    speaker: str
    message_count: int
    average_message_length: float
    common_terms: List[str] = field(default_factory=list)
    punctuation_style: List[str] = field(default_factory=list)
    uses_emoji: bool = False
    example_replies: List[str] = field(default_factory=list)
    addressing: List[str] = field(default_factory=list)
    relationship: str = ""
    catchphrases: List[str] = field(default_factory=list)
    forbidden: List[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN))
    persona_source: str = "heuristic"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "PersonaCard":
        return cls(
            speaker=str(value["speaker"]),
            message_count=int(value["message_count"]),
            average_message_length=float(value["average_message_length"]),
            common_terms=list(value.get("common_terms", [])),
            punctuation_style=list(value.get("punctuation_style", [])),
            uses_emoji=bool(value.get("uses_emoji", False)),
            example_replies=list(value.get("example_replies", [])),
            addressing=list(value.get("addressing", [])),
            relationship=str(value.get("relationship", "")),
            catchphrases=list(value.get("catchphrases", [])),
            forbidden=list(value.get("forbidden") or DEFAULT_FORBIDDEN),
            persona_source=str(value.get("persona_source", "heuristic")),
        )


class PersonaStore:
    """In-memory persona store with optional explicit JSON persistence."""

    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else None
        self._cards: Dict[str, PersonaCard] = {}
        if self.path and self.path.exists():
            self._load()

    def put(self, card: PersonaCard) -> None:
        self._cards[card.speaker] = card
        if self.path:
            self._save()

    def get(self, speaker: str) -> Optional[PersonaCard]:
        return self._cards.get(speaker)

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._cards = {
            speaker: PersonaCard.from_dict(value)
            for speaker, value in data.items()
        }

    def _save(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {speaker: card.to_dict() for speaker, card in self._cards.items()}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


_BIGRAM_STOPWORDS = {
    "什么", "这个", "那个", "就是", "还是", "可以", "知道", "一个", "我们", "你们",
    "他们", "现在", "时候", "如果", "因为", "所以", "但是", "已经", "没有", "不是",
    "真的", "有点", "应该", "一起", "怎么", "这样", "那样",
}


def _extract_addressing(
    messages: Iterable["ChatMessage"], target_name: str, user_name: Optional[str]
) -> List[str]:
    """他称呼对方什么：找"含对方名字里的字、且出现在句首/句尾"的 2–3 字词。

    这是启发式里最难的一类——称呼不是高频词，而是**位置特征**：
    "好的王哥" / "不会忘的王哥" / "随便，王哥决定" 都出现在句首或句尾。
    """

    if not user_name:
        return []
    user_chars = set(user_name)
    scores: Counter = Counter()
    for message in messages:
        for run in _CJK_RUN.findall(message.content):
            for size in (2, 3):
                for index in range(len(run) - size + 1):
                    gram = run[index : index + size]
                    if gram == target_name or not (user_chars & set(gram)):
                        continue
                    if index == 0 or index + size == len(run):
                        scores[gram] += 1
    return [gram for gram, count in scores.most_common(2) if count >= 2]


def _extract_catchphrases(
    messages: Iterable["ChatMessage"], exclude: Iterable[str]
) -> List[str]:
    """词级口头禅：叠字（哈哈/嗯嗯）+ 反复出现的二字词。"""

    banned = set(exclude) | _BIGRAM_STOPWORDS
    counts: Counter = Counter()
    for message in messages:
        for run in _CJK_RUN.findall(message.content):
            for match in _REPEAT_RE.finditer(run):
                counts[match.group(0)] += 1
            for index in range(len(run) - 1):
                gram = run[index : index + 2]
                if gram not in banned:
                    counts[gram] += 1
    return [gram for gram, count in counts.most_common(8) if count >= 2 and gram not in banned][:5]


def build_persona_card(
    chat: ParsedChat, target_speaker: str, user_speaker: Optional[str] = None
) -> PersonaCard:
    messages = [
        message for message in chat.messages if message.speaker == target_speaker
    ]
    if not messages:
        raise ValueError(f"speaker not found in chat: {target_speaker}")

    terms = Counter(token for message in messages for token in _tokens(message.content))
    punctuation = Counter(
        char
        for message in messages
        for char in message.content
        if char in "!?！？。,.，~～"
    )
    examples = [message.content for message in messages[:3]]
    addressing = _extract_addressing(messages, target_speaker, user_speaker)
    catchphrases = _extract_catchphrases(messages, exclude=addressing)
    return PersonaCard(
        speaker=target_speaker,
        message_count=len(messages),
        average_message_length=round(
            sum(len(message.content) for message in messages) / len(messages), 2
        ),
        common_terms=[term for term, _ in terms.most_common(8)],
        punctuation_style=[mark for mark, _ in punctuation.most_common(4)],
        uses_emoji=any(
            any(ord(char) > 0x1F000 for char in message.content)
            for message in messages
        ),
        example_replies=examples,
        addressing=addressing,
        catchphrases=catchphrases,
        forbidden=list(DEFAULT_FORBIDDEN),
        persona_source="heuristic",
    )


PERSONA_SYSTEM = """你在给一段双人聊天里的某个人做"人设卡"。只输出 JSON，不要解释。"""

PERSONA_USER = """目标角色：{target}；对话的另一方：{user}。
请只根据下面的聊天片段归纳，不要编造：
{{
  "addressing": ["{target} 怎么称呼 {user}，没有就 []"],
  "relationship": "朋友/同事/情侣/家人，判断不了就空字符串",
  "catchphrases": ["3-5 个词级口头禅，如 哈哈、随便"],
  "forbidden": ["{target} 绝不会说的话，最多 3 条"]
}}

聊天片段（{target} 的发言 + 紧邻的上文）：
{samples}
"""


def build_persona_card_llm(
    chat: ParsedChat,
    target_speaker: str,
    user_speaker: Optional[str],
    call,
    max_samples: int = 30,
) -> PersonaCard:
    """启发式打底 + LLM 补语义字段（称呼/关系/口头禅/禁用词）。

    LLM 只负责"读不出来"的那部分；统计字段仍由启发式给，避免模型把数字也编了。
    失败时原样返回启发式结果（`persona_source` 会写明）。
    """

    card = build_persona_card(chat, target_speaker, user_speaker)
    samples = []
    previous = None
    for message in chat.messages:
        if message.speaker == target_speaker:
            context = f"（上文：{previous.content}）" if previous else ""
            samples.append(f"- {target_speaker}: {message.content}{context}")
        previous = message
    if not samples:
        return card

    payload = PERSONA_USER.format(
        target=target_speaker,
        user=user_speaker or "对方",
        samples="\n".join(samples[:max_samples]),
    )
    try:
        raw = call(PERSONA_SYSTEM, payload)
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except Exception:  # noqa: BLE001 - 抽取失败不影响主链路
        return card

    addressing = [str(item) for item in (data.get("addressing") or []) if str(item).strip()][:2]
    catchphrases = [str(item) for item in (data.get("catchphrases") or []) if str(item).strip()][:5]
    forbidden = [str(item) for item in (data.get("forbidden") or []) if str(item).strip()][:5]

    merged = PersonaCard.from_dict(card.to_dict())
    if addressing:
        merged.addressing = addressing
    if catchphrases:
        merged.catchphrases = catchphrases
    relationship = str(data.get("relationship") or "").strip()
    if relationship:
        merged.relationship = relationship
    if forbidden:
        merged.forbidden = list(dict.fromkeys([*forbidden, *DEFAULT_FORBIDDEN]))
    merged.persona_source = "heuristic+llm"
    return merged


@dataclass
class ConversationTurn:
    user_input: str
    assistant_reply: str

    def to_dict(self) -> dict:
        return asdict(self)


class ShortTermMemory:
    """Bounded conversation window keyed by the graph's thread.

    窗口只有 6 轮，第 7 轮起旧对话会被挤出去——而验收要求"同一会话多轮
    **不丢称呼和正在聊的主题**"。所以这里加了滚动摘要：被挤出去的那一轮会压成
    一句话追加到 `summary`，摘要再跟着 state 落盘。摘要是有损的，但"正在聊什么"
    通常不需要逐字保留。
    """

    SUMMARY_LIMIT = 400

    def __init__(
        self,
        max_turns: int = 6,
        turns: Optional[Iterable[dict]] = None,
        summary: str = "",
    ):
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        self.max_turns = max_turns
        self.summary = (summary or "").strip()
        self._turns: Deque[ConversationTurn] = deque(maxlen=max_turns)
        for turn in turns or []:
            self._turns.append(
                ConversationTurn(
                    user_input=str(turn["user_input"]),
                    assistant_reply=str(turn["assistant_reply"]),
                )
            )

    @property
    def turns(self) -> List[ConversationTurn]:
        return list(self._turns)

    def add_turn(self, user_input: str, assistant_reply: str) -> None:
        if len(self._turns) == self.max_turns:
            # deque 满时会自动丢弃最旧的一条，所以先把它折进摘要再 append。
            self._fold(self._turns[0])
        self._turns.append(
            ConversationTurn(
                user_input=user_input.strip(), assistant_reply=assistant_reply.strip()
            )
        )

    def _fold(self, turn: ConversationTurn) -> None:
        line = f"用户：{turn.user_input[:40]}／你：{turn.assistant_reply[:40]}"
        merged = f"{self.summary}；{line}" if self.summary else line
        # 只保留尾部：越近的对话越相关，头部旧信息价值低。
        self.summary = merged[-self.SUMMARY_LIMIT :]

    def replace_last_reply(self, assistant_reply: str) -> None:
        """Overwrite the newest reply in place (used by the Critic rewrite loop).

        A rewrite is a replacement of the current turn, not a new turn; appending
        instead would leave duplicated user messages in the window.
        """

        if not self._turns:
            return
        last = self._turns.pop()
        self._turns.append(
            ConversationTurn(
                user_input=last.user_input,
                assistant_reply=assistant_reply.strip(),
            )
        )

    def to_list(self) -> List[dict]:
        return [turn.to_dict() for turn in self._turns]

    def format_context(self) -> str:
        return "\n".join(
            f"用户：{turn.user_input}\n助手：{turn.assistant_reply}"
            for turn in self._turns
        )
