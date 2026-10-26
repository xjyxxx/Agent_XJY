"""官方 LangGraph 引擎（可选依赖）。

方案文档写的是「编排只用 LangGraph」。最初因为环境装不上，用的是同构自研实现
（`agent/graph.py`）。现在 LangGraph 可安装，这里给出**同一个业务图**的官方实现：

    节点函数完全复用 `agent/nodes.py`
    条件边用 `add_conditional_edges`
    中断用官方的 `interrupt()` / `Command(resume=...)`
    断点用官方 `SqliteSaver`（和自研实现的 SQLite checkpoint 对等）

两个引擎的差异只允许在"调度器"这一层，业务节点一行都不重复——否则就是两套系统。
`GRAPH_ENGINE=auto|langgraph|builtin` 选择；auto 时有 langgraph 就用它。

为什么不删掉自研实现：核心链路"零第三方依赖可跑"是这个项目的原则之一，
而且自研版是理解 Pregel 调度的对照物（保留为 fallback 与教学参考）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent.graph import DeltaSink, EventListener, _DELTA_SINK
from agent.nodes import (
    apply_decision,
    critic_node,
    draft_node,
    planner_node,
    retrieve_node,
    review_payload,
    route_after_critic,
)
from agent.state import AgentState
from ingest.session_store import get_session
from llm_client.heuristic import HeuristicReplyModel, ReplyModel


class ReplayState(TypedDict, total=False):
    """LangGraph 的 State schema（字段与 `AgentState` 一一对应）。"""

    thread_id: str
    chat_path: str
    user_speaker: str
    target_speaker: str
    user_input: str
    speakers: List[str]
    persona: Optional[dict]
    retrieved: List[dict]
    draft: str
    reply: str
    critique: Optional[dict]
    rewrite_count: int
    short_memory: List[dict]
    memory_summary: str
    steps: List[str]
    turn_count: int
    chat_id: str
    persona_ready: bool
    status: str
    pending: Optional[dict]
    human_decision: Optional[str]


class _CheckpointerHandle:
    """把官方 saver 包装成 `delete(thread_id)` 接口，与自研 checkpointer 对齐。"""

    def __init__(self, saver):
        self._saver = saver

    def delete(self, thread_id: str) -> None:
        delete_thread = getattr(self._saver, "delete_thread", None)
        if callable(delete_thread):
            delete_thread(thread_id)


def engine_available() -> bool:
    try:
        import langgraph  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


class LangGraphRuntime:
    """与 `PersonaReplayRuntime` 同接口，内部换成官方 StateGraph。"""

    def __init__(
        self,
        checkpoint_path: str = ":memory:",
        model: Optional[ReplyModel] = None,
        max_steps: int = 12,
    ):
        self.model = model or HeuristicReplyModel()
        self.max_steps = max_steps
        self._sqlite_connection = None
        self._saver = self._make_saver(checkpoint_path)
        self.checkpointer = _CheckpointerHandle(self._saver)
        self._on_event: Optional[EventListener] = None
        self.graph = self._build()

    # -- 构建 ------------------------------------------------------------
    def _make_saver(self, checkpoint_path: str):
        if checkpoint_path == ":memory:":
            return InMemorySaver()
        path = Path(checkpoint_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：API 的线程池会跨线程调用
        self._sqlite_connection = sqlite3.connect(
            str(path), check_same_thread=False
        )
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            return SqliteSaver(self._sqlite_connection)
        except ModuleNotFoundError:  # pragma: no cover - 未装 sqlite 扩展时
            return InMemorySaver()

    def _node(self, name: str, fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        def wrapped(state: dict) -> dict:
            update = fn(state)
            merged = {**state, **update, "steps": [*state.get("steps", []), name]}
            if self._on_event is not None:
                try:
                    self._on_event(name, merged)
                except Exception:
                    pass
            return {**update, "steps": merged["steps"]}

        return wrapped

    def _human_review(self, state: dict) -> dict:
        decision = interrupt(review_payload(state))
        return apply_decision(state, decision or {})

    def _build(self):
        graph = StateGraph(ReplayState)
        graph.add_node("planner", self._node("planner", planner_node))
        graph.add_node("retrieve", self._node("retrieve", retrieve_node))
        graph.add_node(
            "draft",
            self._node(
                "draft",
                lambda state: draft_node(state, self.model, _current_delta()),
            ),
        )

        def critic_with_session(state: dict) -> dict:
            session = get_session(str(state.get("chat_id") or ""))
            other = session.other_speaker_messages() if session else ()
            return critic_node(state, other)

        graph.add_node("critic", self._node("critic", critic_with_session))
        graph.add_node("human_review", self._human_review)

        graph.add_edge(START, "planner")
        graph.add_edge("planner", "retrieve")
        graph.add_edge("retrieve", "draft")
        graph.add_edge("draft", "critic")
        graph.add_edge("human_review", END)
        graph.add_conditional_edges(
            "critic",
            route_after_critic,
            {"pass": END, "rewrite": "draft", "review": "human_review"},
        )
        return graph.compile(checkpointer=self._saver)

    # -- 与自研引擎一致的接口 --------------------------------------------
    @property
    def model_name(self) -> str:
        return type(self.model).__name__

    def _config(self, thread_id: str) -> dict:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(self.max_steps, 4),
        }

    def prepare(
        self,
        thread_id: str,
        chat_path: str,
        user_speaker: Optional[str] = None,
        target_speaker: Optional[str] = None,
    ) -> AgentState:
        patch = planner_node(
            {
                "chat_path": chat_path,
                "user_speaker": user_speaker,
                "target_speaker": target_speaker,
            }
        )
        values = {**patch, "thread_id": thread_id, "chat_path": chat_path, "status": "ok"}
        self.graph.update_state(self._config(thread_id), values, as_node=START)
        return AgentState.from_dict(self._snapshot(thread_id))

    def invoke(
        self,
        thread_id: str,
        user_input: str,
        chat_path: Optional[str] = None,
        user_speaker: Optional[str] = None,
        target_speaker: Optional[str] = None,
        on_event: Optional[EventListener] = None,
        on_delta: Optional[DeltaSink] = None,
    ) -> AgentState:
        inputs: Dict[str, Any] = {
            "thread_id": thread_id,
            "user_input": user_input,
            "rewrite_count": 0,
            "critique": None,
            "pending": None,
            "human_decision": None,
            "status": "ok",
            "steps": [],
        }
        if chat_path is not None:
            inputs["chat_path"] = chat_path
        if user_speaker is not None:
            inputs["user_speaker"] = user_speaker
        if target_speaker is not None:
            inputs["target_speaker"] = target_speaker

        self._on_event = on_event
        token = _DELTA_SINK.set(on_delta)
        try:
            result = self.graph.invoke(inputs, self._config(thread_id))
            final = self._finalize(thread_id, result)
            if final.get("status") == "interrupted":
                self._emit_interrupt(final, on_event)
        finally:
            _DELTA_SINK.reset(token)
            self._on_event = None
        return AgentState.from_dict(final)

    def resume(
        self,
        thread_id: str,
        decision: dict,
        on_event: Optional[EventListener] = None,
    ) -> AgentState:
        state = self.load_state(thread_id)
        if state is None:
            raise ValueError(f"unknown thread_id: {thread_id}")
        if state.status != "interrupted":
            # 与自研引擎保持同一套错误语义（API 层据此返回 409）
            raise ValueError(
                f"thread {thread_id!r} is not interrupted (status={state.status!r})"
            )

        self._on_event = on_event
        try:
            result = self.graph.invoke(
                Command(resume=decision), self._config(thread_id)
            )
            final = self._finalize(thread_id, result)
            if final.get("status") == "interrupted":
                self._emit_interrupt(final, on_event)
        finally:
            self._on_event = None
        return AgentState.from_dict(final)

    @staticmethod
    def _emit_interrupt(state: dict, on_event: Optional[EventListener]) -> None:
        """官方引擎用内部异常实现中断，不会走节点回调，所以这里补一条事件。

        SSE 层依赖这个事件来推 `interrupt`；没有它，前端就永远看不到裁决框。
        """

        if on_event is None:
            return
        try:
            on_event("interrupt", dict(state))
        except Exception:
            pass

    def load_state(self, thread_id: str) -> Optional[AgentState]:
        snapshot = self.graph.get_state(self._config(thread_id))
        if not snapshot or not snapshot.values:
            return None
        return AgentState.from_dict(self._snapshot(thread_id))

    def close(self) -> None:
        if self._sqlite_connection is not None:
            self._sqlite_connection.close()
            self._sqlite_connection = None

    # -- 内部 ------------------------------------------------------------
    def _snapshot(self, thread_id: str) -> dict:
        snapshot = self.graph.get_state(self._config(thread_id))
        values = dict(snapshot.values or {})
        interrupts = [
            item
            for task in getattr(snapshot, "tasks", ()) or ()
            for item in getattr(task, "interrupts", ()) or ()
        ]
        if interrupts:
            payload = getattr(interrupts[0], "value", interrupts[0])
            values["status"] = "interrupted"
            values["pending"] = payload
        values.setdefault("thread_id", thread_id)
        return values

    def _finalize(self, thread_id: str, result: dict) -> dict:
        values = self._snapshot(thread_id)
        if values.get("status") == "interrupted":
            # 中断时不覆盖已落盘的状态，直接返回带 pending 的视图
            return values
        return {**result, **values}


def _current_delta() -> Optional[DeltaSink]:
    return _DELTA_SINK.get()


def build_runtime(
    engine: str = "auto",
    checkpoint_path: str = ":memory:",
    model: Optional[ReplyModel] = None,
    max_steps: int = 12,
):
    """按引擎名造 runtime：auto → 有 langgraph 就用官方实现。"""

    name = (engine or "auto").strip().lower()
    if name == "builtin":
        from agent.graph import PersonaReplayRuntime

        return PersonaReplayRuntime(checkpoint_path, model=model, max_steps=max_steps)
    if name == "langgraph":
        if not engine_available():
            raise ValueError(
                "GRAPH_ENGINE=langgraph 但未安装 langgraph："
                "pip install langgraph langgraph-checkpoint-sqlite"
            )
        return LangGraphRuntime(checkpoint_path, model=model, max_steps=max_steps)
    if name != "auto":
        raise ValueError(f"unknown engine: {engine!r} (auto|langgraph|builtin)")

    if engine_available():
        try:
            return LangGraphRuntime(checkpoint_path, model=model, max_steps=max_steps)
        except Exception:  # noqa: BLE001 - 装坏了就退回自研引擎
            pass
    from agent.graph import PersonaReplayRuntime

    return PersonaReplayRuntime(checkpoint_path, model=model, max_steps=max_steps)
