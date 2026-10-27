"""A LangGraph-shaped, dependency-free Persona Replay graph.

The public concepts intentionally match LangGraph: StateGraph, nodes, edges,
conditional edges, compiled invoke, checkpointing, and interrupt/resume. This
keeps the vertical slice runnable in the current environment; a future adapter
can map the same node functions to the real langgraph package.

Day8-Day9:
- 图从 `retrieve → draft` 扩成 `planner → retrieve → draft → critic`。
- 调度器删掉 `visited` 环检测，改成 **步数预算**：Critic 重写天然是回边，
  用"访问过就报错"去防死循环，等于把功能也一起禁掉了。

Day11:
- 新增 `human_review` 节点：不再静默降级，而是抛 `GraphInterrupt` 暂停。
- 中断时把待决状态和 `resume_at` 一起落盘；`resume()` 从 `resume_at` 继续，
  而不是把整轮重跑。
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Dict, List, Optional, Tuple

from agent.control import GraphInterrupt
from agent.nodes import (
    VALID_ACTIONS,
    apply_decision,
    critic_node,
    draft_node,
    human_review_node,
    planner_node,
    retrieve_node,
    route_after_critic,
)
from agent.state import AgentState
from ingest.session_store import get_session
from llm_client.heuristic import HeuristicReplyModel, ReplyModel
from memory.checkpoint import SQLiteCheckpointer
from observability.trace import new_span_id, new_trace_id


START = "__start__"
END = "__end__"
Node = Callable[[dict], dict]
Router = Callable[[dict], str]
EventListener = Callable[[str, dict], None]

# 单轮最多允许的节点执行次数。一次正常回合约 4 步（planner/retrieve/draft/critic），
# 两次重写约 9 步；12 留出余量。
DEFAULT_MAX_STEPS = 12

VALID_DECISIONS = VALID_ACTIONS  # 兼容旧名字

# 流式回调按"调用"传递，而不是按"图"传递：图的节点是闭包，拿不到 invoke 的参数。
# 用 ContextVar 而不是存实例属性，是为了同一张编译好的图能被多个线程/请求并发使用。
DeltaSink = Callable[[str, bool], None]
_DELTA_SINK: ContextVar[Optional[DeltaSink]] = ContextVar("delta_sink", default=None)


def current_delta_sink() -> Optional[DeltaSink]:
    return _DELTA_SINK.get()


class StateGraph:
    """Minimal educational graph builder with explicit state transitions."""

    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        self.edges: Dict[str, str] = {}
        self.routers: Dict[str, Tuple[Router, Dict[str, str]]] = {}
        self.entry_point: Optional[str] = None

    def add_node(self, name: str, node: Node) -> "StateGraph":
        if name in (START, END):
            raise ValueError("reserved graph node name")
        self.nodes[name] = node
        return self

    def add_edge(self, source: str, destination: str) -> "StateGraph":
        self.edges[source] = destination
        return self

    def add_conditional_edges(
        self, source: str, router: Router, mapping: Dict[str, str]
    ) -> "StateGraph":
        if source not in self.nodes:
            raise ValueError(f"unknown conditional source: {source}")
        unknown = [
            target
            for target in mapping.values()
            if target not in self.nodes and target != END
        ]
        if unknown:
            raise ValueError(f"conditional edge targets not in graph: {unknown}")
        self.routers[source] = (router, dict(mapping))
        return self

    def set_entry_point(self, name: str) -> "StateGraph":
        if name not in self.nodes:
            raise ValueError(f"unknown entry node: {name}")
        self.entry_point = name
        return self

    def compile(
        self,
        checkpointer: SQLiteCheckpointer,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> "CompiledGraph":
        if self.entry_point is None:
            raise ValueError("graph entry point has not been configured")
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        return CompiledGraph(self, checkpointer, max_steps)


class CompiledGraph:
    """Step-budgeted scheduler with checkpointing and interrupt/resume."""

    def __init__(
        self,
        graph: StateGraph,
        checkpointer: SQLiteCheckpointer,
        max_steps: int = DEFAULT_MAX_STEPS,
    ):
        self.graph = graph
        self.checkpointer = checkpointer
        self.max_steps = max_steps

    # -- public API ------------------------------------------------------
    def invoke(
        self,
        input_state: dict,
        config: Optional[dict] = None,
        on_event: Optional[EventListener] = None,
        on_delta: Optional[DeltaSink] = None,
    ) -> dict:
        config = config or {}
        thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id:
            raise ValueError("configurable.thread_id is required")

        state = self.checkpointer.load(thread_id) or {}
        state.update(input_state)
        state["thread_id"] = thread_id
        # 每轮重新计数：rewrite_count 与 critique 都是"本轮"的概念，
        # 否则上一轮用满两次重写的会话，下一轮会立刻再停一次。
        state["rewrite_count"] = 0
        state["critique"] = None
        state["pending"] = None
        state["human_decision"] = None
        state["status"] = "ok"

        budget = int(config.get("max_steps", self.max_steps))
        token = _DELTA_SINK.set(on_delta)
        try:
            return self._run(
                state=state,
                current=self.graph.entry_point,
                path=[],
                thread_id=str(thread_id),
                budget=budget,
                on_event=on_event,
            )
        finally:
            _DELTA_SINK.reset(token)

    def resume(
        self,
        thread_id: str,
        decision: dict,
        on_event: Optional[EventListener] = None,
    ) -> dict:
        """Continue after a human decision; resumes at `pending.resume_at`."""

        state = self.checkpointer.load(thread_id)
        if not state:
            raise ValueError(f"unknown thread_id: {thread_id}")
        if state.get("status") != "interrupted":
            raise ValueError(
                f"thread {thread_id!r} is not interrupted "
                f"(status={state.get('status')!r})"
            )

        pending = dict(state.get("pending") or {})
        updates = apply_decision(state, decision)  # 与 LangGraph 引擎共用同一套规则
        state.update(updates)

        if on_event:
            on_event("resume", dict(state))

        resume_at = str(pending.get("resume_at") or END)
        return self._run(
            state=state,
            current=resume_at,
            path=list(state.get("steps", [])),
            thread_id=str(thread_id),
            budget=self.max_steps,
            on_event=on_event,
        )

    # -- scheduler -------------------------------------------------------
    def _run(
        self,
        state: dict,
        current: Optional[str],
        path: List[str],
        thread_id: str,
        budget: int,
        on_event: Optional[EventListener],
    ) -> dict:
        for _ in range(budget):
            if current == END:
                state.update(steps=list(path), status=state.get("status") or "ok")
                self.checkpointer.save(thread_id, state)
                self._emit(on_event, "done", state)
                return state

            path.append(str(current))
            node = self.graph.nodes[str(current)]
            try:
                state.update(node(state))
            except GraphInterrupt as pause:
                payload = dict(pause.payload)
                # 恢复点：当前节点的后继。这样 resume 是从中断点之后继续，
                # 而不是把整轮重跑一遍（replay）——两者的副作用完全不同。
                payload.setdefault(
                    "resume_at", self.graph.edges.get(str(current), END)
                )
                state.update(pending=payload, status="interrupted", steps=list(path))
                self.checkpointer.save(thread_id, state)
                self._emit(on_event, "interrupt", state)
                return state

            state.update(steps=list(path))
            self.checkpointer.save(thread_id, state)  # 每步落盘 = 可断点/可回溯
            self._emit(on_event, str(current), state)

            router_entry = self.graph.routers.get(str(current))
            if router_entry is not None:
                router, mapping = router_entry
                key = router(state)
                if key not in mapping:
                    raise RuntimeError(
                        f"router of {current!r} returned unknown key {key!r}; "
                        f"expected one of {sorted(mapping)}"
                    )
                current = mapping[key]
            else:
                nxt = self.graph.edges.get(str(current))
                if nxt is None:
                    raise RuntimeError(f"node {current!r} has no outgoing edge")
                current = nxt

        # 预算耗尽属于"预期内的失败"：降级，不抛异常，也不返回 500。
        state.update(steps=path, status="budget_exhausted")
        self.checkpointer.save(thread_id, state)
        self._emit(on_event, "done", state)
        return state

    @staticmethod
    def _emit(on_event: Optional[EventListener], name: str, state: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(name, dict(state))
        except Exception:
            # 观测/流式回调失败不能影响主流程（宁可少一条事件，不要 500）。
            pass


def build_persona_replay_graph(
    checkpointer: SQLiteCheckpointer,
    model: Optional[ReplyModel] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> CompiledGraph:
    """Build the Day8-Day11 graph: planner → retrieve → draft → critic."""

    reply_model = model or HeuristicReplyModel()

    llm_check = None
    from llm_client.verifier import critic_llm_enabled, make_llm_check

    if critic_llm_enabled():
        try:
            llm_check = make_llm_check()
        except Exception:  # noqa: BLE001 - 没 key 就退回纯规则
            llm_check = None

    def critic_with_session(state: dict) -> dict:
        session = get_session(str(state.get("chat_id") or ""))
        other = session.other_speaker_messages() if session else ()
        return critic_node(state, other, llm_check=llm_check)

    graph = StateGraph()
    graph.add_node("planner", planner_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node(
        "draft", lambda state: draft_node(state, reply_model, current_delta_sink())
    )
    graph.add_node("critic", critic_with_session)
    graph.add_node("human_review", human_review_node)

    graph.add_edge("planner", "retrieve")
    graph.add_edge("retrieve", "draft")
    graph.add_edge("draft", "critic")
    graph.add_edge("human_review", END)
    graph.add_conditional_edges(
        "critic",
        route_after_critic,
        {"pass": END, "rewrite": "draft", "review": "human_review"},
    )
    graph.set_entry_point("planner")
    return graph.compile(checkpointer, max_steps=max_steps)


class PersonaReplayRuntime:
    """Application-facing wrapper around the compiled graph and checkpoint."""

    def __init__(
        self,
        checkpoint_path: str = ":memory:",
        model: Optional[ReplyModel] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ):
        self.checkpointer = SQLiteCheckpointer(checkpoint_path)
        # 显式持有模型实例：/health 要报"当前用的哪个模型"，测试也要能断言。
        self.model = model or HeuristicReplyModel()
        self.graph = build_persona_replay_graph(
            self.checkpointer, self.model, max_steps=max_steps
        )

    @property
    def model_name(self) -> str:
        return type(self.model).__name__

    def prepare(
        self,
        thread_id: str,
        chat_path: str,
        user_speaker: Optional[str] = None,
        target_speaker: Optional[str] = None,
    ) -> AgentState:
        """Import-time step: build the persona card and index, save the thread.

        Day14 的 `POST /sessions` 用它：会话创建时就固定说话人，之后的每一轮
        只需要 session_id + message。
        """

        patch = planner_node(
            {
                "chat_path": chat_path,
                "user_speaker": user_speaker,
                "target_speaker": target_speaker,
            }
        )
        state = {**patch, "thread_id": thread_id, "chat_path": chat_path}
        state.setdefault("status", "ok")
        self.checkpointer.save(thread_id, state)
        return AgentState.from_dict(state)

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
        inputs: dict = {
            "user_input": user_input,
            # 每轮一个新 trace：观测事件靠它串起来（见 observability/trace.py）
            "trace_id": new_trace_id(),
            "root_span_id": new_span_id(),
        }
        if chat_path is not None:
            inputs["chat_path"] = chat_path
        if user_speaker is not None:
            inputs["user_speaker"] = user_speaker
        if target_speaker is not None:
            inputs["target_speaker"] = target_speaker
        result = self.graph.invoke(
            inputs,
            {"configurable": {"thread_id": thread_id}},
            on_event=on_event,
            on_delta=on_delta,
        )
        return AgentState.from_dict(result)

    def resume(
        self,
        thread_id: str,
        decision: dict,
        on_event: Optional[EventListener] = None,
    ) -> AgentState:
        result = self.graph.resume(thread_id, decision, on_event=on_event)
        return AgentState.from_dict(result)

    def load_state(self, thread_id: str) -> Optional[AgentState]:
        value = self.checkpointer.load(thread_id)
        return AgentState.from_dict(value) if value else None

    def close(self) -> None:
        self.checkpointer.close()
