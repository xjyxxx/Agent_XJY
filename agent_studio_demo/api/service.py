"""会话服务：把 CLI 的一条命令包成可复用的会话对象。

关键约定：
- `session_id` 就是图里的 `thread_id`，两者不做二次映射（少一层就能少一处错位）。
- **同一 session 的请求必须串行**：checkpoint 是"整份 state 覆盖写"，两个人同时
  往同一会话发消息，后写的会把先写的冲掉。
- `create_session` 会在导入时跑一次 planner，之后的每轮只需要 message。
"""

from __future__ import annotations

import threading
import uuid
from typing import Callable, Dict, Optional

from agent.engine import create_runtime
from agent.state import AgentState
from ingest.session_store import ChatSession, get_or_build, get_session
from llm_client.heuristic import ReplyModel


EventListener = Callable[[str, dict], None]


class SessionNotFound(KeyError):
    """Raised when a session id has no checkpoint."""


class SessionService:
    def __init__(
        self,
        checkpoint_path: str = ":memory:",
        model: Optional[ReplyModel] = None,
        max_steps: int = 12,
        engine: Optional[str] = None,
    ):
        self.runtime = create_runtime(
            checkpoint_path, model=model, max_steps=max_steps, engine=engine
        )
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    def _require(self, session_id: str) -> AgentState:
        state = self.runtime.load_state(session_id)
        if state is None:
            raise SessionNotFound(session_id)
        return state

    # -- public API ------------------------------------------------------
    def import_chat(
        self,
        chat_path: str,
        user_speaker: Optional[str] = None,
        target_speaker: Optional[str] = None,
    ) -> ChatSession:
        return get_or_build(chat_path, user_speaker, target_speaker)

    def create_session(
        self,
        chat_path: str,
        user_speaker: Optional[str] = None,
        target_speaker: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> AgentState:
        session = get_or_build(chat_path, user_speaker, target_speaker)
        sid = session_id or uuid.uuid4().hex[:12]
        with self._lock_for(sid):
            return self.runtime.prepare(
                thread_id=sid,
                chat_path=str(chat_path),
                user_speaker=session.user_speaker,
                target_speaker=session.target_speaker,
            )

    def send(
        self,
        session_id: str,
        message: str,
        on_event: Optional[EventListener] = None,
        on_delta=None,
    ) -> AgentState:
        self._require(session_id)
        with self._lock_for(session_id):
            return self.runtime.invoke(
                thread_id=session_id,
                user_input=message,
                on_event=on_event,
                on_delta=on_delta,
            )

    @property
    def model_name(self) -> str:
        return self.runtime.model_name

    def resume(
        self,
        session_id: str,
        decision: dict,
        on_event: Optional[EventListener] = None,
    ) -> AgentState:
        self._require(session_id)
        with self._lock_for(session_id):
            return self.runtime.resume(
                thread_id=session_id, decision=decision, on_event=on_event
            )

    def get(self, session_id: str) -> AgentState:
        return self._require(session_id)

    def delete(self, session_id: str) -> None:
        self._require(session_id)
        with self._lock_for(session_id):
            self.runtime.checkpointer.delete(session_id)

    def close(self) -> None:
        self.runtime.close()


def session_payload(state: AgentState) -> dict:
    """The JSON shape returned by every non-streaming endpoint."""

    return {
        "session_id": state.thread_id,
        "thread_id": state.thread_id,
        "chat_id": state.chat_id,
        "speakers": state.speakers,
        "user_speaker": state.user_speaker,
        "target_speaker": state.target_speaker,
        "persona": state.persona,
        "retrieved": state.retrieved,
        "draft": state.draft,
        "reply": state.reply,
        "critique": state.critique,
        "rewrite_count": state.rewrite_count,
        "turn_count": state.turn_count,
        "status": state.status,
        "pending": state.pending,
        "human_decision": state.human_decision,
        "steps": state.steps,
    }


def persona_for_chat(session: ChatSession) -> dict:
    return session.persona.to_dict()


__all__ = [
    "SessionService",
    "SessionNotFound",
    "session_payload",
    "persona_for_chat",
    "get_session",
]
