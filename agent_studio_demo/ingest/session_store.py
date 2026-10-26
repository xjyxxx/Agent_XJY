"""Imported-chat session cache.

Day7 的核心修正：解析、人设卡、历史索引是 **导入时** 的一次性工作，
不应该每轮重做。这里按「文件路径 + mtime + size + 说话人」做缓存键，
命中时不读文件内容，只 stat 一次。

设计约定（对应进阶篇 §2.2）：
- state 里只放 chat_id（引用），不放 persona/index（派生数据）。
- 缓存失效靠 mtime + size，不靠全文件哈希，避免每条消息都重读整个聊天记录。
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from memory.persona import (
    PersonaCard,
    build_persona_card,
    build_persona_card_llm,
)
from rag.history_index import HistoryIndex
from tools.chat_import import import_chat
from tools.chat_parser import ParsedChat


@dataclass
class ChatSession:
    """Everything derived from one imported chat, built once."""

    chat_id: str
    chat_path: str
    user_speaker: str
    target_speaker: str
    chat: ParsedChat
    persona: PersonaCard
    index: HistoryIndex

    @property
    def speakers(self) -> List[str]:
        return list(self.chat.speakers)

    def other_speaker_messages(self) -> List[str]:
        """Messages from the non-target speaker, used for crosstalk checks."""

        return [
            message.content
            for message in self.chat.messages
            if message.speaker != self.target_speaker
        ]


_CACHE: Dict[str, ChatSession] = {}
_BY_ID: Dict[str, ChatSession] = {}
_LOCK = threading.Lock()


def _cache_key(path: Path, user: Optional[str], target: Optional[str]) -> str:
    stat = path.stat()
    return f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{user}|{target}"


def _short_id(path: Path, user: str, target: str) -> str:
    digest = hashlib.sha256(f"{path.resolve()}".encode()).hexdigest()[:10]
    speakers = hashlib.sha256(f"{user}|{target}".encode()).hexdigest()[:6]
    return f"{digest}_{speakers}"


def _build_persona(
    chat: ParsedChat, target: str, user: str
) -> PersonaCard:
    """`PERSONA_LLM=1` 时用模型补语义字段（称呼/关系/口头禅/禁用词）。

    默认关闭：抽取要花一次调用，而且启发式已经能给出统计字段。
    失败时静默退回启发式——人设卡不该成为启动的失败点。
    """

    if os.environ.get("PERSONA_LLM", "0").strip().lower() in ("", "0", "false", "no"):
        return build_persona_card(chat, target, user)
    try:
        from llm_client.deepseek import chat_once

        return build_persona_card_llm(chat, target, user, call=chat_once)
    except Exception:  # noqa: BLE001
        return build_persona_card(chat, target, user)


def get_or_build(
    chat_path: str | Path,
    user_speaker: Optional[str] = None,
    target_speaker: Optional[str] = None,
) -> ChatSession:
    """Return the cached session for this chat, building it on first use."""

    path = Path(chat_path)
    if not path.is_file():
        raise ValueError(f"chat file not found: {path}")

    key = _cache_key(path, user_speaker, target_speaker)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

        chat = import_chat(path)
        first, second = chat.require_two_speakers()
        user = user_speaker or first
        target = target_speaker or second
        if user == target:
            raise ValueError("user_speaker and target_speaker must be different")
        if user not in chat.speakers or target not in chat.speakers:
            raise ValueError(f"speakers must come from chat: {list(chat.speakers)}")

        session = ChatSession(
            chat_id=_short_id(path, user, target),
            chat_path=str(path),
            user_speaker=user,
            target_speaker=target,
            chat=chat,
            persona=_build_persona(chat, target, user),
            index=HistoryIndex.from_chat(chat, target, user),
        )
        _CACHE[key] = session
        _BY_ID[session.chat_id] = session
        return session


def get_session(chat_id: str) -> Optional[ChatSession]:
    """Look a session up by the id stored in graph state."""

    if not chat_id:
        return None
    with _LOCK:
        return _BY_ID.get(chat_id)


def clear() -> None:
    """Drop all cached sessions (tests and long-running reloads)."""

    with _LOCK:
        _CACHE.clear()
        _BY_ID.clear()
